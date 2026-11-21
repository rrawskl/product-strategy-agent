"""Поведенческие проверки CLI в отдельных процессах и временной папке."""
import json
import os
from pathlib import Path
import subprocess
import sys
import hashlib
sys.dont_write_bytecode = True
import tempfile
import unittest
import importlib.util
import contextlib
import io

CLI = Path(__file__).with_name("agent.py").resolve()


class AgentTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "case"
        self.run_cli("init", "Почему падает удержание?")

    def run_cli(self, command, *args, success=True):
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        result = subprocess.run([sys.executable, str(CLI), command, str(self.root), *args],
                                capture_output=True, env=env)
        stdout = result.stdout.decode("utf-8")
        stderr = result.stderr.decode("utf-8")
        if success:
            self.assertEqual(result.returncode, 0, stderr)
            self.assertEqual(stderr, "")
        else:
            self.assertNotEqual(result.returncode, 0)
        return stdout

    def test_jsonl_unicode_multiple_events(self):
        for event in ("Сохранено", 'Текст с переносом\nи "кавычками"'):
            self.run_cli("log", "--event", event)
        lines = (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        records = [json.loads(line) for line in lines]
        self.assertEqual([r["event"] for r in records],
                         ["Сохранено", 'Текст с переносом\nи "кавычками"'])
        self.assertTrue(all(r["at"] for r in records))

    def test_resume_and_no_overwrite(self):
        self.run_cli("checkpoint", "--stage", "human_choice", "--summary",
                     "Варианты подготовлены; решение не принято", "--next-step",
                     "Запросить выбор руководителя")
        state_file = self.root / "state.json"
        before = state_file.read_bytes()
        restored = json.loads(self.run_cli("status"))
        self.assertEqual(restored["stage"], "human_choice")
        self.assertEqual(restored["question"], "Почему падает удержание?")
        self.assertEqual(restored["next_step"], "Запросить выбор руководителя")
        self.run_cli("init", "Повтор", success=False)
        self.assertEqual(state_file.read_bytes(), before)
        self.assertEqual(len(restored["history"]), 1)

    def test_dependency_invalidation_and_unknown_id(self):
        state_file = self.root / "state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state.update(sources=[{"id": "D2"}],
                     claims=[{"id": "C2", "depends_on": ["D2"]},
                             {"id": "C3", "depends_on": ["C2"]},
                             {"id": "C4", "depends_on": []}],
                     decisions=[{"id": "R1", "depends_on": ["C3"], "status": "proposed"}])
        state_file.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        before = state_file.read_bytes()
        self.run_cli("stale", "--claim", "missing", "--reason", "Ошибка", success=False)
        self.assertEqual(state_file.read_bytes(), before)
        output = json.loads(self.run_cli("stale", "--claim", "D2", "--reason", "Исправлен знаменатель"))
        self.assertEqual(output["affected"], ["C2", "C3", "D2", "R1"])
        restored = json.loads(self.run_cli("status"))
        self.assertEqual(restored["decisions"][0]["status"], "stale")
        self.assertNotIn("status", restored["claims"][2])
        self.assertEqual(restored["history"][-1]["previous"]["R1"], "proposed")
        self.assertIn("R1", restored["next_step"])

    def test_init_and_graph_validation(self):
        self.assertTrue((self.root / "sources/register.csv").is_file())
        self.assertTrue((self.root / "strategy.md").is_file())
        self.assertTrue(json.loads(self.run_cli("validate"))["valid"])
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["claims"] = [{"id": "C1", "text": "x", "type": "hypothesis",
                            "status": "unverified", "depends_on": ["C2"]},
                           {"id": "C2", "text": "y", "type": "hypothesis",
                            "status": "unverified", "depends_on": ["C1"]}]
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        result = json.loads(self.run_cli("validate", success=False))
        self.assertTrue(any("Цикл" in e for e in result["errors"]))
        self.run_cli("checkpoint", "--stage", "x", "--summary", "x", "--next-step", "x", success=False)
        self.assertEqual(before, path.read_bytes())
        state["claims"][1]["id"] = "C1"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])

    def test_malformed_shapes_and_finite_numbers(self):
        path = self.root / "state.json"
        original = path.read_bytes()
        for content in ('[]', '{"sources":[42],"claims":[],"decisions":[]}'):
            path.write_text(content, encoding="utf-8")
            self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])
        path.write_bytes(original)
        for option, value in (("--cost", "1e999"), ("--elapsed-seconds", "nan"), ("--human-minutes", "inf")):
            self.run_cli("log", "--event", "invalid", option, value, success=False)
        for event, currency in (("   ", "USD"), ("valid", ""), ("valid", "   ")):
            self.run_cli("log", "--event", event, "--cost", "1", "--currency", currency, success=False)
        self.assertEqual((self.root / "events.jsonl").read_text(encoding="utf-8"), "")
        self.assertTrue(json.loads(self.run_cli("validate"))["valid"])

    def test_briefing_is_readonly_and_respects_choice(self):
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["decisions"] = [{"id": "R1", "type": "decision", "status": "proposed", "depends_on": [],
                               "text": "Draft A", "briefing": {"summary": "Попробовать вариант A", "next_step": "Обсудить пилот", "unknowns": ["Эффект не измерен"]}}]
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        output = self.run_cli("briefing")
        self.assertIn("Попробовать вариант A", output)
        self.assertIn("Эффект не измерен", output)
        self.assertEqual(path.read_bytes(), before)
        state["decisions"][0]["briefing"] = None
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertIn("Draft A", self.run_cli("briefing"))
        state["decisions"][0]["briefing"] = {"summary": "Попробовать вариант A", "next_step": "Обсудить пилот", "unknowns": ["Эффект не измерен"]}
        state["decisions"][0].update(status="accepted", selected_option="C", rationale="TEST: chosen C", owner="test", review_date="2026-12-01")
        path.write_text(json.dumps(state), encoding="utf-8")
        output = self.run_cli("briefing")
        self.assertIn("Человек выбрал: C", output)
        self.assertNotIn("Попробовать вариант A", output)
        state["decisions"][0]["status"] = "stale"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertNotIn("Попробовать вариант A", self.run_cli("briefing"))
        state["decisions"][0]["briefing"]["unknowns"] = "invalid shape"
        path.write_text(json.dumps(state), encoding="utf-8")
        self.run_cli("briefing", success=False)

    def test_packaging_rejects_source_collision(self):
        spec = importlib.util.spec_from_file_location("package_under_test", CLI.with_name("package_submission.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.ROOT = self.root
        readme = self.root / "README.md"
        readme.write_text("SOURCE MUST SURVIVE", encoding="utf-8")
        before = readme.read_bytes()
        old_args = sys.argv
        try:
            sys.argv = ["package_submission.py", "--output", str(readme)]
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as caught:
                module.main()
            self.assertEqual(caught.exception.code, 2)
        finally:
            sys.argv = old_args
        self.assertEqual(readme.read_bytes(), before)
        self.assertFalse(readme.with_suffix(".md.tmp").exists())

    def test_missing_source_and_dependency(self):
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["sources"] = [{"id": "D1", "type": "document", "status": "read",
                             "locator": "sources/missing.md", "depends_on": []}]
        state["claims"] = [{"id": "C1", "text": "x", "type": "fact", "status": "checked",
                            "depends_on": ["missing"]}]
        path.write_text(json.dumps(state), encoding="utf-8")
        result = json.loads(self.run_cli("validate", success=False))
        self.assertGreaterEqual(len(result["errors"]), 3)

    def test_source_hash_and_register_contract(self):
        source = self.root / "sources/web.md"
        source.write_text("URL: https://example.org/source\nKnown evidence", encoding="utf-8")
        state_file = self.root / "state.json"
        state = json.loads(state_file.read_text(encoding="utf-8"))
        state["sources"] = [{"id": "D1", "type": "public_documentation", "status": "read",
                             "depends_on": [], "locator": "sources/web.md",
                             "sha256": hashlib.sha256(source.read_bytes()).hexdigest()}]
        state["claims"] = [{"id": "C1", "type": "fact", "status": "checked",
                            "text": "Known evidence", "depends_on": ["D1"]}]
        state["decisions"] = [{"id": "R1", "type": "decision", "status": "conditional_proposal", "depends_on": ["C1"]}]
        state_file.write_text(json.dumps(state), encoding="utf-8")
        register = self.root / "sources/register.csv"
        register.write_text("id,type,locator,accessed_at,location,claim_ids,limitations\n"
                            "D1,public_documentation,https://example.org/source,2026-11-08,body,C1,test\n", encoding="utf-8")
        self.assertTrue(json.loads(self.run_cli("validate"))["valid"])
        state["sources"][0]["status"] = "stale"
        state["claims"][0]["status"] = "unverified"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])
        state["sources"][0]["status"] = "read"
        state["claims"][0]["status"] = "checked"
        state_file.write_text(json.dumps(state), encoding="utf-8")
        source.write_text("URL: https://example.org/source\nChanged evidence", encoding="utf-8")
        before = state_file.read_bytes()
        self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])
        self.run_cli("checkpoint", "--stage", "research", "--summary", "x", "--next-step", "x", success=False)
        self.assertEqual(before, state_file.read_bytes())
        approval = {"human_decision": True, "selected_option": "A", "owner_role": "test",
                    "quote": "SIMULATED TEST ONLY", "rationale": "test", "metric": "test",
                    "review_date": "2026-11-10", "decided_at": "2026-11-08", "commitments": []}
        (self.root / "approval.json").write_text(json.dumps(approval), encoding="utf-8")
        self.run_cli("decide", "--decision", "R1", "--record", "approval.json", success=False)
        self.assertEqual(before, state_file.read_bytes())
        state["sources"][0]["locator"] = 42
        state["claims"][0]["text"] = 42
        state_file.write_text(json.dumps(state), encoding="utf-8")
        register.write_text(register.read_text(encoding="utf-8").replace("2026-11-08", "not-a-date").replace(",C1,", ",MISSING,"), encoding="utf-8")
        invalid = json.loads(self.run_cli("validate", success=False))
        self.assertGreaterEqual(len(invalid["errors"]), 4)

    def test_composition_analysis_and_bad_counts(self):
        analysis = CLI.with_name("analyze.py")
        fixture = CLI.parents[1] / "evaluation/retention.csv"
        result = subprocess.run([sys.executable, str(analysis), str(fixture), "--baseline", "Q1", "--current", "Q2"], capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertAlmostEqual(report["change_pp"], -18)
        self.assertAlmostEqual(report["composition_pp"], -18)
        self.assertAlmostEqual(report["within_group_pp"], 0)
        self.assertAlmostEqual(report["current_standardized_to_baseline"], .34)
        invalid = self.root / "bad.csv"
        invalid.write_text("period,channel,accounts,returned\nQ1,paid,1,2\nQ2,paid,1,1\n", encoding="utf-8")
        bad = subprocess.run([sys.executable, str(analysis), str(invalid), "--baseline", "Q1", "--current", "Q2"], capture_output=True)
        self.assertEqual(bad.returncode, 2)
        invalid.write_text("period,channel,accounts,returned\nQ1,organic,100\nQ2,organic,100,30\n", encoding="utf-8")
        short = subprocess.run([sys.executable, str(analysis), str(invalid), "--baseline", "Q1", "--current", "Q2"], capture_output=True)
        self.assertEqual(short.returncode, 2)
        self.assertNotIn(b"Traceback", short.stderr)

    def test_cost_unknown_and_currencies(self):
        self.run_cli("log", "--event", "unknown cost")
        self.run_cli("log", "--event", "known", "--cost", "0.1", "--currency", "USD")
        self.run_cli("log", "--event", "known", "--cost", "0.2", "--currency", "USD")
        self.run_cli("log", "--event", "known", "--cost", "10", "--currency", "RUB")
        report = json.loads(self.run_cli("costs"))
        self.assertEqual(report["known_totals"], {"USD": "0.3", "RUB": "10.0"})
        self.assertFalse(report["complete"])
        self.assertEqual(report["unknown_entries"], 1)
        self.run_cli("log", "--event", "snapshot1", "--cost", "147", "--currency", "CREDITS", "--cost-scope", "cumulative")
        self.run_cli("log", "--event", "snapshot2", "--cost", "230", "--currency", "CREDITS", "--cost-scope", "cumulative")
        self.run_cli("log", "--event", "later operation", "--cost", "2", "--currency", "CREDITS")
        latest = json.loads(self.run_cli("costs"))
        self.assertEqual(latest["known_totals"]["CREDITS"], "232.0")
        self.assertEqual(latest["known_totals"]["USD"], "0.3")
        before = (self.root / "events.jsonl").read_bytes()
        self.run_cli("log", "--event", "invalid", "--cost", "NaN", success=False)
        self.assertEqual(before, (self.root / "events.jsonl").read_bytes())

    def test_lock_and_interrupted_save(self):
        state_file = self.root / "state.json"
        before = state_file.read_bytes()
        (self.root / ".writer.lock").write_text("test")
        self.run_cli("checkpoint", "--stage", "x", "--summary", "x", "--next-step", "x", success=False)
        self.assertEqual(before, state_file.read_bytes())
        (self.root / ".writer.lock").unlink()
        (self.root / "state.json.tmp").write_text('{"interrupted":')
        self.run_cli("checkpoint", "--stage", "x", "--summary", "x", "--next-step", "x", success=False)
        self.assertEqual(before, state_file.read_bytes())
        self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])

    def test_imported_invalid_costs_are_rejected(self):
        path = self.root / "events.jsonl"
        for value in ("NaN", "-5", "not-a-number", None, True):
            path.write_text(json.dumps({"at": "2026-11-08T00:00:00+00:00", "event": "TEST ONLY",
                                       "cost": value, "currency": "USD"}) + "\n", encoding="utf-8")
            self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])
            result = subprocess.run([sys.executable, str(CLI), "costs", str(self.root)], capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertNotIn(b"Traceback", result.stderr)

    def test_checked_claim_needs_source_ancestry(self):
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["claims"] = [{"id": "C1", "text": "UNSUPPORTED TEST FACT", "type": "documentation_fact",
                            "status": "checked", "depends_on": []}]
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        self.assertFalse(json.loads(self.run_cli("validate", success=False))["valid"])
        self.run_cli("checkpoint", "--stage", "recommendation", "--summary", "x", "--next-step", "x", success=False)
        self.assertEqual(before, path.read_bytes())
        state["claims"][0].update(type="hypothesis", status="unverified")
        path.write_text(json.dumps(state), encoding="utf-8")
        self.assertTrue(json.loads(self.run_cli("validate"))["valid"])

    def test_decision_review_and_snapshot(self):
        path = self.root / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["claims"] = [{"id": "H1", "text": "test hypothesis", "type": "hypothesis",
                            "status": "unverified", "depends_on": []}]
        state["decisions"] = [{"id": "R1", "type": "decision", "status": "proposed",
                               "depends_on": ["H1"]}]
        path.write_text(json.dumps(state), encoding="utf-8")
        record = {"human_decision": False, "selected_option": "A", "owner_role": "product owner",
                  "quote": "SIMULATED TEST ONLY", "rationale": "test reversible check", "metric": "coverage",
                  "review_date": "2026-11-10", "decided_at": "2026-11-08", "commitments": [
                      {"id": "T1", "text": "test", "owner_role": "analyst", "metric": "coverage",
                       "due_date": "2026-11-09", "status": "planned"}]}
        (self.root / "approval.json").write_text(json.dumps(record), encoding="utf-8")
        self.run_cli("decide", "--decision", "R1", "--record", "approval.json", success=False)
        record["human_decision"] = True
        (self.root / "approval.json").write_text(json.dumps(record), encoding="utf-8")
        state["decisions"][0].update(status="superseded", superseded_by="R2")
        state["decisions"].append({"id": "R2", "type": "decision", "status": "proposed", "depends_on": ["H1"]})
        path.write_text(json.dumps(state), encoding="utf-8")
        before = path.read_bytes()
        self.run_cli("decide", "--decision", "R1", "--record", "approval.json", success=False)
        self.assertEqual(before, path.read_bytes())
        state["decisions"] = [{"id": "R1", "type": "decision", "status": "proposed", "depends_on": ["H1"]}]
        path.write_text(json.dumps(state), encoding="utf-8")
        self.run_cli("decide", "--decision", "R1", "--record", "approval.json")
        accepted = json.loads(self.run_cli("status"))
        self.assertEqual(accepted["stage"], "monitoring")
        self.assertEqual(accepted["history"][-1]["approval_snapshot"]["quote"], "SIMULATED TEST ONLY")
        review = json.loads(self.run_cli("review", "--as-of", "2026-11-10"))
        self.assertEqual(len(review["attention"]), 2)
        self.run_cli("stale", "--claim", "H1", "--reason", "test")
        saved = json.loads(self.run_cli("status"))
        self.assertEqual(saved["decisions"][0]["status"], "stale")
        self.assertEqual(saved["history"][-1]["previous_nodes"][-1]["selected_option"], "A")
        self.run_cli("decide", "--decision", "R1", "--record", "approval.json", success=False)


if __name__ == "__main__":
    unittest.main(verbosity=2)
