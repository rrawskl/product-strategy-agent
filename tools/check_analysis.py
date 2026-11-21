"""Regression checks for the table comparison entry point."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from analyze import compare


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "evaluation/retention.csv"
HEADER = "period,channel,accounts,returned\n"


class AnalysisTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)

    def write_csv(self, content):
        path = self.root / "input.csv"
        path.write_bytes(content.encode("utf-8"))
        return path

    def write_contract(self, **changes):
        settings = {
            "metric": "retention", "unit": "accounts", "window_days": 30,
            "group_basis": "channel at t0", "mature": True,
        }
        path = self.root / "comparison.json"
        data = {"periods": {"Q1": settings, "Q2": {**settings, **changes}}}
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def test_baseline_and_group_contributions(self):
        result = compare(FIXTURE, "Q1", "Q2")
        self.assertAlmostEqual(result["baseline_rate"], 0.34)
        self.assertAlmostEqual(result["current_rate"], 0.16)
        self.assertAlmostEqual(result["change_pp"], -18)
        self.assertAlmostEqual(result["composition_pp"], -18)
        self.assertAlmostEqual(result["within_group_pp"], 0)
        self.assertAlmostEqual(result["current_standardized_to_baseline"], 0.34)
        groups = {row["channel"]: row for row in result["groups"]}
        self.assertAlmostEqual(groups["organic"]["composition_pp"], -24)
        self.assertAlmostEqual(groups["paid"]["composition_pp"], 6)
        self.assertEqual(result["comparability"]["status"], "unknown")

    def test_reject_duplicate_or_blank_headers(self):
        for header, expected in (
            ("period,channel,accounts,accounts,returned\n", r"Строка 1.*повторяющийся заголовок 'accounts'"),
            ("period,channel,accounts,returned,\n", r"Строка 1, столбец 5.*заголовок"),
            ("period,channel,accounts,returned, accounts\n", r"Строка 1, столбец 5.*заголовок"),
        ):
            with self.subTest(header=header), self.assertRaisesRegex(ValueError, expected):
                compare(self.write_csv(header), "A", "B")

    def test_reject_repeated_group_even_when_counts_match(self):
        for second in ("A,same,100,2\n", "A,same,120,8\n"):
            path = self.write_csv(HEADER + "A,same,100,2\n" + second)
            with self.subTest(second=second), self.assertRaisesRegex(ValueError, r"Строка 3.*повтор группы.*строке 2"):
                compare(path, "A", "B")

    def test_reject_whitespace_in_period_and_channel(self):
        for row, field in ((" A,x,10,1\n", "period"), ("A,x ,10,1\n", "channel")):
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, rf"Строка 2, поле {field}"):
                compare(self.write_csv(HEADER + row), "A", "B")

    def test_bad_cells_and_multiline_line_number(self):
        # The note spans lines 2–3. The next record starts on physical line 4.
        prefix = HEADER.strip() + ',note\nA,x,10,1,"two\nlines"\n'
        for row, expected in (
            ("B,x,10,bad,note\n", r"Строка 4, поле returned: ожидается целое число"),
            ("B,x,0,0,note\n", r"Строка 4, поле accounts"),
            ("B,x,10,12,note\n", r"Строка 4, поле returned"),
            ("B,x,10\n", r"Строка 4, поле 'returned': отсутствует ячейка"),
            ("B,x,10,2,note,extra\n", r"Строка 4, столбец 6: лишняя ячейка"),
            ('B,x,10,2,"unclosed\n', r"Строка 4: повреждённая запись CSV"),
        ):
            with self.subTest(row=row), self.assertRaisesRegex(ValueError, expected):
                compare(self.write_csv(prefix + row), "A", "B")

    def test_parser_and_hash_share_single_byte_snapshot(self):
        content = ("\ufeff" + HEADER + "A,x,10,1\nB,x,10,2\n").encode("utf-8")
        with patch.object(Path, "read_bytes", side_effect=[content]) as read:
            result = compare(self.root / "input.csv", "A", "B")
        read.assert_called_once()
        self.assertEqual(result["input_sha256"], hashlib.sha256(content).hexdigest())
        self.assertAlmostEqual(result["change_pp"], 10)

    def test_contract_declares_or_surfaces_unknown_conditions(self):
        contract = self.write_contract()
        result = compare(FIXTURE, "Q1", "Q2", contract=contract)
        self.assertEqual(result["comparability"]["status"], "declared_comparable")
        self.assertEqual(result["comparability"]["contract_sha256"], hashlib.sha256(contract.read_bytes()).hexdigest())
        result = compare(FIXTURE, "Q1", "Q2", contract=self.write_contract(mature=None))
        self.assertEqual(result["comparability"]["status"], "unknown")
        self.assertEqual(result["comparability"]["unknown_fields"], [{"period": "Q2", "field": "mature"}])

    def test_contract_blocks_mismatch_and_immaturity(self):
        for field, value in (
            ("metric", "activation"), ("unit", "people"), ("window_days", 7),
            ("group_basis", "channel after retention"), ("mature", False), ("window_days", True),
        ):
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, field):
                compare(FIXTURE, "Q1", "Q2", contract=self.write_contract(**{field: value}))

    def test_cli_rejects_immature_period_without_output(self):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools/analyze.py"), str(FIXTURE),
             "--baseline", "Q1", "--current", "Q2", "--contract", str(self.write_contract(mature=False))],
            capture_output=True,
        )
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"mature", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)
