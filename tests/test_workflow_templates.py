import importlib
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


TEST_DB = Path(tempfile.gettempdir()) / f"skein-templates-{os.getpid()}.db"
os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_AUTH_DISABLED"] = "1"
app = importlib.import_module("app")


class WorkflowTemplateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(TEST_DB) + suffix).unlink()
            except FileNotFoundError:
                pass

    def request(self, path, body=None, method=None):
        raw = json.dumps(body).encode() if body is not None else None
        request = Request(
            f"http://127.0.0.1:{self.port}{path}",
            raw,
            {"Content-Type": "application/json"},
            method=method,
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.load(response)

    def valid_template(self):
        return {
            "name": "Documentation delivery",
            "description": "Draft, review, and deliver documentation",
            "objective_template": "Document the requested system",
            "tags": ["documentation", "writing"],
            "shared": False,
            "tasks": [
                {"key": "draft", "title": "Draft complete documentation", "role": "executor", "dependencies": [], "complexity": 0.4, "risk": 0.2, "criticality": 0.6},
                {"key": "review", "title": "Review technical accuracy", "role": "reviewer", "dependencies": ["draft"], "complexity": 0.5, "risk": 0.3, "criticality": 0.7},
                {"key": "deliver", "title": "Deliver the approved documentation", "role": "integrator", "dependencies": ["draft", "review"], "complexity": 0.4, "risk": 0.2, "criticality": 0.8},
            ],
        }

    def test_defaults_are_seeded_and_immutable(self):
        _, templates = self.request("/api/workflow-templates")
        defaults = [template for template in templates if template["system"]]
        self.assertGreaterEqual(len(defaults), 8)
        self.assertTrue({"default-chat", "default-daily-assistance", "default-research", "default-code-specification"}.issubset({template["id"] for template in defaults}))
        self.assertTrue(all(template["validation"]["valid"] for template in defaults))
        with self.assertRaises(HTTPError) as caught:
            self.request(f"/api/workflow-templates/{defaults[0]['id']}", method="DELETE")
        self.assertEqual(caught.exception.code, 409)

    def test_create_update_share_and_delete_template(self):
        status, created = self.request("/api/workflow-templates", self.valid_template())
        self.assertEqual(status, 201)
        self.assertTrue(created["validation"]["valid"])
        self.assertTrue(created["permissions"]["edit"])
        payload = self.valid_template() | {"name": "Shared documentation delivery", "shared": True}
        _, updated = self.request(f"/api/workflow-templates/{created['id']}", payload)
        self.assertEqual(updated["name"], "Shared documentation delivery")
        self.assertTrue(updated["shared"])
        _, deleted = self.request(f"/api/workflow-templates/{created['id']}", method="DELETE")
        self.assertTrue(deleted["deleted"])

    def test_invalid_graph_is_rejected(self):
        payload = self.valid_template()
        payload["tasks"][0]["dependencies"] = ["deliver"]
        with self.assertRaises(HTTPError) as caught:
            self.request("/api/workflow-templates", payload)
        self.assertEqual(caught.exception.code, 400)
        error = json.load(caught.exception)["error"]
        self.assertIn("acyclic", error)

    def test_automatic_selection_and_generation_are_validated(self):
        _, selected = self.request("/api/workflow-templates/select", {"objective": "Build a Python API with complete tests"})
        self.assertEqual(selected["id"], "default-software")
        self.assertEqual(selected["selection"]["method"], "deterministic-test")
        self.assertTrue(selected["validation"]["valid"])
        _, generated = self.request("/api/workflow-templates/generate", {"objective": "Translate a product announcement into French"})
        self.assertEqual(generated["mode"], "simulation")
        self.assertTrue(generated["validation"]["valid"])
        self.assertEqual(generated["validation"]["terminal_task"], generated["tasks"][-1]["key"])

    def test_automatic_selection_covers_core_workflow_families(self):
        cases = (
            ("Chat and explain dependency injection simply", "default-chat"),
            ("Help organize my day and draft an email", "default-daily-assistance"),
            ("Research and compare sources about local inference", "default-research"),
            ("Derive a technical specification from this codebase architecture", "default-code-specification"),
        )
        for objective, expected in cases:
            _, selected = self.request("/api/workflow-templates/select", {"objective": objective})
            self.assertEqual(selected["id"], expected)

    def test_execution_modes_report_their_planning_source(self):
        requests = (
            ({"objective": "Translate this sentence into French", "planning_mode": "automatic"}, "automatic", "default-translation"),
            ({"objective": "Analyze an unusual operational request", "planning_mode": "generate"}, "generate", None),
            ({"objective": "Implement a tested Python module", "planning_mode": "template", "template_id": "default-software"}, "template", "default-software"),
        )
        workflow_ids = []
        for payload, expected_mode, expected_template in requests:
            status, created = self.request("/api/workflows", payload)
            self.assertEqual(status, 201)
            self.assertEqual(created["planning"]["mode"], expected_mode)
            self.assertEqual(created["planning"]["template_id"], expected_template)
            if expected_mode == "automatic":
                self.assertEqual(created["planning"]["selection_method"], "deterministic-test")
                self.assertIn("reason", created["planning"])
            workflow_ids.append(created["id"])
        deadline = time.time() + 20
        while time.time() < deadline:
            states = [self.request(f"/api/workflows/{workflow_id}")[1]["workflow"]["status"] for workflow_id in workflow_ids]
            if all(state in ("COMPLETED", "FAILED") for state in states): break
            time.sleep(.1)
        self.assertTrue(all(state == "COMPLETED" for state in states))


if __name__ == "__main__":
    unittest.main()
