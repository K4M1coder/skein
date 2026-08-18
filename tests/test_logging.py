"""The rotating, exhaustive operational log: every action/error via the HTTP dispatcher,
model/auth/settings lifecycle events, workflow events via the emit() hook, and rotation itself."""
import importlib
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

WORKSPACE = Path(tempfile.gettempdir()) / f"skein-logging-{os.getpid()}"
WORKSPACE.mkdir(parents=True, exist_ok=True)
TEST_DB = WORKSPACE / "skein.db"

os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_AUTH_DISABLED"] = "1"
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_LOG_LEVEL"] = "DEBUG"  # so GET requests (normally debug-only) show up too
os.environ["SKEIN_LOG_CONSOLE_LEVEL"] = "CRITICAL"  # keep the test's own console quiet
app = importlib.import_module("app")

LOG_FILE = app.LOG_DIR / "skein.log"


def log_text():
    for handler in app.logger.handlers:
        if hasattr(handler, "flush"): handler.flush()
    return LOG_FILE.read_text(encoding="utf-8", errors="replace") if LOG_FILE.exists() else ""


class LoggingSetupTest(unittest.TestCase):
    def test_rotating_file_handler_is_configured_alongside_a_console_handler(self):
        kinds = {type(h).__name__ for h in app.logger.handlers}
        self.assertIn("RotatingFileHandler", kinds)
        self.assertIn("StreamHandler", kinds)

    def test_reconfiguring_does_not_stack_duplicate_handlers(self):
        before = len(app.logger.handlers)
        app.configure_logging()
        self.assertEqual(len(app.logger.handlers), before)

    def test_relocating_the_log_directory_moves_the_file_handler_and_future_writes(self):
        """init_db() calls this when the default DB location is unwritable and it falls back
        to another directory; LOG_DIR was already fixed at import time (before init_db can
        run), so without an explicit relocation the log file would keep writing to a
        directory unrelated to where the database ended up living."""
        original_dir, original_file = app.LOG_DIR, app.LOG_FILE
        new_dir = WORKSPACE / "relocated-db"
        try:
            app.relocate_log_directory(new_dir)
            self.assertEqual(app.LOG_DIR, new_dir / "logs")
            self.assertEqual(app.LOG_FILE, new_dir / "logs" / "skein.log")
            file_handlers = [h for h in app.logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler)]
            self.assertEqual(len(file_handlers), 1, "the old file handler must be replaced, not stacked")
            self.assertEqual(Path(file_handlers[0].baseFilename), (new_dir / "logs" / "skein.log").resolve())
            marker = "distinctive-marker-after-relocation"
            app.logger_system.info(marker)
            for handler in app.logger.handlers:
                if hasattr(handler, "flush"): handler.flush()
            self.assertIn(marker, app.LOG_FILE.read_text("utf-8"))
        finally:
            app.relocate_log_directory(original_dir.parent)
            self.assertEqual(app.LOG_DIR, original_dir); self.assertEqual(app.LOG_FILE, original_file)

    def test_rotation_actually_produces_a_backup_file_under_a_tiny_size_cap(self):
        # A dedicated small-cap logger proves RotatingFileHandler rolls over correctly,
        # independent of however much the shared app logger has already written this run.
        rotate_dir = WORKSPACE / "rotation-probe"
        rotate_dir.mkdir(exist_ok=True)
        probe = logging.getLogger("skein-rotation-probe")
        handler = logging.handlers.RotatingFileHandler(rotate_dir / "probe.log", maxBytes=500, backupCount=2, encoding="utf-8")
        probe.addHandler(handler); probe.setLevel(logging.INFO)
        try:
            for i in range(200):
                probe.info(f"padding line {i} to force the file past its rollover threshold")
        finally:
            probe.removeHandler(handler); handler.close()
        self.assertTrue((rotate_dir / "probe.log").exists())
        self.assertTrue((rotate_dir / "probe.log.1").exists(), "expected at least one rotated backup file")


