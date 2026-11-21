#!/usr/bin/env python3
"""Состояние агента; Python 3.10+, стандартная библиотека, без LLM API."""
import argparse
import datetime
import json
import os
import csv
import copy
import math
import sys
import hashlib
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def save(root, state):
    state["updated_at"] = now()
    target = root / "state.json"
    temp = root / "state.json.tmp"
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.flush()
        os.fsync(stream.fileno())
    temp.replace(target)


@contextmanager
def writer(root):
    """Отказ второму писателю; после аварии lock проверяет человек, не удаляем молча."""
    lock = root / ".writer.lock"
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def nodes(state):
    return sum((state.get(k, []) for k in ("sources", "claims", "decisions")), [])


def graph_errors(state, strict=False):
    errors = []
    if not isinstance(state, dict):
        return ["state.json должен содержать объект"]
    for key in ("sources", "claims", "decisions"):
        if not isinstance(state.get(key), list):
            errors.append(f"{key}: требуется массив")
    if errors:
        return errors
    items = nodes(state)
    ids = [n.get("id") for n in items if isinstance(n, dict)]
    if len(ids) != len(items) or any(not isinstance(i, str) or not i for i in ids):
        return ["Каждый узел должен иметь непустой строковый id"]
    if len(ids) != len(set(ids)):
        errors.append("ID должны быть уникальны во всём графе")
    known = set(ids)
    edges = {}
    for n in items:
        deps = n.get("depends_on", [])
        if not isinstance(deps, list) or any(not isinstance(d, str) for d in deps):
            errors.append(f"{n['id']}: depends_on должен быть массивом ID")
            deps = []
        if set(deps) - known:
            errors.append(f"{n['id']}: неизвестные зависимости {sorted(set(deps)-known)}")
        edges[n["id"]] = deps
        if strict:
            for field in ("type", "status", "depends_on"):
                if field not in n or (field != "depends_on" and (not isinstance(n[field], str) or not n[field])):
                    errors.append(f"{n['id']}: нет {field}")
            if n in state["claims"] and (not isinstance(n.get("text"), str) or not n["text"].strip()):
                errors.append(f"{n['id']}: нет текста утверждения")
    visited, active = set(), set()
    def visit(node):
        if node in active:
            errors.append(f"Цикл зависимостей у {node}")
            return
        if node in visited:
            return
        active.add(node)
        for dep in edges.get(node, []):
            visit(dep)
        active.remove(node)
        visited.add(node)
    for node in edges:
        visit(node)
    return errors


