from __future__ import annotations

import csv, io, json, mimetypes, os, shlex, shutil, sqlite3, subprocess, sys, tempfile, threading, time, uuid, zipfile, hashlib, hmac, secrets
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("SKEIN_DB_PATH", ROOT / "work" / "skein.db"))
STATIC = ROOT / "static"
DB_PATH.parent.mkdir(exist_ok=True)


def stamp(): return round(time.time(), 3)


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=15, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


@contextmanager
def db():
    conn = connect()
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def init_db():
    global DB_PATH
    schema = """
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS workflows(
          id TEXT PRIMARY KEY, objective TEXT NOT NULL, status TEXT NOT NULL,
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS tasks(
          id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, position INTEGER NOT NULL,
          title TEXT NOT NULL, role TEXT NOT NULL, dependencies TEXT NOT NULL,
          complexity REAL NOT NULL, risk REAL NOT NULL, criticality REAL NOT NULL,
          status TEXT NOT NULL, model TEXT, routing_score REAL, confidence REAL,
          result TEXT, attempts INTEGER NOT NULL DEFAULT 0,
          started_at REAL, finished_at REAL);
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
          task_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pools(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL, color TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gpu_assignments(
          gpu_id TEXT PRIMARY KEY, pool_id TEXT, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS models(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, backend TEXT NOT NULL,
          model_path TEXT NOT NULL, runtime_path TEXT NOT NULL, context_size INTEGER NOT NULL,
          port INTEGER NOT NULL, pool_id TEXT, status TEXT NOT NULL, pid INTEGER,
          endpoint TEXT, last_error TEXT, updated_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS artifacts(
          id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, task_id TEXT NOT NULL,
          relative_path TEXT NOT NULL, disk_path TEXT NOT NULL, kind TEXT NOT NULL,
          validation TEXT, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS executions(
          id TEXT PRIMARY KEY, workflow_id TEXT NOT NULL, artifact_id TEXT NOT NULL,
          runtime TEXT NOT NULL, image TEXT, status TEXT NOT NULL, exit_code INTEGER,
          stdout TEXT, stderr TEXT, duration REAL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS users(
          id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, password_hash TEXT NOT NULL,
          role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(
          token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """
    try:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with db() as conn: conn.executescript(schema)
    except sqlite3.OperationalError:
        if os.getenv("SKEIN_DB_PATH"): raise
        DB_PATH = Path(os.getenv("LOCALAPPDATA", tempfile.gettempdir())) / "Skein" / "skein.db"
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        with db() as conn: conn.executescript(schema)
    with db() as conn:
        columns={r[1] for r in conn.execute("PRAGMA table_info(workflows)")}
        if "owner_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN owner_id TEXT")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('reasoner','Reasoner','reasoner','#78a7ff')")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('workers','Workers','worker','#ffb44c')")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('retrieval','Retrieval','service','#b9f45c')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES('users_can_choose_execution_mode','false')")
        if not conn.execute("SELECT 1 FROM users").fetchone():
            username=os.getenv("SKEIN_ADMIN_USER","admin"); password=os.getenv("SKEIN_ADMIN_PASSWORD","admin")
            conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)",(str(uuid.uuid4()),username,password_hash(password),"admin",1,stamp()))
    discover_local_models()


def password_hash(password,salt=None):
    salt=salt or secrets.token_hex(16); digest=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),210000)
    return f"pbkdf2_sha256$210000${salt}${digest.hex()}"


def password_valid(password,encoded):
    try:
        _,rounds,salt,digest=encoded.split("$",3)
        actual=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),int(rounds)).hex()
        return hmac.compare_digest(actual,digest)
    except (ValueError,TypeError): return False


def setting_bool(key,default=False):
    with db() as conn: row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return (row["value"].lower()=="true") if row else default


def discover_local_models():
    home = Path.home()
    runtimes = [home / ".unsloth/llama.cpp/build/bin/Release/llama-server.exe"]
    runtimes += list((home / ".lmstudio/extensions/backends").glob("llama.cpp-win-*-nvidia-*/llama-server.exe")) if (home / ".lmstudio/extensions/backends").exists() else []
    # Prefer the proven standalone build; LM Studio entries can be tiny launcher shims.
    runtime = next((p for p in runtimes if p.is_file()), None)
    roots = [home / ".cache/huggingface/hub", home / ".lmstudio/models"]
    candidates=[]
    for root in roots:
        if root.exists(): candidates.extend(p for p in root.rglob("*.gguf") if "mmproj" not in p.name.lower() and p.stat().st_size > 1_000_000_000)
    model = min(candidates, key=lambda p:p.stat().st_size, default=None)
    if not runtime or not model: return
    with db() as conn:
        for role,port in (("reasoner",8001),("worker",8002)):
            if conn.execute("SELECT 1 FROM models WHERE role=?",(role,)).fetchone(): continue
            mid=str(uuid.uuid4())
            conn.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid,f"Auto {role} · {model.stem}",role,"llama.cpp",str(model),str(runtime),8192,port,None,"STOPPED",None,None,None,stamp()))


def run_text(args, timeout=4):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def nvidia_gpus():
    exe = shutil.which("nvidia-smi")
    if not exe: return []
    fields = "index,uuid,name,memory.total,memory.used,utilization.gpu,power.draw,power.limit,temperature.gpu"
    output = run_text([exe, f"--query-gpu={fields}", "--format=csv,noheader,nounits"])
    result = []
    for row in csv.reader(io.StringIO(output)):
        if len(row) != 9: continue
        vals = [v.strip() for v in row]
        def num(v):
            try: return float(v)
            except ValueError: return None
        result.append({"id": vals[1], "index": int(vals[0]), "vendor": "NVIDIA", "name": vals[2],
          "memory_total_mb": num(vals[3]), "memory_used_mb": num(vals[4]), "utilization": num(vals[5]),
          "power_w": num(vals[6]), "power_limit_w": num(vals[7]), "temperature_c": num(vals[8]),
          "metrics_source": "nvidia-smi"})
    return result


def windows_video_controllers():
    script = "Get-CimInstance Win32_VideoController | Select Name,PNPDeviceID,AdapterRAM,DriverVersion | ConvertTo-Json -Compress"
    output = run_text(["powershell", "-NoProfile", "-Command", script], 8)
    try: rows = json.loads(output) if output else []
    except json.JSONDecodeError: return []
    if isinstance(rows, dict): rows = [rows]
    return rows


def hardware_snapshot():
    gpus = nvidia_gpus(); known = {g["name"] for g in gpus}
    for pos, item in enumerate(windows_video_controllers()):
        name = item.get("Name", "GPU")
        if name in known: continue
        pnp = item.get("PNPDeviceID") or f"windows-{pos}"
        vendor = "Intel" if "Intel" in name else ("AMD" if "AMD" in name or "Radeon" in name else "Other")
        ram = item.get("AdapterRAM") or 0
        gpus.append({"id": pnp, "index": pos, "vendor": vendor, "name": name,
          "memory_total_mb": round(ram/1048576) if ram else None, "memory_used_mb": None,
          "utilization": None, "power_w": None, "power_limit_w": None, "temperature_c": None,
          "metrics_source": "Windows inventory"})
    with db() as conn:
        assignments = {r["gpu_id"]: r["pool_id"] for r in conn.execute("SELECT * FROM gpu_assignments")}
        pools = [dict(r) for r in conn.execute("SELECT * FROM pools ORDER BY rowid")]
    for gpu in gpus: gpu["pool_id"] = assignments.get(gpu["id"])
    cpu_script = "(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average"
    try: cpu = float(run_text(["powershell","-NoProfile","-Command",cpu_script],6) or 0)
    except ValueError: cpu = None
    return {"node":{"name":os.environ.get("COMPUTERNAME","local-node"),"cpu_utilization":cpu,
      "gpu_power_w":round(sum(g["power_w"] or 0 for g in gpus),1),"gpu_count":len(gpus)},
      "gpus":gpus,"pools":pools,"timestamp":stamp()}


