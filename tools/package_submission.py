"""Переносимый комплект по allowlist; не публикует и не отправляет файлы."""
import hashlib
import json
from pathlib import Path
import zipfile
import sys
import argparse

ROOT = Path(__file__).resolve().parents[1]
ALLOWED_SUFFIXES = {".py", ".md", ".json", ".jsonl", ".csv", ".txt"}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/product-strategy-agent.zip")
    args = parser.parse_args()
    archive = args.output.resolve()
    if archive.suffix.lower() != ".zip":
        parser.error("Выходной файл должен иметь расширение .zip; исходные файлы не перезаписываются")
    if not archive.is_relative_to(ROOT.resolve()):
        parser.error("Архив должен находиться внутри рабочей папки")
    if archive.is_relative_to((ROOT / "releases").resolve()):
        parser.error("Сохранённые лучшие версии в releases не перезаписываются; выберите выход в dist")
    selected = [ROOT / name for name in ("README.md", "START.md", "AGENTS.md", "TZ-AUDIT.md", ".gitignore", "requirements-import.txt")]
    if (ROOT / "BEST.md").is_file():
        selected.append(ROOT / "BEST.md")
    for folder in ("tools", "templates", "knowledge", "projects", "docs", "evidence", "evaluation", "qualification-v1", "releases"):
        selected.extend(p for p in (ROOT / folder).rglob("*")
                        if p.is_file() and p.suffix in ALLOWED_SUFFIXES
                        and not p.is_symlink() and p.resolve().is_relative_to(ROOT.resolve())
                        and not any(part in {"__pycache__", ".komanda", ".git", ".venv"} for part in p.parts))
    pdf = ROOT / "output/pdf/agent-description.pdf"
    if pdf.is_file():
        selected.append(pdf)
    # Отдельный зафиксированный BEST-1 включается как резервная копия, не как текущий выход.
    frozen_best = ROOT / "releases/BEST-1/agent.zip"
    if frozen_best.is_file() and not frozen_best.is_symlink():
        selected.append(frozen_best)
    # Только опубликованные авторские примеры; произвольные вложения не включаются.
    selected.extend(p for p in (ROOT / "projects/intake-case/sources/imports").rglob("original.*")
                    if p.is_file() and not p.is_symlink() and p.resolve().is_relative_to(ROOT.resolve())
                    and p.suffix in (".pdf", ".docx", ".xlsx"))
    selected.extend(sorted(p for p in (ROOT / "output/screenshots").glob("*") if p.suffix in (".png", ".jpg")))
    # Авторские рисунки новых кейсов и отчётов: ссылки работают и в распакованной копии.
    for name in ("projects/course-sales-test/evidence/comparison.png",
                 "evaluation/course-benchmark-v1/metrics.png",
                 "evaluation/agent-improvement-v2/metrics.png",
                 "qualification-v1/changes.patch"):
        chart = ROOT / name
        if chart.is_file() and not chart.is_symlink() and chart.resolve().is_relative_to(ROOT.resolve()):
            selected.append(chart)
    archive.parent.mkdir(parents=True, exist_ok=True)
    temporary = archive.with_suffix(archive.suffix + ".tmp")
    manifest = {}
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for path in sorted(set(selected)):
            name = path.relative_to(ROOT).as_posix()
            content = path.read_bytes()
            manifest[name] = hashlib.sha256(content).hexdigest()
            z.writestr(name, content)
        z.writestr("MANIFEST.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    temporary.replace(archive)
    print(json.dumps({"archive": str(archive), "files": len(manifest),
                      "sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
                      "published": False}, ensure_ascii=False))


if __name__ == "__main__":
    main()