def validate(root, state):
    errors = graph_errors(state, strict=True)
    if not isinstance(state, dict) or any(not isinstance(state.get(k), list)
        or any(not isinstance(n, dict) or not isinstance(n.get("id"), str) for n in state[k]) for k in ("sources", "claims", "decisions")):
        return {"valid": False, "errors": errors, "scope": "files and graph, not truth of claims"}
    by_id = {n["id"]: n for n in nodes(state)}
    for decision in state["decisions"]:
        card = decision.get("briefing")
        if card is None:
            continue
        if not isinstance(card, dict) or any(not isinstance(card.get(k), str) or not card[k].strip()
                                             for k in ("summary", "next_step")):
            errors.append(f"{decision['id']}: карточка должна содержать непустые summary/next_step")
        elif not isinstance(card.get("unknowns", []), list) or any(not isinstance(v, str) or not v.strip() for v in card.get("unknowns", [])):
            errors.append(f"{decision['id']}: unknowns карточки должен быть массивом строк")
    source_ids = {n["id"] for n in state["sources"]}
    for claim in state["claims"]:
        if claim.get("status") != "checked" or claim.get("type") in ("hypothesis", "forecast", "unknown"):
            continue
        todo, seen = [claim["id"]], set()
        while todo:
            dependency = todo.pop()
            if not isinstance(dependency, str) or dependency in seen:
                continue
            seen.add(dependency)
            deps = by_id.get(dependency, {}).get("depends_on", [])
            if isinstance(deps, list):
                todo.extend(deps)
        if not seen.intersection(source_ids):
            errors.append(f"{claim['id']}: проверенный вывод не связан с источником; укажите основание или маркируйте гипотезой")
    for node in nodes(state):
        if node.get("status") not in ("checked", "read", "proposed", "conditional_proposal", "accepted"):
            continue
        todo, seen = list(node.get("depends_on", [])) if isinstance(node.get("depends_on", []), list) else [], set()
        while todo:
            dependency = todo.pop()
            if not isinstance(dependency, str) or dependency in seen:
                continue
            seen.add(dependency)
            parent = by_id.get(dependency, {})
            if parent.get("status") == "stale":
                errors.append(f"{node['id']}: активный вывод использует устаревшее основание {dependency}")
            if parent.get("index_locator") and parent.get("status") == "indexed":
                errors.append(f"{node['id']}: импорт {dependency} ещё не прочитан; используйте excerpt и source-read")
            if isinstance(parent.get("depends_on", []), list):
                todo.extend(parent.get("depends_on", []))
    register = root / "sources/register.csv"
    rows = []
    if not register.is_file():
        errors.append("Нет sources/register.csv")
    else:
        with register.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"id", "type", "locator", "accessed_at", "location", "claim_ids", "limitations"}
            if not required.issubset(reader.fieldnames or []):
                errors.append("Неполный заголовок реестра источников")
            rows = list(reader)
        registered = [r.get("id") for r in rows]
        if len(registered) != len(set(registered)):
            errors.append("Повтор ID в реестре источников")
        source_ids = {n["id"] for n in state["sources"]}
        claim_ids = {n["id"] for n in state["claims"]}
        if set(registered) != source_ids:
            errors.append("ID реестра должны совпадать с узлами источников")
        for row in rows:
            try:
                datetime.date.fromisoformat(row.get("accessed_at", ""))
            except (ValueError, TypeError):
                errors.append(f"{row.get('id')}: accessed_at должен быть датой YYYY-MM-DD")
            refs = {v.strip() for v in (row.get("claim_ids") or "").split(",") if v.strip()}
            if refs - claim_ids:
                errors.append(f"{row.get('id')}: неизвестные claim_ids {sorted(refs-claim_ids)}")
        for source in state.get("sources", []):
            matching = [r for r in rows if r.get("id") == source.get("id")]
            if not matching:
                errors.append(f"{source.get('id')}: нет в реестре")
            elif any(not matching[0].get(k) for k in ("locator", "accessed_at", "location", "limitations")):
                errors.append(f"{source['id']}: неполная карточка источника")
    for source in state.get("sources", []):
        locator = source.get("locator", "")
        if not isinstance(locator, str) or not locator.strip():
            errors.append(f"{source.get('id')}: нет locator")
            continue
        matching = next((r for r in rows if r.get("id") == source.get("id")), {})
        if locator.startswith(("https://", "http://")):
            errors.append(f"{source['id']}: нужна локальная заметка с URL для продолжения без повторной загрузки")
            continue
        path = (root / locator).resolve()
        if not path.is_relative_to(root.resolve()):
            errors.append(f"{source['id']}: материал должен находиться внутри проекта")
            continue
        if not path.is_file():
            errors.append(f"{source['id']}: локальный материал недоступен: {locator}")
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if source.get("sha256") != digest:
            errors.append(f"{source['id']}: sha256 отсутствует/изменился; выполните stale, перечитайте и зарегистрируйте новую версию")
        for part in ("original", "index"):
            if part + "_locator" not in source:
                continue
            attachment = (root / source[part + "_locator"]).resolve()
            if not attachment.is_relative_to(root.resolve()) or not attachment.is_file():
                errors.append(f"{source['id']}: {part} недоступен/вне проекта")
            elif hashlib.sha256(attachment.read_bytes()).hexdigest() != source.get(part + "_sha256"):
                errors.append(f"{source['id']}: {part} sha256 изменился; stale и новая версия после перечитывания")
        if source.get("index_locator") and source.get("status") == "read_excerpt" and not source.get("reviewed_excerpts"):
            errors.append(f"{source['id']}: нет записи прочитанного фрагмента")
        registered_locator = matching.get("locator") or ""
        if registered_locator.startswith(("http://", "https://")):
            if registered_locator not in path.read_text(encoding="utf-8-sig"):
                errors.append(f"{source['id']}: URL реестра отсутствует в заметке")
        elif registered_locator and (root / registered_locator).resolve() != path:
            errors.append(f"{source['id']}: locator реестра не совпадает с материалом")
    _, event_errors = read_events(root)
    errors.extend(event_errors)
    if (root / "state.json.tmp").exists():
        errors.append("Обнаружен state.json.tmp: сверить после сбоя; не повторять внешние действия")
    if (root / "import.pending.json").exists():
        errors.append("Незавершённый импорт: сначала recover-import; checkpoint запрещён")
    return {"valid": not errors, "errors": errors, "scope": "files and graph, not truth of claims"}


