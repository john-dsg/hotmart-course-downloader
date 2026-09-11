"""Backup local de cursos Hotmart acessíveis pela conta do próprio usuário.

O programa não usa cookies do navegador, não persiste credenciais e não tenta
contornar DRM. Fluxos HLS criptografados são identificados e ignorados.
"""

from __future__ import annotations

import argparse
import getpass
import glob
import logging
import mimetypes
import re
import shutil
import subprocess  # nosec B404
import tempfile
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

import m3u8
import requests
from bs4 import BeautifulSoup
from markdownify import markdownify
from yt_dlp import YoutubeDL

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131 Safari/537.36"
)
AUTH_URL = "https://api-sec-vlc.hotmart.com/security/oauth/login"
CHECK_TOKEN_URL = "https://api-sec-vlc.hotmart.com/security/oauth/check_token"  # nosec B105
CLUB_API = "https://api-club.hotmart.com/hot-club-api/rest/v3"
COURSES_ROOT = Path("Cursos")
LOG_PATH = Path("download.log")
TEMP_ROOT = Path("temp")
WINDOWS_RESERVED = {
    "CON",
    "PRN",
    "AUX",
    "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
FILE_EXTENSIONS = {
    ".pdf",
    ".zip",
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".csv",
    ".txt",
    ".rtf",
    ".odt",
    ".ods",
    ".odp",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".webp",
    ".svg",
    ".epub",
    ".rar",
    ".7z",
}
TRACKER_HOST_PARTS = (
    "google-analytics",
    "googletagmanager",
    "doubleclick",
    "facebook.com/tr",
    "hotjar",
    "segment.io",
    "mixpanel",
    "clarity.ms",
)
SENSITIVE_KEY = re.compile(
    r"(?i)(token|access[_-]?token|refresh[_-]?token|authorization|password|senha|cookie|session)"
)
BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]+")
KEY_VALUE_SECRET = re.compile(
    r"(?i)(token|access[_-]?token|refresh[_-]?token|authorization|password|senha|cookie|session)"
    r"\s*[:=]\s*[^\s,;&]+"
)


class SafeFormatter(logging.Formatter):
    """Última barreira contra segredos acidentais em mensagens de log."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def redact(value: object) -> str:
    text = str(value)
    text = BEARER.sub("Bearer [REDACTED]", text)
    text = KEY_VALUE_SECRET.sub(lambda match: f"{match.group(1)}=[REDACTED]", text)
    url_pattern = re.compile(r"https?://[^\s'\"<>]+")

    def clean_url(match: re.Match[str]) -> str:
        raw = match.group(0)
        try:
            parsed = urlparse(raw)
            query = [
                (key, "[REDACTED]" if SENSITIVE_KEY.search(key) else val)
                for key, val in parse_qsl(parsed.query, keep_blank_values=True)
            ]
            return urlunparse(parsed._replace(query=urlencode(query)))
        except ValueError:
            return "[URL REDACTED]"

    return url_pattern.sub(clean_url, text)


def build_logger(path: Path = LOG_PATH) -> logging.Logger:
    logger = logging.getLogger(f"hotmart_backup.{path.resolve()}")
    for old_handler in logger.handlers:
        old_handler.close()
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(
        SafeFormatter("[%(asctime)s] [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    return logger


LOGGER = build_logger()


def safe_error(operation: str, error: BaseException) -> str:
    """Retorna somente o tipo do erro; mensagens podem carregar URLs assinadas."""
    return f"{operation} ({type(error).__name__})"


def sanitize_filename(
    value: object, fallback: str = "Sem título", max_length: int = 150
) -> str:
    """Preserva acentos e remove somente caracteres incompatíveis com Windows."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", str(value)).strip().rstrip(". ")
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = fallback
    stem = name.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED:
        name = f"_{name}"
    return name[:max_length].rstrip(". ") or fallback


def numbered_name(index: int, title: object) -> str:
    return f"{index:02d} - {sanitize_filename(title)}"


def is_hotmart_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "hotmart.com" or host.endswith(
        (".hotmart.com", ".sparkleapp.com.br")
    )


def operational_request_json(
    session: requests.Session, url: str, **kwargs: object
) -> dict:
    response = session.get(url, timeout=30, **kwargs)
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}")
    data = response.json()
    if not isinstance(data, dict):
        raise TypeError("Resposta inesperada da API")
    return data


