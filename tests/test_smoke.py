import importlib, json, os, tempfile, threading, time, unittest
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError
from urllib.parse import quote

TEST_DB = Path(tempfile.gettempdir()) / f"skein-smoke-{os.getpid()}.db"
os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_AUTH_DISABLED"] = "1"
app = importlib.import_module("app")


class SmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.init_db()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.port = cls.server.server_port
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        app.POOL.shutdown(wait=True)
        for suffix in ("", "-wal", "-shm"):
            try: Path(str(TEST_DB) + suffix).unlink()
            except FileNotFoundError: pass

    def request(self, path, body=None):
        raw = json.dumps(body).encode() if body else None
        req = Request(f"http://127.0.0.1:{self.port}{path}", raw,
                      {"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return response.status, json.load(response)

    def test_complete_workflow(self):
        status, created = self.request("/api/workflows", {"objective": "Construire une API OAuth2 sécurisée"})
        self.assertEqual(status, 201)
        wid = created["id"]
        deadline = time.time() + 12
        while time.time() < deadline:
            _, data = self.request(f"/api/workflows/{wid}")
            if data["workflow"]["status"] in ("COMPLETED", "FAILED"): break
            time.sleep(.15)
        self.assertEqual(data["workflow"]["status"], "COMPLETED")
        self.assertEqual(len(data["tasks"]), 5)
        self.assertEqual(data["tasks"][-1]["role"], "workflow-reporter")
        self.assertTrue(all(t["result"] and t["confidence"] for t in data["tasks"]))
        self.assertIn("worker-general", {t["model"] for t in data["tasks"]})
        self.assertIn("reasoner-large", {t["model"] for t in data["tasks"]})
        self.assertTrue(any(e["kind"] == "workflow.completed" for e in data["events"]))
        self.assertIsNotNone(data["final_output"])
        self.assertIsNotNone(data["execution_report"])
        self.assertIn("No file", data["artifact_notice"])
        with urlopen(f"http://127.0.0.1:{self.port}/api/workflows/{wid}/report") as response:
            report=response.read().decode()
        self.assertIn("# Skein Report",report)
        self.assertIn("## Step 5",report)

    def test_artifact_persistence_validation_and_download(self):
        wid=app.create_workflow("Créer un script Python hello world","owner-test","session-test")
        tid=app.workflow_data(wid)["tasks"][0]["id"]
        saved=app.persist_artifacts(wid,tid,[{"path":"src/hello.py","content":"def hello():\n    return 'Hello'\n"},{"path":"../escape.py","content":"bad"}])
        self.assertEqual(len(saved),1)
        self.assertEqual(saved[0]["validation"]["status"],"PASS")
        data=app.workflow_data(wid)
        self.assertEqual(data["artifacts"][0]["relative_path"],"src/hello.py")
        with app.db() as conn: artifact_path=Path(conn.execute("SELECT disk_path FROM artifacts WHERE workflow_id=?",(wid,)).fetchone()[0])
        expected_root=app.DB_PATH.parent/"users"/"owner-test"/"sessions"/"session-test"/"workflows"/wid/"artifacts"
        self.assertTrue(artifact_path.is_relative_to(expected_root))
        with urlopen(f"http://127.0.0.1:{self.port}{data['artifacts'][0]['download_url']}") as response:
            self.assertIn(b"def hello",response.read())

    def test_tool_plane_mode_and_preview_api(self):
        wid=app.create_workflow("Créer une preview HTML")
        tid=app.workflow_data(wid)["tasks"][0]["id"]
        saved=app.persist_artifacts(wid,tid,[{"path":"index.html","content":"<!doctype html><html><head></head><body>ok</body></html>"},{"path":"style.css","content":"body{color:red}"}])
        html=next(x for x in saved if x["path"]=="index.html")
        _,cap=self.request("/api/sandbox/capabilities")
        self.assertIn("python",cap["runtimes"])
        _,local=self.request("/api/execution-mode",{"mode":"local"})
        self.assertEqual(local["mode"],"local")
        _,sandbox=self.request("/api/execution-mode",{"mode":"sandbox"})
        self.assertEqual(sandbox["mode"],"sandbox")
        _,preview=self.request(f"/api/artifacts/{html['id']}/preview")
        self.assertEqual(preview["type"],"html")
        self.assertIn("body{color:red}",preview["content"])

    def test_dashboard_and_health(self):
        with urlopen(f"http://127.0.0.1:{self.port}/", timeout=5) as response:
            self.assertIn(b"LOCAL AGENT RUNTIME", response.read())
        _, health = self.request("/api/health")
        self.assertEqual(health["status"], "ok")

    def test_hardware_pools_and_model_registry(self):
        _, hardware = self.request("/api/hardware")
        self.assertIn("node", hardware)
        self.assertIn("gpus", hardware)
        self.assertGreaterEqual(len(hardware["pools"]), 3)
        if hardware["gpus"]:
            gpu_id = quote(hardware["gpus"][0]["id"], safe="")
            _, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "reasoner"})
            self.assertEqual(assigned["pool_id"], "reasoner")
        status, created = self.request("/api/models", {"name":"Reasoner test","role":"reasoner",
          "backend":"llama.cpp","model_path":"Z:/missing/model.gguf",
          "runtime_path":"Z:/missing/llama-server.exe","context_size":32768,"port":18001})
        self.assertEqual(status, 201)
        _, configured = self.request(f"/api/models/{created['id']}/configure", {"role":"worker","pool_id":"workers"})
        self.assertEqual(configured["role"], "worker")
        self.assertEqual(configured["pool_id"], "workers")
        self.assertGreater(configured["port"], 0)
        _, activation = self.request(f"/api/models/{created['id']}/activate", {"pool_id":"workers"})
        self.assertEqual(activation["status"], "CONFIGURED")
        self.assertIn("not found", activation["error"])

    def test_pool_telemetry_and_session_continuation(self):
        telemetry = app.pool_telemetry(300)
        self.assertIn("current", telemetry)
        self.assertIn("pool_metrics", telemetry["current"])
        self.assertTrue(all("domain" in item and "power_w" in item for item in telemetry["current"]["pool_metrics"]))
        status, first = self.request("/api/workflows", {"objective": "Create a session that can be continued"})
        self.assertEqual(status, 201)
        status, continued = self.request("/api/workflows", {"objective": "Continue the previous session with a follow-up", "continue_workflow_id": first["id"]})
        self.assertEqual(status, 201)
        self.assertEqual(continued["continued_from"], first["id"])
        source = app.workflow_data(first["id"])["workflow"]
        follow_up = app.workflow_data(continued["id"])["workflow"]
        self.assertEqual(source["session_id"], follow_up["session_id"])
        self.assertEqual(follow_up["continued_from"], first["id"])
        deadline = time.time() + 20
        while time.time() < deadline:
            states = [app.workflow_data(workflow_id)["workflow"]["status"] for workflow_id in (first["id"], continued["id"])]
            if all(state in ("COMPLETED", "FAILED") for state in states):
                break
            time.sleep(0.05)
        self.assertTrue(all(state == "COMPLETED" for state in states))

    def test_workflow_queue_reports_position_and_runs_fifo(self):
        previous_limit=app.MAX_PARALLEL_WORKFLOWS; app.MAX_PARALLEL_WORKFLOWS=1
        try:
            first=app.create_workflow("First queued scheduling test","queue-owner","queue-session")
            second=app.create_workflow("Second queued scheduling test","queue-owner","queue-session")
            self.assertTrue((app.DB_PATH.parent/"users"/"queue-owner"/"sessions"/"queue-session"/"workflows"/first).is_dir())
            self.assertTrue((app.DB_PATH.parent/"users"/"queue-owner"/"sessions"/"queue-session"/"workflows"/second).is_dir())
            self.assertTrue(app.start_workflow(first)); self.assertTrue(app.start_workflow(second))
            second_data=app.workflow_data(second)
            self.assertEqual(second_data["workflow"]["status"],"QUEUED")
            self.assertEqual(second_data["workflow"]["queue_position"],1)
            deadline=time.time()+15
            while time.time()<deadline:
                first_data=app.workflow_data(first); second_data=app.workflow_data(second)
                if first_data["workflow"]["status"]=="COMPLETED" and second_data["workflow"]["status"]=="COMPLETED": break
                time.sleep(.1)
            self.assertEqual(first_data["workflow"]["status"],"COMPLETED")
            self.assertEqual(second_data["workflow"]["status"],"COMPLETED")
            self.assertLessEqual(first_data["workflow"]["updated_at"],second_data["workflow"]["updated_at"])
        finally: app.MAX_PARALLEL_WORKFLOWS=previous_limit

    def test_real_mode_preflight_is_explicit(self):
        previous = os.environ.pop("SKEIN_ALLOW_SIMULATION", None)
        try:
            with self.assertRaises(HTTPError) as caught:
                self.request("/api/workflows", {"objective":"Tester sans modèles actifs"})
            self.assertEqual(caught.exception.code, 409)
            payload=json.load(caught.exception)
            self.assertEqual(payload["error"], "Real models are not loaded")
            self.assertCountEqual(payload["missing_roles"], ["reasoner","worker"])
        finally:
            if previous is not None: os.environ["SKEIN_ALLOW_SIMULATION"] = previous


if __name__ == "__main__": unittest.main()
