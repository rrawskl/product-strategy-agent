"""Воспроизводимая декомпозиция изменения доли по составу групп; stdlib.

CSV: period,channel,accounts,returned. Перед сравнением проверьте одинаковые
определение, окно наблюдения и признак группы. Причины не устанавливает.
"""
import argparse
import csv
import hashlib
import io
import json
from pathlib import Path
import sys


REQUIRED_COLUMNS = ("period", "channel", "accounts", "returned")
CONTRACT_FIELDS = ("metric", "unit", "window_days", "group_basis", "mature")


def read_record(reader):
    start = reader.line_num + 1
    try:
        return start, next(reader, None)
    except csv.Error as exc:
        raise ValueError(f"Строка {start}: повреждённая запись CSV ({exc})") from exc


def clean_label(value):
    # Пробелы внутри имён допустимы. Краевые пробелы могли бы скрыть дубль.
    return bool(value) and value == value.strip() and not any(c in value for c in "\0\r\n")


def read_groups(data, baseline, current):
    stream = io.StringIO(data.decode("utf-8-sig"), newline="")
    reader = csv.reader(stream, strict=True)
    _, header = read_record(reader)
    if not header:
        raise ValueError("Строка 1: отсутствуют заголовки CSV")
    columns = {}
    for index, name in enumerate(header, 1):
        if not clean_label(name):
            raise ValueError(f"Строка 1, столбец {index}: пустой или неоднозначный заголовок {name!r}")
        if name in columns:
            raise ValueError(
                f"Строка 1, столбец {index}: повторяющийся заголовок {name!r} "
                f"(ранее столбец {columns[name] + 1})"
            )
        columns[name] = index - 1
    missing = [name for name in REQUIRED_COLUMNS if name not in columns]
    if missing:
        raise ValueError(f"Строка 1: отсутствуют обязательные заголовки {', '.join(missing)}")

    groups = {baseline: {}, current: {}}
    first_lines = {}
    while True:
        line, values = read_record(reader)
        if values is None:
            return groups
        if not values:
            continue  # Пустые физические строки разделяют записи.
        if len(values) < len(header):
            raise ValueError(f"Строка {line}, поле {header[len(values)]!r}: отсутствует ячейка")
        if len(values) > len(header):
            raise ValueError(f"Строка {line}, столбец {len(header) + 1}: лишняя ячейка")
        row = {field: values[columns[field]] for field in REQUIRED_COLUMNS}
        for field in ("period", "channel"):
            if not clean_label(row[field]):
                raise ValueError(f"Строка {line}, поле {field}: пустое или неоднозначное имя {row[field]!r}")
        period, channel = row["period"], row["channel"]
        if period not in groups:
            raise ValueError(f"Строка {line}, поле period: неожиданный период {period!r}")
        key = (period, channel)
        if key in first_lines:
            raise ValueError(
                f"Строка {line}, поля period и channel: повтор группы {period!r}/{channel!r} "
                f"(первая запись в строке {first_lines[key]})"
            )
        counts = {}
        for field in ("accounts", "returned"):
            try:
                counts[field] = int(row[field])
            except ValueError as exc:
                raise ValueError(f"Строка {line}, поле {field}: ожидается целое число") from exc
        n, y = counts["accounts"], counts["returned"]
        if n <= 0:
            raise ValueError(f"Строка {line}, поле accounts: число должно быть больше нуля")
        if not 0 <= y <= n:
            raise ValueError(f"Строка {line}, поле returned: требуется 0 <= returned <= accounts")
        first_lines[key] = line
        groups[period][channel] = (n, y)


def unique_json_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Контракт сравнения: повторяющийся ключ {key!r}")
        result[key] = value
    return result