def authenticate() -> tuple[requests.Session, dict[str, str]]:
    TEMP_ROOT.mkdir(exist_ok=True)
    email = input("Email da Hotmart: ").strip()
    password = getpass.getpass("Senha da Hotmart: ")
    if not email or not password:
        raise RuntimeError("Email e senha são obrigatórios")

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Origin": "https://app.hotmart.com",
            "Referer": "https://app.hotmart.com/",
        }
    )
    payload = {"username": email, "password": password, "grant_type": "password"}
    LOGGER.info("Autenticação iniciada")
    try:
        response = session.post(
            AUTH_URL, data=payload, timeout=30, allow_redirects=False
        )
    finally:
        payload.clear()
        password = None
        email = ""

    if response.status_code != 200:
        LOGGER.error("Falha na autenticação (HTTP %s)", response.status_code)
        if response.status_code == 403:
            raise RuntimeError(
                "A Hotmart recusou o login ou exigiu uma verificação adicional. "
                "Abra a conta no navegador e conclua qualquer CAPTCHA/2FA antes de tentar novamente."
            )
        raise RuntimeError(
            "Não foi possível autenticar. Confira as credenciais e tente novamente."
        )
    try:
        auth_data = response.json()
        access_token = auth_data["access_token"]
    except (ValueError, KeyError, TypeError) as error:
        LOGGER.error(safe_error("Resposta de autenticação inválida", error))
        raise RuntimeError("A Hotmart não retornou uma sessão válida.") from None

    session.headers.update({"Authorization": f"Bearer {access_token}"})
    LOGGER.info("Autenticação concluída")
    return session, {"token": access_token}


def configured_subdomains(cli_values: Iterable[str]) -> list[str]:
    values = [value.strip() for value in cli_values if value.strip()]
    for module_name in ("config_local", "config_cursos"):
        try:
            module = __import__(module_name)
            values.extend(getattr(module, "CURSOS_SUBDOMINIOS", []))
        except ImportError:
            continue
    clean: list[str] = []
    for value in values:
        value = str(value).strip()
        if re.fullmatch(r"[A-Za-z0-9-]+", value) and value not in clean:
            clean.append(value)
    return clean


def discover_courses(
    session: requests.Session, token_params: dict[str, str], subdomains: list[str]
) -> list[dict]:
    products: list[dict] = []
    try:
        check = operational_request_json(session, CHECK_TOKEN_URL, params=token_params)
        resources = check.get("resources", [])
        if isinstance(resources, list):
            products.extend(item for item in resources if isinstance(item, dict))
    except Exception as error:  # noqa: BLE001 - falha da API não deve encerrar o fluxo alternativo
        LOGGER.warning(safe_error("Listagem automática indisponível", error))

    if not products:
        products = [
            {
                "resource": {"subdomain": domain, "status": "ACTIVE"},
                "roles": ["STUDENT"],
            }
            for domain in subdomains
        ]

    courses: list[dict] = []
    for product in products:
        resource = product.get("resource") or {}
        domain = str(resource.get("subdomain", "")).strip()
        if (
            not domain
            or resource.get("status") != "ACTIVE"
            or "STUDENT" not in product.get("roles", [])
        ):
            continue
        try:
            set_course_headers(session, domain)
            membership = operational_request_json(
                session, f"{CLUB_API}/membership?attach_token=false"
            )
            name = sanitize_filename(membership.get("name", domain))
            courses.append({"name": name, "subdomain": domain})
            LOGGER.info("Curso encontrado")
        except Exception as error:  # noqa: BLE001 - um curso inválido não bloqueia os demais
            LOGGER.warning(safe_error("Falha ao validar um curso configurado", error))
    return courses


def set_course_headers(session: requests.Session, domain: str) -> None:
    base = f"https://{domain}.club.hotmart.com/"
    session.headers.update(
        {"Origin": base.rstrip("/"), "Referer": base, "club": domain}
    )


def choose_course(courses: list[dict], selected: int | None) -> dict:
    if not courses:
        raise RuntimeError(
            "Nenhum curso foi identificado. Confira o subdomínio em config_local.py."
        )
    print(f"\nCursos disponíveis ({len(courses)}):")
    for index, course in enumerate(courses, 1):
        print(f"  {index}. {course['name']}")
    if selected is None:
        selected = int(input("Qual curso deseja baixar? ").strip())
    if selected < 1 or selected > len(courses):
        raise RuntimeError("Curso selecionado é inválido")
    return courses[selected - 1]


