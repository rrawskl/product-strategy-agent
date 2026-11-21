"""Prepare a fresh synthetic project from the public brief and source notes."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

import agent

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "projects/public-case"
QUESTION = "Как команде вести регулярные задачи и нужен ли ей собственный трекер?"
SOURCES = (
    ("P1", "brief.md", "own_material", "sources/brief.md", "Учебный бриф и критерии", "F1"),
    ("P6", "calendar.md", "public_documentation", "https://www.todoist.com/help/todoist/features/use-the-calendar-layout-in-todoist-lPHRQTu0o", "Available for; View future occurrences", "F6"),
    ("P8", "pilot-template.csv", "own_prototype", "sources/pilot-template.csv", "Строки 2–21", "F8"),
)


def command(action, project, *args):
    result = subprocess.run([sys.executable, str(ROOT / "tools/agent.py"), action, str(project), *args],
                            env=dict(os.environ, PYTHONIOENCODING="utf-8"),
                            capture_output=True, encoding="utf-8")
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout


def prepare(project):
    command("init", project, QUESTION)
    state = json.loads((project / "state.json").read_text(encoding="utf-8"))
    with agent.writer(project):
        with (project / "sources/register.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(("id", "type", "locator", "accessed_at", "location", "claim_ids", "limitations"))
            for source_id, file, kind, locator, position, claim in SOURCES:
                destination = project / "sources" / file
                shutil.copyfile(DEMO / "sources" / file, destination)
                state["sources"].append({
                    "id": source_id, "type": kind, "status": "read", "depends_on": [],
                    "locator": f"sources/{file}", "sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
                })
                writer.writerow((source_id, kind, locator, "2026-11-08" if source_id == "P6" else "2026-11-09",
                                 position, claim, "Сохранённые публичные материалы учебного кейса."))
        state["claims"] = [
            {"id": "F1", "type": "fact_about_file", "status": "checked", "depends_on": ["P1"],
             "text": "В учебном брифе предусмотрено пять операций на участника за неделю."},
            {"id": "F6", "type": "fact_about_file", "status": "checked", "depends_on": ["P6"],
             "text": "Справка описывает будущие повторения в календаре с ограничением по режиму и плану."},
            {"id": "F8", "type": "fact_about_file", "status": "checked", "depends_on": ["P8"],
             "text": "В авторском прототипе 20 учебных задач на четыре недели."},
        ]
        state["decisions"] = [{
            "id": "R2", "type": "decision", "status": "proposed", "depends_on": ["F1", "F6", "F8"],
            "owner": None, "commitments": [], "text": "Предложить ограниченную проверку улучшенного шаблона.",
            "briefing": {
                "summary": "Предлагаю на две недели попробовать улучшенный шаблон.",
                "next_step": "Проверить первую задачу и выбрать, стоит ли начинать пилот.",
                "unknowns": ["Реальное время работы", "Доступность готового сервиса", "Согласованный владелец"],
            },
        }]
        agent.save(project, state)
        (project / "strategy.md").write_text(
            "# Стартовая карточка демонстрации\n\nПредлагается двухнедельный тест улучшенного шаблона [F1, F8]. "
            "Будущие повторы описаны и у готового сервиса [F6]. Решение о пилоте пока открыто. "
            "Полный разбор четырёх вариантов находится в projects/public-case/strategy.md исходной папки.\n",
            encoding="utf-8",
        )
    command("log", project, "--event", "Создана новая локальная копия демонстрации", "--tool", "replay_demo.py")
    command("checkpoint", project, "--stage", "human_choice", "--summary", "R2 ждёт выбора.",
            "--next-step", "Открыть полную стратегию и сравнить варианты.")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Проверить продолжение и пересмотр во временной копии")
    args = parser.parse_args()
    if args.check:
        with tempfile.TemporaryDirectory(prefix="product-demo-") as tmp:
            project = Path(tmp) / "public-case"
            prepare(project)
            state = json.loads(command("status", project))
            assert state["decisions"][0]["status"] == "proposed"
            assert state["next_step"] == "Открыть полную стратегию и сравнить варианты."
            result = json.loads(command("stale", project, "--claim", "P6", "--reason", "Тест нового графа"))
            assert {"P6", "F6", "R2"}.issubset(result["affected"])
            print("Демонстрация проверена: состояние восстановлено, пересмотр затронул P6, F6 и R2.")
        return
    project = ROOT / "demo-local/public-case"
    if project.exists():
        raise SystemExit("Локальная демонстрация уже есть. Продолжите работу в demo-local/public-case.")
    prepare(project)
    print(command("briefing", project).strip())


if __name__ == "__main__":
    main()
