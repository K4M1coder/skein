"""A task fails only when it produced nothing usable, and a failure never
cancels the branches that do not depend on it."""
import importlib
import json
import os
import tempfile
import unittest
from pathlib import Path

TEST_DB = Path(tempfile.gettempdir()) / f"skein-resilience-{os.getpid()}.db"
os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_AUTH_DISABLED"] = "1"
app = importlib.import_module("app")

BACKEND_ERROR = {"summary": "Inference server failure", "deliverable": "", "files": [], "confidence": 0.0,
                 "assumptions": [], "evidence": [], "next_actions": [], "mode": "error", "error": "Backend unavailable"}

# root ─┬─ branch_a1 ── branch_a2 ─┬─ final ── report
#       └─ branch_b1 ──────────────┘
DIAMOND = [
    {"title": "root", "role": "architect", "dependencies": []},
    {"title": "branch_a1", "role": "coder", "dependencies": [0]},
    {"title": "branch_a2", "role": "tester", "dependencies": [1]},
    {"title": "branch_b1", "role": "translator", "dependencies": [0]},
    {"title": "final", "role": "integrator", "dependencies": [2, 3]},
    {"title": "report", "role": "workflow-reporter", "dependencies": [0, 1, 2, 3, 4]},
]


class WorkflowResilienceTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()

    @classmethod
    def tearDownClass(cls):
        app.POOL.shutdown(wait=True)
        for suffix in ("", "-wal", "-shm"):
            try: Path(str(TEST_DB) + suffix).unlink()
            except FileNotFoundError: pass

    def setUp(self):
        self.original_generate = app.ModelClient.generate
        self.calls = []

    def tearDown(self):
        app.ModelClient.generate = self.original_generate

    def run_workflow(self, specs, mutate=None):
        """Run one workflow whose model answers can be rewritten per task."""
        calls = self.calls

        def generate(client, task, objective, dependency_results=None, retry=False):
            calls.append(task["title"])
            result = client.simulate(task)
            replacement = mutate(task, calls) if mutate else None
            return replacement if replacement is not None else result

        app.ModelClient.generate = generate
        workflow_id = app.create_workflow("resilience objective", specs=specs)
        app.orchestrate(workflow_id)
        with app.db() as conn:
            workflow = conn.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()["status"]
            tasks = {row["title"]: dict(row) for row in
                     conn.execute("SELECT title,status,confidence,attempts,result FROM tasks WHERE workflow_id=?", (workflow_id,))}
        return workflow, tasks

    def test_missing_confidence_does_not_fail_a_task(self):
        def drop_confidence(task, calls):
            if task["title"] != "root": return None
            answer = {"summary": "done", "deliverable": "a real answer", "files": [], "mode": "live"}
            return answer

        status, tasks = self.run_workflow(DIAMOND, drop_confidence)
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(tasks["root"]["status"], "COMPLETED")
        self.assertIsNone(tasks["root"]["confidence"])
        self.assertEqual([t["status"] for t in tasks.values()], ["COMPLETED"] * len(DIAMOND))

    def test_low_confidence_never_fails_a_task(self):
        status, tasks = self.run_workflow(DIAMOND, lambda task, calls:
            {**task_answer(), "confidence": 0.05} if task["title"] == "branch_b1" else None)
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(tasks["branch_b1"]["status"], "COMPLETED")

    def test_unparsable_confidence_does_not_crash_the_orchestrator(self):
        status, tasks = self.run_workflow(DIAMOND, lambda task, calls:
            {**task_answer(), "confidence": "high"} if task["title"] == "root" else None)
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(tasks["root"]["confidence"], 0.85)

    def test_failure_blocks_only_its_own_descendants(self):
        status, tasks = self.run_workflow(DIAMOND, lambda task, calls:
            dict(BACKEND_ERROR) if task["title"] == "branch_a1" else None)
        self.assertEqual(status, "FAILED")
        self.assertEqual(tasks["branch_a1"]["status"], "FAILED")
        self.assertEqual(tasks["branch_a2"]["status"], "BLOCKED")
        self.assertEqual(tasks["final"]["status"], "BLOCKED")
        # The independent branch and the audit step still run.
        self.assertEqual(tasks["branch_b1"]["status"], "COMPLETED")
        self.assertEqual(tasks["report"]["status"], "COMPLETED")
        self.assertIn("branch_b1", self.calls)
        self.assertEqual(json.loads(tasks["branch_a2"]["result"])["mode"], "blocked")

    def test_a_transient_failure_is_retried(self):
        status, tasks = self.run_workflow(DIAMOND, lambda task, calls:
            dict(BACKEND_ERROR) if task["title"] == "branch_a1" and calls.count("branch_a1") == 1 else None)
        self.assertEqual(status, "COMPLETED")
        self.assertEqual(tasks["branch_a1"]["status"], "COMPLETED")
        self.assertGreaterEqual(self.calls.count("branch_a1"), 2)

    def test_retries_are_bounded(self):
        status, tasks = self.run_workflow(DIAMOND, lambda task, calls:
            dict(BACKEND_ERROR) if task["title"] == "branch_a1" else None)
        self.assertEqual(status, "FAILED")
        self.assertLessEqual(tasks["branch_a1"]["attempts"], app.MAX_TASK_ATTEMPTS)

    def test_a_crashing_task_is_recorded_instead_of_killing_the_workflow(self):
        def explode(task, calls):
            if task["title"] == "branch_a1": raise RuntimeError("simulated driver crash")
            return None

        status, tasks = self.run_workflow(DIAMOND, explode)
        self.assertEqual(status, "FAILED")
        self.assertEqual(tasks["branch_a1"]["status"], "FAILED")
        self.assertIn("simulated driver crash", json.loads(tasks["branch_a1"]["result"])["error"])
        self.assertEqual(tasks["branch_b1"]["status"], "COMPLETED")
        self.assertEqual(tasks["report"]["status"], "COMPLETED")

    def test_a_crash_before_the_task_starts_still_bounds_retries(self):
        """A task that dies before claiming itself must not be retried forever."""
        specs = [
            {"title": "solo", "role": "executor", "dependencies": []},
            {"title": "report", "role": "workflow-reporter", "dependencies": [0]},
        ]
        workflow_id = app.create_workflow("crash before start", specs=specs)
        original_run_task = app.run_task
        calls = []

        def crash(wid, tid, objective):
            with app.db() as conn:
                role = conn.execute("SELECT role FROM tasks WHERE id=?", (tid,)).fetchone()["role"]
            if role == "workflow-reporter": return original_run_task(wid, tid, objective)
            calls.append(tid)
            # Stop crashing eventually so an unbounded retry loop shows up as a call count, not a hang.
            if len(calls) > app.MAX_TASK_ATTEMPTS + 2: return original_run_task(wid, tid, objective)
            raise RuntimeError("died before claiming the task")

        app.run_task = crash
        app.ModelClient.generate = lambda client, task, objective, deps=None, retry=False: client.simulate(task)
        try:
            app.orchestrate(workflow_id)
        finally:
            app.run_task = original_run_task
        with app.db() as conn:
            row = conn.execute("SELECT status,attempts FROM tasks WHERE workflow_id=? AND title='solo'", (workflow_id,)).fetchone()
            status = conn.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()["status"]
        self.assertEqual(row["status"], "FAILED")
        self.assertEqual(row["attempts"], app.MAX_TASK_ATTEMPTS)
        self.assertEqual(len(calls), app.MAX_TASK_ATTEMPTS)
        self.assertEqual(status, "FAILED")

    def test_unknown_dependency_blocks_instead_of_raising(self):
        specs = [
            {"title": "solo", "role": "executor", "dependencies": []},
            {"title": "orphan", "role": "reviewer", "dependencies": []},
            {"title": "report", "role": "workflow-reporter", "dependencies": [0, 1]},
        ]
        workflow_id = app.create_workflow("dangling dependency", specs=specs)
        with app.db() as conn:
            conn.execute("UPDATE tasks SET dependencies=? WHERE workflow_id=? AND title='orphan'",
                         (json.dumps(["does-not-exist"]), workflow_id))
        app.ModelClient.generate = lambda client, task, objective, deps=None, retry=False: client.simulate(task)
        app.orchestrate(workflow_id)
        with app.db() as conn:
            rows = {row["title"]: row["status"] for row in
                    conn.execute("SELECT title,status FROM tasks WHERE workflow_id=?", (workflow_id,))}
            status = conn.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()["status"]
        self.assertEqual(rows["orphan"], "BLOCKED")
        self.assertEqual(rows["solo"], "COMPLETED")
        self.assertEqual(rows["report"], "COMPLETED")
        self.assertEqual(status, "FAILED")


