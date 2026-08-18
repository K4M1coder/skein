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
            # A GPU can belong to several pools at once (one card commonly serves reasoner,
            # worker, and retrieval together), so assigning a second pool must add to the set.
            gpu_id = quote(hardware["gpus"][0]["id"], safe="")
            _, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "reasoner", "assigned": True})
            self.assertEqual(assigned["pool_ids"], ["reasoner"])
            _, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "workers", "assigned": True})
            self.assertEqual(sorted(assigned["pool_ids"]), ["reasoner", "workers"])
            _, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "reasoner", "assigned": False})
            self.assertEqual(assigned["pool_ids"], ["workers"])
            # Leave the GPU unassigned again: the later assertion in this same test expects
            # the "workers" pool to have no GPU, to prove a running model without one is
            # still surfaced correctly.
            _, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "workers", "assigned": False})
            self.assertEqual(assigned["pool_ids"], [])
        status, created = self.request("/api/models", {"name":"Reasoner test","role":"reasoner",
          "backend":"llama.cpp","model_path":"Z:/missing/model.gguf",
          "runtime_path":"Z:/missing/llama-server.exe","context_size":32768,"port":18001})
        self.assertEqual(status, 201)
        _, configured = self.request(f"/api/models/{created['id']}/configure", {"role":"worker","pool_id":"workers"})
        self.assertEqual(configured["role"], "worker")
        self.assertEqual(configured["pool_id"], "workers")
        self.assertGreater(configured["port"], 0)
        # The paths do not exist, so loading must report a failure instead of a silent success.
        with self.assertRaises(HTTPError) as failed_load:
            self.request(f"/api/models/{created['id']}/activate", {"pool_id":"workers"})
        self.assertEqual(failed_load.exception.code, 502)
        activation = json.load(failed_load.exception)
        self.assertEqual(activation["status"], "CONFIGURED")
        self.assertIn("not found", activation["error"])
        self.assertEqual(activation["pool_id"], "workers")

        # A model can be RUNNING in a pool that has no GPU assigned to it (the runtime
        # started without CUDA_VISIBLE_DEVICES); the hardware snapshot must surface that
        # gap instead of silently reporting the pool as empty. Give it a real, live pid/image
        # pair so the status-reconciliation hardware_snapshot() now applies recognizes it as
        # genuinely running instead of correcting it back to STOPPED.
        with app.db() as conn:
            conn.execute("UPDATE models SET status='RUNNING',pid=?,runtime_path=? WHERE id=?",
                         (os.getpid(), Path(app.sys.executable).name, created["id"]))
        _, hardware_after = self.request("/api/hardware")
        workers_metrics = next(pool for pool in hardware_after["pool_metrics"] if pool["pool_id"] == "workers")
        self.assertEqual(workers_metrics["assigned_gpus"], 0)
        self.assertEqual(workers_metrics["running_models"], 1)

    def test_pool_metrics_list_the_models_configured_for_them(self):
        """A pool card only showed anonymous numbers; an operator glancing at it could not
        tell which model those numbers belonged to without leaving the page."""
        status, created = self.request("/api/models", {"name": "Pool model list test", "role": "worker",
          "backend": "llama.cpp", "model_path": "Z:/missing/model.gguf",
          "runtime_path": "Z:/missing/llama-server.exe", "context_size": 8192, "port": 18097})
        self.assertEqual(status, 201)
        self.request(f"/api/models/{created['id']}/configure", {"role": "worker", "pool_id": "workers"})
        _, hardware = self.request("/api/hardware")
        workers = next(pool for pool in hardware["pool_metrics"] if pool["pool_id"] == "workers")
        self.assertIn({"name": "Pool model list test", "role": "worker", "status": "STOPPED"}, workers["models"])
        reasoner = next(pool for pool in hardware["pool_metrics"] if pool["pool_id"] == "reasoner")
        self.assertNotIn("Pool model list test", [model["name"] for model in reasoner["models"]])

    def test_a_crashed_runtime_is_reconciled_the_same_way_on_the_hardware_page(self):
        """/api/models corrects a stale RUNNING status by checking the live process; that
        correction must also reach hardware_snapshot() instead of the Hardware page still
        showing a crashed runtime as RUNNING while the Models page already shows STOPPED."""
        status, created = self.request("/api/models", {"name": "Crash desync test", "role": "worker",
          "backend": "llama.cpp", "model_path": "Z:/missing/crashed-model.gguf",
          "runtime_path": "Z:/missing/llama-server.exe", "context_size": 8192, "port": 18098})
        self.assertEqual(status, 201)
        mid = created["id"]
        self.request(f"/api/models/{mid}/configure", {"role": "worker", "pool_id": "workers"})
        with app.db() as conn:
            conn.execute("UPDATE models SET status='RUNNING', pid=999999999, "
                         "endpoint='http://127.0.0.1:59999/v1/chat/completions' WHERE id=?", (mid,))
        _, models = self.request("/api/models")
        row = next(m for m in models if m["id"] == mid)
        self.assertEqual(row["status"], "STOPPED")
        _, hardware = self.request("/api/hardware")
        workers = next(pool for pool in hardware["pool_metrics"] if pool["pool_id"] == "workers")
        self.assertEqual(workers["running_models"], 0)
        self.assertIn({"name": "Crash desync test", "role": "worker", "status": "STOPPED"}, workers["models"])

    def test_a_gpu_can_serve_every_pool_at_once(self):
        """The common single-GPU setup: one card runs reasoner, worker, and retrieval
        together. Each pool must report the card's full telemetry, not a zero-sum split."""
        fake_gpu = {"id": "GPU-smoke-fake", "index": 0, "vendor": "NVIDIA", "name": "Fake GPU",
          "memory_total_mb": 24576.0, "memory_used_mb": 12000.0, "utilization": 42.0,
          "power_w": 210.0, "power_limit_w": 350.0, "temperature_c": 57.0, "metrics_source": "nvidia-smi"}
        original = app.nvidia_gpus
        app.nvidia_gpus = lambda: [dict(fake_gpu)]
        try:
            gpu_id = quote(fake_gpu["id"], safe="")
            for pool in ("reasoner", "workers", "retrieval"):
                status, assigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": pool, "assigned": True})
                self.assertEqual(status, 200)
            self.assertEqual(sorted(assigned["pool_ids"]), ["reasoner", "retrieval", "workers"])
            _, hardware = self.request("/api/hardware")
            by_pool = {row["pool_id"]: row for row in hardware["pool_metrics"]}
            for pool in ("reasoner", "workers", "retrieval"):
                self.assertEqual(by_pool[pool]["assigned_gpus"], 1)
                self.assertEqual(by_pool[pool]["power_w"], 210.0)
                self.assertEqual(by_pool[pool]["utilization"], 42.0)
            _, unassigned = self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "retrieval", "assigned": False})
            self.assertEqual(sorted(unassigned["pool_ids"]), ["reasoner", "workers"])
            _, hardware = self.request("/api/hardware")
            by_pool = {row["pool_id"]: row for row in hardware["pool_metrics"]}
            self.assertEqual(by_pool["retrieval"]["assigned_gpus"], 0)
            self.assertEqual(by_pool["reasoner"]["assigned_gpus"], 1)
        finally:
            app.nvidia_gpus = original
            with app.db() as conn: conn.execute("DELETE FROM gpu_assignments WHERE gpu_id=?", (fake_gpu["id"],))

    def test_estimated_vram_by_model_is_labeled_and_disappears_when_nothing_runs(self):
        """Per-process VRAM cannot be measured (no nvidia-smi per-process data on Windows
        GeForce/WDDM), so the estimate must carry its method disclosure and vanish once no
        model is actually running there — never a stray note on an idle GPU."""
        fake_gpu = {"id": "GPU-vram-smoke", "index": 0, "vendor": "NVIDIA", "name": "Fake GPU",
          "memory_total_mb": 24576.0, "memory_used_mb": 4000.0, "utilization": 5.0,
          "power_w": 40.0, "power_limit_w": 350.0, "temperature_c": 45.0, "metrics_source": "nvidia-smi"}
        original = app.nvidia_gpus
        app.nvidia_gpus = lambda: [dict(fake_gpu)]
        weight = Path(tempfile.gettempdir()) / f"skein-vram-smoke-{os.getpid()}.gguf"
        weight.write_bytes(b"\0" * 10_000_000)
        try:
            gpu_id = quote(fake_gpu["id"], safe="")
            self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "workers", "assigned": True})
            status, created = self.request("/api/models", {"name": "VRAM estimate test", "role": "worker",
              "backend": "llama.cpp", "model_path": str(weight), "runtime_path": "Z:/missing/llama-server.exe",
              "context_size": 8192, "port": 18099})
            self.assertEqual(status, 201)
            self.request(f"/api/models/{created['id']}/configure", {"role": "worker", "pool_id": "workers"})
            # A real, live pid/image pair so model_running()'s reconciliation recognizes this
            # as genuinely running, the same convention used in test_model_manager.py.
            with app.db() as conn:
                conn.execute("UPDATE models SET status='RUNNING',pid=?,runtime_path=? WHERE id=?",
                             (os.getpid(), Path(app.sys.executable).name, created["id"]))
            _, hardware = self.request("/api/hardware")
            gpu = next(row for row in hardware["gpus"] if row["id"] == fake_gpu["id"])
            self.assertEqual(len(gpu["estimated_models"]), 1)
            self.assertEqual(gpu["estimated_models"][0]["name"], "VRAM estimate test")
            self.assertGreater(gpu["estimated_models"][0]["estimated_vram_mb"], 0)
            self.assertIn("estimate", gpu["vram_estimation_method"].lower())
            self.assertIn("nvidia-smi", gpu["vram_estimation_method"])
            # A real stop_model() clears pid too; leaving a still-alive pid behind would have
            # the reconciliation correctly treat this as still running.
            with app.db() as conn: conn.execute("UPDATE models SET status='STOPPED',pid=NULL WHERE id=?", (created["id"],))
            _, hardware = self.request("/api/hardware")
            gpu = next(row for row in hardware["gpus"] if row["id"] == fake_gpu["id"])
            self.assertEqual(gpu["estimated_models"], [])
            self.assertIsNone(gpu["vram_estimation_method"])
        finally:
            app.nvidia_gpus = original
            with app.db() as conn: conn.execute("DELETE FROM gpu_assignments WHERE gpu_id=?", (fake_gpu["id"],))
            weight.unlink(missing_ok=True)

    def test_a_pool_less_running_model_is_still_estimated_on_a_lone_gpu(self):
        """A model can be running without ever having picked a pool. With exactly one
        physical GPU that belongs to some pool, that is unambiguous and must still be
        estimated; with several GPUs there is no honest way to guess which one, so it
        must be left out; and a lone GPU assigned to no pool at all must show nothing,
        since the operator has explicitly said it is not part of any monitored pool."""
        gpu_a = {"id": "GPU-lone-a", "index": 0, "vendor": "NVIDIA", "name": "A",
          "memory_total_mb": 24576.0, "memory_used_mb": 1000.0, "utilization": 5.0,
          "power_w": 40.0, "power_limit_w": 350.0, "temperature_c": 40.0, "metrics_source": "nvidia-smi"}
        gpu_b = {**gpu_a, "id": "GPU-lone-b", "index": 1, "name": "B"}
        original_gpus, original_windows = app.nvidia_gpus, app.windows_video_controllers
        app.windows_video_controllers = lambda: []  # isolate from this machine's own real GPU
        weight = Path(tempfile.gettempdir()) / f"skein-vram-lone-{os.getpid()}.gguf"
        weight.write_bytes(b"\0" * 5_000_000)
        try:
            status, created = self.request("/api/models", {"name": "Pool-less model", "role": "worker",
              "backend": "llama.cpp", "model_path": str(weight), "runtime_path": "Z:/missing/llama-server.exe",
              "context_size": 4096, "port": 18098})
            self.assertEqual(status, 201)
            # A real, live pid/image pair so model_running()'s reconciliation recognizes this
            # as genuinely running, the same convention used in test_model_manager.py.
            with app.db() as conn:
                conn.execute("UPDATE models SET status='RUNNING',pid=?,runtime_path=? WHERE id=?",
                             (os.getpid(), Path(app.sys.executable).name, created["id"]))

            app.nvidia_gpus = lambda: [dict(gpu_a)]
            _, hardware = self.request("/api/hardware")
            self.assertEqual(hardware["gpus"][0]["estimated_models"], [],
              "a lone GPU assigned to no pool must show no estimate at all")

            gpu_id = quote(gpu_a["id"], safe="")
            self.request(f"/api/gpus/{gpu_id}/assign", {"pool_id": "workers", "assigned": True})
            _, hardware = self.request("/api/hardware")
            self.assertEqual([m["name"] for m in hardware["gpus"][0]["estimated_models"]], ["Pool-less model"])

            app.nvidia_gpus = lambda: [dict(gpu_a), dict(gpu_b)]
            _, hardware = self.request("/api/hardware")
            for gpu in hardware["gpus"]:
                self.assertEqual(gpu["estimated_models"], [])
        finally:
            app.nvidia_gpus, app.windows_video_controllers = original_gpus, original_windows
            with app.db() as conn: conn.execute("DELETE FROM gpu_assignments WHERE gpu_id=?", (gpu_a["id"],))
            weight.unlink(missing_ok=True)

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
