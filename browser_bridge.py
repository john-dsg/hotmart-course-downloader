"""Recebe um manifesto Hotmart do Chrome e cria um MP4 local sem DRM.

A URL assinada existe somente em memória. Ela não é exibida nem gravada em log,
arquivo temporário, configuração ou linha de comando do FFmpeg.
"""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urljoin, urlparse

import m3u8
import requests

from hotmark import (
    USER_AGENT,
    find_executable,
    playlist_is_encrypted,
    valid_video,
    write_stream,
)

MAX_BODY_SIZE = 32 * 1024


def valid_manifest_url(raw_url: object) -> bool:
    if not isinstance(raw_url, str) or len(raw_url) > 16_384:
        return False
    try:
        parsed = urlparse(raw_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return (
        parsed.scheme == "https"
        and (host == "hotmart.com" or host.endswith(".hotmart.com"))
        and parsed.path.lower().endswith(".m3u8")
    )


def best_variant(playlist: m3u8.M3U8) -> object:
    return max(
        playlist.playlists,
        key=lambda item: (
            (item.stream_info.resolution or (0, 0))[0]
            * (item.stream_info.resolution or (0, 0))[1],
            item.stream_info.bandwidth or 0,
        ),
    )


def download_manifest(raw_url: str, target: Path) -> str:
    if valid_video(target):
        return "skipped"

    client = requests.Session()
    client.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.apple.mpegurl, application/x-mpegURL, */*",
            "Referer": "https://hotmart.com/",
        }
    )
    response = client.get(raw_url, timeout=30)
    if not response.ok:
        raise RuntimeError(f"Manifesto recusado (HTTP {response.status_code})")
    master = m3u8.loads(response.text, uri=raw_url)
    if playlist_is_encrypted(master, response.text):
        return "protected"

    if master.playlists:
        variant_url = urljoin(raw_url, best_variant(master).uri)
        response = client.get(variant_url, timeout=30)
        if not response.ok:
            raise RuntimeError(f"Qualidade recusada (HTTP {response.status_code})")
        playlist = m3u8.loads(response.text, uri=variant_url)
        playlist_url = variant_url
        raw_playlist = response.text
    else:
        playlist = master
        playlist_url = raw_url
        raw_playlist = response.text

    if playlist_is_encrypted(playlist, raw_playlist):
        return "protected"
    if not playlist.segments:
        raise RuntimeError("O manifesto não contém segmentos de vídeo")

    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg não encontrado")

    target.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="hotmart-browser-") as temporary:
        temp_dir = Path(temporary)
        init_files: dict[str, str] = {}
        for index, segment in enumerate(playlist.segments, 1):
            if segment.init_section and segment.init_section.uri:
                init_url = urljoin(playlist_url, segment.init_section.uri)
                if init_url not in init_files:
                    init_name = f"init-{len(init_files) + 1:03d}.mp4"
                    write_stream(
                        client.get(init_url, stream=True, timeout=60),
                        temp_dir / init_name,
                    )
                    init_files[init_url] = init_name
                segment.init_section.uri = init_files[init_url]

            segment_url = urljoin(playlist_url, segment.uri)
            segment_name = f"segment-{index:06d}.ts"
            write_stream(
                client.get(segment_url, stream=True, timeout=60),
                temp_dir / segment_name,
            )
            segment.uri = segment_name

        local_playlist = temp_dir / "playlist.m3u8"
        local_playlist.write_text(playlist.dumps(), encoding="utf-8")
        partial = target.with_name(target.name + ".part.mp4")
        partial.unlink(missing_ok=True)

        result = subprocess.run(  # nosec B603
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-allowed_extensions",
                "ALL",
                "-i",
                str(local_playlist),
                "-c",
                "copy",
                str(partial),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0 or not valid_video(partial):
            partial.unlink(missing_ok=True)
            raise RuntimeError("FFmpeg não conseguiu montar o vídeo")
        partial.replace(target)

    return "downloaded"


class CaptureState:
    def __init__(self, target: Path):
        self.target = target
        self.done = threading.Event()
        self.result: str | None = None
        self.error: str | None = None
        self.lock = threading.Lock()

    def capture(self, raw_url: str) -> None:
        if not self.lock.acquire(blocking=False):
            return
        try:
            if self.done.is_set():
                return
            self.result = download_manifest(raw_url, self.target)
        except Exception as error:  # noqa: BLE001 - fronteira do servidor local
            self.error = type(error).__name__
        finally:
            self.done.set()
            self.lock.release()


def handler_for(state: CaptureState) -> type[BaseHTTPRequestHandler]:
    class CaptureHandler(BaseHTTPRequestHandler):
        def log_message(self, _format: str, *_args: object) -> None:
            return

        def send_json(self, status: int, payload: dict[str, object]) -> None:
            body = json.dumps(payload).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
            self.end_headers()

        def do_POST(self) -> None:
            if self.path != "/manifest":
                self.send_json(404, {"ok": False})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            if length <= 0 or length > MAX_BODY_SIZE:
                self.send_json(400, {"ok": False})
                return
            try:
                payload = json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_json(400, {"ok": False})
                return
            raw_url = payload.get("url") if isinstance(payload, dict) else None
            if not valid_manifest_url(raw_url):
                self.send_json(400, {"ok": False})
                return
            self.send_json(202, {"ok": True})
            state.capture(raw_url)

    return CaptureHandler


def receive_once(target: Path, timeout: int = 120) -> str:
    state = CaptureState(target)
    server = HTTPServer(("127.0.0.1", 8765), handler_for(state))
    server.timeout = 1
    deadline = time.monotonic() + timeout
    print("Ponte local pronta. Recarregue e reproduza a aula no Chrome.")
    while not state.done.is_set() and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if not state.done.is_set():
        raise TimeoutError("Nenhum manifesto foi recebido dentro do prazo")
    if state.error:
        raise RuntimeError(f"A captura falhou ({state.error})")
    return state.result or "failed"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Captura uma aula Hotmart pelo Chrome")
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--timeout", type=int, default=120)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        result = receive_once(args.output, args.timeout)
    except Exception as error:  # noqa: BLE001 - mensagem sem URL ou segredo
        print(f"Falha no teste ({type(error).__name__}).")
        return 1
    if result == "protected":
        print("Vídeo criptografado/protegido; download não realizado.")
        return 2
    if result == "skipped":
        print("O vídeo local já está completo; nada foi baixado.")
        return 0
    print("Vídeo baixado e validado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