class HttpDispatchLoggingTest(unittest.TestCase):
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

    def call(self, path, body=None, method=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, payload, {"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_a_get_request_is_logged(self):
        marker = "/api/health"
        self.call(marker)
        self.assertIn(f"GET {marker} 200", log_text())

    def test_a_post_action_is_logged_at_info_with_its_outcome(self):
        status, _ = self.call("/api/pools", {"name": "Logging-Test-Pool", "domain": "worker"}, "POST")
        self.assertEqual(status, 201)
        self.assertIn("INFO", log_text())
        self.assertIn("POST /api/pools 201", log_text())
        self.assertIn("pool created", log_text())

    def test_an_unhandled_exception_returns_a_clean_500_and_is_logged(self):
        original = app.model_file_entries
        app.model_file_entries = lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic failure for logging test"))
        try:
            status, data = self.call("/api/models/files")
        finally:
            app.model_file_entries = original
        self.assertEqual(status, 500)
        self.assertEqual(data["error"], "Internal server error")
        text = log_text()
        self.assertIn("raised an unhandled exception", text)
        self.assertIn("synthetic failure for logging test", text)
        self.assertIn("ERROR", text)

    def test_a_denied_request_logs_at_warning_not_info(self):
        original = os.environ.pop("SKEIN_AUTH_DISABLED", None)
        try:
            status, _ = self.call("/api/models")
            self.assertEqual(status, 401)
        finally:
            if original is not None: os.environ["SKEIN_AUTH_DISABLED"] = original
        self.assertIn("GET /api/models 401", log_text())

    def test_a_client_disconnect_is_not_logged_as_a_server_error(self):
        # A closed tab or a navigation mid-request raises Connection*Error inside the
        # handler; this is routine, not a bug, so it must never inflate ERROR-level noise.
        # Exercised directly against dispatch() with a stub self: a real socket round-trip
        # would just hang, since (unlike the real scenario) this test's client is still there
        # waiting for a response that a genuine disconnect handler intentionally never sends.
        class FakeHandler:
            path = "/api/fake-disconnect-probe"
            def send_response(self, code, *a, **kw): pass
            def current_user(self): return None
            def json(self, data, status=200): raise AssertionError("must not attempt a response after a client disconnect")
        app.Handler.dispatch(FakeHandler(), "GET", lambda: (_ for _ in ()).throw(ConnectionAbortedError("synthetic")))
        text = log_text()
        self.assertIn("client disconnected", text)
        self.assertNotIn("raised an unhandled exception", text.rsplit("client disconnected", 1)[-1])


class AuthAndModelLifecycleLoggingTest(unittest.TestCase):
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

    def call(self, path, body=None, method=None):
        payload = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, payload, {"Content-Type": "application/json"}, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def test_a_failed_login_is_logged_without_leaking_the_password(self):
        secret_password = "Sup3rSecretMarker!"
        self.call("/api/auth/login", {"username": "nonexistent-user", "password": secret_password}, "POST")
        text = log_text()
        self.assertIn("login failed", text)
        self.assertIn("nonexistent-user", text)
        self.assertNotIn(secret_password, text)

    def test_a_workflow_objective_is_not_written_to_the_log(self):
        """Reading the log requires settings.manage, which grants no cross-user workflow
        access. Logging the objective verbatim would hand every user's request text to a
        Settings Manager who cannot read those workflows through the API."""
        objective = "Refactor the ACME payroll importer before the audit"
        wid = app.create_workflow(objective)
        text = log_text()
        self.assertIn(f"workflow.created workflow={wid}", text)
        self.assertNotIn(objective, text)
        self.assertIn(f"<{len(objective)} chars>", text)
        with app.db() as conn:
            payload = conn.execute("SELECT payload FROM events WHERE workflow_id=? AND kind='workflow.created'", (wid,)).fetchone()
        self.assertEqual(json.loads(payload["payload"])["objective"], objective)

    def test_a_workflow_event_is_logged_via_the_emit_hook(self):
        wid = "logging-test-workflow"
        with app.db() as conn:
            conn.execute("INSERT OR IGNORE INTO workflows(id,objective,status,created_at,updated_at) VALUES(?,?,?,?,?)",
                         (wid, "test objective", "running", app.stamp(), app.stamp()))
        app.emit(wid, "task.failed", {"error": "synthetic"}, tid="task-1")
        matches = [line for line in log_text().splitlines() if f"task.failed workflow={wid} task=task-1" in line]
        self.assertTrue(matches, "expected a log line for the emitted task.failed event")
        self.assertIn("ERROR", matches[-1])


class AdminLogsEndpointTest(unittest.TestCase):
    """The Administration > System log page reads through these two routes."""

    @classmethod
    def setUpClass(cls):
        app.init_db()
        app.logger_settings.info("distinctive-marker-for-admin-logs-test level=info")
        app.logger_settings.warning("distinctive-marker-for-admin-logs-test level=warning")
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        app.POOL.shutdown(wait=True)

    def get_bytes(self, path):
        try:
            with urlopen(self.base + path, timeout=20) as response:
                return response.status, response.read()
        except HTTPError as error:
            return error.code, error.read()

    def get_json(self, path):
        status, body = self.get_bytes(path)
        return status, json.loads(body)

    def test_records_and_file_summary_are_returned(self):
        status, data = self.get_json("/api/admin/logs?limit=2000")
        self.assertEqual(status, 200)
        self.assertTrue(any("distinctive-marker-for-admin-logs-test" in r["message"] for r in data["records"]))
        self.assertIn("skein.log", [f["name"] for f in data["files"]])
        self.assertEqual(data["level_options"], list(app.LOG_LEVEL_NAMES))

    def test_level_filter_excludes_other_levels(self):
        status, data = self.get_json("/api/admin/logs?level=WARNING&limit=2000")
        self.assertEqual(status, 200)
        self.assertTrue(data["records"])
        self.assertTrue(all(r["level"] == "WARNING" for r in data["records"]))

    def test_an_unknown_level_is_rejected(self):
        status, data = self.get_json("/api/admin/logs?level=NOPE")
        self.assertEqual(status, 400)
        self.assertIn("NOPE", data["error"])

    def test_text_search_narrows_to_matching_lines(self):
        status, data = self.get_json("/api/admin/logs?q=distinctive-marker-for-admin-logs-test&limit=2000")
        self.assertEqual(status, 200)
        self.assertTrue(data["records"])
        self.assertTrue(all("distinctive-marker-for-admin-logs-test" in r["message"] for r in data["records"]))

    def test_the_active_log_file_can_be_downloaded(self):
        status, body = self.get_bytes("/api/admin/logs/download?file=skein.log")
        self.assertEqual(status, 200)
        self.assertIn("distinctive-marker-for-admin-logs-test", body.decode("utf-8", "replace"))

    def test_a_path_outside_the_log_directory_is_rejected(self):
        status, data = self.get_json("/api/admin/logs/download?file=../../../../etc/passwd")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
