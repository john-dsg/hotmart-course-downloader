"""Cria uma entrega enxuta com apenas aulas que tenham recursos úteis."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path
from urllib.parse import urlparse

from hotmark import numbered_name, sanitize_filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("index", type=Path)
    parser.add_argument("details", type=Path)
    parser.add_argument("destination", type=Path)
    return parser.parse_args()


def content_hash(url: str) -> str:
    return Path(urlparse(url).path).name


def material_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return [path for path in directory.rglob("*") if path.is_file()]


def markdown_for(lesson: dict, detail: dict) -> str:
    title = str(lesson["title"])
    source = str(lesson["href"])
    summary = str(detail.get("summary") or "").strip()
    lines = [f"# {title}", "", summary, "", "## Conteúdo", ""]
    lines.append(f"- [Abrir a aula na Hotmart]({source})")

    seen: set[str] = set()
    for link in detail.get("links") or []:
        url = str(link["url"])
        if url in seen:
            continue
        seen.add(url)
        label = str(link.get("label") or "Abrir recurso")
        label = label.replace("[", "\\[").replace("]", "\\]")
        lines.append(f"- [{label}]({url})")
    return "\n".join(lines).strip() + "\n"


def main() -> None:
    args = parse_args()
    if args.destination.exists():
        raise SystemExit(f"O destino já existe: {args.destination}")

    modules = json.loads(args.index.read_text(encoding="utf-8"))
    details = {
        str(row["hash"]): row
        for row in json.loads(args.details.read_text(encoding="utf-8"))
    }
    source_root = args.index.parent
    selected_lessons = 0
    copied_files = 0
    online_links = 0

    for module in modules:
        module_number = int(module["number"])
        module_title = sanitize_filename(str(module["title"]))
        source_module = source_root / numbered_name(module_number, module_title)

        for lesson_number, lesson in enumerate(module["lessons"], 1):
            lesson_hash = content_hash(str(lesson["href"]))
            detail = details.get(lesson_hash)
            lesson_title = sanitize_filename(str(lesson["title"]))
            source_lesson = source_module / numbered_name(lesson_number, lesson_title)
            files = material_files(source_lesson / "Materiais")
            if detail is None and not files:
                continue

            detail = detail or {"summary": "Arquivos úteis disponíveis nesta aula."}
            destination_module = args.destination / numbered_name(
                module_number, module_title
            )
            destination_lesson = destination_module / numbered_name(
                lesson_number, lesson_title
            )
            destination_lesson.mkdir(parents=True, exist_ok=True)
            (destination_lesson / "conteudo.md").write_text(
                markdown_for(lesson, detail), encoding="utf-8"
            )

            if files:
                shutil.copytree(
                    source_lesson / "Materiais",
                    destination_lesson / "Materiais",
                )
                copied_files += len(files)

            selected_lessons += 1
            online_links += len(
                {str(link["url"]) for link in detail.get("links") or []}
            )

    if selected_lessons == 0:
        raise SystemExit("Nenhuma aula útil foi encontrada.")

    readme = (
        "# MAAP — conteúdo útil organizado\n\n"
        "Esta é uma seleção enxuta: contém somente aulas com arquivos baixados "
        "ou links realmente úteis citados no curso. Não há pastas vazias, nem "
        "arquivos genéricos de descrição.\n\n"
        f"- Aulas selecionadas: {selected_lessons}\n"
        f"- Arquivos físicos: {copied_files}\n"
        f"- Links úteis preservados: {online_links}\n\n"
        "## Vídeos\n\n"
        "Os vídeos não foram salvos. Os testes realizados no player encontraram "
        "transmissões HLS criptografadas/protegidas e não houve tentativa de "
        "contornar essa proteção. Cada aula selecionada mantém o link para ser "
        "assistida normalmente na Hotmart.\n"
    )
    (args.destination / "LEIA-ME.md").write_text(readme, encoding="utf-8")
    print(
        json.dumps(
            {
                "lessons": selected_lessons,
                "files": copied_files,
                "links": online_links,
                "destination": str(args.destination.resolve()),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
