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

    def delete(self, client, path):
        request = Request(self.base + path, method="DELETE")
        with client.open(request, timeout=10) as response:
            return response.status, json.load(response)

    def wait_for_workflow(self, client, workflow_id, timeout=40):
        """Each workflow gets its own budget: a shared deadline makes the second wait flaky."""
        deadline = time.time() + timeout
        while True:
            _, data = self.request(client, f"/api/workflows/{workflow_id}")
            if data["workflow"]["status"] in ("COMPLETED", "FAILED") or time.time() >= deadline:
                return data
            time.sleep(0.1)

    def test_history_deletion_respects_owner_and_global_permissions(self):
        admin = self.client()
        _, admin_session = self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.assertIn("workflows.delete_all", admin_session["user"]["permissions"])
        _, created = self.request(admin, "/api/users", {"username": "history-owner", "password": "history-password", "profiles": ["workflow_operator"]})
        owner_id = created["id"]
        owner = self.client()
        _, owner_session = self.request(owner, "/api/auth/login", {"username": "history-owner", "password": "history-password"})
        self.assertIn("workflows.delete_own", owner_session["user"]["permissions"])
        self.assertNotIn("workflows.delete_all", owner_session["user"]["permissions"])

        own_workflow = "history-own-workflow"
        admin_workflow = "history-admin-workflow"
        now = app.stamp()
        with app.db() as conn:
            conn.execute("INSERT INTO workflows(id,objective,status,created_at,updated_at,owner_id) VALUES(?,?,?,?,?,?)", (own_workflow,"Owner history","COMPLETED",now,now,owner_id))
            conn.execute("INSERT INTO workflows(id,objective,status,created_at,updated_at,owner_id) VALUES(?,?,?,?,?,?)", (admin_workflow,"Admin history","COMPLETED",now,now,admin_session["user"]["id"]))
        artifact_path = app.artifact_root(own_workflow) / "result.txt"
        artifact_path.write_text("history deliverable", encoding="utf-8")
        workflow_storage = artifact_path.parents[1]
        with app.db() as conn:
            conn.execute("INSERT INTO artifacts(id,workflow_id,task_id,relative_path,disk_path,kind,validation,created_at) VALUES(?,?,?,?,?,?,?,?)", ("history-artifact",own_workflow,"task","result.txt",str(artifact_path),"text","{}",now))

        with self.assertRaises(HTTPError) as global_denied:
            self.delete(owner, "/api/workflows/history?scope=all")
        self.assertEqual(global_denied.exception.code, 403)
        status, own_result = self.delete(owner, "/api/workflows/history?scope=own")
        self.assertEqual(status, 200)
        self.assertEqual(own_result["deleted_workflows"], 1)
        self.assertEqual(own_result["deleted_artifacts"], 1)
        self.assertFalse(workflow_storage.exists(),f"{workflow_storage} {own_result}")
        with app.db() as conn:
            self.assertIsNone(conn.execute("SELECT 1 FROM workflows WHERE id=?", (own_workflow,)).fetchone())
            self.assertIsNotNone(conn.execute("SELECT 1 FROM workflows WHERE id=?", (admin_workflow,)).fetchone())

        _, all_result = self.delete(admin, "/api/workflows/history?scope=all")
        self.assertGreaterEqual(all_result["deleted_workflows"], 1)
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM workflows").fetchone()[0], 0)

    def test_workflow_template_sharing_respects_ownership(self):
        admin = self.client()
        _, admin_session = self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.assertIn("workflow_templates.manage_all", admin_session["user"]["permissions"])
        self.request(admin, "/api/users", {"username": "template-owner", "password": "template-password", "profiles": ["workflow_operator"]})
        self.request(admin, "/api/users", {"username": "template-reader", "password": "template-password", "profiles": ["workflow_operator"]})
        owner = self.client(); reader = self.client()
        _, owner_session = self.request(owner, "/api/auth/login", {"username": "template-owner", "password": "template-password"})
        self.request(reader, "/api/auth/login", {"username": "template-reader", "password": "template-password"})
        self.assertIn("workflow_templates.manage_own", owner_session["user"]["permissions"])
        payload = {
            "name": "Owned review workflow", "description": "Private review flow", "objective_template": "Review the requested content", "tags": ["review"], "shared": False,
            "tasks": [
                {"key": "review", "title": "Review the content", "role": "reviewer", "dependencies": [], "complexity": .4, "risk": .3, "criticality": .6},
                {"key": "deliver", "title": "Deliver the reviewed content", "role": "integrator", "dependencies": ["review"], "complexity": .3, "risk": .2, "criticality": .8},
            ],
        }
        _, created = self.request(owner, "/api/workflow-templates", payload)
        _, reader_private = self.request(reader, "/api/workflow-templates")
        self.assertNotIn(created["id"], {template["id"] for template in reader_private})
        _, shared = self.request(owner, f"/api/workflow-templates/{created['id']}", payload | {"shared": True})
        self.assertTrue(shared["shared"])
        _, reader_visible = self.request(reader, "/api/workflow-templates")
        self.assertIn(created["id"], {template["id"] for template in reader_visible})
        with self.assertRaises(HTTPError) as update_denied:
            self.request(reader, f"/api/workflow-templates/{created['id']}", payload | {"name": "Unauthorized edit"})
        self.assertEqual(update_denied.exception.code, 403)
        _, admin_update = self.request(admin, f"/api/workflow-templates/{created['id']}", payload | {"name": "Administrator edit", "shared": True})
        self.assertEqual(admin_update["name"], "Administrator edit")
        with self.assertRaises(HTTPError) as delete_denied:
            self.delete(reader, f"/api/workflow-templates/{created['id']}")
        self.assertEqual(delete_denied.exception.code, 403)
        _, deleted = self.delete(owner, f"/api/workflow-templates/{created['id']}")
        self.assertTrue(deleted["deleted"])

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
        with self.assertRaises(HTTPError) as telemetry_denied:
            self.request(user, "/api/hardware/telemetry")
        self.assertEqual(telemetry_denied.exception.code, 403)

        status, workflow = self.request(user, "/api/workflows", {"objective": "Return a short hello message"})
        self.assertEqual(status, 201)
        with self.assertRaises(HTTPError) as continuation_denied:
            self.request(admin, "/api/workflows", {"objective": "Attempt to access another user's session", "continue_workflow_id": workflow["id"]})
        self.assertEqual(continuation_denied.exception.code, 403)
        workflow_data = self.wait_for_workflow(user, workflow["id"])
        self.assertEqual(workflow_data["workflow"]["status"], "COMPLETED")
        _, continuation = self.request(user, "/api/workflows", {"objective": "Continue my own session", "continue_workflow_id": workflow["id"]})
        with app.db() as conn:
            source_session = conn.execute("SELECT session_id FROM workflows WHERE id=?", (workflow["id"],)).fetchone()[0]
            continued = conn.execute("SELECT session_id,continued_from FROM workflows WHERE id=?", (continuation["id"],)).fetchone()
        self.assertEqual(continued["session_id"], source_session)
        self.assertEqual(continued["continued_from"], workflow["id"])
        continuation_data = self.wait_for_workflow(user, continuation["id"])
        self.assertEqual(continuation_data["workflow"]["status"], "COMPLETED")
        _, user_runs = self.request(user, "/api/workflows")
        self.assertEqual([row["id"] for row in user_runs], [continuation["id"], workflow["id"]])

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
        self.assertTrue({"super_admin", "user_manager", "settings_manager", "model_manager", "workflow_operator", "workflow_runner", "workflow_designer", "stats_auditor"}.issubset(profile_ids))
        profiles_by_id = {profile["id"]: profile for profile in profiles}
        self.assertCountEqual(profiles_by_id["workflow_designer"]["permissions"], ["workflow_templates.manage_own", "workflow_templates.read"])
        self.assertIn("workflows.execute", profiles_by_id["workflow_runner"]["permissions"])
        self.assertNotIn("workflow_templates.manage_own", profiles_by_id["workflow_runner"]["permissions"])

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
        self.assertCountEqual(settings_session["user"]["permissions"], ["email.manage", "settings.manage"])
        _, current_settings = self.request(settings_manager, "/api/admin/settings")
        self.assertIn("users_can_choose_execution_mode", current_settings)
        _, smtp = self.request(settings_manager, "/api/admin/email", {"host": "smtp.example.test", "port": 587, "security": "starttls", "username": "mailer", "password": "smtp-test-secret", "from_address": "skein@example.test"})
        self.assertTrue(smtp["configured"])
        self.assertNotIn("password", smtp)
        self.assertEqual(app.smtp_configuration(True)["password"], "smtp-test-secret")
        sent = []
        original_sender = app.send_email
        app.send_email = lambda recipient, subject, body: sent.append(recipient)
        try:
            _, test_delivery = self.request(settings_manager, "/api/admin/email/test", {"recipient": "admin@example.test"})
            self.assertTrue(test_delivery["sent"])
            self.assertEqual(sent, ["admin@example.test"])
        finally:
            app.send_email = original_sender
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

    def test_user_creation_rejects_case_variant_duplicates(self):
        """Login matches usernames case-insensitively, so a case-variant duplicate would
        permanently shadow one of the two accounts behind the same login name."""
        admin = self.client()
        self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.request(admin, "/api/users", {"username": "case-owner", "password": "case-password", "profiles": ["workflow_operator"]})
        with self.assertRaises(HTTPError) as duplicate:
            self.request(admin, "/api/users", {"username": "Case-Owner", "password": "case-password-2", "profiles": ["workflow_operator"]})
        self.assertEqual(duplicate.exception.code, 409)
        variant = self.client()
        status, session = self.request(variant, "/api/auth/login", {"username": "CASE-OWNER", "password": "case-password"})
        self.assertEqual(status, 200)
        self.assertEqual(session["user"]["username"], "case-owner")

    def test_login_failures_are_throttled_per_account(self):
        admin = self.client()
        self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.request(admin, "/api/users", {"username": "throttle-target", "password": "throttle-password", "profiles": ["workflow_operator"]})
        attacker = self.client()
        for _ in range(5):
            with self.assertRaises(HTTPError) as failed:
                self.request(attacker, "/api/auth/login", {"username": "throttle-target", "password": "wrong-password"})
            self.assertEqual(failed.exception.code, 401)
        with self.assertRaises(HTTPError) as throttled:
            self.request(attacker, "/api/auth/login", {"username": "throttle-target", "password": "throttle-password"})
        self.assertEqual(throttled.exception.code, 429)
        # Only failures consume quota: other accounts from the same address stay unaffected.
        fresh = self.client()
        status, _ = self.request(fresh, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        self.assertEqual(status, 200)

    def test_password_reset_evicts_existing_sessions(self):
        admin = self.client()
        self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        _, created = self.request(admin, "/api/users", {"username": "reset-target", "password": "reset-password", "profiles": ["workflow_operator"]})
        victim = self.client()
        self.request(victim, "/api/auth/login", {"username": "reset-target", "password": "reset-password"})
        status, _ = self.request(victim, "/api/auth/me")
        self.assertEqual(status, 200)
        self.request(admin, f"/api/users/{created['id']}", {"password": "reset-password-2"})
        with self.assertRaises(HTTPError) as evicted:
            self.request(victim, "/api/auth/me")
        self.assertEqual(evicted.exception.code, 401)
        status, _ = self.request(admin, "/api/auth/me")
        self.assertEqual(status, 200)

    def test_health_and_runtime_overview_require_a_session(self):
        for path in ("/api/health", "/api/runtime-overview"):
            with self.assertRaises(HTTPError) as denied:
                self.request(self.client(), path)
            self.assertEqual(denied.exception.code, 401)
        admin = self.client()
        self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
        status, health = self.request(admin, "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(health["status"], "ok")

    def test_registration_email_code_lifecycle_and_manual_approval(self):
        delivered = []
        original_sender = app.send_email
        app.send_email = lambda recipient, subject, body: delivered.append({"recipient": recipient, "subject": subject, "body": body})
        try:
            anonymous = self.client()
            status, registered = self.request(anonymous, "/api/auth/register", {"username": "pending-user", "email": "pending@example.test", "password": "pending-password", "language": "en"})
            self.assertEqual(status, 201)
            self.assertTrue(registered["verification_required"])
            self.assertTrue(registered["email_sent"])
            first_code = delivered[-1]["body"].split("Your Skein code is: ", 1)[1].splitlines()[0]

            pending = self.client()
            _, pending_session = self.request(pending, "/api/auth/login", {"username": "pending-user", "password": "pending-password"})
            self.assertFalse(pending_session["user"]["verified"])
            self.assertEqual(pending_session["user"]["permissions"], [])
            with self.assertRaises(HTTPError) as blocked:
                self.request(pending, "/api/workflows", {"objective": "This must remain blocked"})
            self.assertEqual(blocked.exception.code, 403)
            with self.assertRaises(HTTPError) as invalid:
                self.request(pending, "/api/auth/verify", {"code": "111111"})
            self.assertEqual(invalid.exception.code, 400)
            with self.assertRaises(HTTPError) as early_resend:
                self.request(pending, "/api/auth/resend", {"language": "en"})
            self.assertEqual(early_resend.exception.code, 429)

            with app.db() as conn:
                user_id = conn.execute("SELECT id FROM users WHERE username='pending-user'").fetchone()[0]
                conn.execute("UPDATE email_verification_codes SET created_at=created_at-61 WHERE user_id=?", (user_id,))
            _, resent = self.request(pending, "/api/auth/resend", {"language": "en"})
            self.assertTrue(resent["sent"])
            second_code = delivered[-1]["body"].split("Your Skein code is: ", 1)[1].splitlines()[0]
            self.assertNotEqual(first_code, second_code)
            with self.assertRaises(HTTPError):
                self.request(pending, "/api/auth/verify", {"code": first_code})
            _, verified = self.request(pending, "/api/auth/verify", {"code": second_code})
            self.assertTrue(verified["verified"])
            with self.assertRaises(HTTPError) as reused:
                self.request(pending, "/api/auth/verify", {"code": second_code})
            self.assertEqual(reused.exception.code, 400)

            self.request(anonymous, "/api/auth/register", {"username": "expired-user", "email": "expired@example.test", "password": "expired-password", "language": "en"})
            expired_code = delivered[-1]["body"].split("Your Skein code is: ", 1)[1].splitlines()[0]
            expired = self.client(); self.request(expired, "/api/auth/login", {"username": "expired-user", "password": "expired-password"})
            with app.db() as conn: conn.execute("UPDATE email_verification_codes SET expires_at=? WHERE user_id=(SELECT id FROM users WHERE username='expired-user')", (app.stamp()-1,))
            with self.assertRaises(HTTPError) as expired_response:
                self.request(expired, "/api/auth/verify", {"code": expired_code})
            self.assertEqual(expired_response.exception.code, 410)

            self.request(anonymous, "/api/auth/register", {"username": "manual-user", "email": "manual@example.test", "password": "manual-password", "language": "en"})
            admin = self.client(); _, admin_session = self.request(admin, "/api/auth/login", {"username": "admin", "password": "admin-test-password"})
            _, users = self.request(admin, "/api/users"); manual = next(user for user in users if user["username"] == "manual-user")
            _, approved = self.request(admin, f"/api/users/{manual['id']}/approve", {})
            self.assertTrue(approved["verified"])
            manual_client = self.client(); _, manual_session = self.request(manual_client, "/api/auth/login", {"username": "manual-user", "password": "manual-password"})
            self.assertTrue(manual_session["user"]["verified"])
            self.assertIn("workflows.execute", manual_session["user"]["permissions"])
        finally:
            app.send_email = original_sender


if __name__ == "__main__":
    unittest.main()
