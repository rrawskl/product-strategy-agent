"""Проверяемые расчёты экономики, платежей и нагрузки по одному JSON-файлу.

См. templates/calculation-example.json. Укажите общий период, валюту для
денежных секций и часы для нагрузки. В каждой позиции укажите сумму, ID
источника и точное место. null обозначает неизвестное, пустой список затрат
в виде {} обозначает отсутствие позиций в выбранном объёме расчёта.
Денежные и часовые результаты выдаются десятичными строками.
"""

import argparse
import hashlib
import json
import re
import sys
from decimal import Decimal, DecimalException, localcontext
from pathlib import Path


MAX_INPUT_BYTES = 1024 * 1024
AMOUNT_PATTERN = re.compile(r"[0-9]{1,20}(?:\.[0-9]{1,12})?\Z")
PAYMENT_FIELDS = (
    "upfront",
    "first_period_later",
    "recurring_at_start",
    "recurring_later",
)


def unique_keys(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError(f"Повтор поля JSON: {name}")
        result[name] = value
    return result


def reject_constant(value):
    raise ValueError(f"Недопустимое число: {value}")


def fields(value, path, required, optional=()):
    if not isinstance(value, dict):
        raise ValueError(f"{path}: ожидается объект JSON")
    missing = set(required) - value.keys()
    if missing:
        raise ValueError(f"{path}: отсутствуют поля {', '.join(sorted(missing))}")
    extra = value.keys() - set(required) - set(optional)
    if extra:
        names = ", ".join(sorted(extra))
        raise ValueError(f"{path}: неизвестные поля {names}. Используйте общий период, валюту и часы исходного формата")


def nonempty_text(value, path):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: требуется непустая строка")
    return value.strip()


def pointer_field(name):
    return name.replace("~", "~0").replace("/", "~1")


def decimal_text(value):
    if value == 0:
        return "0"
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class Inputs:
    def __init__(self):
        self.evidence = {}
        self.unknown = []

    def amount(self, value, path):
        if value is None:
            self.unknown.append(path)
            return None
        fields(value, path, ("amount", "source_id", "locator"))
        amount = value["amount"]
        if isinstance(amount, bool) or not isinstance(amount, (str, int, Decimal)):
            raise ValueError(f"{path}/amount: требуется неотрицательное десятичное число")
        if isinstance(amount, Decimal):
            valid = (amount.is_finite() and not amount.is_signed()
                     and amount.as_tuple().exponent >= -12 and amount.adjusted() <= 19)
        else:
            valid = AMOUNT_PATTERN.fullmatch(str(amount))
        if not valid:
            raise ValueError(f"{path}/amount: требуется от 1 до 20 цифр до точки и до 12 после неё")
        number = Decimal(amount)
        self.evidence[path] = {
            "amount": decimal_text(number),
            "source_id": nonempty_text(value["source_id"], f"{path}/source_id"),
            "locator": nonempty_text(value["locator"], f"{path}/locator"),
        }
        return number

    def amounts(self, value, path):
        if not isinstance(value, dict):
            raise ValueError(f"{path}: ожидается объект с именами и значениями позиций")
        amounts = []
        for name, entry in value.items():
            if nonempty_text(name, path) != name:
                raise ValueError(f"{path}: уберите пробелы по краям названия позиции")
            amounts.append(self.amount(entry, f"{path}/{pointer_field(name)}"))
        return amounts


def known_sum(values):
    return sum((value for value in values if value is not None), Decimal(0))


def total(values):
    return None if None in values else known_sum(values)


def difference(left, right):
    return None if left is None or right is None else left - right


def fits_limit(values, limit):
    """Известный нижний предел уже может доказать превышение лимита."""
    if limit is None:
        return None
    if known_sum(values) > limit:
        return False
    return None if None in values else True


def economics(data, inputs):
    fields(data, "/economics", ("receipts", "refunds", "costs"))
    receipts = inputs.amount(data["receipts"], "/economics/receipts")
    refunds = inputs.amount(data["refunds"], "/economics/refunds")
    costs = inputs.amounts(data["costs"], "/economics/costs")
    net_receipts = difference(receipts, refunds)
    return {
        "receipts": receipts,
        "refunds": refunds,
        "net_receipts": net_receipts,
        "known_costs_total": known_sum(costs),
        "all_listed_costs_total": total(costs),
        "result_after_known_costs": difference(net_receipts, known_sum(costs)),
        "result_after_all_listed_costs": difference(net_receipts, total(costs)),
    }


def payments(data, inputs):
    fields(data, "/payments", (*PAYMENT_FIELDS, "start_limit", "first_period_limit"))
    amounts = {name: inputs.amounts(data[name], f"/payments/{name}") for name in PAYMENT_FIELDS}
    start_amounts = amounts["upfront"] + amounts["recurring_at_start"]
    first_amounts = start_amounts + amounts["first_period_later"] + amounts["recurring_later"]
    recurring_amounts = amounts["recurring_at_start"] + amounts["recurring_later"]
    start_limit = inputs.amount(data["start_limit"], "/payments/start_limit")
    first_limit = inputs.amount(data["first_period_limit"], "/payments/first_period_limit")
    return {
        "before_launch": total(start_amounts),
        "first_period": total(first_amounts),
        "recurring_per_period": total(recurring_amounts),
        "known_before_launch": known_sum(start_amounts),
        "known_first_period": known_sum(first_amounts),
        "start_limit": start_limit,
        "first_period_limit": first_limit,
        "start_budget_headroom": difference(start_limit, total(start_amounts)),
        "first_period_budget_headroom": difference(first_limit, total(first_amounts)),
        "fits_start_limit": fits_limit(start_amounts, start_limit),
        "fits_first_period_limit": fits_limit(first_amounts, first_limit),
    }


def capacity(data, inputs):
    fields(data, "/capacity", ("available_hours", "work_hours", "preparation_hours"))
    work = inputs.amounts(data["work_hours"], "/capacity/work_hours")
    preparation = inputs.amount(data["preparation_hours"], "/capacity/preparation_hours")
    available = inputs.amount(data["available_hours"], "/capacity/available_hours")
    workload = work + [preparation]
    balance = difference(available, total(workload))
    return {
        "unit": "hours",
        "available_hours": available,
        "work_hours": total(work),
        "preparation_hours": preparation,
        "total_load_hours": total(workload),
        "known_load_hours": known_sum(workload),
        "gap_hours": None if balance is None else max(-balance, Decimal(0)),
        "reserve_hours": None if balance is None else max(balance, Decimal(0)),
        "fits_capacity": fits_limit(workload, available),
    }


def calculate(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        raise ValueError("Размер входного расчёта должен быть не больше 1 МиБ")
    data = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique_keys,
                      parse_float=Decimal, parse_constant=reject_constant)
    sections = {"economics": economics, "payments": payments, "capacity": capacity}
    fields(data, "/", ("period",), ("currency", "basis", *sections))
    active = [name for name in sections if name in data]
    if not active:
        raise ValueError("Добавьте секцию economics, payments или capacity")
    result = {
        "input_sha256": hashlib.sha256(raw).hexdigest(),
        "period": nonempty_text(data["period"], "/period"),
        "scope": "Расчёт по указанным значениям. Итоги охватывают только перечисленные статьи и не подтверждают полноту расходов или фактический эффект.",
    }
    if "basis" in data:
        result["basis"] = nonempty_text(data["basis"], "/basis")
    if "currency" in data or "economics" in data or "payments" in data:
        currency = data.get("currency")
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("/currency: используйте один трёхбуквенный код валюты, например RUB")
        result["currency"] = currency
    inputs = Inputs()
    # При лимите 1 МиБ и 20+12 цифрах на число 48 цифр хватает для точного суммирования.
    with localcontext() as context:
        context.prec = 48
        for name in active:
            section = sections[name](data[name], inputs)
            result[name] = {
                field: decimal_text(value) if isinstance(value, Decimal) else value
                for field, value in section.items()
            }
    result["evidence"] = inputs.evidence
    result["unknown_inputs"] = inputs.unknown
    return result


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="Файл исходных данных в формате JSON")
    args = parser.parse_args()
    try:
        result = calculate(args.input)
    except (ValueError, OSError, RecursionError, DecimalException) as error:
        parser.exit(2, f"Ошибка данных: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
