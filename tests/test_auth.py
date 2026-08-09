import http.cookiejar
import importlib
import json
import os
import tempfile
import threading
import time
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
        deadline = time.time() + 15
        while time.time() < deadline:
            _, workflow_data = self.request(user, f"/api/workflows/{workflow['id']}")
            if workflow_data["workflow"]["status"] in ("COMPLETED", "FAILED"):
                break
            time.sleep(0.1)
        _, user_runs = self.request(user, "/api/workflows")
        self.assertEqual([row["id"] for row in user_runs], [workflow["id"]])

        _, policy = self.request(admin, "/api/admin/settings", {"users_can_choose_execution_mode": True})
        self.assertTrue(policy["users_can_choose_execution_mode"])
        _, selected = self.request(user, "/api/execution-mode", {"mode": "sandbox"})
        self.assertEqual(selected["mode"], "sandbox")

    def test_default_profiles_and_privacy_safe_statistics(self):
        admin = self.client()
        _, session = self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.assertIn("users.manage", session["user"]["permissions"])
        _, profiles = self.request(admin, "/api/rbac/profiles")
        profile_ids = {profile["id"] for profile in profiles}
        self.assertTrue({"super_admin", "user_manager", "settings_manager", "model_manager", "workflow_operator", "stats_auditor"}.issubset(profile_ids))

        _, auditor_created = self.request(admin, "/api/users", {"username": "auditor", "password": "auditor-password", "profiles": ["stats_auditor"]})
        self.assertEqual([profile["id"] for profile in auditor_created["profiles"]], ["stats_auditor"])
        auditor = self.client()
        _, auditor_session = self.request(auditor, "/api/auth/login", {"username": "auditor", "password": "auditor-password"})
        self.assertEqual(auditor_session["user"]["permissions"], ["server_stats.read"])
        secret_objective = "Private customer result must never appear in statistics"
        _, private_workflow = self.request(admin, "/api/workflows", {"objective": secret_objective})
        deadline = time.time() + 15
        while time.time() < deadline:
            _, private_data = self.request(admin, f"/api/workflows/{private_workflow['id']}")
            if private_data["workflow"]["status"] in ("COMPLETED", "FAILED"): break
            time.sleep(0.1)
        _, stats = self.request(auditor, "/api/server-stats")
        serialized = json.dumps(stats).lower()
        self.assertNotIn(secret_objective.lower(), serialized)
        for forbidden in ("objective", "prompt", "deliverable", "artifacts", "username", "user_id"):
            self.assertNotIn(f'"{forbidden}":', serialized)
        self.assertTrue(stats["privacy"]["content_excluded"])
        self.assertGreater(len(stats["requests"]), 0)
        self.assertTrue(all(row["started_at"] and "total_tokens" in row and "energy_wh" in row for row in stats["requests"]))
        with self.assertRaises(HTTPError) as workflow_denied:
            self.request(auditor, "/api/workflows")
        self.assertEqual(workflow_denied.exception.code, 403)
        with self.assertRaises(HTTPError) as users_denied:
            self.request(auditor, "/api/users")
        self.assertEqual(users_denied.exception.code, 403)

        _, manager_created = self.request(admin, "/api/users", {"username": "user-manager", "password": "manager-password", "profiles": ["user_manager"]})
        manager = self.client()
        self.request(manager, "/api/auth/login", {"username": "user-manager", "password": "manager-password"})
        _, managed_users = self.request(manager, "/api/users")
        self.assertGreaterEqual(len(managed_users), 3)
        with self.assertRaises(HTTPError) as settings_denied:
            self.request(manager, "/api/admin/settings")
        self.assertEqual(settings_denied.exception.code, 403)
        with self.assertRaises(HTTPError) as models_denied:
            self.request(manager, "/api/models")
        self.assertEqual(models_denied.exception.code, 403)

        self.request(admin, "/api/users", {"username": "settings-manager", "password": "settings-password", "profiles": ["settings_manager"]})
        settings_manager = self.client()
        _, settings_session = self.request(settings_manager, "/api/auth/login", {"username": "settings-manager", "password": "settings-password"})
        self.assertEqual(settings_session["user"]["permissions"], ["settings.manage"])
        _, current_settings = self.request(settings_manager, "/api/admin/settings")
        self.assertIn("users_can_choose_execution_mode", current_settings)
        with self.assertRaises(HTTPError) as settings_models_denied:
            self.request(settings_manager, "/api/models")
        self.assertEqual(settings_models_denied.exception.code, 403)

        self.request(admin, "/api/users", {"username": "model-manager", "password": "models-password", "profiles": ["model_manager"]})
        model_manager = self.client()
        _, model_session = self.request(model_manager, "/api/auth/login", {"username": "model-manager", "password": "models-password"})
        self.assertCountEqual(model_session["user"]["permissions"], ["models.manage", "server_stats.read"])
        _, models = self.request(model_manager, "/api/models")
        self.assertIsInstance(models, list)
        _, model_stats = self.request(model_manager, "/api/server-stats")
        self.assertTrue(model_stats["privacy"]["content_excluded"])
        with self.assertRaises(HTTPError) as model_users_denied:
            self.request(model_manager, "/api/users")
        self.assertEqual(model_users_denied.exception.code, 403)

        admin_id = session["user"]["id"]
        with self.assertRaises(HTTPError) as last_super_admin:
            self.request(admin, f"/api/users/{admin_id}", {"profiles": ["user_manager"]})
        self.assertEqual(last_super_admin.exception.code, 409)


if __name__ == "__main__":
    unittest.main()