RUNTIMES, ACTIVE_ENDPOINTS = {}, {}


def activate_model(model_id, pool_id):
    with db() as conn:
        model = conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
        gpu_rows = conn.execute("SELECT gpu_id FROM gpu_assignments WHERE pool_id=?",(pool_id,)).fetchall()
    if not model: return {"error":"Model not found"}, 404
    if model_id in RUNTIMES and RUNTIMES[model_id].poll() is None:
        return {"error":"Model already active"}, 409
    runtime, model_path = Path(model["runtime_path"]), Path(model["model_path"])
    error, pid, status = None, None, "CONFIGURED"
    paths_valid = runtime.is_file() and (model_path.is_file() or model["backend"] == "vllm")
    if paths_valid:
        env = os.environ.copy(); gpu_ids = [r["gpu_id"] for r in gpu_rows]
        indices = [str(g["index"]) for g in nvidia_gpus() if g["id"] in gpu_ids]
        if indices: env["CUDA_VISIBLE_DEVICES"] = ",".join(indices)
        if model["backend"] == "vllm":
            args = [str(runtime), "-m", "vllm.entrypoints.openai.api_server", "--model", model["model_path"],
                    "--host", "127.0.0.1", "--port", str(model["port"]), "--max-model-len", str(model["context_size"])]
        else:
            args = [str(runtime), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(model["port"]), "-c", str(model["context_size"]), "-ngl", "999"]
        try:
            proc = subprocess.Popen(args, cwd=str(runtime.parent), env=env, stdout=subprocess.DEVNULL,
              stderr=subprocess.DEVNULL, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            RUNTIMES[model_id]=proc; pid=proc.pid; status="STARTING"
        except OSError as exc: error=str(exc); status="ERROR"
    else:
        error="Runtime or model path not found; assignment saved without starting."
    endpoint=f"http://127.0.0.1:{model['port']}/v1/chat/completions"
    if status == "STARTING": ACTIVE_ENDPOINTS[model["role"]] = endpoint
    with db() as conn:
        conn.execute("UPDATE models SET pool_id=?,status=?,pid=?,endpoint=?,last_error=?,updated_at=? WHERE id=?",
          (pool_id,status,pid,endpoint,error,stamp(),model_id))
    return {"id":model_id,"status":status,"pid":pid,"endpoint":endpoint,"error":error}, 200


def stop_model(model_id):
    with db() as conn: model=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    proc=RUNTIMES.pop(model_id,None)
    if proc and proc.poll() is None: proc.terminate()
    if model and ACTIVE_ENDPOINTS.get(model["role"]) == model["endpoint"]: ACTIVE_ENDPOINTS.pop(model["role"],None)
    with db() as conn: conn.execute("UPDATE models SET status='STOPPED',pid=NULL,updated_at=? WHERE id=?",(stamp(),model_id))
    return {"id":model_id,"status":"STOPPED"}


def endpoint_ready(endpoint, timeout=2):
    try:
        with urlopen(endpoint.rsplit("/v1/",1)[0]+"/health",timeout=timeout) as response: return response.status==200
    except Exception: return False


def supervisor_call(action, method="GET"):
    try:
        req=Request(f"http://127.0.0.1:8777/{action}",data=b"{}" if method=="POST" else None,
                    headers={"Content-Type":"application/json"},method=method)
        with urlopen(req,timeout=3) as response: return json.load(response),response.status
    except Exception as exc:
        return {"error":"Supervisor unavailable","details":str(exc),
                "action":"Start the application with run-skein.cmd"},503


def autoload_models():
    discover_local_models()
    with db() as conn:
        rows=conn.execute("SELECT * FROM models WHERE role IN ('reasoner','worker') ORDER BY updated_at DESC").fetchall()
    selected={}
    for row in rows: selected.setdefault(row["role"],row)
    missing=[r for r in ("reasoner","worker") if r not in selected]
    if missing: return {"error":"Local profiles not found","missing":missing},400
    started=[]
    for role,row in selected.items():
        result,status=activate_model(row["id"],"reasoner" if role=="reasoner" else "workers")
        if status not in (200,409): return result,status
        started.append({"role":role,"id":row["id"],"endpoint":row["endpoint"] or result.get("endpoint")})
    deadline=time.time()+90
    while time.time()<deadline:
        if all(endpoint_ready(item["endpoint"]) for item in started): return {"status":"READY","models":started},200
        time.sleep(1)
    return {"error":"Runtimes did not reach the READY state","models":started},503


def emit(wid, kind, payload=None, tid=None):
    with db() as conn:
        conn.execute("INSERT INTO events(workflow_id,task_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
                     (wid, tid, kind, json.dumps(payload or {}, ensure_ascii=False), stamp()))


def plan_for(objective):
    text = objective.lower()
    sensitive = .86 if any(k in text for k in ("oauth", "auth", "secret", "sécur", "paiement")) else .38
    translation = any(k in text for k in ("traduis", "traduire", "traduction", "translate", "translation"))
    code = any(k in text for k in ("code", "programme", "script", "python", "javascript", "typescript", "html", "css", "api", "application", "app ", "page web", "fonction"))
    if translation:
        return [("Produce the requested translation", "translator", [], .35, .12, .65),
          ("Verify fidelity, language, and terminology", "reviewer", [0], .58, .18, .78),
          ("Deliver the final translation without unnecessary commentary", "integrator", [0,1], .42, .12, .85)]
    if code:
        return [("Define a minimal and directly usable implementation", "architect", [], .62, sensitive, .76),
          ("Write the complete implementation files", "coder", [0], .68, sensitive, .86),
          ("Add or correct tests and verify the code", "tester", [0,1], .58, sensitive, .84),
          ("Deliver the final result with usage instructions", "integrator", [1,2], .64, sensitive, .92)]
    return [("Execute the request concretely", "executor", [], .48, .25, .70),
      ("Verify accuracy and correct the result", "reviewer", [0], .61, .32, .82),
      ("Deliver the final directly usable answer", "integrator", [0,1], .52, .25, .90)]


def create_workflow(objective,owner_id=None):
    wid, created = str(uuid.uuid4()), stamp()
    specs = plan_for(objective)
    ids = [str(uuid.uuid4()) for _ in specs]
    with db() as conn:
        conn.execute("INSERT INTO workflows(id,objective,status,created_at,updated_at,owner_id) VALUES(?,?,?,?,?,?)", (wid, objective, "READY", created, created,owner_id))
        for pos, spec in enumerate(specs):
            title, role, deps, complexity, risk, criticality = spec
            conn.execute("""INSERT INTO tasks(id,workflow_id,position,title,role,dependencies,
              complexity,risk,criticality,status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
              (ids[pos], wid, pos, title, role, json.dumps([ids[i] for i in deps]),
               complexity, risk, criticality, "READY"))
    emit(wid, "workflow.created", {"objective": objective, "tasks": len(specs)})
    return wid


def route(task):
    score = round(.40*task["complexity"] + .30*task["risk"] + .30*task["criticality"], 3)
    if task["role"] in ("coder","tester","translator","executor") and task["risk"] < .90:
        return "worker-general", score
    if score >= .68 or task["role"] in ("architect", "security-reviewer", "integrator"):
        return "reasoner-large", score
    return "worker-general", score


class ModelClient:
    def __init__(self, tier):
        self.tier = tier
        prefix = "REASONER" if tier == "reasoner-large" else "WORKER"
        role = "reasoner" if tier == "reasoner-large" else "worker"
        self.url = os.getenv(f"SKEIN_{prefix}_URL", "") or ACTIVE_ENDPOINTS.get(role, "")
        self.model = os.getenv(f"SKEIN_{prefix}_MODEL", tier)

    def generate(self, task, objective, dependency_results=None, retry=False):
        if not self.url:
            if os.getenv("SKEIN_ALLOW_SIMULATION", "0") == "1": return self.simulate(task)
            return {"summary":"No real model is loaded","confidence":0.0,"assumptions":[],
              "evidence":[],"next_actions":["Load a model for this role in Model Plane"],
              "mode":"error","error":f"No active endpoint for {self.tier}"}
        dependency_results=dependency_results or []
        previous=json.dumps(dependency_results,ensure_ascii=False)[:12000]
        role_instruction = {
          "translator":"Perform the translation. deliverable must contain the final translated text.",
          "coder":"Write the actual code. files must contain every complete file with path and content.",
          "tester":"Verify previous files and include only required test files or corrections in files.",
          "integrator":"Produce the final usable answer in deliverable. Do not replace the result with a plan.",
          "executor":"Execute the request and place the complete result in deliverable.",
          "reviewer":"Verify the previous result, correct it, and place the corrected version in deliverable.",
        }.get(task["role"],"Complete the task concretely instead of only explaining how to do it.")
        prompt = ("/no_think\n"+("REPAIR ATTEMPT: the previous JSON was invalid. Verify every quote and escape sequence.\n" if retry else "")+role_instruction+"\nReturn only one JSON object with exactly these fields: "
          "summary (short), deliverable (complete usable result), files (list of {path, content}), "
          "confidence (0..1), assumptions, evidence, next_actions. Lists other than files contain at most 3 items. "
          "Do not wrap JSON in Markdown. Never claim that a file exists: include its complete content in files.\n"
          "USER OBJECTIVE:\n"+objective+"\nCURRENT TASK:\n"+task["title"]+"\nDEPENDENCY RESULTS:\n"+previous)
        body = json.dumps({"model": self.model, "messages": [{"role":"user","content":prompt}],
                           "temperature": 0 if retry else .15, "max_tokens": 4096,
                           "response_format": {"type":"json_object"},
                           "chat_template_kwargs": {"enable_thinking": False}}).encode()
        inference_started=time.perf_counter()
        try:
            with urlopen(Request(self.url, body, {"Content-Type":"application/json"}), timeout=120) as res:
                response=json.load(res); content = response["choices"][0]["message"]["content"].strip()
            if content.startswith("```"): content = content.split("\n",1)[1].rsplit("```",1)[0]
            parsed = json.loads(content)
            for key in ("assumptions","evidence","next_actions"):
                value=parsed.get(key,[])
                parsed[key]=value if isinstance(value,list) else ([value] if value else [])
            files=parsed.get("files",[])
            parsed["files"]=[f for f in files if isinstance(f,dict) and f.get("path") and isinstance(f.get("content"),str)] if isinstance(files,list) else []
            parsed["deliverable"]=str(parsed.get("deliverable") or parsed.get("summary") or "")
            parsed["mode"] = "live"
            usage=response.get("usage") or {}
            duration=max(.001,time.perf_counter()-inference_started)
            prompt_tokens=int(usage.get("prompt_tokens") or 0)
            completion_tokens=int(usage.get("completion_tokens") or 0)
            parsed["metrics"]={"prompt_tokens":prompt_tokens,"completion_tokens":completion_tokens,
              "total_tokens":int(usage.get("total_tokens") or prompt_tokens+completion_tokens),
              "inference_seconds":round(duration,3),"tokens_per_second":round(completion_tokens/duration,2)}
            return parsed
        except (URLError, TimeoutError, KeyError, json.JSONDecodeError) as exc:
            if isinstance(exc,json.JSONDecodeError) and not retry:
                return self.generate(task,objective,dependency_results,True)
            return {"summary":"Inference server failure","confidence":0.0,
              "assumptions":[],"evidence":[],"next_actions":["Check the runtime and its endpoint"],
              "mode":"error","error":f"Backend unavailable: {exc}"}

    def simulate(self, task):
        time.sleep(.35 + task["complexity"] * .55)
        base = .94 if self.tier == "reasoner-large" else .88 - task["complexity"] * .32
        confidence = round(max(.42, min(.97, base)), 2)
        return {"summary": f"{task['role']} processed '{task['title']}'.",
                "deliverable": f"Simulated result for {task['title']}", "files": [],
                "confidence": confidence,
                "assumptions": ["Demonstration without external tools"],
                "evidence": ["Dependencies completed", f"Route: {self.tier}"],
                "next_actions": ["Validate the produced artifact"], "mode": "simulation"}


POOL = ThreadPoolExecutor(max_workers=4, thread_name_prefix="skein-worker")
ACTIVE, ACTIVE_LOCK = set(), threading.Lock()


class PowerSampler:
    """Best-effort GPU power attribution for one task; shared GPU load remains an estimate."""
    def __init__(self): self.samples=[]; self.stop_event=threading.Event(); self.thread=None
    def start(self):
        def sample():
            while not self.stop_event.is_set():
                watts=sum(float(g.get("power_w") or 0) for g in nvidia_gpus())
                if watts>0: self.samples.append((time.monotonic(),watts))
                self.stop_event.wait(.5)
        self.thread=threading.Thread(target=sample,daemon=True); self.thread.start(); return self
    def stop(self,duration):
        self.stop_event.set()
        if self.thread: self.thread.join(2)
        values=[x[1] for x in self.samples]
        avg=sum(values)/len(values) if values else 0
        return {"average_power_w":round(avg,2),"peak_power_w":round(max(values,default=0),2),
          "energy_wh":round(avg*duration/3600,4),"power_samples":len(values),
          "energy_method":"estimated_nvidia_smi_task_window"}


def artifact_root(wid):
    root=DB_PATH.parent/"workflows"/wid/"artifacts"; root.mkdir(parents=True,exist_ok=True); return root


def validate_artifact(path):
    try:
        suffix=path.suffix.lower()
        if suffix==".py":
            proc=subprocess.run([sys.executable,"-B","-m","py_compile",str(path)],capture_output=True,text=True,timeout=15,
              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            return {"status":"PASS" if proc.returncode==0 else "FAIL","check":"python syntax","details":(proc.stderr or proc.stdout).strip()}
        if suffix==".json": json.loads(path.read_text(encoding="utf-8")); return {"status":"PASS","check":"JSON parse","details":""}
        if suffix in (".js",".mjs") and shutil.which("node"):
            proc=subprocess.run([shutil.which("node"),"--check",str(path)],capture_output=True,text=True,timeout=15,
              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            return {"status":"PASS" if proc.returncode==0 else "FAIL","check":"JavaScript syntax","details":(proc.stderr or proc.stdout).strip()}
        if suffix in (".html",".htm"):
            text=path.read_text(encoding="utf-8").lower(); ok="<html" in text or "<!doctype" in text
            return {"status":"PASS" if ok else "WARN","check":"HTML structure","details":"" if ok else "Balise html/doctype absente"}
        return {"status":"PASS","check":"file created","details":""}
    except Exception as exc: return {"status":"FAIL","check":"validation","details":str(exc)}


SANDBOXES={
  "python":{"extensions":{'.py'},"image":"python:3.12-alpine"},
  "node":{"extensions":{'.js','.mjs'},"image":"node:22-alpine"},
  "java":{"extensions":{'.java'},"image":"eclipse-temurin:21-jdk-alpine"},
  "php":{"extensions":{'.php'},"image":"php:8.4-cli-alpine"},
  "html":{"extensions":{'.html','.htm','.css'},"image":None},
}
EXECUTION_MODE="sandbox"


def docker_image_ready(image):
    if not shutil.which("docker"): return False
    proc=subprocess.run([shutil.which("docker"),"image","inspect",image],capture_output=True,timeout=8,
      creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    return proc.returncode==0


def sandbox_capabilities():
    docker=bool(shutil.which("docker"))
    return {name:{"extensions":sorted(cfg["extensions"]),"image":cfg["image"],
      "available":True if cfg["image"] is None else docker and docker_image_ready(cfg["image"])} for name,cfg in SANDBOXES.items()}


def runtime_for(path):
    suffix=Path(path).suffix.lower()
    return next((name for name,cfg in SANDBOXES.items() if suffix in cfg["extensions"]),None)


def local_runtime_command(runtime,target):
    if runtime=="python": return [sys.executable,"-B",str(target)]
    if runtime=="node" and shutil.which("node"): return [shutil.which("node"),str(target)]
    if runtime=="php" and shutil.which("php"): return [shutil.which("php"),str(target)]
    if runtime=="java" and shutil.which("javac") and shutil.which("java"):
        return ["powershell","-NoProfile","-Command",f"& '{shutil.which('javac')}' '{target}'; if($LASTEXITCODE -eq 0){{& '{shutil.which('java')}' -cp '{target.parent}' '{target.stem}'}}"]
    return None


def execute_in_sandbox(artifact_id,timeout=20,mode=None):
    mode=mode or EXECUTION_MODE
    with db() as conn: artifact=conn.execute("SELECT * FROM artifacts WHERE id=?",(artifact_id,)).fetchone()
    if not artifact: return {"error":"artifact introuvable"},404
    runtime=runtime_for(artifact["relative_path"])
    if not runtime: return {"error":"runtime non supporté","extension":Path(artifact["relative_path"]).suffix},400
    eid=str(uuid.uuid4()); cfg=SANDBOXES[runtime]; started=time.time()
    if runtime=="html":
        result={"id":eid,"status":"PREVIEW_READY","runtime":runtime,"exit_code":0,"stdout":"Aperçu isolé disponible","stderr":"","duration":0}
    elif mode=="local":
        target=Path(artifact["disk_path"]); command=local_runtime_command(runtime,target)
        if not command: result={"id":eid,"status":"UNAVAILABLE","runtime":runtime,"exit_code":None,"stdout":"","stderr":f"Runtime local indisponible: {runtime}","duration":0}
        else:
            try:
                proc=subprocess.run(command,cwd=target.parent,capture_output=True,text=True,timeout=max(1,min(int(timeout),60)),
                  creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
                result={"id":eid,"status":"PASS" if proc.returncode==0 else "FAIL","runtime":runtime,"exit_code":proc.returncode,
                  "stdout":proc.stdout[-20000:],"stderr":proc.stderr[-20000:],"duration":round(time.time()-started,3)}
            except subprocess.TimeoutExpired as exc: result={"id":eid,"status":"TIMEOUT","runtime":runtime,"exit_code":None,
              "stdout":(exc.stdout or "")[-20000:] if isinstance(exc.stdout,str) else "","stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
    elif not docker_image_ready(cfg["image"]):
        result={"id":eid,"status":"UNAVAILABLE","runtime":runtime,"exit_code":None,"stdout":"","stderr":f"Image Docker absente: {cfg['image']}","duration":0}
    else:
        source=Path(artifact["disk_path"]); workspace=artifact_root(artifact["workflow_id"])
        scratch=Path(tempfile.mkdtemp(prefix="skein-sandbox-")); shutil.copytree(workspace,scratch/"workspace",dirs_exist_ok=True)
        rel=Path(artifact["relative_path"]); target="/workspace/"+rel.as_posix(); name="skein-"+eid[:12]
        if runtime=="python": command=["python",target]
        elif runtime=="node": command=["node",target]
        elif runtime=="php": command=["php",target]
        else:
            stem=rel.stem; command=["sh","-lc",f"/opt/java/openjdk/bin/javac {shlex.quote(target)} -d /tmp/classes && /opt/java/openjdk/bin/java -cp /tmp/classes {shlex.quote(stem)}"]
        args=[shutil.which("docker"),"run","--rm","--name",name,"--network","none","--cpus","1","--memory","384m",
          "--pids-limit","64","--read-only","--tmpfs","/tmp:rw,nosuid,size=96m","-v",f"{scratch/'workspace'}:/workspace:ro","-w","/workspace",cfg["image"],*command]
        try:
            proc=subprocess.run(args,capture_output=True,text=True,timeout=max(1,min(int(timeout),60)),
              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result={"id":eid,"status":"PASS" if proc.returncode==0 else "FAIL","runtime":runtime,"exit_code":proc.returncode,
              "stdout":proc.stdout[-20000:],"stderr":proc.stderr[-20000:],"duration":round(time.time()-started,3)}
        except subprocess.TimeoutExpired as exc:
            subprocess.run([shutil.which("docker"),"rm","-f",name],capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result={"id":eid,"status":"TIMEOUT","runtime":runtime,"exit_code":None,"stdout":(exc.stdout or "")[-20000:] if isinstance(exc.stdout,str) else "",
              "stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
        finally: shutil.rmtree(scratch,ignore_errors=True)
    with db() as conn: conn.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (eid,artifact["workflow_id"],artifact_id,runtime,cfg["image"] if mode=="sandbox" else "LOCAL",result["status"],result["exit_code"],result["stdout"],result["stderr"],result["duration"],stamp()))
    return result,200


def execute_command(wid,command,timeout=20,mode=None):
    mode=mode or EXECUTION_MODE; root=artifact_root(wid); eid=str(uuid.uuid4()); started=time.time()
    if not command or len(command)>4000: return {"error":"commande vide ou trop longue"},400
    if mode=="local":
        args=["powershell","-NoProfile","-Command",command]; cwd=root; image="LOCAL"
    else:
        image="alpine:3.20"
        if not docker_image_ready(image): return {"error":"image de terminal sandbox absente","image":image},503
        scratch=Path(tempfile.mkdtemp(prefix="skein-shell-")); shutil.copytree(root,scratch/"workspace",dirs_exist_ok=True)
        name="skein-shell-"+eid[:10]; args=[shutil.which("docker"),"run","--rm","--name",name,"--network","none","--cpus","1","--memory","256m",
          "--pids-limit","64","--read-only","--tmpfs","/tmp:rw,nosuid,size=64m","-v",f"{scratch/'workspace'}:/workspace:rw","-w","/workspace",image,"sh","-lc",command]; cwd=ROOT
    try:
        proc=subprocess.run(args,cwd=cwd,capture_output=True,text=True,timeout=max(1,min(int(timeout),60)),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        result={"id":eid,"status":"PASS" if proc.returncode==0 else "FAIL","mode":mode,"exit_code":proc.returncode,
          "stdout":proc.stdout[-20000:],"stderr":proc.stderr[-20000:],"duration":round(time.time()-started,3)}
    except subprocess.TimeoutExpired as exc:
        if mode=="sandbox": subprocess.run([shutil.which("docker"),"rm","-f",name],capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        result={"id":eid,"status":"TIMEOUT","mode":mode,"exit_code":None,"stdout":"","stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
    finally:
        if mode=="sandbox": shutil.rmtree(scratch,ignore_errors=True)
    with db() as conn: conn.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (eid,wid,"command",f"shell-{mode}",image,result["status"],result["exit_code"],result["stdout"],result["stderr"],result["duration"],stamp()))
    return result,200


def artifact_preview(artifact_id):
    with db() as conn:
        artifact=conn.execute("SELECT * FROM artifacts WHERE id=?",(artifact_id,)).fetchone()
        if not artifact: return None
        siblings=conn.execute("SELECT * FROM artifacts WHERE workflow_id=?",(artifact["workflow_id"],)).fetchall()
    path=Path(artifact["disk_path"]); content=path.read_text(encoding="utf-8")
    suffix=path.suffix.lower()
    if suffix in (".html",".htm"):
        css="\n".join(Path(s["disk_path"]).read_text(encoding="utf-8") for s in siblings if Path(s["relative_path"]).suffix.lower()==".css" and Path(s["disk_path"]).is_file())
        content=content.replace("</head>",f"<style>{css}</style></head>") if "</head>" in content.lower() else f"<style>{css}</style>"+content
        return {"type":"html","content":content,"sandbox":"scripts disabled"}
    return {"type":"markdown" if suffix==".md" else "text","content":content,"language":suffix.lstrip(".")}


def persist_artifacts(wid,tid,files):
    allowed={".py",".js",".mjs",".ts",".tsx",".jsx",".java",".php",".html",".css",".json",".md",".txt",".yaml",".yml",".toml",".ini",".sh",".ps1",".sql",".xml",".csv"}
    saved=[]; root=artifact_root(wid)
    for item in files[:20]:
        relative=str(item.get("path","")).replace("\\","/").lstrip("/")
        parts=[p for p in relative.split("/") if p not in ("",".")]
        if not parts or ".." in parts or Path(parts[-1]).suffix.lower() not in allowed: continue
        content=item.get("content","")[:1_000_000]; target=root.joinpath(*parts).resolve()
        if root.resolve() not in target.parents: continue
        target.parent.mkdir(parents=True,exist_ok=True); target.write_text(content,encoding="utf-8")
        validation=validate_artifact(target); aid=str(uuid.uuid4())
        with db() as conn: conn.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?,?,?)",
          (aid,wid,tid,"/".join(parts),str(target),"file",json.dumps(validation,ensure_ascii=False),stamp()))
        saved.append({"id":aid,"path":"/".join(parts),"size":len(content.encode()),"validation":validation})
    return saved


def execute_task(wid, tid, objective):
    with db() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
        dependency_results=[]
        for dep in json.loads(task["dependencies"]):
            row=conn.execute("SELECT title,result FROM tasks WHERE id=?",(dep,)).fetchone()
            if row and row["result"]: dependency_results.append({"task":row["title"],"result":json.loads(row["result"])})
        model, score = route(task)
        conn.execute("UPDATE tasks SET status='RUNNING',model=?,routing_score=?,attempts=attempts+1,started_at=? WHERE id=?",
                     (model, score, stamp(), tid))
    emit(wid, "task.started", {"model": model, "routing_score": score}, tid)
    task_started=time.perf_counter(); power=PowerSampler().start()
    result = ModelClient(model).generate(task, objective, dependency_results)
    if model == "worker-general" and float(result.get("confidence", 0)) < .65:
        emit(wid, "task.escalated", {"from": model, "confidence": result.get("confidence")}, tid)
        model, result = "reasoner-large", ModelClient("reasoner-large").generate(task, objective, dependency_results)
    duration=max(.001,time.perf_counter()-task_started)
    metrics=result.setdefault("metrics",{})
    metrics.update({"execution_seconds":round(duration,3),**power.stop(duration)})
    result["artifacts"]=persist_artifacts(wid,tid,result.get("files",[])) if result.get("files") else []
    confidence = float(result.get("confidence", 0))
    status = "COMPLETED" if confidence >= .55 else "FAILED"
    with db() as conn:
        conn.execute("UPDATE tasks SET status=?,model=?,confidence=?,result=?,finished_at=? WHERE id=?",
                     (status, model, confidence, json.dumps(result, ensure_ascii=False), stamp(), tid))
    emit(wid, "task.completed" if status == "COMPLETED" else "task.failed",
         {"model": model, "confidence": confidence}, tid)


def orchestrate(wid):
    try:
        with db() as conn:
            wf = conn.execute("SELECT * FROM workflows WHERE id=?", (wid,)).fetchone()
            if not wf: return
            conn.execute("UPDATE tasks SET status='READY' WHERE workflow_id=? AND status='RUNNING'", (wid,))
            conn.execute("UPDATE workflows SET status='RUNNING',updated_at=? WHERE id=?", (stamp(), wid))
        emit(wid, "workflow.started")
        while True:
            with db() as conn:
                tasks = conn.execute("SELECT * FROM tasks WHERE workflow_id=? ORDER BY position", (wid,)).fetchall()
            state = {t["id"]: t["status"] for t in tasks}
            if all(s == "COMPLETED" for s in state.values()):
                emit(wid, "workflow.completed")
                with db() as conn: conn.execute("UPDATE workflows SET status='COMPLETED',updated_at=? WHERE id=?", (stamp(),wid))
                return
            if any(s == "FAILED" for s in state.values()):
                emit(wid, "workflow.failed")
                with db() as conn: conn.execute("UPDATE workflows SET status='FAILED',updated_at=? WHERE id=?", (stamp(),wid))
                return
            ready = [t for t in tasks if t["status"] == "READY" and all(state[d] == "COMPLETED" for d in json.loads(t["dependencies"]))]
            if ready:
                futures = [POOL.submit(execute_task, wid, t["id"], wf["objective"]) for t in ready]
                for future in futures: future.result()
            else: time.sleep(.15)
    finally:
        with ACTIVE_LOCK: ACTIVE.discard(wid)


def start_workflow(wid):
    with ACTIVE_LOCK:
        if wid in ACTIVE: return False
        ACTIVE.add(wid)
    threading.Thread(target=orchestrate, args=(wid,), daemon=True).start()
    return True


def workflow_data(wid):
    with db() as conn:
        wf = conn.execute("SELECT * FROM workflows WHERE id=?",(wid,)).fetchone()
        if not wf: return None
        tasks = conn.execute("SELECT * FROM tasks WHERE workflow_id=? ORDER BY position",(wid,)).fetchall()
        events = conn.execute("SELECT * FROM events WHERE workflow_id=? ORDER BY id DESC LIMIT 100",(wid,)).fetchall()
        artifact_rows=conn.execute("SELECT * FROM artifacts WHERE workflow_id=? ORDER BY created_at DESC",(wid,)).fetchall()
    out=[]
    for row in tasks:
        item=dict(row); item["dependencies"]=json.loads(item["dependencies"]); item["result"]=json.loads(item["result"]) if item["result"] else None; out.append(item)
    ev=[]
    for row in events:
        item=dict(row); item["payload"]=json.loads(item["payload"]); ev.append(item)
    artifacts=[]; seen=set()
    for row in artifact_rows:
        if row["relative_path"] in seen: continue
        seen.add(row["relative_path"]); item=dict(row); item["validation"]=json.loads(item["validation"]) if item["validation"] else None
        item.pop("disk_path",None); item["download_url"]=f"/api/artifacts/{item['id']}"; artifacts.append(item)
    completed=[t for t in out if t["status"]=="COMPLETED" and t["result"]]
    final=completed[-1]["result"] if completed else None
    task_metrics=[t["result"].get("metrics",{}) for t in out if t.get("result")]
    total_tokens=sum(int(m.get("total_tokens") or 0) for m in task_metrics)
    completion_tokens=sum(int(m.get("completion_tokens") or 0) for m in task_metrics)
    execution_seconds=sum(float(m.get("execution_seconds") or 0) for m in task_metrics)
    workflow_seconds=max(0,float(wf["updated_at"])-(float(wf["created_at"])))
    summary={"task_count":len(out),"completed_tasks":len(completed),"total_tokens":total_tokens,
      "prompt_tokens":sum(int(m.get("prompt_tokens") or 0) for m in task_metrics),
      "completion_tokens":completion_tokens,"execution_seconds":round(execution_seconds,3),
      "wall_clock_seconds":round(workflow_seconds,3),
      "average_tokens_per_second":round(completion_tokens/execution_seconds,2) if execution_seconds else 0,
      "average_power_w":round(sum(float(m.get("average_power_w") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "peak_power_w":round(max([float(m.get("peak_power_w") or 0) for m in task_metrics] or [0]),2),
      "energy_wh":round(sum(float(m.get("energy_wh") or 0) for m in task_metrics),4),
      "energy_note":"Estimated from nvidia-smi samples; simultaneous GPU load may be attributed to more than one task."}
    return {"workflow":dict(wf),"tasks":out,"events":ev,"final_output":final,"artifacts":artifacts,"summary":summary,
            "deliverable":{"kind":"none" if not artifacts else ("file" if len(artifacts)==1 else "project"),"file_count":len(artifacts),"archive_url":f"/api/workflows/{wid}/deliverable.zip" if artifacts else None},
            "artifact_notice":f"{len(artifacts)} file(s) produced and validated." if artifacts else "No file was required or produced for this request."}


def workflow_report(wid):
    data=workflow_data(wid)
    if not data: return None
    w=data["workflow"]; s=data["summary"]; lines=[f"# Skein Report — {w['objective']}","",f"Status: **{w['status']}**","",data["artifact_notice"],"",
      "## Execution summary","",f"- Tokens: **{s['total_tokens']}** ({s['prompt_tokens']} input, {s['completion_tokens']} output)",
      f"- Average throughput: **{s['average_tokens_per_second']} tokens/s**",f"- Cumulative task time: **{s['execution_seconds']} s**",
      f"- Workflow wall time: **{s['wall_clock_seconds']} s**",f"- Average / peak GPU power: **{s['average_power_w']} W / {s['peak_power_w']} W**",
      f"- Estimated GPU energy: **{s['energy_wh']} Wh**","",f"> {s['energy_note']}",""]
    for i,task in enumerate(data["tasks"],1):
        result=task["result"] or {}
        lines += [f"## Step {i} — {task['title']}","",f"- Role: `{task['role']}`",f"- Model: `{task['model'] or 'not executed'}`",
          f"- Status: `{task['status']}`",f"- Confidence: {task['confidence'] if task['confidence'] is not None else 'N/A'}"]
        metrics=result.get("metrics",{})
        if metrics: lines += [f"- Tokens: {metrics.get('total_tokens',0)} ({metrics.get('tokens_per_second',0)} tokens/s)",
          f"- Duration: {metrics.get('execution_seconds',0)} s",f"- Average power: {metrics.get('average_power_w',0)} W",
          f"- Estimated energy: {metrics.get('energy_wh',0)} Wh"]
        lines += [""]
        if result:
            lines += [result.get("summary",""),"", "### Deliverable", "", result.get("deliverable",result.get("summary","")),""]
            for label,key in (("Assumptions","assumptions"),("Evidence","evidence"),("Next actions","next_actions")):
                values=result.get(key) or []
                if not isinstance(values,list): values=[values]
                if values: lines += [f"### {label}",""]+[f"- {v}" for v in values]+[""]
        elif task["status"]!="COMPLETED": lines += ["_Step not executed._",""]
    return "\n".join(lines)


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(STATIC),**kwargs)
    def log_message(self,fmt,*args): pass
    def json(self,data,status=200):
        raw=json.dumps(data,ensure_ascii=False).encode(); self.send_response(status)
        self.send_header("Content-Type","application/json; charset=utf-8"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def text(self,data,content_type="text/plain; charset=utf-8",filename=None,status=200):
        raw=data.encode(); self.send_response(status); self.send_header("Content-Type",content_type)
        if filename: self.send_header("Content-Disposition",f'attachment; filename="{filename}"')
        self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def file(self,path,filename):
        raw=path.read_bytes(); self.send_response(200); self.send_header("Content-Type",mimetypes.guess_type(filename)[0] or "application/octet-stream")
        self.send_header("Content-Disposition",f'attachment; filename="{Path(filename).name}"'); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def binary(self,raw,content_type,filename):
        self.send_response(200); self.send_header("Content-Type",content_type)
        self.send_header("Content-Disposition",f'attachment; filename="{filename}"'); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def cookie(self,name):
        for part in self.headers.get("Cookie","").split(";"):
            key,sep,value=part.strip().partition("=")
            if sep and key==name: return value
        return None
    def current_user(self):
        if os.getenv("SKEIN_AUTH_DISABLED","0")=="1": return {"id":"test-admin","username":"test-admin","role":"admin","active":1}
        token=self.cookie("skein_session")
        if not token: return None
        with db() as conn:
            row=conn.execute("SELECT u.id,u.username,u.role,u.active FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?",(token,stamp())).fetchone()
        return dict(row) if row and row["active"] else None
    def deny(self,status=401,message="Authentication required"):
        return self.json({"error":message,"action":"Sign in with an authorized account."},status)
    def authorize(self,admin=False):
        user=self.current_user()
        if not user: self.deny(); return None
        if admin and user["role"]!="admin": self.deny(403,"Administrator access required"); return None
        return user
    def workflow_allowed(self,user,wid):
        if user["role"]=="admin": return True
        with db() as conn: row=conn.execute("SELECT owner_id FROM workflows WHERE id=?",(wid,)).fetchone()
        return bool(row and row["owner_id"]==user["id"])
    def send_session(self,user,token):
        raw=json.dumps({"user":user,"policy":{"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")}},ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Set-Cookie",f"skein_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
        self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path=="/api/health": return self.json({"status":"ok","active":len(ACTIVE),"database":str(DB_PATH),"execution_mode":EXECUTION_MODE,"active_models":ACTIVE_ENDPOINTS,"backends":{"worker":bool(os.getenv("SKEIN_WORKER_URL") or ACTIVE_ENDPOINTS.get("worker")),"reasoner":bool(os.getenv("SKEIN_REASONER_URL") or ACTIVE_ENDPOINTS.get("reasoner"))}})
        if self.path=="/api/auth/me":
            user=self.authorize()
            return self.json({"user":user,"policy":{"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")}}) if user else None
        if self.path.startswith("/api/"):
            admin=self.path in ("/api/users","/api/admin/settings")
            user=self.authorize(admin)
            if not user: return
            if self.path.startswith("/api/workflows/"):
                wid=self.path.split("/")[3]
                if not self.workflow_allowed(user,wid): return self.deny(403,"This workflow belongs to another user")
            if self.path.startswith("/api/artifacts/") and user["role"]!="admin":
                aid=self.path.split("/")[3]
                with db() as conn: row=conn.execute("SELECT workflow_id FROM artifacts WHERE id=?",(aid,)).fetchone()
                if not row or not self.workflow_allowed(user,row["workflow_id"]): return self.deny(403,"Artifact access denied")
        if self.path=="/api/users":
            with db() as conn: rows=conn.execute("SELECT id,username,role,active,created_at FROM users ORDER BY username").fetchall()
            return self.json([dict(r) for r in rows])
        if self.path=="/api/admin/settings": return self.json({"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")})
        if self.path=="/api/sandbox/capabilities": return self.json({"mode":EXECUTION_MODE,"runtimes":sandbox_capabilities()})
        if self.path=="/api/execution-mode": return self.json({"mode":EXECUTION_MODE,"warning":"Local mode runs on the host without isolation." if EXECUTION_MODE=="local" else None})
        if self.path=="/api/hardware": return self.json(hardware_snapshot())
        if self.path=="/api/pools":
            with db() as conn: rows=conn.execute("SELECT * FROM pools ORDER BY rowid").fetchall()
            return self.json([dict(r) for r in rows])
        if self.path=="/api/models":
            with db() as conn: rows=conn.execute("SELECT * FROM models ORDER BY updated_at DESC").fetchall()
            models=[]
            for row in rows:
                item=dict(row); proc=RUNTIMES.get(item["id"])
                if proc and proc.poll() is None and item["status"]=="STARTING": item["status"]="RUNNING"
                elif proc and proc.poll() is not None: item["status"]="STOPPED"
                models.append(item)
            return self.json(models)
        if self.path=="/api/stack/status":
            result,status=supervisor_call("status"); return self.json(result,status)
        if self.path=="/api/workflows":
            with db() as conn:
                rows=conn.execute("SELECT * FROM workflows ORDER BY created_at DESC LIMIT 30").fetchall() if user["role"]=="admin" else conn.execute("SELECT * FROM workflows WHERE owner_id=? ORDER BY created_at DESC LIMIT 30",(user["id"],)).fetchall()
            return self.json([dict(r) for r in rows])
        if self.path.startswith("/api/artifacts/"):
            if self.path.endswith("/preview"):
                aid=self.path.split("/")[-2]; preview=artifact_preview(aid)
                return self.json(preview or {"error":"artifact introuvable"},200 if preview else 404)
            aid=self.path.rsplit("/",1)[-1]
            with db() as conn: row=conn.execute("SELECT * FROM artifacts WHERE id=?",(aid,)).fetchone()
            if not row: return self.json({"error":"artifact introuvable"},404)
            path=Path(row["disk_path"])
            if not path.is_file(): return self.json({"error":"fichier absent du disque"},410)
            return self.file(path,row["relative_path"])
        if self.path.startswith("/api/workflows/") and self.path.endswith("/executions"):
            wid=self.path.split("/")[-2]
            with db() as conn: rows=conn.execute("SELECT * FROM executions WHERE workflow_id=? ORDER BY created_at DESC LIMIT 100",(wid,)).fetchall()
            return self.json([dict(r) for r in rows])
        if self.path.startswith("/api/workflows/") and self.path.endswith("/deliverable.zip"):
            wid=self.path.split("/")[-2]; data=workflow_data(wid)
            if not data: return self.json({"error":"workflow introuvable"},404)
            with db() as conn: rows=conn.execute("SELECT relative_path,disk_path FROM artifacts WHERE workflow_id=? ORDER BY created_at",(wid,)).fetchall()
            if not rows: return self.json({"error":"aucun livrable fichier"},404)
            buffer=io.BytesIO()
            with zipfile.ZipFile(buffer,"w",zipfile.ZIP_DEFLATED) as archive:
                added=set()
                for row in rows:
                    if row["relative_path"] in added or not Path(row["disk_path"]).is_file(): continue
                    added.add(row["relative_path"]); archive.write(row["disk_path"],row["relative_path"])
                archive.writestr("SKEIN-WORKFLOW-REPORT.md",workflow_report(wid) or "")
            return self.binary(buffer.getvalue(),"application/zip",f"skein-{wid[:8]}-livrable.zip")
        if self.path.startswith("/api/workflows/") and self.path.endswith("/report"):
            wid=self.path.split("/")[-2]; report=workflow_report(wid)
            return self.text(report or "Workflow introuvable","text/markdown; charset=utf-8",f"skein-{wid[:8]}.md",200 if report else 404)
        if self.path.startswith("/api/workflows/"):
            data=workflow_data(self.path.rsplit("/",1)[-1]); return self.json(data or {"error":"introuvable"},200 if data else 404)
        return super().do_GET()
    def do_POST(self):
        global EXECUTION_MODE
        try: body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))) or b"{}")
        except json.JSONDecodeError: return self.json({"error":"JSON invalide"},400)
        if self.path=="/api/auth/login":
            username=str(body.get("username","")).strip(); password=str(body.get("password",""))
            with db() as conn: row=conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(username,)).fetchone()
            if not row or not row["active"] or not password_valid(password,row["password_hash"]): return self.deny(401,"Invalid credentials")
            token=secrets.token_urlsafe(32)
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE expires_at<=?",(stamp(),)); conn.execute("INSERT INTO sessions VALUES(?,?,?,?)",(token,row["id"],stamp()+43200,stamp()))
            return self.send_session({"id":row["id"],"username":row["username"],"role":row["role"]},token)
        if self.path=="/api/auth/logout":
            token=self.cookie("skein_session")
            if token:
                with db() as conn: conn.execute("DELETE FROM sessions WHERE token=?",(token,))
            raw=b'{"ok":true}'; self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Set-Cookie","skein_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); return self.wfile.write(raw)
        admin=self.path.startswith(("/api/models","/api/pools","/api/gpus","/api/stack","/api/users","/api/admin"))
        user=self.authorize(admin)
        if not user: return
        if self.path.startswith("/api/workflows/"):
            wid=self.path.split("/")[3]
            if not self.workflow_allowed(user,wid): return self.deny(403,"This workflow belongs to another user")
        if self.path.startswith("/api/artifacts/") and user["role"]!="admin":
            aid=self.path.split("/")[3]
            with db() as conn: artifact_owner=conn.execute("SELECT workflow_id FROM artifacts WHERE id=?",(aid,)).fetchone()
            if not artifact_owner or not self.workflow_allowed(user,artifact_owner["workflow_id"]): return self.deny(403,"Artifact access denied")
        if self.path=="/api/users":
            username=str(body.get("username","")).strip(); password=str(body.get("password","")); role=str(body.get("role","user"))
            if len(username)<3 or len(password)<8 or role not in ("admin","user"): return self.json({"error":"Username must be at least 3 characters, password at least 8, and role admin or user"},400)
            try:
                uid=str(uuid.uuid4())
                with db() as conn: conn.execute("INSERT INTO users VALUES(?,?,?,?,?,?)",(uid,username,password_hash(password),role,1,stamp()))
                return self.json({"id":uid,"username":username,"role":role,"active":1},201)
            except sqlite3.IntegrityError: return self.json({"error":"This username already exists"},409)
        if self.path.startswith("/api/users/"):
            uid=self.path.rsplit("/",1)[-1]; fields=[]; values=[]
            with db() as conn: existing=conn.execute("SELECT role,active FROM users WHERE id=?",(uid,)).fetchone()
            if not existing: return self.json({"error":"User not found"},404)
            resulting_role=body.get("role",existing["role"]); resulting_active=(1 if body["active"] else 0) if "active" in body else existing["active"]
            if existing["role"]=="admin" and existing["active"] and (resulting_role!="admin" or not resulting_active):
                with db() as conn: remaining=conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1 AND id<>?",(uid,)).fetchone()[0]
                if remaining==0: return self.json({"error":"At least one active administrator is required"},409)
            if body.get("role") in ("admin","user"): fields.append("role=?"); values.append(body["role"])
            if "active" in body: fields.append("active=?"); values.append(1 if body["active"] else 0)
            if body.get("password"):
                if len(str(body["password"]))<8: return self.json({"error":"Password is too short"},400)
                fields.append("password_hash=?"); values.append(password_hash(str(body["password"])))
            if not fields: return self.json({"error":"No changes supplied"},400)
            with db() as conn: conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?",(*values,uid))
            return self.json({"ok":True})
        if self.path=="/api/admin/settings":
            allowed=bool(body.get("users_can_choose_execution_mode"))
            with db() as conn: conn.execute("INSERT INTO settings VALUES('users_can_choose_execution_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("true" if allowed else "false",))
            return self.json({"users_can_choose_execution_mode":allowed})
        if self.path=="/api/workflows":
            objective=str(body.get("objective","")).strip()
            if len(objective)<5: return self.json({"error":"Objective is too short"},400)
            if os.getenv("SKEIN_ALLOW_SIMULATION","0") != "1":
                missing=[role for role in ("reasoner","worker") if not endpoint_ready(ACTIVE_ENDPOINTS.get(role,""))]
                if missing: return self.json({"error":"Real models are not loaded","missing_roles":missing,
                  "action":"Use Auto-detect and load local models in Model Plane."},409)
            wid=create_workflow(objective,user["id"]); start_workflow(wid); return self.json({"id":wid},201)
        if self.path=="/api/execution-mode":
            if user["role"]!="admin" and not setting_bool("users_can_choose_execution_mode"): return self.deny(403,"Execution mode selection is disabled by the administrator")
            mode=str(body.get("mode","")).lower()
            if mode not in ("sandbox","local"): return self.json({"error":"Invalid mode"},400)
            EXECUTION_MODE=mode; return self.json({"mode":mode,"warning":"Unisolated local execution" if mode=="local" else None})
        if self.path.startswith("/api/artifacts/") and self.path.endswith("/execute"):
            aid=self.path.split("/")[-2]; result,status=execute_in_sandbox(aid,int(body.get("timeout",20)),EXECUTION_MODE)
            return self.json(result,status)
        if self.path.startswith("/api/workflows/") and self.path.endswith("/command"):
            wid=self.path.split("/")[-2]; result,status=execute_command(wid,str(body.get("command","")),int(body.get("timeout",20)),EXECUTION_MODE)
            return self.json(result,status)
        if self.path=="/api/pools":
            name=str(body.get("name","")).strip(); domain=str(body.get("domain","worker")).strip()
            if not name: return self.json({"error":"nom requis"},400)
            pid=str(uuid.uuid4()); color=str(body.get("color","#b9f45c"))
            with db() as conn: conn.execute("INSERT INTO pools VALUES(?,?,?,?)",(pid,name,domain,color))
            return self.json({"id":pid,"name":name,"domain":domain,"color":color},201)
        if self.path.startswith("/api/gpus/") and self.path.endswith("/assign"):
            gpu_id=unquote(self.path[len("/api/gpus/"):-len("/assign")].rstrip("/"))
            pool_id=body.get("pool_id")
            with db() as conn:
                if pool_id and not conn.execute("SELECT 1 FROM pools WHERE id=?",(pool_id,)).fetchone(): return self.json({"error":"pool introuvable"},404)
                conn.execute("INSERT INTO gpu_assignments VALUES(?,?,?) ON CONFLICT(gpu_id) DO UPDATE SET pool_id=excluded.pool_id,updated_at=excluded.updated_at",(gpu_id,pool_id,stamp()))
            return self.json({"gpu_id":gpu_id,"pool_id":pool_id})
        if self.path=="/api/models":
            required=("name","role","backend","model_path","runtime_path")
            if any(not str(body.get(k,"")).strip() for k in required): return self.json({"error":"nom, rôle, backend et chemins requis"},400)
            mid=str(uuid.uuid4()); port=int(body.get("port",8001)); context=int(body.get("context_size",32768))
            with db() as conn: conn.execute("INSERT INTO models VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid,body["name"],body["role"],body["backend"],body["model_path"],body["runtime_path"],context,port,None,"STOPPED",None,None,None,stamp()))
            return self.json({"id":mid},201)
        if self.path=="/api/models/autoload":
            result,status=autoload_models(); return self.json(result,status)
        if self.path in ("/api/stack/start","/api/stack/stop","/api/stack/restart"):
            result,status=supervisor_call(self.path.rsplit("/",1)[-1],"POST"); return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/activate"):
            mid=self.path.split("/")[-2]; result,status=activate_model(mid,str(body.get("pool_id",body.get("role","workers"))))
            return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/stop"):
            return self.json(stop_model(self.path.split("/")[-2]))
        if self.path.startswith("/api/workflows/") and self.path.endswith("/run"):
            return self.json({"started":start_workflow(self.path.split("/")[-2])})
        return self.json({"error":"route inconnue"},404)


if __name__=="__main__":
    init_db(); server=ThreadingHTTPServer(("127.0.0.1",int(os.getenv("SKEIN_PORT","8787"))),Handler)
    print(f"Skein disponible sur http://127.0.0.1:{server.server_port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