def valid_existing_file(path: Path, minimum_size: int = 1) -> bool:
    return (
        path.is_file()
        and path.stat().st_size >= minimum_size
        and not path.name.endswith(".part")
    )


def find_executable(name: str) -> str | None:
    """Localiza FFmpeg/FFprobe inclusive após instalação recente via WinGet."""
    resolved = shutil.which(name)
    if resolved:
        return resolved
    local_app_data = Path.home() / "AppData" / "Local"
    pattern = str(
        local_app_data
        / "Microsoft"
        / "WinGet"
        / "Packages"
        / "Gyan.FFmpeg_*"
        / "ffmpeg-*"
        / "bin"
        / f"{name}.exe"
    )
    matches = glob.glob(pattern)
    return matches[0] if matches else None


def valid_video(path: Path) -> bool:
    if not valid_existing_file(path, 1024):
        return False
    ffprobe = find_executable("ffprobe")
    if not ffprobe:
        return True
    result = subprocess.run(  # nosec B603
        [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        return result.returncode == 0 and float(result.stdout.strip()) > 0
    except ValueError:
        return False


def write_stream(response: requests.Response, target: Path) -> None:
    if not response.ok:
        raise RuntimeError(f"HTTP {response.status_code}")
    if "text/html" in response.headers.get("Content-Type", "").lower():
        raise RuntimeError("O servidor retornou HTML em vez de arquivo")
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)
    written = 0
    with partial.open("wb") as handle:
        for chunk in response.iter_content(1024 * 256):
            if chunk:
                handle.write(chunk)
                written += len(chunk)
    expected = response.headers.get("Content-Length")
    if written == 0 or (expected and expected.isdigit() and written != int(expected)):
        partial.unlink(missing_ok=True)
        raise RuntimeError("Arquivo recebido incompleto")
    partial.replace(target)


def unique_name(original: str, seen: dict[str, int]) -> str:
    safe = sanitize_filename(original, "material")
    key = safe.casefold()
    seen[key] = seen.get(key, 0) + 1
    if seen[key] == 1:
        return safe
    path = Path(safe)
    return f"{path.stem} ({seen[key]}){path.suffix}"


def valid_material(path: Path) -> bool:
    if not valid_existing_file(path):
        return False
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        with path.open("rb") as handle:
            header = handle.read(5)
            handle.seek(max(0, path.stat().st_size - 2048))
            tail = handle.read()
        return header == b"%PDF-" and b"%%EOF" in tail
    if suffix in {".zip", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
        return zipfile.is_zipfile(path)
    if suffix in {".jpg", ".jpeg"}:
        with path.open("rb") as handle:
            return handle.read(3) == b"\xff\xd8\xff"
    if suffix == ".png":
        with path.open("rb") as handle:
            return handle.read(8) == b"\x89PNG\r\n\x1a\n"
    return path.stat().st_size > 0


def attachment_download(
    session: requests.Session, attachment: dict, target: Path
) -> bool:
    if valid_material(target):
        return False
    attachment_id = attachment.get("fileMembershipId")
    if not attachment_id:
        raise RuntimeError("Anexo sem identificador")
    info = operational_request_json(
        session, f"{CLUB_API}/attachment/{attachment_id}/download"
    )
    clean_session = requests.Session()
    clean_session.headers.update({"User-Agent": USER_AGENT})
    if info.get("directDownloadUrl"):
        response = clean_session.get(info["directDownloadUrl"], stream=True, timeout=90)
    elif info.get("lambdaUrl"):
        lambda_session = requests.Session()
        lambda_session.headers.update(
            {"User-Agent": USER_AGENT, "token": str(info.get("token", ""))}
        )
        url_response = lambda_session.get(
            info["lambdaUrl"], timeout=30, allow_redirects=False
        )
        if not url_response.ok:
            raise RuntimeError(f"HTTP {url_response.status_code}")
        response = clean_session.get(url_response.text.strip(), stream=True, timeout=90)
    else:
        raise RuntimeError("Anexo sem URL de download")
    write_stream(response, target)
    return True


def extension_from_response(response: requests.Response, default: str = ".pdf") -> str:
    content_type = response.headers.get("Content-Type", "").split(";", 1)[0]
    return mimetypes.guess_extension(content_type) or default


def google_drive_download(
    file_id: str, materials_dir: Path, seen: dict[str, int]
) -> bool:
    clean_session = requests.Session()
    clean_session.headers.update({"User-Agent": USER_AGENT})
    response = clean_session.get(
        "https://drive.google.com/uc",
        params={"export": "download", "id": file_id},
        stream=True,
        timeout=90,
    )
    content_type = response.headers.get("Content-Type", "").lower()
    if "text/html" in content_type:
        raise RuntimeError("Google Drive requer interação")
    filename = unique_name(
        f"google-drive-{file_id}{extension_from_response(response)}", seen
    )
    target = materials_dir / filename
    if valid_material(target):
        return False
    write_stream(response, target)
    return True


def direct_material_download(
    url: str, name: str, materials_dir: Path, seen: dict[str, int]
) -> bool:
    target = materials_dir / unique_name(name, seen)
    if valid_material(target):
        return False
    clean_session = requests.Session()
    clean_session.headers.update({"User-Agent": USER_AGENT})
    response = clean_session.get(url, stream=True, timeout=90, allow_redirects=True)
    if "text/html" in response.headers.get("Content-Type", "").lower():
        raise RuntimeError("Link não retornou um arquivo")
    write_stream(response, target)
    return True


def drive_ids(html: str) -> list[str]:
    return list(
        dict.fromkeys(
            re.findall(r"drive\.google\.com/file/d/([A-Za-z0-9_-]+)", html or "")
        )
    )


def embedded_material_links(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html or "", "html.parser")
    result: list[tuple[str, str]] = []
    for node in soup.find_all(["a", "iframe"]):
        url = str(node.get("href") or node.get("src") or "").strip()
        if not url.startswith(("https://", "http://")):
            continue
        suffix = Path(urlparse(url).path).suffix.lower()
        if suffix not in FILE_EXTENSIONS:
            continue
        label = (
            node.get_text(" ", strip=True)
            or Path(urlparse(url).path).name
            or "material"
        )
        if not Path(label).suffix:
            label += suffix
        result.append((url, sanitize_filename(label)))
    return list(dict.fromkeys(result))


def relevant_links(page: dict, html: str) -> list[tuple[str, str, str]]:
    links: list[tuple[str, str, str]] = []
    for item in page.get("complementaryReadings") or []:
        url = str(item.get("articleUrl", "")).strip()
        name = str(item.get("articleName", "Link complementar")).strip()
        if url.startswith(("https://", "http://")):
            links.append(("Material complementar", name, url))
    soup = BeautifulSoup(html or "", "html.parser")
    for anchor in soup.find_all("a", href=True):
        url = str(anchor["href"]).strip()
        if not url.startswith(("https://", "http://")):
            continue
        lowered = url.lower()
        if any(part in lowered for part in TRACKER_HOST_PARTS):
            continue
        name = anchor.get_text(" ", strip=True) or "Referência"
        links.append(("Referência", name, url))
    return list(dict.fromkeys(links))


def markdown_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def write_links(path: Path, links: list[tuple[str, str, str]]) -> int:
    if not links:
        return 0
    rows = ["# Links da aula", "", "| Tipo | Nome | Link |", "| --- | --- | --- |"]
    for kind, name, url in links:
        rows.append(
            f"| {markdown_cell(kind)} | {markdown_cell(name)} | {markdown_cell(url)} |"
        )
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return len(links)


def write_description(path: Path, html: str) -> bool:
    if not html or not BeautifulSoup(html, "html.parser").get_text(" ", strip=True):
        return False
    text = markdownify(html, heading_style="ATX", bullets="-").strip()
    if not text:
        return False
    path.write_text(f"# Descrição da aula\n\n{text}\n", encoding="utf-8")
    return True


def iframe_videos(html: str) -> list[str]:
    result: list[str] = []
    for iframe in BeautifulSoup(html or "", "html.parser").find_all("iframe", src=True):
        url = str(iframe["src"]).strip()
        host = (urlparse(url).hostname or "").lower()
        if any(
            name in host
            for name in ("youtube.com", "youtu.be", "vimeo.com", "wistia.com")
        ):
            result.append(url)
    return list(dict.fromkeys(result))


class QuietYtdlpLogger:
    def debug(self, _message: str) -> None:
        pass

    def warning(self, _message: str) -> None:
        LOGGER.warning("Aviso ao processar vídeo externo")

    def error(self, _message: str) -> None:
        LOGGER.error("Falha ao processar vídeo externo")


def external_video_download(url: str, target: Path, referer: str) -> str:
    if "wistia.com" in (urlparse(url).hostname or "").lower():
        return "protected"
    if valid_video(target):
        return "skipped"
    with tempfile.TemporaryDirectory(dir=TEMP_ROOT, prefix="external-") as temp:
        output = str(Path(temp) / "video.%(ext)s")
        options = {
            "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "merge_output_format": "mp4",
            "outtmpl": output,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "logger": QuietYtdlpLogger(),
            "http_headers": {"Referer": referer, "User-Agent": USER_AGENT},
        }
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if info and (info.get("_has_drm") or info.get("has_drm")):
                return "protected"
            ydl.download([url])
        candidates = [
            path
            for path in Path(temp).iterdir()
            if path.is_file() and not path.name.endswith((".part", ".ytdl"))
        ]
        if not candidates:
            raise RuntimeError("Vídeo externo não gerou arquivo")
        source = max(candidates, key=lambda path: path.stat().st_size)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), target)
    return "downloaded" if valid_video(target) else "failed"


