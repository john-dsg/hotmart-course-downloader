"""Cria a estrutura local de um curso a partir do índice coletado no navegador."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse

from hotmark import numbered_name, sanitize_filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    return parser.parse_args()


def write_if_missing(path: Path, content: str) -> None:
    if not path.exists():
        path.write_text(content, encoding="utf-8")


def is_video(module_number: int, title: str, duration: str) -> bool:
    if duration:
        return True
    return module_number != 7 and title.casefold() != "link do grupo"


def content_hash(url: str) -> str:
    return Path(urlparse(url).path).name


def load_support_details(root: Path) -> dict[str, dict]:
    path = root / "support_details.json"
    if not path.exists():
        return {}
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {str(row["hash"]): row for row in rows}


def main() -> None:
    args = parse_args()
    modules = json.loads(args.index.read_text(encoding="utf-8"))
    root = args.index.parent
    support_details = load_support_details(root)
    video_count = 0
    support_count = 0

    for module in modules:
        module_number = int(module["number"])
        module_title = sanitize_filename(module["title"])
        module_dir = root / numbered_name(module_number, module_title)
        module_dir.mkdir(parents=True, exist_ok=True)

        for lesson_number, lesson in enumerate(module["lessons"], 1):
            title = sanitize_filename(lesson["title"])
            duration = str(lesson.get("duration") or "")
            source = str(lesson["href"])
            lesson_dir = module_dir / numbered_name(lesson_number, title)
            lesson_dir.mkdir(parents=True, exist_ok=True)
            (lesson_dir / "Materiais").mkdir(exist_ok=True)

            video = is_video(module_number, title, duration)
            video_count += int(video)
            support_count += int(not video)
            status = (
                "Vídeo ainda não salvo. O teste do Hotmart Player retornou um "
                "manifesto criptografado/protegido; não houve tentativa de contorno."
                if video
                else "Conteúdo de texto/material de apoio; não há vídeo nesta página."
            )
            duration_line = f"\n- Duração exibida: {duration}" if duration else ""
            write_if_missing(
                lesson_dir / "descricao.md",
                f"# {title}\n\n"
                f"- Módulo: {module_title}{duration_line}\n"
                f"- Status: {status}\n",
            )
            write_if_missing(
                lesson_dir / "links.md",
                f"# Links\n\n- [Abrir conteúdo na Hotmart]({source})\n",
            )

            detail = support_details.get(content_hash(source))
            if detail:
                description = str(detail.get("description") or "").strip()
                (lesson_dir / "descricao.md").write_text(
                    f"# {title}\n\n{description}\n",
                    encoding="utf-8",
                )
                link_lines = [f"- [Abrir conteúdo na Hotmart]({source})"]
                for link in detail.get("links") or []:
                    label = str(link.get("text") or "Abrir material").replace(
                        "[", "\\["
                    ).replace("]", "\\]")
                    link_lines.append(f"- [{label}]({link['href']})")
                (lesson_dir / "links.md").write_text(
                    "# Links\n\n" + "\n".join(link_lines) + "\n",
                    encoding="utf-8",
                )

    material_files = [
        path for path in root.glob("**/Materiais/*") if path.is_file()
    ]
    online_material_links = sum(
        len(row.get("links") or []) for row in support_details.values()
    )
    summary = (
        "# Resumo do curso\n\n"
        f"- Módulos: {len(modules)}\n"
        f"- Conteúdos: {sum(len(m['lessons']) for m in modules)}\n"
        f"- Páginas com vídeo: {video_count}\n"
        f"- Páginas de texto/material: {support_count}\n\n"
        f"- Arquivos de material salvos: {len(material_files)}\n"
        f"- Links de material online preservados: {online_material_links}\n\n"
        "Dois testes representativos, no módulo-base e no bônus, encontraram "
        "HLS criptografado/protegido. "
        "Conforme a regra de não contornar DRM ou criptografia, nenhum segmento "
        "protegido foi baixado. Os materiais e links permitidos ficam nas pastas "
        "de cada conteúdo.\n"
    )
    (root / "RESUMO.md").write_text(summary, encoding="utf-8")


if __name__ == "__main__":
    main()
