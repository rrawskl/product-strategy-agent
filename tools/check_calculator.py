"""Проверки точности расчёта и отказа от неоднозначных данных."""

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
from case_calculator import calculate


CLI = Path(__file__).with_name("case_calculator.py").resolve()
EXAMPLE = CLI.parents[1] / "templates/calculation-example.json"


def amount(value, locator="строка 3"):
    return {"amount": value, "source_id": "S1", "locator": locator}


class CalculatorTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "расчёт.json"

    def run_calculator(self, data):
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return calculate(self.path)

    def test_closed_cohort_arithmetic_and_provenance(self):
        result = self.run_calculator({
            "currency": "RUB",
            "period": "Зрелая когорта A за 7 дней",
            "economics": {
                "receipts": amount("140000", "/cohorts/A/gross_payments"),
                "refunds": amount("10000"),
                "costs": {
                    "Обучение": amount("98000"), "Привлечение": amount("80000"),
                    "Обработка": amount("20000"), "Дополнительные часы": amount("3600"),
                    "Налоги": None,
                },
            },
        })
        self.assertEqual(result["economics"]["net_receipts"], "130000")
        self.assertEqual(result["economics"]["known_costs_total"], "201600")
        self.assertEqual(result["economics"]["result_after_known_costs"], "-71600")
        self.assertIsNone(result["economics"]["result_after_all_listed_costs"])
        self.assertEqual(result["unknown_inputs"], ["/economics/costs/Налоги"])
        self.assertEqual(result["evidence"]["/economics/receipts"],
                         amount("140000", "/cohorts/A/gross_payments"))
        self.assertEqual(result["input_sha256"], hashlib.sha256(self.path.read_bytes()).hexdigest())

    def test_cash_schedule_not_used_as_income_costs(self):
        result = self.run_calculator({
            "currency": "RUB", "period": "7 дней",
            "payments": {
                "upfront": {"Настройка": amount("30000")},
                "first_period_later": {"Замер": amount("600")},
                "recurring_at_start": {"Подписка": amount("10000")},
                "recurring_later": {"Бухгалтерия": None},
                "start_limit": amount("15000"), "first_period_limit": amount("40000"),
            },
            "capacity": {
                "available_hours": amount("20"),
                "work_hours": {"Заявки": amount("28")},
                "preparation_hours": amount("2"),
            },
        })
        payments = result["payments"]
        self.assertEqual(payments["before_launch"], "40000")
        self.assertEqual(payments["start_budget_headroom"], "-25000")
        self.assertEqual(payments["known_first_period"], "40600")
        self.assertIsNone(payments["first_period"])
        self.assertIsNone(payments["recurring_per_period"])
        self.assertFalse(payments["fits_first_period_limit"])
        self.assertNotIn("economics", result)
        capacity = result["capacity"]
        self.assertEqual(capacity["total_load_hours"], "30")
        self.assertEqual(capacity["gap_hours"], "10")
        self.assertEqual(capacity["reserve_hours"], "0")
        self.assertFalse(capacity["fits_capacity"])

    def test_decimal_zero_and_unknown_are_distinct(self):
        data = {"currency": "RUB", "period": "Месяц", "economics": {
            "receipts": amount(0.3), "refunds": amount(0),
            "costs": {"A/B": amount(0.1), "C~D": amount(0.2)},
        }}
        result = self.run_calculator(data)
        self.assertEqual(result["economics"]["result_after_all_listed_costs"], "0")
        self.assertIn("/economics/costs/A~1B", result["evidence"])
        self.assertIn("/economics/costs/C~0D", result["evidence"])
        data["economics"]["refunds"] = None
        missing = self.run_calculator(data)["economics"]
        self.assertIsNone(missing["net_receipts"])
        self.assertIsNone(missing["result_after_known_costs"])
        self.assertEqual(missing["known_costs_total"], "0.3")

    def test_rejects_duplicates_invalid_amounts_and_mixed_currency(self):
        self.path.write_text('{"period":"A","period":"B","capacity":{}}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Повтор поля JSON: period"):
            calculate(self.path)
        for invalid in (True, "-1", "NaN", "1e100", "0.0000000000001"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(ValueError, "/capacity/available_hours/amount"):
                self.run_calculator({"period": "7 дней", "capacity": {
                    "available_hours": amount(invalid), "work_hours": {}, "preparation_hours": amount(0),
                }})
        with self.assertRaisesRegex(ValueError, "/economics/receipts.*currency"):
            self.run_calculator({"period": "7 дней", "currency": "RUB", "economics": {
                "receipts": dict(amount(10), currency="USD"), "refunds": amount(0), "costs": {},
            }})
        with self.assertRaisesRegex(ValueError, "/capacity/available_hours/locator"):
            self.run_calculator({"period": "7 дней", "capacity": {
                "available_hours": amount(5, " "), "work_hours": {}, "preparation_hours": amount(0),
            }})

    def test_normal_entry_point_with_template(self):
        before = EXAMPLE.read_bytes()
        process = subprocess.run([sys.executable, str(CLI), str(EXAMPLE)], capture_output=True,
                                 encoding="utf-8", check=True)
        result = json.loads(process.stdout)
        self.assertEqual(result["payments"]["before_launch"], "10000")
        self.assertEqual(result["payments"]["first_period"], "12500")
        self.assertEqual(result["payments"]["recurring_per_period"], "5500")
        self.assertEqual(result["capacity"]["reserve_hours"], "3")
        self.assertTrue(result["capacity"]["fits_capacity"])
        self.assertEqual(EXAMPLE.read_bytes(), before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
