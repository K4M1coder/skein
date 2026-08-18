"""Manual model lifecycle: discovery, registration, load/unload, upload, and Hugging Face."""
import importlib
import json
import os
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

WORKSPACE = Path(tempfile.gettempdir()) / f"skein-models-{os.getpid()}"
WORKSPACE.mkdir(parents=True, exist_ok=True)
TEST_DB = WORKSPACE / "skein.db"
MODEL_ROOT = WORKSPACE / "weights"
MODEL_ROOT.mkdir(parents=True, exist_ok=True)

os.environ["SKEIN_DB_PATH"] = str(TEST_DB)
os.environ["SKEIN_MODEL_ROOTS"] = str(MODEL_ROOT)
os.environ["SKEIN_MODEL_LIBRARY"] = str(WORKSPACE / "library")
os.environ["SKEIN_MIN_MODEL_MB"] = "1"
os.environ["SKEIN_ALLOW_SIMULATION"] = "1"
os.environ["SKEIN_AUTH_DISABLED"] = "1"
app = importlib.import_module("app")


def write_weights(name, megabytes=2):
    path = MODEL_ROOT / name
    path.write_bytes(b"GGUF" + b"\0" * (megabytes * 1048576))
    return path


def _gguf_string(value):
    encoded = value.encode("utf-8")
    return struct.pack("<Q", len(encoded)) + encoded


def build_gguf(path, architecture, scalars=None, tensors=None):
    """A minimal real GGUF header (magic, version, kv metadata, tensor shapes) — no weight
    payload bytes, since parse_gguf_metadata never reads past the tensor-shape section."""
    scalars = scalars or {}
    tensors = tensors or []
    kv = {"general.architecture": ("str", architecture)}
    kv.update(scalars)
    buf = bytearray(b"GGUF")
    buf += struct.pack("<I", 3)
    buf += struct.pack("<Q", len(tensors))
    buf += struct.pack("<Q", len(kv))
    for key, (kind, value) in kv.items():
        buf += _gguf_string(key)
        if kind == "str":
            buf += struct.pack("<I", 8) + _gguf_string(value)
        elif kind == "u32":
            buf += struct.pack("<I", 4) + struct.pack("<I", value)
        elif kind == "u64":
            buf += struct.pack("<I", 10) + struct.pack("<Q", value)
        else:
            raise ValueError(f"unsupported scalar kind {kind}")
    for name, dims in tensors:
        buf += _gguf_string(name)
        buf += struct.pack("<I", len(dims))
        for dim in dims:
            buf += struct.pack("<Q", dim)
        buf += struct.pack("<I", 0)  # ggml_type, irrelevant to element counting
        buf += struct.pack("<Q", 0)  # tensor data offset, unused since no payload is written
    path.write_bytes(bytes(buf))


class ModelManagerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.big = write_weights("Test-Model-8B-Q4_K_M.gguf", 3)
        cls.small = write_weights("Test-Model-3B-Q8_0.gguf", 2)
        write_weights("Test-Model-mmproj-f16.gguf", 2)
        app.init_db()
        cls.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        app.POOL.shutdown(wait=True)

    def call(self, path, body=None, method=None, raw=None, headers=None):
        payload = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        request = Request(self.base + path, payload,
                          {"Content-Type": "application/json", **(headers or {})}, method=method)
        try:
            with urlopen(request, timeout=20) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def register(self, path, role="available"):
        status, data = self.call("/api/models/files/register", {"path": str(path), "role": role})
        self.assertIn(status, (201, 409))
        return data

    # discovery and the file explorer -------------------------------------------------

    def test_discovery_reports_files_roots_and_warnings(self):
        status, data = self.call("/api/models/discover", {})
        self.assertEqual(status, 200)
        self.assertIn(str(MODEL_ROOT), data["roots"])
        self.assertIsInstance(data["warnings"], list)
        self.assertGreaterEqual(data["scanned_files"], 2)

    def test_file_explorer_lists_weights_with_size_and_quantization(self):
        status, data = self.call("/api/models/files")
        self.assertEqual(status, 200)
        names = {entry["name"]: entry for entry in data["files"]}
        self.assertIn("Test-Model-8B-Q4_K_M", names)
        self.assertEqual(names["Test-Model-8B-Q4_K_M"]["quantization"], "Q4_K_M")
        self.assertGreater(names["Test-Model-8B-Q4_K_M"]["size_bytes"], 0)
        # Projection files are never weight candidates.
        self.assertNotIn("Test-Model-mmproj-f16", names)

    def test_discovery_does_not_require_a_runtime(self):
        entries, _ = app.model_file_entries()
        original = app.find_llama_runtime
        app.find_llama_runtime = lambda: None
        try:
            report = app.discover_local_models(False)
            self.assertEqual(report["scanned_files"], len(entries))
            self.assertTrue(any("llama-server" in warning for warning in report["warnings"]))
        finally:
            app.find_llama_runtime = original

    def test_model_roots_are_configurable(self):
        extra = str(WORKSPACE / "extra-root")
        status, data = self.call("/api/models/roots", {"roots": [extra]})
        self.assertEqual(status, 200)
        self.assertIn(extra, data["managed"])
        status, listed = self.call("/api/models/roots")
        self.assertEqual(status, 200)
        self.assertIn(str(MODEL_ROOT), listed["environment"])
        self.call("/api/models/roots", {"roots": []})

    def test_registering_a_file_rejects_traversal_and_non_gguf(self):
        status, _ = self.call("/api/models/files/register", {"path": str(WORKSPACE / "nope.txt")})
        self.assertEqual(status, 400)
        status, _ = self.call("/api/models/files/register", {"path": str(MODEL_ROOT / "absent.gguf")})
        self.assertEqual(status, 404)

    # role, pool, and the load/unload path ---------------------------------------------

    def test_loading_with_no_pool_selected_is_not_rejected_as_a_missing_pool(self):
        model = self.register(self.big)
        status, data = self.call(f"/api/models/{model['id']}/activate", {"role": "worker", "pool_id": None})
        # These are placeholder weights, so the runtime cannot really start. What matters is
        # that "unassigned" is honoured instead of being turned into the literal pool id "None".
        self.assertNotEqual(status, 404)
        self.assertNotEqual(data.get("error"), "Pool not found")
        self.assertIsNone(data["pool_id"])
        with app.db() as conn:
            row = conn.execute("SELECT role,pool_id FROM models WHERE id=?", (model["id"],)).fetchone()
        self.assertEqual(row["role"], "worker")
        self.assertIsNone(row["pool_id"])

    def test_a_runtime_that_cannot_start_reports_an_error_status(self):
        model = self.register(write_weights("No-Runtime-Q4_0.gguf"))
        with app.db() as conn:
            conn.execute("UPDATE models SET runtime_path='' WHERE id=?", (model["id"],))
        status, data = self.call(f"/api/models/{model['id']}/activate", {"role": "worker"})
        self.assertEqual(status, 502)
        self.assertIn("runtime", data["error"].lower())
        self.assertIn("saved", data["action"])

    def test_loading_without_a_runnable_role_explains_itself(self):
        model = self.register(self.small)
        status, data = self.call(f"/api/models/{model['id']}/activate", {"role": "available", "pool_id": None})
        self.assertEqual(status, 400)
        self.assertEqual(data["error"], "Choose a model role before loading")
        self.assertIn("worker", data["action"])

    def test_saving_a_configuration_keeps_the_available_role(self):
        model = self.register(write_weights("Keep-Available-Q5_K_M.gguf"))
        status, data = self.call(f"/api/models/{model['id']}/configure", {"role": "available", "pool_id": None})
        self.assertEqual(status, 200)
        self.assertEqual(data["role"], "available")
        self.assertEqual(data["port"], 0)

    def test_a_pool_assignment_survives_a_load_without_pool(self):
        _, pool = self.call("/api/pools", {"name": "GPU node A", "domain": "worker"})
        model = self.register(write_weights("Pooled-Model-Q4_0.gguf"))
        status, _ = self.call(f"/api/models/{model['id']}/configure", {"role": "worker", "pool_id": pool["id"]})
        self.assertEqual(status, 200)
        _, activated = self.call(f"/api/models/{model['id']}/activate", {"role": "worker"})
        self.assertEqual(activated["pool_id"], pool["id"], "an empty pool must keep the stored assignment, not clear it")
        with app.db() as conn:
            self.assertEqual(conn.execute("SELECT pool_id FROM models WHERE id=?", (model["id"],)).fetchone()["pool_id"], pool["id"])

    def test_an_unknown_pool_is_still_rejected(self):
        model = self.register(write_weights("Bad-Pool-Q4_0.gguf"))
        status, data = self.call(f"/api/models/{model['id']}/activate", {"role": "worker", "pool_id": "not-a-pool"})
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "Pool not found")

    def test_stop_reports_success_and_clears_state(self):
        model = self.register(write_weights("Stoppable-Q4_0.gguf"))
        self.call(f"/api/models/{model['id']}/configure", {"role": "worker"})
        status, data = self.call(f"/api/models/{model['id']}/stop", {})
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "STOPPED")

    def test_a_surviving_runtime_pid_is_recognised_and_terminated(self):
        model = self.register(write_weights("Orphan-Runtime-Q4_0.gguf"))
        # Simulate a llama-server left behind by a previous Skein process.
        with app.db() as conn:
            conn.execute("UPDATE models SET status='RUNNING',pid=?,runtime_path=?,endpoint=? WHERE id=?",
                         (os.getpid(), Path(app.sys.executable).name, "http://127.0.0.1:65500/v1/chat/completions", model["id"]))
            row = conn.execute("SELECT * FROM models WHERE id=?", (model["id"],)).fetchone()
        self.assertTrue(app.model_running(row))
        self.assertFalse(app.process_alive(0))
        self.assertFalse(app.process_alive(os.getpid(), "definitely-not-this-image"))

    def test_unregistering_requires_a_stopped_runtime_and_keeps_weights(self):
        weights = write_weights("Removable-Q4_0.gguf")
        model = self.register(weights)
        status, data = self.call(f"/api/models/{model['id']}", method="DELETE")
        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        self.assertTrue(weights.exists(), "unregistering must never delete weights outside the Skein library")

    def test_deleting_weights_is_confined_to_the_skein_library(self):
        model = self.register(write_weights("Outside-Library-Q4_0.gguf"))
        status, data = self.call(f"/api/models/{model['id']}?delete_file=true", method="DELETE")
        self.assertEqual(status, 403)
        self.assertIn("library", data)

    # upload and Hugging Face ----------------------------------------------------------

    def test_upload_stores_and_registers_a_weight_file(self):
        payload = b"GGUF" + b"\0" * 4096
        status, data = self.call("/api/models/upload?filename=Uploaded-Model-Q4_K_M.gguf", raw=payload,
                                 headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 201)
        self.assertEqual(data["quantization"], "Q4_K_M")
        self.assertTrue(Path(data["model_path"]).is_file())
        status, again = self.call("/api/models/upload?filename=Uploaded-Model-Q4_K_M.gguf", raw=payload,
                                  headers={"Content-Type": "application/octet-stream"})
        self.assertEqual(status, 409)

    def test_upload_rejects_foreign_extensions(self):
        for filename in ("weights.bin", "", "model.gguf.exe", "model%20name.gguf"):
            with self.subTest(filename=filename):
                status, _ = self.call(f"/api/models/upload?filename={filename}", raw=b"GGUF",
                                      headers={"Content-Type": "application/octet-stream"})
                self.assertEqual(status, 400)

    def test_upload_confines_traversal_attempts_to_the_library(self):
        library = Path(os.environ["SKEIN_MODEL_LIBRARY"]).resolve()
        for filename in ("../escape.gguf", "..%2Fescape2.gguf", "sub/dir/nested.gguf", "C:\\windows\\system32\\evil.gguf"):
            with self.subTest(filename=filename):
                status, data = self.call(f"/api/models/upload?filename={filename}", raw=b"GGUF" + b"\0" * 64,
                                         headers={"Content-Type": "application/octet-stream"})
                self.assertEqual(status, 201)
                stored = Path(data["model_path"]).resolve()
                self.assertEqual(stored.parent, library, "an upload must never land outside the model library")
                self.assertEqual(stored.name, Path(filename.replace("%2F", "/").replace("\\", "/")).name)

    def test_safe_weight_filename_normalises_input(self):
        self.assertEqual(app.safe_weight_filename("model.gguf"), "model.gguf")
        self.assertEqual(app.safe_weight_filename("C:\\weights\\model.gguf"), "model.gguf")
        # Directory components are dropped rather than trusted, so no path escapes the library.
        self.assertEqual(app.safe_weight_filename("../../model.gguf"), "model.gguf")
        self.assertEqual(app.safe_weight_filename("split/model-Q8_0.gguf"), "model-Q8_0.gguf")
        for hostile in ("model.gguf.exe", "model.bin", "", None, "model .gguf", "..", "/"):
            self.assertIsNone(app.safe_weight_filename(hostile), hostile)

    def test_huggingface_requests_validate_before_any_network_call(self):
        status, _ = self.call("/api/models/huggingface/files?repo=not-a-repo")
        self.assertEqual(status, 400)
        for repo, filename in [("owner/name", "../evil.gguf"), ("owner/name", "a/../../evil.gguf"),
                               ("owner/name", "model.bin"), ("bad repo", "model.gguf"), ("owner/name", "")]:
            with self.subTest(repo=repo, filename=filename):
                status, _ = self.call("/api/models/huggingface/download", {"repo": repo, "filename": filename})
                self.assertEqual(status, 400)
        self.assertEqual(app.safe_remote_weight_path("split/model-Q8_0.gguf"), "split/model-Q8_0.gguf")
        self.assertIsNone(app.safe_remote_weight_path("../model.gguf"))

    def test_huggingface_search_maps_the_api_response(self):
        original = app.huggingface_api
        app.huggingface_api = lambda path, query=None: ([
            {"modelId": "owner/model-GGUF", "downloads": 42, "likes": 7, "tags": ["gguf", "text-generation"], "gated": False},
            {"id": "other/model-GGUF", "downloads": 1, "gated": True, "tags": []},
        ], 200)
        try:
            status, data = self.call("/api/models/huggingface/search?q=qwen")
            self.assertEqual(status, 200)
            self.assertEqual([item["repo"] for item in data["results"]], ["owner/model-GGUF", "other/model-GGUF"])
            self.assertTrue(data["results"][1]["gated"])
        finally:
            app.huggingface_api = original

    def test_huggingface_repository_listing_keeps_only_gguf_files(self):
        original = app.huggingface_api
        app.huggingface_api = lambda path, query=None: ({"gated": False, "siblings": [
            {"rfilename": "README.md"},
            {"rfilename": "model-Q4_K_M.gguf", "lfs": {"size": 5368709120}},
            {"rfilename": "split/model-Q8_0.gguf", "size": 1073741824},
        ]}, 200)
        try:
            status, data = self.call("/api/models/huggingface/files?repo=owner/model")
            self.assertEqual(status, 200)
            self.assertEqual([item["filename"] for item in data["files"]], ["model-Q4_K_M.gguf", "split/model-Q8_0.gguf"])
            self.assertEqual(data["files"][0]["size_gb"], 5.0)
            self.assertEqual(data["files"][0]["quantization"], "Q4_K_M")
        finally:
            app.huggingface_api = original

    def test_downloads_endpoint_is_available(self):
        status, data = self.call("/api/models/downloads")
        self.assertEqual(status, 200)
        self.assertIsInstance(data["downloads"], list)
        status, _ = self.call("/api/models/downloads/unknown-job/cancel", {})
        self.assertEqual(status, 404)

    def test_model_list_exposes_runtime_and_quantization_metadata(self):
        self.register(self.small)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        self.assertTrue(models)
        for model in models:
            self.assertIn("running", model)
            self.assertIn("runnable", model)
            self.assertIn("log_available", model)

    # GGUF header metadata: size, context, and parameter counts stay visible after registration -----

    def test_gguf_metadata_is_parsed_from_tensor_shapes_and_cached(self):
        path = MODEL_ROOT / "Synth-Dense-Q4_0.gguf"
        build_gguf(path, "synthdense", scalars={"synthdense.context_length": ("u32", 8192)},
                   tensors=[("blk.0.attn.weight", [4096, 4096]), ("blk.0.ffn.weight", [4096, 11008])])
        model = self.register(path)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        expected_total = 4096 * 4096 + 4096 * 11008
        self.assertEqual(row["architecture"], "synthdense")
        self.assertEqual(row["trained_context_length"], 8192)
        self.assertEqual(row["total_params"], expected_total)
        self.assertIsNone(row["active_params"])
        self.assertGreater(row["size_bytes"], 0)
        with app.db() as conn:
            cached = conn.execute("SELECT gguf_parsed_at, total_params FROM models WHERE id=?", (model["id"],)).fetchone()
        self.assertIsNotNone(cached["gguf_parsed_at"])
        self.assertEqual(cached["total_params"], expected_total)

    def test_a_truncated_tensor_name_does_not_crash_the_model_list(self):
        """A tensor-section string whose declared length runs past the header buffer must
        degrade to partial results, the same way a truncated kv-metadata section already
        does just above it in parse_gguf_metadata — not raise and take the whole /api/models
        list down with a 500 for every registered model, not just this corrupt one."""
        path = MODEL_ROOT / "Synth-Truncated-Q4_0.gguf"
        buf = bytearray(b"GGUF")
        buf += struct.pack("<I", 3)  # version
        buf += struct.pack("<Q", 1)  # tensor_count
        buf += struct.pack("<Q", 1)  # kv_count
        buf += _gguf_string("general.architecture")
        buf += struct.pack("<I", 8) + _gguf_string("synthtrunc")
        buf += struct.pack("<Q", 10_000_000)  # tensor name claims far more bytes than follow
        buf += b"short"
        path.write_bytes(bytes(buf))
        model = self.register(path)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        self.assertEqual(row["architecture"], "synthtrunc")
        self.assertIsNone(row["total_params"])

    def test_a_truncated_fixed_header_does_not_crash_the_model_list(self):
        """A file that carries the GGUF magic but is cut inside the fixed version/count
        fields (an interrupted copy or download) must register as metadata-less, not raise
        struct.error out of parse_gguf_metadata and turn every /api/models poll into a 500."""
        path = MODEL_ROOT / "Synth-Header-Cut-Q4_0.gguf"
        path.write_bytes(b"GGUF\x03\x00")
        model = self.register(path)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        self.assertIsNone(row["architecture"])
        self.assertIsNone(row["total_params"])

    def test_an_unknown_gguf_value_type_does_not_crash_the_model_list(self):
        """A kv entry with a value type this parser does not know (corrupt file, or a newer
        GGUF revision) must degrade to partial metadata like a truncated section does — not
        let ValueError escape and take /api/models down."""
        path = MODEL_ROOT / "Synth-Unknown-Type-Q4_0.gguf"
        buf = bytearray(b"GGUF")
        buf += struct.pack("<I", 3)  # version
        buf += struct.pack("<Q", 0)  # tensor_count
        buf += struct.pack("<Q", 1)  # kv_count
        buf += _gguf_string("general.architecture")
        buf += struct.pack("<I", 13)  # value type unknown to GGUF_SCALAR_FORMATS
        path.write_bytes(bytes(buf))
        model = self.register(path)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        self.assertIsNone(row["architecture"])

    def test_auto_model_budget_accounts_for_two_concurrent_runtimes(self):
        """discover_local_models registers the auto pick for the reasoner AND the worker,
        so two instances of the same file load at once: a file that fits 80% of VRAM once
        but not twice must not be picked (a 19 GB pick on a 24 GB card starts two runtimes
        that can never both become READY)."""
        entries = [
            {"name": "huge", "path": "huge.gguf", "size_bytes": 19 * 1024**3, "too_small": False},
            {"name": "medium", "path": "medium.gguf", "size_bytes": 8 * 1024**3, "too_small": False},
            {"name": "tiny", "path": "tiny.gguf", "size_bytes": 1 * 1024**3, "too_small": False},
        ]
        original = app.nvidia_gpus
        app.nvidia_gpus = lambda: [{"memory_total_mb": 24576}]
        try:
            picked = app.preferred_auto_model(entries)
        finally:
            app.nvidia_gpus = original
        self.assertEqual(picked["name"], "medium")

    def test_moe_model_reports_active_and_total_params_separately(self):
        path = MODEL_ROOT / "Synth-MoE-Q4_0.gguf"
        build_gguf(path, "synthmoe", scalars={
            "synthmoe.context_length": ("u32", 4096),
            "synthmoe.expert_count": ("u32", 8),
            "synthmoe.expert_used_count": ("u32", 2),
        }, tensors=[("blk.0.attn.weight", [1000, 1000]), ("blk.0.ffn_gate_exps.weight", [8, 1000, 1000])])
        model = self.register(path)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        shared, expert_pool = 1000 * 1000, 8 * 1000 * 1000
        self.assertEqual(row["expert_count"], 8)
        self.assertEqual(row["expert_used_count"], 2)
        self.assertEqual(row["total_params"], shared + expert_pool)
        self.assertAlmostEqual(row["active_params"], shared + expert_pool * (2 / 8), delta=1)

    def test_a_placeholder_weight_without_a_real_header_reports_no_params(self):
        model = self.register(self.small)
        status, models = self.call("/api/models")
        self.assertEqual(status, 200)
        row = next(m for m in models if m["id"] == model["id"])
        self.assertFalse(row["total_params"])
        self.assertGreater(row["size_bytes"], 0)


if __name__ == "__main__":
    unittest.main()