def log_event(root, record):
    with (root / "events.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"at": now(), **record}, ensure_ascii=False, allow_nan=False) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def nonnegative(value):
    try:
        number = Decimal(value)
        if not number.is_finite() or number < 0:
            raise ValueError("Ожидается конечное неотрицательное число")
        result = float(number)
        if not math.isfinite(result):
            raise ValueError("Слишком большое число")
        return result
    except (InvalidOperation, TypeError) as exc:
        raise ValueError("Ожидается число или unknown") from exc


def read_events(root):
    records, errors = [], []
    path = root / "events.jsonl"
    if not path.is_file():
        return records, errors
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            record = json.loads(line)
            if not isinstance(record, dict) or not all(isinstance(record.get(k), str) and record[k].strip() for k in ("event", "at")):
                raise ValueError("нет строковых at/event")
            datetime.datetime.fromisoformat(record["at"])
            for field in ("cost", "elapsed_seconds", "retries", "human_minutes"):
                value = record.get(field)
                if field == "cost" and value == "unknown":
                    continue
                if value is not None:
                    if isinstance(value, bool):
                        raise ValueError(f"{field}: логическое значение вместо числа")
                    nonnegative(str(value))
                elif field == "cost" and field in record:
                    raise ValueError("cost: укажите число или unknown")
            if "cost" in record and (not isinstance(record.get("currency"), str) or not record["currency"].strip()):
                raise ValueError("нет валюты")
            if record.get("cost_scope", "operation") not in ("operation", "cumulative"):
                raise ValueError("неизвестный cost_scope")
            records.append(record)
        except (ValueError, TypeError) as exc:
            errors.append(f"events.jsonl:{number}: {exc}")
    return records, errors


def briefing(root, state):
    """Показывает сохранённое изложение; не создаёт стратегию и не вызывает LLM."""
    report = validate(root, state)
    if not report["valid"]:
        raise ValueError("Сначала нужно перепроверить материалы: " + "; ".join(report["errors"]))
    print("# " + state.get("question", "Текущий вопрос"))
    active = [n for n in state["decisions"] if n.get("status") in ("proposed", "conditional_proposal", "accepted")]
    stale = [n["id"] for n in state["decisions"] if n.get("status") == "stale"]
    if stale:
        print("\nЕсть устаревшие основания. Перед новым выбором нужно пересмотреть: " + ", ".join(stale))
    if not active:
        print("\nСохранённого актуального предложения пока нет.")
        print("Следующий шаг: " + state.get("next_step", "Уточнить вопрос и доступные материалы"))
    for decision in active:
        if decision["status"] == "accepted":
            print("\n## Человек выбрал: " + decision.get("selected_option", "см. запись выбора"))
            print(decision.get("rationale", "Основания сохранены в записи выбора"))
            print("Ответственный: " + (decision.get("owner") or "не указан"))
            print("Дата пересмотра: " + decision.get("review_date", "не указана"))
            print("Следующий шаг: проверить согласованные обязательства и метрики.")
        else:
            card = decision.get("briefing") or {}
            print("\n## Что я предлагаю")
            print(card.get("summary", decision.get("text", "Откройте полную стратегию")))
            print("\n## Что сделать сейчас")
            print(card.get("next_step", "Обсудить предложение с человеком, который принимает решение."))
            if card.get("unknowns"):
                print("\n## Что ещё нужно проверить")
                for item in card["unknowns"]:
                    print("- " + item)
            print("\nЭто предложение. Выбор человека и разрешение на расходы ещё не зафиксированы.")
        print("\nОснования " + decision["id"] + ": " + ", ".join(decision.get("depends_on", [])))
    print("\nПодробности, расчёты и источники: strategy.md")


def run(args, parser):
    root = Path(args.path)
    if args.cmd == "init":
        root.mkdir(parents=True, exist_ok=False)
        (root / "sources").mkdir()
        templates = Path(__file__).resolve().parents[1] / "templates"
        for source, target in (("brief.md", "sources/brief.md"), ("strategy.md", "strategy.md")):
            (root / target).write_text((templates / source).read_text(encoding="utf-8"), encoding="utf-8")
        (root / "sources/register.csv").write_text(
            "id,type,locator,accessed_at,location,claim_ids,limitations\n", encoding="utf-8")
        save(root, {"schema_version": 1, "question": args.question, "stage": "brief",
                    "summary": "", "next_step": "Уточнить цель и ограничения",
                    "sources": [], "claims": [], "decisions": [], "history": []})
        (root / "events.jsonl").write_text("", encoding="utf-8")
        print("created")
        return
    readonly = args.cmd in ("status", "validate", "review", "costs", "search", "briefing", "excerpt")
    if not readonly and args.cmd != "recover-import" and (root / "import.pending.json").exists():
        raise ValueError("Незавершённый импорт: recover-import вернёт локальные снимки до новой записи")
    if not readonly:
        with writer(root):
            operate(root, args, parser)
    else:
        operate(root, args, parser)


def operate(root, args, parser):
    state = json.loads((root / "state.json").read_text(encoding="utf-8-sig"))
    if args.cmd == "status":
        print(json.dumps(state, ensure_ascii=False, indent=2))
    elif args.cmd == "briefing":
        briefing(root, state)
    elif args.cmd == "validate":
        result = validate(root, state)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if not result["valid"]:
            raise SystemExit(1)
    elif args.cmd in ("ingest", "excerpt", "source-read", "recover-import"):
        # Парсер документов не загружается на обычном пути status/briefing/init.
        import material_import
        if args.cmd == "recover-import":
            result = material_import.recover(root, log_event)
        else:
            report = validate(root, state)
            if not report["valid"]:
                raise ValueError("Операция с материалом отклонена: " + "; ".join(report["errors"]))
            if args.cmd == "ingest":
                result = material_import.ingest(root, state, args, save, log_event, now)
            elif args.cmd == "source-read":
                result = material_import.mark_read(root, state, args, save, log_event, now)
            else:
                print(material_import.excerpt(root, state, args))
                return
        print(json.dumps(result, ensure_ascii=False))
    elif args.cmd == "search":
        query = args.query.casefold()
        hits = [{"id": n["id"], "text": n.get("text", ""), "locator": n.get("locator")}
                for n in nodes(state) if query in json.dumps(n, ensure_ascii=False).casefold()]
        for file in sorted((root / "sources").glob("*.md")):
            for line, content in enumerate(file.read_text(encoding="utf-8").splitlines(), 1):
                if query in content.casefold():
                    hits.append({"file": file.relative_to(root).as_posix(), "line": line, "text": content})
        print(json.dumps({"method": "lexical, not semantic", "hits": hits}, ensure_ascii=False, indent=2))
    elif args.cmd == "costs":
        totals, unknown, count, snapshots = {}, 0, 0, {}
        records, errors = read_events(root)
        if errors:
            raise ValueError("Смета не рассчитана: " + "; ".join(errors))
        for record in records:
            if "cost" not in record:
                continue
            count += 1
            if record["cost"] == "unknown" or record.get("currency", "unknown") == "unknown":
                unknown += 1
            else:
                currency = record["currency"]
                if record.get("cost_scope") == "cumulative":
                    totals[currency] = Decimal(str(record["cost"]))
                    snapshots[currency] = record["at"]
                else:
                    totals[currency] = totals.get(currency, Decimal("0")) + Decimal(str(record["cost"]))
        print(json.dumps({"known_totals": {k: str(v) for k, v in totals.items()},
                          "unknown_entries": unknown, "recorded_operations": count,
                          "complete": count > 0 and unknown == 0,
                          "latest_cumulative_snapshots": snapshots,
                          "scope": "recorded operations only; not provider billing"}, ensure_ascii=False))
    elif args.cmd == "review":
        as_of = datetime.date.fromisoformat(args.as_of)
        pending, stale = [], [n["id"] for n in nodes(state) if n.get("status") == "stale"]
        for decision in state["decisions"]:
            if decision.get("review_date") and datetime.date.fromisoformat(decision["review_date"]) <= as_of:
                pending.append({"decision": decision["id"], "reason": "review due"})
            for task in decision.get("commitments", []):
                if task.get("status") != "done" and datetime.date.fromisoformat(task["due_date"]) <= as_of:
                    pending.append({"decision": decision["id"], "commitment": task["id"], "reason": "commitment due"})
        print(json.dumps({"as_of": args.as_of, "stale": stale, "attention": pending,
                          "background_monitoring": False}, ensure_ascii=False, indent=2))
    elif args.cmd == "log":
        if not args.event.strip() or not args.currency.strip():
            raise ValueError("Событие и валюта не могут быть пустыми")
        cost = "unknown" if args.cost == "unknown" else nonnegative(args.cost)
        record = {"event": args.event, "model": args.model, "tool": args.tool,
                  "elapsed_seconds": args.elapsed_seconds, "cost": cost, "currency": args.currency,
                  "retries": args.retries, "human_minutes": args.human_minutes,
                  "cost_scope": args.cost_scope}
        if any(v is not None and (not math.isfinite(v) or v < 0) for v in (args.elapsed_seconds, args.retries, args.human_minutes)):
            raise ValueError("Время и повторы должны быть конечными неотрицательными числами")
        log_event(root, record)
        print("event recorded")
    else:
        errors = graph_errors(state)
        if errors:
            raise ValueError("; ".join(errors))
        if (root / "state.json.tmp").exists():
            raise ValueError("Найден state.json.tmp. Сверьте состояние и журнал до записи")
        if args.cmd == "checkpoint":
            report = validate(root, state)
            if not report["valid"]:
                raise ValueError("Checkpoint отклонён: " + "; ".join(report["errors"]))
            state.update(stage=args.stage, summary=args.summary, next_step=args.next_step)
            state.setdefault("history", []).append({"at": now(), "event": "checkpoint", "stage": args.stage})
            save(root, state)
            log_event(root, {"event": "checkpoint", "stage": args.stage,
                             "summary": args.summary, "next_step": args.next_step})
            print("checkpoint saved")
        elif args.cmd == "decide":
            report = validate(root, state)
            if not report["valid"]:
                raise ValueError("Выбор не записан: " + "; ".join(report["errors"]))
            decision = next((n for n in state["decisions"] if n["id"] == args.decision), None)
            if decision is None:
                raise ValueError("Неизвестное решение")
            if decision.get("status") == "superseded" or decision.get("superseded_by"):
                raise ValueError("Решение заменено: используйте актуальное предложение; пересмотр сначала сохраните явно")
            record_path = (root / args.record).resolve()
            if not record_path.is_relative_to(root.resolve()):
                raise ValueError("Запись выбора должна находиться внутри проекта")
            record = json.loads(record_path.read_text(encoding="utf-8-sig"))
            if not isinstance(record, dict):
                raise ValueError("Запись выбора должна быть объектом")
            for key in ("human_decision", "selected_option", "owner_role", "quote", "rationale",
                        "metric", "review_date", "decided_at", "commitments"):
                if key not in record or (key != "commitments" and not record[key]):
                    raise ValueError(f"В записи выбора нет {key}")
            if record["human_decision"] is not True:
                raise ValueError("Нужно реальное решение человека; предложение LLM не принимается")
            if any(not isinstance(record[k], str) for k in ("selected_option", "owner_role", "quote", "rationale", "metric", "review_date", "decided_at")):
                raise ValueError("Поля выбора должны быть строками")
            for date in (record["review_date"], record["decided_at"]):
                datetime.date.fromisoformat(date)
            if record["review_date"] < record["decided_at"]:
                raise ValueError("Пересмотр не может предшествовать решению")
            if not isinstance(record["commitments"], list):
                raise ValueError("commitments должен быть массивом")
            task_ids = set()
            for task in record["commitments"]:
                if not isinstance(task, dict):
                    raise ValueError("Обязательство должно быть объектом")
                for key in ("id", "text", "owner_role", "due_date", "metric", "status"):
                    if not isinstance(task.get(key), str) or not task[key]:
                        raise ValueError(f"В обязательстве нет {key}")
                datetime.date.fromisoformat(task["due_date"])
                if task["id"] in task_ids:
                    raise ValueError("Дубли ID обязательств")
                task_ids.add(task["id"])
            affected = {decision["id"]}
            while True:
                expanded = affected | {dep for n in nodes(state) if n["id"] in affected for dep in n.get("depends_on", [])}
                if expanded == affected:
                    break
                affected = expanded
            if any(n.get("status") == "stale" for n in nodes(state) if n["id"] in affected):
                raise ValueError("Сначала перепроверьте устаревшие основания")
            state.setdefault("history", []).append({"at": now(), "event": "human_decision",
                                                    "previous_node": copy.deepcopy(decision),
                                                    "approval_snapshot": copy.deepcopy(record),
                                                    "record": args.record})
            decision.update(status="accepted", owner=record["owner_role"], type="decision",
                            selected_option=record["selected_option"], metric=record["metric"],
                            review_date=record["review_date"], decided_at=record["decided_at"],
                            rationale=record["rationale"], approval_record=args.record,
                            commitments=record["commitments"])
            pending = [n["id"] for n in state["decisions"] if n.get("status") in ("proposed", "conditional_proposal")]
            state.update(stage="human_choice" if pending else "monitoring",
                         summary=f"Зафиксирован выбор {record['selected_option']} для {args.decision}",
                         next_step=("Получить выбор по " + ", ".join(pending)) if pending else
                         "Проверить обязательства и метрики командой review; внешние действия не выполнены")
            save(root, state)
            print("human decision recorded; no external actions executed")
        elif args.cmd == "stale":
            items = nodes(state)
            if args.claim not in {node["id"] for node in items}:
                parser.error("Неизвестный ID: состояние не изменено")
            affected = {args.claim}
            while True:
                more = {n["id"] for n in items if affected.intersection(n.get("depends_on", []))}
                if more.issubset(affected):
                    break
                affected.update(more)
            previous = {n["id"]: n.get("status", "current") for n in items if n["id"] in affected}
            snapshots = [copy.deepcopy(n) for n in items if n["id"] in affected]
            for node in items:
                if node["id"] in affected:
                    node["status"] = "stale"
            state.setdefault("history", []).append(
                {"at": now(), "event": "invalidate", "root": args.claim, "reason": args.reason,
                 "affected": sorted(affected), "previous": previous, "previous_nodes": snapshots})
            state["next_step"] = "Перепроверить " + ", ".join(sorted(affected)) + "; согласовать изменение решения"
            save(root, state)
            print(json.dumps({"affected": sorted(affected)}, ensure_ascii=False))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    init = sub.add_parser("init")
    init.add_argument("path")
    init.add_argument("question")
    for name in ("status", "log", "stale", "checkpoint", "validate", "decide", "review", "costs", "search", "briefing", "ingest", "excerpt", "source-read", "recover-import"):
        cmd = sub.add_parser(name)
        cmd.add_argument("path")
        if name == "ingest":
            cmd.add_argument("file")
            cmd.add_argument("--id", required=True)
            cmd.add_argument("--source-type", choices=("own_material", "public_documentation"), required=True)
            cmd.add_argument("--url")
            cmd.add_argument("--allowed-material", action="store_true", help="Открытый/собственный материал без персональных/закрытых данных")
        elif name in ("excerpt", "source-read"):
            cmd.add_argument("--source", required=True)
            cmd.add_argument("--unit", type=int, required=name == "source-read")
            cmd.add_argument("--start", type=int, default=1, help="Первая единица обзора")
            cmd.add_argument("--offset", type=int, default=0)
            cmd.add_argument("--length", type=int, default=2000)
            if name == "source-read":
                cmd.add_argument("--note", required=True, help="Что действительно прочитано и ограничения")
        elif name == "log":
            cmd.add_argument("--event", required=True)
            cmd.add_argument("--model", default="unknown")
            cmd.add_argument("--tool", default="unknown")
            cmd.add_argument("--elapsed-seconds", type=float)
            cmd.add_argument("--cost", default="unknown")
            cmd.add_argument("--cost-scope", choices=("operation", "cumulative"), default="operation")
            cmd.add_argument("--currency", default="unknown")
            cmd.add_argument("--retries", type=int, default=0)
            cmd.add_argument("--human-minutes", type=float)
        elif name == "decide":
            cmd.add_argument("--decision", required=True)
            cmd.add_argument("--record", required=True)
        elif name == "review":
            cmd.add_argument("--as-of", required=True, help="Дата сессии YYYY-MM-DD")
        elif name == "search":
            cmd.add_argument("--query", required=True)
        elif name == "stale":
            cmd.add_argument("--claim", required=True)
            cmd.add_argument("--reason", required=True)
        elif name == "checkpoint":
            cmd.add_argument("--stage", required=True)
            cmd.add_argument("--summary", required=True)
            cmd.add_argument("--next-step", required=True)
    args = parser.parse_args()
    try:
        if args.cmd in ("excerpt", "source-read") and (args.start < 1 or args.offset < 0 or not 1 <= args.length <= 5000 or (args.unit is not None and args.unit < 1)):
            raise ValueError("start/unit ≥1, offset ≥0, length 1–5000")
        run(args, parser)
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        parser.exit(2, f"Ошибка: {exc}\n")


if __name__ == "__main__":
    main()
