import http.cookiejar
import importlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener


TEST_DB = Path(tempfile.gettempdir()) / f"skein-auth-{os.getpid()}.db"
os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_ADMIN_PASSWORD"] = "admin-test-password"
os.environ.pop("SKEIN_AUTH_DISABLED", None)
app = importlib.import_module("app")


class AuthTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        app.POOL.shutdown(wait=True)
        for suffix in ("", "-wal", "-shm"):
            try:
                Path(str(TEST_DB) + suffix).unlink()
            except FileNotFoundError:
                pass

    def client(self):
        return build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(self, client, path, body=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, payload, {"Content-Type": "application/json"})
        with client.open(request, timeout=10) as response:
            return response.status, json.load(response)

    def test_roles_policy_and_workflow_ownership(self):
        anonymous = self.client()
        with self.assertRaises(HTTPError) as denied:
            self.request(anonymous, "/api/workflows")
        self.assertEqual(denied.exception.code, 401)

        admin = self.client()
        _, session = self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.assertEqual(session["user"]["role"], "admin")
        status, created = self.request(admin, "/api/users", {"username": "operator", "password": "operator-password", "role": "user"})
        self.assertEqual(status, 201)
        self.assertEqual(created["role"], "user")

        user = self.client()
        _, user_session = self.request(user, "/api/auth/login", {"username": "operator", "password": "operator-password"})
        self.assertFalse(user_session["policy"]["users_can_choose_execution_mode"])
        with self.assertRaises(HTTPError) as mode_denied:
            self.request(user, "/api/execution-mode", {"mode": "local"})
        self.assertEqual(mode_denied.exception.code, 403)
        with self.assertRaises(HTTPError) as model_denied:
            self.request(user, "/api/models/autoload", {})
        self.assertEqual(model_denied.exception.code, 403)

        status, workflow = self.request(user, "/api/workflows", {"objective": "Return a short hello message"})
        self.assertEqual(status, 201)
        _, user_runs = self.request(user, "/api/workflows")
        self.assertEqual([row["id"] for row in user_runs], [workflow["id"]])

        _, policy = self.request(admin, "/api/admin/settings", {"users_can_choose_execution_mode": True})
        self.assertTrue(policy["users_can_choose_execution_mode"])
        _, selected = self.request(user, "/api/execution-mode", {"mode": "sandbox"})
        self.assertEqual(selected["mode"], "sandbox")


if __name__ == "__main__":
    unittest.main()