class ConfidenceParsingTest(unittest.TestCase):
    def test_supported_forms(self):
        for raw, expected in [(0.8, 0.8), (1, 1.0), (85, 0.85), ("0.42", 0.42), ("85%", 0.85),
                              ("0,5", 0.5), ("high", 0.85), ("low", 0.35), (True, 1.0), (2.5, 0.025)]:
            with self.subTest(raw=raw):
                self.assertEqual(app.parse_confidence(raw), expected)

    def test_unknown_forms_stay_unknown(self):
        for raw in (None, "", "definitely", [], {}, float("nan"), float("inf")):
            with self.subTest(raw=raw):
                self.assertIsNone(app.parse_confidence(raw))

    def test_usability_ignores_confidence(self):
        self.assertTrue(app.llm_result_usable({"deliverable": "content"}))
        self.assertTrue(app.llm_result_usable({"summary": "", "files": [{"path": "a", "content": "b"}]}))
        self.assertFalse(app.llm_result_usable({"deliverable": "", "summary": ""}))
        self.assertFalse(app.llm_result_usable({"deliverable": "x", "error": "backend down"}))
        self.assertFalse(app.llm_result_usable({"deliverable": "x", "mode": "error"}))


def task_answer():
    return {"summary": "done", "deliverable": "a real answer", "files": [], "mode": "live"}


if __name__ == "__main__":
    unittest.main()