def compare_contract(path, baseline, current):
    if path is None:
        return {"status": "unknown", "reason": "Контракт сравнения не предоставлен"}
    data = Path(path).read_bytes()
    try:
        contract = json.loads(data.decode("utf-8-sig"), object_pairs_hook=unique_json_keys)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Контракт сравнения: неверный JSON в строке {exc.lineno}") from exc
    if not isinstance(contract, dict) or set(contract) != {"periods"}:
        raise ValueError("Контракт сравнения: нужен объект с полем periods")
    periods = contract["periods"]
    if not isinstance(periods, dict) or set(periods) != {baseline, current}:
        raise ValueError("Контракт сравнения: periods должен содержать оба указанных периода")

    unknown_fields = []
    for period, details in periods.items():
        if not isinstance(details, dict) or set(details) != set(CONTRACT_FIELDS):
            raise ValueError(f"Контракт сравнения, период {period!r}: нужны {', '.join(CONTRACT_FIELDS)}")
        for field, value in details.items():
            if value is None:
                unknown_fields.append({"period": period, "field": field})
                continue
            if field == "window_days":
                valid = type(value) is int and value > 0
            elif field == "mature":
                valid = type(value) is bool
            else:
                valid = isinstance(value, str) and clean_label(value)
            if not valid:
                raise ValueError(f"Контракт сравнения, период {period!r}, поле {field}: неверное значение")
        if details["mature"] is False:
            raise ValueError(f"Контракт сравнения, период {period!r}, поле mature: окно наблюдения не завершено")
    for field in CONTRACT_FIELDS:
        a, b = periods[baseline][field], periods[current][field]
        if a is not None and b is not None and a != b:
            raise ValueError(f"Контракт сравнения, поле {field}: значения периодов {baseline!r} и {current!r} различаются")
    return {
        "status": "unknown" if unknown_fields else "declared_comparable",
        "contract_sha256": hashlib.sha256(data).hexdigest(),
        "unknown_fields": unknown_fields,
        "scope": "Проверена согласованность деклараций. Первичные данные не проверены.",
    }


def compare(path, baseline, current, *, contract=None):
    if baseline == current or not all(isinstance(p, str) and clean_label(p) for p in (baseline, current)):
        raise ValueError("Нужны два разных и однозначно названных периода")
    # Хеш относится к тому же снимку байтов, который разобрал CSV-парсер.
    data = Path(path).read_bytes()
    groups = read_groups(data, baseline, current)
    a, b = groups[baseline], groups[current]
    if not a or a.keys() != b.keys():
        raise ValueError("Нужны два разных периода с одинаковым непустым набором групп")
    comparability = compare_contract(contract, baseline, current)
    na, nb = sum(v[0] for v in a.values()), sum(v[0] for v in b.values())
    ra, rb = sum(v[1] for v in a.values()) / na, sum(v[1] for v in b.values()) / nb
    details, mix, within, standardized = [], 0, 0, 0
    for group in sorted(a):
        wa, wb = a[group][0] / na, b[group][0] / nb
        pa, pb = a[group][1] / a[group][0], b[group][1] / b[group][0]
        group_mix = (wb-wa)*pa
        group_within = wb*(pb-pa)
        mix += group_mix
        within += group_within
        standardized += wa*pb
        details.append({"channel": group, "baseline_n": a[group][0], "current_n": b[group][0],
                        "baseline_rate": pa, "current_rate": pb, "change_pp": 100*(pb-pa),
                        "baseline_weight": wa, "current_weight": wb,
                        "composition_pp": 100*group_mix, "within_group_pp": 100*group_within})
    return {"input_sha256": hashlib.sha256(data).hexdigest(),
            "baseline": baseline, "current": current, "baseline_rate": ra, "current_rate": rb,
            "change_pp": 100*(rb-ra), "composition_pp": 100*mix, "within_group_pp": 100*within,
            "residual_pp": 100*((rb-ra)-mix-within), "current_standardized_to_baseline": standardized,
            "groups": details,
            "method": "sum((w2-w1)*p1) + sum(w2*(p2-p1)); standardization sum(w1*p2)",
            "causality": "not established; shared definitions and maturity must be verified",
            "comparability": comparability}


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv")
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--current", required=True)
    parser.add_argument("--contract", help="JSON с определениями и зрелостью для обоих периодов")
    args = parser.parse_args()
    try:
        print(json.dumps(compare(args.csv, args.baseline, args.current, contract=args.contract), ensure_ascii=False, indent=2))
    except (ValueError, OSError, KeyError) as exc:
        parser.exit(2, f"Ошибка данных: {exc}\n")


if __name__ == "__main__":
    main()