def playlist_is_encrypted(playlist: m3u8.M3U8, raw_text: str) -> bool:
    if "#EXT-X-SESSION-KEY" in raw_text.upper():
        return True
    for key in playlist.keys or []:
        if key and str(key.method or "NONE").upper() != "NONE":
            return True
    for segment in playlist.segments:
        if segment.key and str(segment.key.method or "NONE").upper() != "NONE":
            return True
    return False


def hls_get(session: requests.Session, url: str, **kwargs: object) -> requests.Response:
    """Nunca envia o bearer da Hotmart a um host externo de CDN."""
    if is_hotmart_url(url):
        return session.get(url, **kwargs)
    clean_session = requests.Session()
    clean_session.headers.update({"User-Agent": USER_AGENT})
    return clean_session.get(url, **kwargs)


def hotmart_video_download(session: requests.Session, media: dict, target: Path) -> str:
    if valid_video(target):
        return "skipped"
    media_code = media.get("mediaCode")
    if not media_code:
        raise RuntimeError("Vídeo sem identificador")
    source_hint = str(media.get("mediaSrcUrl") or "")
    if source_hint and is_hotmart_url(source_hint):
        session.get(source_hint, timeout=20)
    master_url = f"https://contentplayer.hotmart.com/video/{media_code}/hls/master.m3u8"
    master_response = hls_get(session, master_url, timeout=30)
    if not master_response.ok:
        raise RuntimeError(f"HTTP {master_response.status_code}")
    master = m3u8.loads(master_response.text, uri=master_url)
    if playlist_is_encrypted(master, master_response.text):
        return "protected"
    if master.playlists:
        variant = max(
            master.playlists,
            key=lambda item: (
                (item.stream_info.resolution or (0, 0))[0]
                * (item.stream_info.resolution or (0, 0))[1],
                item.stream_info.bandwidth or 0,
            ),
        )
        playlist_url = urljoin(master_url, variant.uri)
        playlist_response = hls_get(session, playlist_url, timeout=30)
        if not playlist_response.ok:
            raise RuntimeError(f"HTTP {playlist_response.status_code}")
        playlist = m3u8.loads(playlist_response.text, uri=playlist_url)
        raw = playlist_response.text
    else:
        playlist_url, playlist, raw = master_url, master, master_response.text
    if playlist_is_encrypted(playlist, raw):
        return "protected"
    if not playlist.segments:
        raise RuntimeError("Playlist sem segmentos")

    with tempfile.TemporaryDirectory(dir=TEMP_ROOT, prefix="hls-") as temp:
        temp_dir = Path(temp)
        downloaded_maps: dict[str, str] = {}
        for index, segment in enumerate(playlist.segments, 1):
            if segment.init_section and segment.init_section.uri:
                map_url = urljoin(playlist_url, segment.init_section.uri)
                if map_url not in downloaded_maps:
                    local_map = f"init-{len(downloaded_maps) + 1:03d}.mp4"
                    write_stream(
                        hls_get(session, map_url, stream=True, timeout=60),
                        temp_dir / local_map,
                    )
                    downloaded_maps[map_url] = local_map
                segment.init_section.uri = downloaded_maps[map_url]
            segment_url = urljoin(playlist_url, segment.uri)
            local_name = f"segment-{index:06d}.ts"
            write_stream(
                hls_get(session, segment_url, stream=True, timeout=60),
                temp_dir / local_name,
            )
            segment.uri = local_name
        local_playlist = temp_dir / "playlist.m3u8"
        local_playlist.write_text(playlist.dumps(), encoding="utf-8")
        partial = target.with_name(target.name + ".part.mp4")
        partial.unlink(missing_ok=True)
        ffmpeg = find_executable("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("FFmpeg não encontrado")
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


def video_target(lesson_dir: Path, index: int, total: int) -> Path:
    return lesson_dir / ("aula.mp4" if total == 1 else f"{index:02d} - video.mp4")


@dataclass
class Summary:
    lessons_found: int = 0
    lessons_processed: int = 0
    videos_downloaded: int = 0
    materials_downloaded: int = 0
    links_found: int = 0
    failures: int = 0
    protected_videos: int = 0


def process_lesson(
    session: requests.Session,
    lesson: dict,
    lesson_dir: Path,
    referer: str,
    summary: Summary,
) -> None:
    lesson_dir.mkdir(parents=True, exist_ok=True)
    page_hash = lesson.get("hash")
    if not page_hash:
        raise RuntimeError("Aula sem identificador")
    page = operational_request_json(session, f"{CLUB_API}/page/{page_hash}")
    html = str(page.get("content") or "")
    write_description(lesson_dir / "descricao.md", html)
    links = relevant_links(page, html)
    summary.links_found += write_links(lesson_dir / "links.md", links)

    materials = lesson_dir / "Materiais"
    seen_names: dict[str, int] = {}
    for attachment in page.get("attachments") or []:
        try:
            materials.mkdir(exist_ok=True)
            filename = unique_name(
                str(attachment.get("fileName") or "material"), seen_names
            )
            if attachment_download(session, attachment, materials / filename):
                summary.materials_downloaded += 1
                LOGGER.info("Material baixado")
        except Exception as error:  # noqa: BLE001 - um arquivo não bloqueia os demais
            summary.failures += 1
            LOGGER.error(safe_error("Falha ao baixar arquivo", error))

    for file_id in drive_ids(html):
        try:
            materials.mkdir(exist_ok=True)
            if google_drive_download(file_id, materials, seen_names):
                summary.materials_downloaded += 1
                LOGGER.info("Material baixado")
        except Exception as error:  # noqa: BLE001 - um arquivo não bloqueia os demais
            summary.failures += 1
            LOGGER.warning(
                safe_error("Material do Google Drive não pôde ser baixado", error)
            )

    for url, name in embedded_material_links(html):
        if "drive.google.com/file/d/" in url:
            continue
        try:
            materials.mkdir(exist_ok=True)
            if direct_material_download(url, name, materials, seen_names):
                summary.materials_downloaded += 1
                LOGGER.info("Material baixado")
        except Exception as error:  # noqa: BLE001 - um arquivo não bloqueia os demais
            summary.failures += 1
            LOGGER.warning(
                safe_error("Arquivo incorporado não pôde ser baixado", error)
            )

    internal_videos = [
        item for item in page.get("mediasSrc") or [] if isinstance(item, dict)
    ]
    external_videos = iframe_videos(html) if not internal_videos else []
    total_videos = len(internal_videos) + len(external_videos)
    for index, media in enumerate(internal_videos, 1):
        try:
            result = hotmart_video_download(
                session, media, video_target(lesson_dir, index, total_videos)
            )
            if result == "downloaded":
                summary.videos_downloaded += 1
                LOGGER.info("Aula baixada")
            elif result == "protected":
                summary.protected_videos += 1
                LOGGER.warning("Vídeo protegido; download não realizado")
        except Exception as error:  # noqa: BLE001 - um vídeo não bloqueia os demais
            summary.failures += 1
            LOGGER.error(safe_error("Falha ao baixar vídeo", error))
    for offset, url in enumerate(external_videos, len(internal_videos) + 1):
        try:
            result = external_video_download(
                url, video_target(lesson_dir, offset, total_videos), referer
            )
            if result == "downloaded":
                summary.videos_downloaded += 1
                LOGGER.info("Aula baixada")
            elif result == "protected":
                summary.protected_videos += 1
                LOGGER.warning(
                    "Vídeo protegido ou não suportado; download não realizado"
                )
        except Exception as error:  # noqa: BLE001 - um vídeo não bloqueia os demais
            summary.failures += 1
            LOGGER.error(safe_error("Falha ao baixar vídeo externo", error))


def run(args: argparse.Namespace) -> Summary:
    if not find_executable("ffmpeg"):
        raise RuntimeError("FFmpeg não foi encontrado no PATH")
    session, token_params = authenticate()
    courses = discover_courses(
        session, token_params, configured_subdomains(args.subdomain)
    )
    selected = choose_course(courses, args.curso)
    set_course_headers(session, selected["subdomain"])
    navigation = operational_request_json(session, f"{CLUB_API}/navigation")
    modules = sorted(
        navigation.get("modules") or [], key=lambda item: item.get("moduleOrder", 0)
    )
    summary = Summary(
        lessons_found=sum(len(module.get("pages") or []) for module in modules)
    )
    course_dir = COURSES_ROOT / sanitize_filename(selected["name"])
    course_dir.mkdir(parents=True, exist_ok=True)
    referer = f"https://{selected['subdomain']}.club.hotmart.com/"
    processed_limit = 0
    for module_index, module in enumerate(modules, 1):
        module_dir = course_dir / numbered_name(
            module_index, module.get("name", "Módulo")
        )
        pages = sorted(
            module.get("pages") or [], key=lambda item: item.get("pageOrder", 0)
        )
        for lesson_index, lesson in enumerate(pages, 1):
            if args.limite_aulas is not None and processed_limit >= args.limite_aulas:
                break
            lesson_dir = module_dir / numbered_name(
                lesson_index, lesson.get("name", "Aula")
            )
            print(f"Processando: {module_dir.name} / {lesson_dir.name}")
            try:
                process_lesson(session, lesson, lesson_dir, referer, summary)
                summary.lessons_processed += 1
                LOGGER.info("Aula processada")
            except Exception as error:  # noqa: BLE001 - uma aula não bloqueia as demais
                lesson_dir.mkdir(parents=True, exist_ok=True)
                summary.failures += 1
                LOGGER.error(safe_error("Falha ao processar aula", error))
            processed_limit += 1
        if args.limite_aulas is not None and processed_limit >= args.limite_aulas:
            break
    LOGGER.info("Processamento concluído")
    print_summary(selected["name"], summary)
    return summary


def print_summary(course_name: str, summary: Summary) -> None:
    print(
        f"\nCurso: {course_name}\n\n"
        f"Aulas encontradas: {summary.lessons_found}\n"
        f"Aulas processadas: {summary.lessons_processed}\n"
        f"Vídeos baixados: {summary.videos_downloaded}\n"
        f"Materiais baixados: {summary.materials_downloaded}\n"
        f"Links encontrados: {summary.links_found}\n"
        f"Vídeos protegidos/não suportados: {summary.protected_videos}\n"
        f"Falhas: {summary.failures}\n\n"
        f"Log seguro: {LOG_PATH.resolve()}"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backup local de cursos Hotmart")
    parser.add_argument(
        "--subdomain", action="append", default=[], help="Subdomínio adicional do curso"
    )
    parser.add_argument("--curso", type=int, help="Número do curso, começando em 1")
    parser.add_argument(
        "--limite-aulas", type=int, help="Limita o teste às primeiras N aulas"
    )
    return parser.parse_args()


def main() -> int:
    try:
        run(parse_args())
        return 0
    except KeyboardInterrupt:
        LOGGER.warning("Execução interrompida pelo usuário")
        print("\nExecução interrompida. Rode novamente para continuar.")
        return 130
    except Exception as error:  # noqa: BLE001 - fronteira da aplicação
        LOGGER.error(safe_error("Execução encerrada", error))
        if isinstance(error, RuntimeError):
            print(f"Erro: {error}")
        else:
            print(f"Erro: a execução não pôde continuar ({type(error).__name__}).")
        print(f"Consulte o log seguro em: {LOG_PATH.resolve()}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
