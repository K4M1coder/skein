import importlib
import json
import os
import tempfile
import threading
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
        self.assertGreaterEqual(len(defaults), 4)
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


if __name__ == "__main__":
    unittest.main()
