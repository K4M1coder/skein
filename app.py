from __future__ import annotations

import base64, csv, ctypes, io, json, mimetypes, os, re, shlex, shutil, sqlite3, subprocess, sys, tempfile, threading, time, uuid, zipfile, hashlib, hmac, secrets, smtplib, ssl
from collections import deque
from email.message import EmailMessage
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
          token TEXT PRIMARY KEY, user_id TEXT NOT NULL, expires_at REAL NOT NULL, created_at REAL NOT NULL,
          storage_id TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rbac_profiles(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL, system INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS rbac_permissions(
          id TEXT PRIMARY KEY, description TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS rbac_profile_permissions(
          profile_id TEXT NOT NULL, permission_id TEXT NOT NULL, PRIMARY KEY(profile_id,permission_id));
        CREATE TABLE IF NOT EXISTS user_profiles(
          user_id TEXT NOT NULL, profile_id TEXT NOT NULL, PRIMARY KEY(user_id,profile_id));
        CREATE TABLE IF NOT EXISTS email_verification_codes(
          id TEXT PRIMARY KEY, user_id TEXT NOT NULL, code_hash TEXT NOT NULL,
          expires_at REAL NOT NULL, used_at REAL, attempts INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS auth_rate_limits(
          id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL, subject TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS workflow_templates(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT NOT NULL,
          objective_template TEXT NOT NULL, tasks TEXT NOT NULL, tags TEXT NOT NULL,
          owner_id TEXT, shared INTEGER NOT NULL DEFAULT 0, system INTEGER NOT NULL DEFAULT 0,
          created_at REAL NOT NULL, updated_at REAL NOT NULL);
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
        if "session_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN session_id TEXT")
        if "template_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN template_id TEXT")
        if "planning_mode" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN planning_mode TEXT DEFAULT 'legacy'")
        user_columns={r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns: conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "verified_at" not in user_columns: conn.execute("ALTER TABLE users ADD COLUMN verified_at REAL")
        session_columns={r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        if "storage_id" not in session_columns: conn.execute("ALTER TABLE sessions ADD COLUMN storage_id TEXT")
        for session in conn.execute("SELECT token FROM sessions WHERE storage_id IS NULL OR storage_id='' ").fetchall():
            conn.execute("UPDATE sessions SET storage_id=? WHERE token=?",(str(uuid.uuid4()),session["token"]))
        conn.execute("UPDATE users SET verified_at=created_at WHERE verified_at IS NULL AND email IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('reasoner','Reasoner','reasoner','#78a7ff')")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('workers','Workers','worker','#ffb44c')")
        conn.execute("INSERT OR IGNORE INTO pools VALUES('retrieval','Retrieval','service','#b9f45c')")
        conn.execute("INSERT OR IGNORE INTO settings VALUES('users_can_choose_execution_mode','false')")
        if not conn.execute("SELECT 1 FROM users").fetchone():
            username=os.getenv("SKEIN_ADMIN_USER","admin"); password=os.getenv("SKEIN_ADMIN_PASSWORD","admin")
            created=stamp(); conn.execute("INSERT INTO users(id,username,password_hash,role,active,created_at,email,verified_at) VALUES(?,?,?,?,?,?,?,?)",(str(uuid.uuid4()),username,password_hash(password),"admin",1,created,None,created))
        permissions={
          "users.manage":"Create, update, activate, and assign profiles to users",
          "settings.manage":"Manage execution policy, GPU pools, and the application stack",
          "models.manage":"Register, load, stop, and assign models",
          "workflows.execute":"Create workflows and execute their artifacts or commands",
          "workflows.read_own":"Read workflows owned by the current user",
          "workflows.read_all":"Read all workflow reports and deliverables",
          "workflows.delete_own":"Delete workflows owned by the current user",
          "workflows.delete_all":"Delete workflows owned by any user",
          "workflow_templates.read":"Read system, shared, and owned workflow templates",
          "workflow_templates.manage_own":"Create, update, share, and delete owned workflow templates",
          "workflow_templates.manage_all":"Manage every non-system workflow template",
          "server_stats.read":"Read privacy-preserving server and inference statistics",
          "email.manage":"Configure and test the outbound SMTP server",
          "users.verify":"Manually approve pending user registrations",
        }
        for pid,description in permissions.items(): conn.execute("INSERT OR IGNORE INTO rbac_permissions VALUES(?,?)",(pid,description))
        profiles={
          "super_admin":("Super Administrator","Full access to all Skein capabilities",list(permissions)),
          "user_manager":("User Manager","Manage users, profile assignments, and account verification",["users.manage","users.verify"]),
          "settings_manager":("Settings Manager","Manage execution policy, GPU pools, stack controls, and SMTP",["settings.manage","email.manage"]),
          "model_manager":("Model Manager","Manage model registry and runtimes",["models.manage","server_stats.read"]),
          "workflow_operator":("Workflow Operator","Execute workflows and manage owned workflow templates",["workflows.execute","workflows.read_own","workflows.delete_own","workflow_templates.read","workflow_templates.manage_own"]),
          "stats_auditor":("Statistics Auditor","Read anonymized operational statistics only",["server_stats.read"]),
        }
        for profile_id,(name,description,grants) in profiles.items():
            conn.execute("INSERT OR IGNORE INTO rbac_profiles VALUES(?,?,?,1)",(profile_id,name,description))
            for permission in grants: conn.execute("INSERT OR IGNORE INTO rbac_profile_permissions VALUES(?,?)",(profile_id,permission))
        for user in conn.execute("SELECT id,role FROM users").fetchall():
            if not conn.execute("SELECT 1 FROM user_profiles WHERE user_id=?",(user["id"],)).fetchone():
                conn.execute("INSERT OR IGNORE INTO user_profiles VALUES(?,?)",(user["id"],"super_admin" if user["role"]=="admin" else "workflow_operator"))
        seed_default_workflow_templates(conn)
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


def access_for_user(user_id):
    with db() as conn:
        profiles=[dict(r) for r in conn.execute("SELECT p.id,p.name,p.description FROM rbac_profiles p JOIN user_profiles up ON up.profile_id=p.id WHERE up.user_id=? ORDER BY p.name",(user_id,)).fetchall()]
        permissions=[r[0] for r in conn.execute("SELECT DISTINCT pp.permission_id FROM rbac_profile_permissions pp JOIN user_profiles up ON up.profile_id=pp.profile_id WHERE up.user_id=? ORDER BY pp.permission_id",(user_id,)).fetchall()]
    return profiles,permissions


def protect_secret(value):
    if not value: return ""
    if os.name!="nt": return "env:SKEIN_SMTP_PASSWORD"
    class Blob(ctypes.Structure): _fields_=[("size",ctypes.c_ulong),("data",ctypes.POINTER(ctypes.c_byte))]
    raw=value.encode("utf-8"); buffer=ctypes.create_string_buffer(raw); source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_byte))); target=Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source),"Skein SMTP",None,None,None,0,ctypes.byref(target)):
        raise RuntimeError("Windows DPAPI could not protect the SMTP password")
    try: encrypted=ctypes.string_at(target.data,target.size)
    finally: ctypes.windll.kernel32.LocalFree(target.data)
    return "dpapi:"+base64.b64encode(encrypted).decode("ascii")


def unprotect_secret(value):
    if not value: return os.getenv("SKEIN_SMTP_PASSWORD","")
    if value.startswith("env:"): return os.getenv(value[4:],"")
    if not value.startswith("dpapi:") or os.name!="nt": return ""
    class Blob(ctypes.Structure): _fields_=[("size",ctypes.c_ulong),("data",ctypes.POINTER(ctypes.c_byte))]
    raw=base64.b64decode(value[6:]); buffer=ctypes.create_string_buffer(raw); source=Blob(len(raw),ctypes.cast(buffer,ctypes.POINTER(ctypes.c_byte))); target=Blob()
    if not ctypes.windll.crypt32.CryptUnprotectData(ctypes.byref(source),None,None,None,None,0,ctypes.byref(target)):
        raise RuntimeError("Windows DPAPI could not unlock the SMTP password")
    try: decrypted=ctypes.string_at(target.data,target.size)
    finally: ctypes.windll.kernel32.LocalFree(target.data)
    return decrypted.decode("utf-8")


def smtp_configuration(include_password=False):
    keys=("smtp_host","smtp_port","smtp_username","smtp_password","smtp_from","smtp_security")
    with db() as conn: values={r["key"]:r["value"] for r in conn.execute("SELECT key,value FROM settings WHERE key IN (%s)"%",".join("?"*len(keys)),keys)}
    config={"host":values.get("smtp_host",""),"port":int(values.get("smtp_port","587")),"username":values.get("smtp_username",""),
      "from_address":values.get("smtp_from",""),"security":values.get("smtp_security","starttls"),"configured":bool(values.get("smtp_host") and values.get("smtp_from"))}
    if include_password: config["password"]=unprotect_secret(values.get("smtp_password",""))
    return config


def send_email(recipient,subject,text_body):
    config=smtp_configuration(True)
    if not config["configured"]: raise RuntimeError("SMTP server is not configured")
    message=EmailMessage(); message["From"]=config["from_address"]; message["To"]=recipient; message["Subject"]=subject; message.set_content(text_body)
    context=ssl.create_default_context()
    if config["security"]=="ssl": server=smtplib.SMTP_SSL(config["host"],config["port"],timeout=15,context=context)
    else:
        server=smtplib.SMTP(config["host"],config["port"],timeout=15)
        if config["security"]=="starttls": server.starttls(context=context)
    try:
        if config["username"]: server.login(config["username"],config["password"])
        server.send_message(message)
    finally: server.quit()


def issue_verification_code(user_id,language="en",force=False):
    now=stamp()
    with db() as conn:
        user=conn.execute("SELECT id,username,email,verified_at,active FROM users WHERE id=?",(user_id,)).fetchone()
        latest=conn.execute("SELECT created_at FROM email_verification_codes WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
    if not user or not user["active"]: return {"error":"Account not found or inactive"},404
    if user["verified_at"]: return {"error":"Account is already verified"},409
    if not user["email"]: return {"error":"No email address is associated with this account"},400
    if latest and not force and now-latest["created_at"]<60: return {"error":"Please wait before requesting another code","retry_after_seconds":round(60-(now-latest["created_at"]))},429
    code=f"{secrets.randbelow(1_000_000):06d}"; code_id=str(uuid.uuid4())
    with db() as conn:
        conn.execute("UPDATE email_verification_codes SET used_at=? WHERE user_id=? AND used_at IS NULL",(now,user_id))
        conn.execute("INSERT INTO email_verification_codes VALUES(?,?,?,?,?,?,?)",(code_id,user_id,password_hash(code),now+600,None,0,now))
    french=str(language).lower().startswith("fr")
    subject="Votre code de vérification Skein" if french else "Your Skein verification code"
    body=(f"Bonjour {user['username']},\n\nVotre code Skein est : {code}\n\nIl expire dans 10 minutes et ne peut être utilisé qu'une seule fois."
      if french else f"Hello {user['username']},\n\nYour Skein code is: {code}\n\nIt expires in 10 minutes and can be used only once.")
    try: send_email(user["email"],subject,body)
    except Exception as exc: return {"error":"Verification email could not be sent","details":str(exc),"registration_pending":True},503
    return {"sent":True,"expires_in_seconds":600,"resend_after_seconds":60,"email_hint":user["email"][:2]+"***@"+user["email"].split("@")[-1]},200


def verify_user_code(user_id,code):
    now=stamp()
    with db() as conn: rows=conn.execute("SELECT * FROM email_verification_codes WHERE user_id=? AND used_at IS NULL ORDER BY created_at DESC",(user_id,)).fetchall()
    if not rows: return {"error":"No active verification code"},400
    current=rows[0]
    if current["expires_at"]<now:
        with db() as conn: conn.execute("UPDATE email_verification_codes SET used_at=? WHERE id=?",(now,current["id"]))
        return {"error":"Verification code expired"},410
    if current["attempts"]>=5: return {"error":"Too many invalid attempts; request a new code"},429
    if not password_valid(str(code).strip(),current["code_hash"]):
        with db() as conn: conn.execute("UPDATE email_verification_codes SET attempts=attempts+1 WHERE id=?",(current["id"],))
        return {"error":"Invalid verification code","attempts_remaining":max(0,4-current["attempts"])},400
    with db() as conn:
        changed=conn.execute("UPDATE email_verification_codes SET used_at=? WHERE id=? AND used_at IS NULL",(now,current["id"])).rowcount
        if changed!=1: return {"error":"Verification code was already used"},409
        conn.execute("UPDATE users SET verified_at=? WHERE id=? AND verified_at IS NULL",(now,user_id))
    return {"verified":True},200


def consume_rate_limit(action,subject,limit,window_seconds):
    now=stamp()
    with db() as conn:
        conn.execute("DELETE FROM auth_rate_limits WHERE created_at<?",(now-max(window_seconds,86400),))
        count=conn.execute("SELECT COUNT(*) FROM auth_rate_limits WHERE action=? AND subject=? AND created_at>?",(action,subject,now-window_seconds)).fetchone()[0]
        if count>=limit: return False
        conn.execute("INSERT INTO auth_rate_limits(action,subject,created_at) VALUES(?,?,?)",(action,subject,now))
    return True


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


WORKFLOW_ROLES = {
    "architect", "analyst", "coder", "executor", "integrator", "researcher",
    "reviewer", "security-reviewer", "tester", "translator",
}


def task_specs_to_template(specs):
    keys = [f"step_{index + 1}" for index in range(len(specs))]
    return [
        {
            "key": keys[index],
            "title": title,
            "role": role,
            "dependencies": [keys[dependency] for dependency in dependencies],
            "complexity": complexity,
            "risk": risk,
            "criticality": criticality,
        }
        for index, (title, role, dependencies, complexity, risk, criticality) in enumerate(specs)
    ]


def validate_workflow_tasks(tasks):
    errors = []
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= 12:
        return {"valid": False, "errors": ["A workflow must contain between 1 and 12 tasks"]}
    keys = []
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            errors.append(f"Task {index + 1} must be an object")
            continue
        key = str(task.get("key", "")).strip()
        title = str(task.get("title", "")).strip()
        role = str(task.get("role", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", key): errors.append(f"Task {index + 1} has an invalid key")
        if not 3 <= len(title) <= 200: errors.append(f"Task {index + 1} title must contain 3 to 200 characters")
        if role not in WORKFLOW_ROLES: errors.append(f"Task {index + 1} has an unsupported role")
        for field in ("complexity", "risk", "criticality"):
            try: value = float(task.get(field, 0.5))
            except (TypeError, ValueError): errors.append(f"Task {index + 1} {field} must be a number"); continue
            if not 0 <= value <= 1: errors.append(f"Task {index + 1} {field} must be between 0 and 1")
        keys.append(key)
    if len(set(keys)) != len(keys): errors.append("Task keys must be unique")
    key_set = set(keys); edges = {key: [] for key in keys}; indegree = {key: 0 for key in keys}; referenced = set()
    for index, task in enumerate(tasks):
        if not isinstance(task, dict): continue
        dependencies = task.get("dependencies", [])
        if not isinstance(dependencies, list): errors.append(f"Task {index + 1} dependencies must be a list"); continue
        if len(set(dependencies)) != len(dependencies): errors.append(f"Task {index + 1} dependencies must be unique")
        for dependency in dependencies:
            if dependency not in key_set: errors.append(f"Task {index + 1} references unknown dependency {dependency}"); continue
            if dependency == task.get("key"): errors.append(f"Task {index + 1} cannot depend on itself"); continue
            edges[dependency].append(task.get("key")); indegree[task.get("key")] += 1; referenced.add(dependency)
    queue = deque(key for key, degree in indegree.items() if degree == 0); visited = []
    while queue:
        key = queue.popleft(); visited.append(key)
        for dependent in edges.get(key, []):
            indegree[dependent] -= 1
            if indegree[dependent] == 0: queue.append(dependent)
    if len(visited) != len(keys): errors.append("Task dependencies must form an acyclic graph")
    terminals = [key for key in keys if key not in referenced]
    if len(terminals) != 1: errors.append("A workflow must have exactly one terminal task")
    elif tasks[keys.index(terminals[0])].get("role") != "integrator": errors.append("The terminal task must use the integrator role")
    return {"valid": not errors, "errors": errors, "task_count": len(tasks), "terminal_task": terminals[0] if len(terminals) == 1 else None}


DEFAULT_WORKFLOW_TEMPLATES = (
    ("default-general", "General delivery", "Execute, verify, and deliver a general request", "Complete the user request", "general,answer,analysis", plan_for("general request")),
    ("default-software", "Software implementation", "Design, implement, test, and deliver usable software", "Build the requested software", "code,software,python,javascript,api,app", plan_for("build a software application with code and tests")),
    ("default-translation", "Translation and review", "Translate, review terminology, and deliver the final text", "Translate the requested content", "translate,translation,language,localization", plan_for("translate this content")),
    ("default-security", "Security-sensitive change", "Design, implement, test, and review a security-sensitive change", "Implement the requested security change", "security,oauth,authentication,secrets,permissions", plan_for("build a secure OAuth API")),
    ("default-chat", "Simple chat", "Answer a conversational question directly without unnecessary orchestration", "Answer the user clearly and concisely", "chat,conversation,question,explain,brainstorm", [
      ("Answer the conversation directly", "integrator", [], .20, .10, .35)]),
    ("default-daily-assistance", "Daily assistance", "Help with everyday planning, writing, organization, and practical decisions", "Provide practical daily assistance", "daily,assistant,planning,writing,organize,email,decision", [
      ("Understand the practical request and constraints", "analyst", [], .28, .15, .45),
      ("Prepare a useful and actionable response", "executor", [0], .35, .18, .55),
      ("Deliver the concise final assistance", "integrator", [0,1], .25, .12, .65)]),
    ("default-research", "Research and synthesis", "Investigate a question, compare reliable evidence, and produce a sourced synthesis", "Research and synthesize the requested topic", "research,compare,sources,evidence,investigate,analysis", [
      ("Define the research questions and evidence criteria", "analyst", [], .42, .25, .65),
      ("Collect and organize relevant evidence", "researcher", [0], .62, .32, .72),
      ("Review contradictions, limitations, and source quality", "reviewer", [0,1], .58, .38, .78),
      ("Deliver the sourced research synthesis", "integrator", [0,1,2], .52, .28, .85)]),
    ("default-code-specification", "Specification from code", "Inspect an existing codebase and derive accurate functional and technical specifications", "Produce specifications grounded in the supplied codebase", "specification,spec,codebase,architecture,requirements,reverse-engineer", [
      ("Map the codebase structure and entry points", "researcher", [], .58, .28, .72),
      ("Trace behavior, contracts, data flows, and dependencies", "analyst", [0], .72, .38, .82),
      ("Draft functional and technical specifications from evidence", "architect", [0,1], .68, .35, .86),
      ("Review specifications against the code and identify unknowns", "reviewer", [0,1,2], .62, .32, .88),
      ("Deliver the verified specification", "integrator", [0,1,2,3], .55, .25, .92)]),
)


def seed_default_workflow_templates(conn):
    now = stamp()
    for template_id, name, description, objective_template, tags, specs in DEFAULT_WORKFLOW_TEMPLATES:
        tasks = task_specs_to_template(specs)
        validation = validate_workflow_tasks(tasks)
        if not validation["valid"]: raise RuntimeError(f"Invalid default workflow template {template_id}: {validation['errors']}")
        conn.execute("""INSERT OR IGNORE INTO workflow_templates
          (id,name,description,objective_template,tasks,tags,owner_id,shared,system,created_at,updated_at)
          VALUES(?,?,?,?,?,?,NULL,1,1,?,?)""",
          (template_id, name, description, objective_template, json.dumps(tasks), tags, now, now))


def workflow_template_payload(row, user_id=None, manage_all=False):
    item = dict(row); item["tasks"] = json.loads(item["tasks"]); item["tags"] = [tag for tag in item["tags"].split(",") if tag]
    item["shared"] = bool(item["shared"]); item["system"] = bool(item["system"])
    owned = bool(user_id and item["owner_id"] == user_id)
    item["permissions"] = {"edit": not item["system"] and (owned or manage_all), "delete": not item["system"] and (owned or manage_all), "share": not item["system"] and (owned or manage_all)}
    item["validation"] = validate_workflow_tasks(item["tasks"])
    return item


def visible_workflow_templates(user_id, manage_all=False):
    with db() as conn:
        rows = conn.execute("SELECT * FROM workflow_templates ORDER BY system DESC,name" if manage_all else "SELECT * FROM workflow_templates WHERE system=1 OR shared=1 OR owner_id=? ORDER BY system DESC,name", () if manage_all else (user_id,)).fetchall()
    return [workflow_template_payload(row, user_id, manage_all) for row in rows]


def normalize_workflow_template(body):
    name = str(body.get("name", "")).strip()
    description = str(body.get("description", "")).strip()
    objective_template = str(body.get("objective_template", "")).strip()
    if not 3 <= len(name) <= 80: raise ValueError("Template name must contain 3 to 80 characters")
    if len(description) > 500: raise ValueError("Template description must not exceed 500 characters")
    if not 3 <= len(objective_template) <= 500: raise ValueError("Objective template must contain 3 to 500 characters")
    tasks = body.get("tasks")
    validation = validate_workflow_tasks(tasks)
    if not validation["valid"]: raise ValueError("; ".join(validation["errors"]))
    raw_tags = body.get("tags", [])
    if isinstance(raw_tags, str): raw_tags = raw_tags.split(",")
    if not isinstance(raw_tags, list): raise ValueError("Tags must be a list or comma-separated string")
    tags = []
    for raw_tag in raw_tags:
        tag = re.sub(r"[^a-z0-9_-]", "", str(raw_tag).strip().lower())[:32]
        if tag and tag not in tags: tags.append(tag)
    return {"name":name,"description":description,"objective_template":objective_template,"tasks":tasks,"tags":tags[:20],"shared":bool(body.get("shared")),"validation":validation}


def objective_tokens(text):
    stop_words = {"about","avec","dans","des","for","from","les","pour","that","the","this","une","with"}
    return {token for token in re.findall(r"[a-z0-9_-]{3,}", str(text).lower()) if token not in stop_words}


def score_workflow_template(objective,user_id,manage_all=False):
    objective_words = objective_tokens(objective)
    templates = visible_workflow_templates(user_id,manage_all)
    best = None
    for template in templates:
        tags = set(template["tags"]); names = objective_tokens(template["name"]); description = objective_tokens(template["description"]+" "+template["objective_template"])
        score = 5*len(objective_words & tags)+3*len(objective_words & names)+len(objective_words & description)
        if template["id"] == "default-general": score += .1
        candidate = (score,1 if template["owner_id"]==user_id else 0,template["updated_at"],template)
        if best is None or candidate[:3] > best[:3]: best = candidate
    if not best: return None
    selected = best[3]; selected["selection"]={"score":best[0],"matched_terms":sorted(objective_words & (set(selected["tags"])|objective_tokens(selected["name"]+" "+selected["description"]+" "+selected["objective_template"])))[:12]}
    return selected


def select_workflow_template(objective,user_id,manage_all=False):
    templates=visible_workflow_templates(user_id,manage_all)
    if not templates: return None
    decision=ModelClient("reasoner-large").select_workflow_template(objective,templates)
    if decision.get("error"): return {"error":decision["error"],"action":decision.get("action")}
    selected=next((template for template in templates if template["id"]==decision.get("template_id")),None)
    if not selected: return {"error":"The reasoner selected an unavailable workflow template"}
    selected["selection"]={"method":"llm" if decision.get("mode")=="live" else "deterministic-test","reason":str(decision.get("reason") or ""),"confidence":max(0,min(1,float(decision.get("confidence",0))))}
    return selected


def generate_validated_workflow_template(objective):
    if not 5 <= len(objective.strip()) <= 4000: return {"error":"Objective must contain 5 to 4000 characters"},400
    proposal = ModelClient("reasoner-large").generate_workflow_template(objective)
    if proposal.get("error"): return proposal,409 if "No active endpoint" in proposal["error"] else 502
    try: normalized = normalize_workflow_template(proposal)
    except ValueError as first_error:
        repaired = ModelClient("reasoner-large").generate_workflow_template(objective,True)
        if repaired.get("error"): return repaired,502
        try: normalized = normalize_workflow_template(repaired)
        except ValueError as second_error: return {"error":"Generated workflow failed validation","details":str(second_error),"first_validation_error":str(first_error)},422
        proposal = repaired
    return {**normalized,"mode":proposal.get("mode","live"),"metrics":proposal.get("metrics",{})},200


def template_task_specs(tasks):
    validation = validate_workflow_tasks(tasks)
    if not validation["valid"]: raise ValueError("; ".join(validation["errors"]))
    positions = {task["key"]: index for index, task in enumerate(tasks)}
    return [(task["title"], task["role"], [positions[key] for key in task["dependencies"]], float(task.get("complexity", .5)), float(task.get("risk", .5)), float(task.get("criticality", .5))) for task in tasks]


def create_workflow(objective,owner_id=None,session_id=None,specs=None,template_id=None,planning_mode="automatic"):
    wid, created = str(uuid.uuid4()), stamp()
    specs = specs or plan_for(objective)
    ids = [str(uuid.uuid4()) for _ in specs]
    with db() as conn:
        conn.execute("INSERT INTO workflows(id,objective,status,created_at,updated_at,owner_id,session_id,template_id,planning_mode) VALUES(?,?,?,?,?,?,?,?,?)", (wid, objective, "READY", created, created,owner_id,session_id,template_id,planning_mode))
        for pos, spec in enumerate(specs):
            title, role, deps, complexity, risk, criticality = spec
            conn.execute("""INSERT INTO tasks(id,workflow_id,position,title,role,dependencies,
              complexity,risk,criticality,status) VALUES(?,?,?,?,?,?,?,?,?,?)""",
               (ids[pos], wid, pos, title, role, json.dumps([ids[i] for i in deps]),
               complexity, risk, criticality, "READY"))
    workflow_storage_root(wid,owner_id,session_id).mkdir(parents=True,exist_ok=True)
    emit(wid, "workflow.created", {"objective": objective, "tasks": len(specs), "template_id": template_id, "planning_mode": planning_mode})
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

    def select_workflow_template(self, objective, templates, retry=False):
        if not self.url:
            if os.getenv("SKEIN_ALLOW_SIMULATION", "0") == "1":
                selected=score_workflow_template(objective,None,True)
                return {"template_id":selected["id"],"reason":"Deterministic test-mode routing","confidence":1.0,"mode":"simulation"}
            return {"error":"No active endpoint for reasoner-large","action":"Load a reasoner model in Model Plane."}
        candidates=[{"id":item["id"],"name":item["name"],"description":item["description"],"objective_template":item["objective_template"],"tags":item["tags"],"task_roles":[task["role"] for task in item["tasks"]]} for item in templates]
        repair="The previous selection was invalid. Return corrected JSON only.\n" if retry else ""
        prompt=("/no_think\n"+repair+"Choose exactly one available workflow template for the user request. "
          "Always select the smallest specialized workflow that fully handles the request; General delivery is only a fallback when no specialized workflow applies. "
          "A direct question, explanation, brainstorming request, or ordinary conversation MUST select default-chat. Practical everyday help, planning, writing, email, or organization MUST select default-daily-assistance. "
          "A request requiring external facts, source comparison, or evidence gathering MUST select default-research. Requirements or documentation derived from an existing codebase MUST select default-code-specification. "
          "Return only JSON with template_id, reason, and confidence from 0 to 1. The template_id must exactly match one candidate.\n"
          "USER REQUEST:\n"+objective+"\nAVAILABLE WORKFLOWS:\n"+json.dumps(candidates,ensure_ascii=False))
        body=json.dumps({"model":self.model,"messages":[{"role":"user","content":prompt}],"temperature":0,"max_tokens":512,"response_format":{"type":"json_object"},"chat_template_kwargs":{"enable_thinking":False}}).encode()
        try:
            with urlopen(Request(self.url,body,{"Content-Type":"application/json"}),timeout=120) as response:
                payload=json.load(response); content=payload["choices"][0]["message"]["content"].strip()
            if content.startswith("```"): content=content.split("\n",1)[1].rsplit("```",1)[0]
            decision=json.loads(content)
            if decision.get("template_id") not in {item["id"] for item in templates}:
                if not retry: return self.select_workflow_template(objective,templates,True)
                return {"error":"The reasoner did not select an available workflow template"}
            decision["mode"]="live"; return decision
        except (URLError,TimeoutError,KeyError,json.JSONDecodeError,TypeError,ValueError) as exc:
            if not retry: return self.select_workflow_template(objective,templates,True)
            return {"error":f"Workflow selection failed: {exc}","action":"Check the reasoner endpoint and retry."}

    def generate_workflow_template(self, objective, retry=False):
        if not self.url:
            if os.getenv("SKEIN_ALLOW_SIMULATION", "0") == "1":
                tasks = task_specs_to_template(plan_for(objective))
                return {"name":"Generated workflow","description":"Generated and validated for the supplied objective","objective_template":objective,"tags":sorted(objective_tokens(objective))[:8],"tasks":tasks,"mode":"simulation","metrics":{}}
            return {"error":"No active endpoint for reasoner-large","action":"Load a reasoner model in Model Plane."}
        repair = "The previous proposal was invalid. Return corrected JSON only.\n" if retry else ""
        prompt = ("/no_think\n"+repair+"You are the Skein workflow architect. Design a directly executable task DAG for the user objective. "
          "Return only one JSON object with name, description, objective_template, tags, and tasks. "
          "tasks contains 1 to 12 objects with key, title, role, dependencies, complexity, risk, and criticality. "
          "Use stable lowercase keys; dependencies reference earlier task keys. Scores are numbers from 0 to 1. "
          "Allowed roles: "+", ".join(sorted(WORKFLOW_ROLES))+". The graph must be acyclic, have exactly one terminal task, "
          "and that terminal task must use the integrator role. Include implementation and verification tasks when required.\n"
          "USER OBJECTIVE:\n"+objective)
        body=json.dumps({"model":self.model,"messages":[{"role":"user","content":prompt}],"temperature":0 if retry else .1,"max_tokens":4096,"response_format":{"type":"json_object"},"chat_template_kwargs":{"enable_thinking":False}}).encode()
        started=time.perf_counter()
        try:
            with urlopen(Request(self.url,body,{"Content-Type":"application/json"}),timeout=120) as response:
                payload=json.load(response); content=payload["choices"][0]["message"]["content"].strip()
            if content.startswith("```"): content=content.split("\n",1)[1].rsplit("```",1)[0]
            proposal=json.loads(content); proposal["mode"]="live"
            usage=payload.get("usage") or {}; duration=max(.001,time.perf_counter()-started)
            proposal["metrics"]={"prompt_tokens":int(usage.get("prompt_tokens") or 0),"completion_tokens":int(usage.get("completion_tokens") or 0),"total_tokens":int(usage.get("total_tokens") or 0),"inference_seconds":round(duration,3)}
            return proposal
        except (URLError,TimeoutError,KeyError,json.JSONDecodeError) as exc:
            if not retry: return self.generate_workflow_template(objective,True)
            return {"error":f"Workflow generation backend unavailable: {exc}","action":"Check the reasoner runtime and retry."}


POOL = ThreadPoolExecutor(max_workers=max(1,int(os.getenv("SKEIN_TASK_WORKERS","4"))), thread_name_prefix="skein-worker")
MAX_PARALLEL_WORKFLOWS=max(1,int(os.getenv("SKEIN_MAX_PARALLEL_WORKFLOWS","2")))
ACTIVE, WORKFLOW_QUEUE, ACTIVE_LOCK = set(), deque(), threading.Lock()


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


def workflow_storage_root(wid,owner_id=None,session_id=None):
    if owner_id is None or session_id is None:
        with db() as conn: row=conn.execute("SELECT owner_id,session_id FROM workflows WHERE id=?",(wid,)).fetchone()
        owner_id=owner_id or (row["owner_id"] if row else None); session_id=session_id or (row["session_id"] if row else None)
    owner_folder=owner_id or "system"; session_folder=session_id or "legacy"
    return DB_PATH.parent/"users"/owner_folder/"sessions"/session_folder/"workflows"/wid


def artifact_root(wid):
    root=workflow_storage_root(wid)/"artifacts"; root.mkdir(parents=True,exist_ok=True); return root


def delete_workflow_history(user_id,delete_all=False):
    with db() as conn:
        rows=conn.execute("SELECT id,owner_id,session_id FROM workflows" if delete_all else "SELECT id,owner_id,session_id FROM workflows WHERE owner_id=?",() if delete_all else (user_id,)).fetchall()
    workflow_ids=[row["id"] for row in rows]
    with ACTIVE_LOCK:
        running=[wid for wid in workflow_ids if wid in ACTIVE]
        queued=[wid for wid in workflow_ids if wid in WORKFLOW_QUEUE]
    if running or queued:
        return {"error":"Workflow history cannot be deleted while matching workflows are active or queued","running_workflow_ids":running,"queued_workflow_ids":queued},409
    if not workflow_ids: return {"deleted_workflows":0,"deleted_artifacts":0,"scope":"all" if delete_all else "own","warnings":[]},200
    storage_by_id={row["id"]:workflow_storage_root(row["id"],row["owner_id"],row["session_id"]) for row in rows}
    placeholders=",".join("?" for _ in workflow_ids)
    with db() as conn:
        artifact_count=conn.execute(f"SELECT COUNT(*) FROM artifacts WHERE workflow_id IN ({placeholders})",workflow_ids).fetchone()[0]
        conn.execute(f"DELETE FROM executions WHERE workflow_id IN ({placeholders})",workflow_ids)
        conn.execute(f"DELETE FROM artifacts WHERE workflow_id IN ({placeholders})",workflow_ids)
        conn.execute(f"DELETE FROM events WHERE workflow_id IN ({placeholders})",workflow_ids)
        conn.execute(f"DELETE FROM tasks WHERE workflow_id IN ({placeholders})",workflow_ids)
        conn.execute(f"DELETE FROM workflows WHERE id IN ({placeholders})",workflow_ids)
    warnings=[]; users_root=(DB_PATH.parent/"users").resolve()
    for wid in workflow_ids:
        workflow_path=storage_by_id[wid].resolve()
        if users_root not in workflow_path.parents:
            warnings.append(f"Skipped unsafe workflow path for {wid}"); continue
        try: shutil.rmtree(workflow_path) if workflow_path.exists() else None
        except OSError as exc: warnings.append(f"Could not remove files for {wid}: {exc}")
    return {"deleted_workflows":len(workflow_ids),"deleted_artifacts":artifact_count,"scope":"all" if delete_all else "own","warnings":warnings},200


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
        dispatch_workflows()


def dispatch_workflows():
    launches=[]
    with ACTIVE_LOCK:
        while WORKFLOW_QUEUE and len(ACTIVE)<MAX_PARALLEL_WORKFLOWS:
            wid=WORKFLOW_QUEUE.popleft(); ACTIVE.add(wid); launches.append(wid)
    for wid in launches: threading.Thread(target=orchestrate,args=(wid,),daemon=True).start()


def start_workflow(wid):
    with ACTIVE_LOCK:
        if wid in ACTIVE or wid in WORKFLOW_QUEUE: return False
        WORKFLOW_QUEUE.append(wid); position=len(WORKFLOW_QUEUE)
    with db() as conn: conn.execute("UPDATE workflows SET status='QUEUED',updated_at=? WHERE id=?",(stamp(),wid))
    emit(wid,"workflow.queued",{"position":position,"parallel_limit":MAX_PARALLEL_WORKFLOWS})
    dispatch_workflows(); return True


def recover_pending_workflows():
    with db() as conn:
        rows=conn.execute("SELECT id FROM workflows WHERE status IN ('READY','QUEUED','RUNNING') ORDER BY created_at").fetchall()
        conn.execute("UPDATE tasks SET status='READY' WHERE status='RUNNING'")
    for row in rows: start_workflow(row["id"])
    return len(rows)


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
    workflow=dict(wf)
    with ACTIVE_LOCK:
        workflow["queue_position"]=(list(WORKFLOW_QUEUE).index(wid)+1) if wid in WORKFLOW_QUEUE else None
        workflow["parallel_limit"]=MAX_PARALLEL_WORKFLOWS
    return {"workflow":workflow,"tasks":out,"events":ev,"final_output":final,"artifacts":artifacts,"summary":summary,
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


def privacy_safe_server_stats(limit=200):
    """Return operational metadata only; never expose prompts, objectives, outputs, artifacts, or usernames."""
    with db() as conn:
        rows=conn.execute("SELECT t.workflow_id,t.position,t.role,t.model,t.status,t.started_at,t.finished_at,t.result FROM tasks t WHERE t.started_at IS NOT NULL ORDER BY t.started_at DESC LIMIT ?",(max(1,min(limit,1000)),)).fetchall()
    requests=[]
    for row in rows:
        result=json.loads(row["result"]) if row["result"] else {}; metrics=result.get("metrics",{})
        requests.append({
          "request_ref":hashlib.sha256(row["workflow_id"].encode()).hexdigest()[:12],
          "step":row["position"]+1,"role":row["role"],"model":row["model"],"status":row["status"],
          "started_at":row["started_at"],"finished_at":row["finished_at"],
          "prompt_tokens":int(metrics.get("prompt_tokens") or 0),"completion_tokens":int(metrics.get("completion_tokens") or 0),
          "total_tokens":int(metrics.get("total_tokens") or 0),"tokens_per_second":float(metrics.get("tokens_per_second") or 0),
          "duration_seconds":float(metrics.get("execution_seconds") or 0),"average_power_w":float(metrics.get("average_power_w") or 0),
          "peak_power_w":float(metrics.get("peak_power_w") or 0),"energy_wh":float(metrics.get("energy_wh") or 0),
        })
    total_duration=sum(r["duration_seconds"] for r in requests); total_completion=sum(r["completion_tokens"] for r in requests)
    return {"privacy":{"content_excluded":True,"excluded_fields":["objective","prompt","result","deliverable","artifacts","username","user_id","email"],"request_reference":"SHA-256 workflow identifier truncated to 12 characters"},
      "summary":{"request_steps":len(requests),"total_tokens":sum(r["total_tokens"] for r in requests),"total_duration_seconds":round(total_duration,3),
        "average_tokens_per_second":round(total_completion/total_duration,2) if total_duration else 0,"energy_wh":round(sum(r["energy_wh"] for r in requests),4),
        "average_power_w":round(sum(r["average_power_w"]*r["duration_seconds"] for r in requests)/total_duration,2) if total_duration else 0},
      "requests":requests}


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
        if os.getenv("SKEIN_AUTH_DISABLED","0")=="1": return {"id":"test-admin","session_id":"test-session","username":"test-admin","role":"admin","active":1,"profiles":[{"id":"super_admin","name":"Super Administrator"}],"permissions":["users.manage","settings.manage","models.manage","workflows.execute","workflows.read_own","workflows.read_all","workflows.delete_own","workflows.delete_all","workflow_templates.read","workflow_templates.manage_own","workflow_templates.manage_all","server_stats.read"]}
        token=self.cookie("skein_session")
        if not token: return None
        with db() as conn:
            row=conn.execute("SELECT u.id,u.username,u.email,u.verified_at,u.role,u.active,s.storage_id AS session_id FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires_at>?",(token,stamp())).fetchone()
        if not row or not row["active"]: return None
        user=dict(row); user["verified"]=bool(user.pop("verified_at")); user["profiles"],grants=access_for_user(user["id"]); user["permissions"]=grants if user["verified"] else []; return user
    def deny(self,status=401,message="Authentication required"):
        return self.json({"error":message,"action":"Sign in with an authorized account."},status)
    def authorize(self,permission=None):
        user=self.current_user()
        if not user: self.deny(); return None
        if not user.get("verified",True): self.deny(403,"Email verification required"); return None
        if permission and permission not in user["permissions"]: self.deny(403,f"Permission required: {permission}"); return None
        return user
    def authorize_any(self,*permissions):
        user=self.current_user()
        if not user: self.deny(); return None
        if not user.get("verified",True): self.deny(403,"Email verification required"); return None
        if permissions and not any(permission in user["permissions"] for permission in permissions):
            self.deny(403,"One of these permissions is required: "+", ".join(permissions)); return None
        return user
    def workflow_allowed(self,user,wid):
        if "workflows.read_all" in user["permissions"]: return True
        with db() as conn: row=conn.execute("SELECT owner_id FROM workflows WHERE id=?",(wid,)).fetchone()
        return bool(row and row["owner_id"]==user["id"])
    def template_row(self,template_id):
        with db() as conn: return conn.execute("SELECT * FROM workflow_templates WHERE id=?",(template_id,)).fetchone()
    def template_visible(self,user,row):
        return bool(row and (row["system"] or row["shared"] or row["owner_id"]==user["id"] or "workflow_templates.manage_all" in user["permissions"]))
    def template_editable(self,user,row):
        return bool(row and not row["system"] and (row["owner_id"]==user["id"] and "workflow_templates.manage_own" in user["permissions"] or "workflow_templates.manage_all" in user["permissions"]))
    def send_session(self,user,token):
        raw=json.dumps({"user":user,"policy":{"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")}},ensure_ascii=False).encode()
        self.send_response(200); self.send_header("Content-Type","application/json; charset=utf-8")
        self.send_header("Set-Cookie",f"skein_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200")
        self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path=="/api/health": return self.json({"status":"ok","active":len(ACTIVE),"queued":len(WORKFLOW_QUEUE),"parallel_workflow_limit":MAX_PARALLEL_WORKFLOWS,"database":str(DB_PATH),"execution_mode":EXECUTION_MODE,"active_models":ACTIVE_ENDPOINTS,"backends":{"worker":bool(os.getenv("SKEIN_WORKER_URL") or ACTIVE_ENDPOINTS.get("worker")),"reasoner":bool(os.getenv("SKEIN_REASONER_URL") or ACTIVE_ENDPOINTS.get("reasoner"))}})
        if self.path=="/api/auth/me":
            user=self.current_user()
            if not user: return self.deny()
            return self.json({"user":user,"policy":{"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")}})
        if self.path.startswith("/api/"):
            if self.path in ("/api/users","/api/rbac/profiles"): user=self.authorize("users.manage")
            elif self.path=="/api/admin/email": user=self.authorize("email.manage")
            elif self.path=="/api/admin/settings": user=self.authorize("settings.manage")
            elif self.path=="/api/models": user=self.authorize("models.manage")
            elif self.path.startswith("/api/workflow-templates"): user=self.authorize("workflow_templates.read")
            elif self.path=="/api/hardware": user=self.authorize_any("server_stats.read","settings.manage","models.manage")
            elif self.path=="/api/server-stats": user=self.authorize("server_stats.read")
            else: user=self.authorize()
            if not user: return
            if self.path.startswith("/api/workflows/"):
                if not any(p in user["permissions"] for p in ("workflows.read_own","workflows.read_all")): return self.deny(403,"Workflow read permission required")
                wid=self.path.split("/")[3]
                if not self.workflow_allowed(user,wid): return self.deny(403,"This workflow belongs to another user")
            if self.path.startswith("/api/artifacts/") and user["role"]!="admin":
                if not any(p in user["permissions"] for p in ("workflows.read_own","workflows.read_all")): return self.deny(403,"Workflow read permission required")
                aid=self.path.split("/")[3]
                with db() as conn: row=conn.execute("SELECT workflow_id FROM artifacts WHERE id=?",(aid,)).fetchone()
                if not row or not self.workflow_allowed(user,row["workflow_id"]): return self.deny(403,"Artifact access denied")
        if self.path=="/api/users":
            with db() as conn: rows=conn.execute("SELECT id,username,email,verified_at,role,active,created_at FROM users ORDER BY username").fetchall()
            result=[]
            for row in rows:
                item=dict(row); item["verified"]=bool(item.pop("verified_at")); item["profiles"],item["permissions"]=access_for_user(item["id"]); result.append(item)
            return self.json(result)
        if self.path=="/api/rbac/profiles":
            with db() as conn:
                rows=conn.execute("SELECT * FROM rbac_profiles ORDER BY name").fetchall(); grants=conn.execute("SELECT * FROM rbac_profile_permissions ORDER BY permission_id").fetchall()
            by_profile={r["id"]:[] for r in rows}
            for grant in grants: by_profile.setdefault(grant["profile_id"],[]).append(grant["permission_id"])
            return self.json([{**dict(row),"permissions":by_profile.get(row["id"],[])} for row in rows])
        if self.path=="/api/admin/settings": return self.json({"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")})
        if self.path=="/api/admin/email":
            config=smtp_configuration(); return self.json(config)
        if self.path=="/api/server-stats": return self.json(privacy_safe_server_stats())
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
        if self.path=="/api/workflow-templates":
            return self.json(visible_workflow_templates(user["id"],"workflow_templates.manage_all" in user["permissions"]))
        if self.path.startswith("/api/workflow-templates/"):
            template_id=self.path.rsplit("/",1)[-1]; row=self.template_row(template_id)
            if not self.template_visible(user,row): return self.json({"error":"Workflow template not found"},404)
            return self.json(workflow_template_payload(row,user["id"],"workflow_templates.manage_all" in user["permissions"]))
        if self.path=="/api/stack/status":
            result,status=supervisor_call("status"); return self.json(result,status)
        if self.path=="/api/workflows":
            if not any(p in user["permissions"] for p in ("workflows.read_own","workflows.read_all")): return self.deny(403,"Workflow read permission required")
            with db() as conn:
                rows=conn.execute("SELECT * FROM workflows ORDER BY created_at DESC LIMIT 30").fetchall() if "workflows.read_all" in user["permissions"] else conn.execute("SELECT * FROM workflows WHERE owner_id=? ORDER BY created_at DESC LIMIT 30",(user["id"],)).fetchall()
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
        if self.path=="/api/auth/register":
            remote=self.client_address[0]
            if not consume_rate_limit("register",remote,5,600): return self.json({"error":"Too many registration attempts; try again later"},429)
            username=str(body.get("username","")).strip(); email=str(body.get("email","")).strip().lower(); password=str(body.get("password","")); language=str(body.get("language","en"))
            if len(username)<3 or len(password)<8 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return self.json({"error":"Valid email, username of 3+ characters, and password of 8+ characters are required"},400)
            uid=str(uuid.uuid4()); created=stamp()
            try:
                with db() as conn:
                    conn.execute("INSERT INTO users(id,username,password_hash,role,active,created_at,email,verified_at) VALUES(?,?,?,?,?,?,?,NULL)",(uid,username,password_hash(password),"user",1,created,email))
                    conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,"workflow_operator"))
            except sqlite3.IntegrityError: return self.json({"error":"Username or email already registered"},409)
            delivery,status=issue_verification_code(uid,language,True)
            response={"registered":True,"verification_required":True,"email_sent":status==200,"expires_in_seconds":600}
            if status==200: response.update(delivery)
            else: response["message"]="Account created, but email delivery failed. Retry later or ask an authorized user manager for manual approval."
            return self.json(response,201)
        if self.path=="/api/auth/login":
            username=str(body.get("username","")).strip(); password=str(body.get("password",""))
            with db() as conn: row=conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(username,)).fetchone()
            if not row or not row["active"] or not password_valid(password,row["password_hash"]): return self.deny(401,"Invalid credentials")
            token=secrets.token_urlsafe(32)
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE expires_at<=?",(stamp(),)); conn.execute("INSERT INTO sessions(token,user_id,expires_at,created_at,storage_id) VALUES(?,?,?,?,?)",(token,row["id"],stamp()+43200,stamp(),str(uuid.uuid4())))
            profiles,permissions=access_for_user(row["id"])
            verified=bool(row["verified_at"])
            return self.send_session({"id":row["id"],"username":row["username"],"email":row["email"],"verified":verified,"role":row["role"],"profiles":profiles,"permissions":permissions if verified else []},token)
        if self.path in ("/api/auth/verify","/api/auth/resend"):
            user=self.current_user()
            if not user: return self.deny()
            if self.path.endswith("/verify"):
                result,status=verify_user_code(user["id"],str(body.get("code",""))); return self.json(result,status)
            result,status=issue_verification_code(user["id"],str(body.get("language","en"))); return self.json(result,status)
        if self.path=="/api/auth/logout":
            token=self.cookie("skein_session")
            if token:
                with db() as conn: conn.execute("DELETE FROM sessions WHERE token=?",(token,))
            raw=b'{"ok":true}'; self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Set-Cookie","skein_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); return self.wfile.write(raw)
        if self.path.startswith("/api/models"): user=self.authorize("models.manage")
        elif self.path.startswith("/api/workflow-templates"): user=self.authorize_any("workflow_templates.manage_own","workflow_templates.manage_all")
        elif self.path.startswith("/api/users") or self.path.startswith("/api/rbac"): user=self.authorize("users.manage")
        elif self.path.startswith("/api/admin/email"): user=self.authorize("email.manage")
        elif self.path.startswith(("/api/pools","/api/gpus","/api/stack","/api/admin")): user=self.authorize("settings.manage")
        elif self.path=="/api/workflows" or self.path.endswith("/run") or self.path.endswith("/execute") or self.path.endswith("/command"): user=self.authorize("workflows.execute")
        else: user=self.authorize()
        if not user: return
        if self.path.startswith("/api/workflows/"):
            wid=self.path.split("/")[3]
            if not self.workflow_allowed(user,wid): return self.deny(403,"This workflow belongs to another user")
        if self.path.startswith("/api/artifacts/") and user["role"]!="admin":
            aid=self.path.split("/")[3]
            with db() as conn: artifact_owner=conn.execute("SELECT workflow_id FROM artifacts WHERE id=?",(aid,)).fetchone()
            if not artifact_owner or not self.workflow_allowed(user,artifact_owner["workflow_id"]): return self.deny(403,"Artifact access denied")
        if self.path=="/api/workflow-templates/generate":
            result,status=generate_validated_workflow_template(str(body.get("objective","")).strip()); return self.json(result,status)
        if self.path=="/api/workflow-templates/select":
            objective=str(body.get("objective","")).strip()
            if len(objective)<5: return self.json({"error":"Objective is too short"},400)
            selected=select_workflow_template(objective,user["id"],"workflow_templates.manage_all" in user["permissions"])
            if not selected: return self.json({"error":"No visible workflow template"},404)
            if selected.get("error"): return self.json(selected,502)
            return self.json(selected)
        if self.path=="/api/workflow-templates":
            try: template=normalize_workflow_template(body)
            except ValueError as exc: return self.json({"error":str(exc)},400)
            template_id=str(uuid.uuid4()); now=stamp()
            with db() as conn:
                conn.execute("""INSERT INTO workflow_templates
                  (id,name,description,objective_template,tasks,tags,owner_id,shared,system,created_at,updated_at)
                  VALUES(?,?,?,?,?,?,?,?,0,?,?)""",(template_id,template["name"],template["description"],template["objective_template"],json.dumps(template["tasks"],ensure_ascii=False),",".join(template["tags"]),user["id"],1 if template["shared"] else 0,now,now))
                row=conn.execute("SELECT * FROM workflow_templates WHERE id=?",(template_id,)).fetchone()
            return self.json(workflow_template_payload(row,user["id"],"workflow_templates.manage_all" in user["permissions"]),201)
        if self.path.startswith("/api/workflow-templates/"):
            template_id=self.path.rsplit("/",1)[-1]; row=self.template_row(template_id)
            if not row: return self.json({"error":"Workflow template not found"},404)
            if not self.template_editable(user,row): return self.deny(403,"Workflow template edit permission required")
            merged={"name":body.get("name",row["name"]),"description":body.get("description",row["description"]),"objective_template":body.get("objective_template",row["objective_template"]),"tasks":body.get("tasks",json.loads(row["tasks"])),"tags":body.get("tags",row["tags"]),"shared":body.get("shared",bool(row["shared"]))}
            try: template=normalize_workflow_template(merged)
            except ValueError as exc: return self.json({"error":str(exc)},400)
            with db() as conn:
                conn.execute("""UPDATE workflow_templates SET name=?,description=?,objective_template=?,tasks=?,tags=?,shared=?,updated_at=? WHERE id=?""",(template["name"],template["description"],template["objective_template"],json.dumps(template["tasks"],ensure_ascii=False),",".join(template["tags"]),1 if template["shared"] else 0,stamp(),template_id))
                updated=conn.execute("SELECT * FROM workflow_templates WHERE id=?",(template_id,)).fetchone()
            return self.json(workflow_template_payload(updated,user["id"],"workflow_templates.manage_all" in user["permissions"]))
        if self.path=="/api/users":
            username=str(body.get("username","")).strip(); password=str(body.get("password","")); profiles=body.get("profiles") or ["workflow_operator"]
            if not isinstance(profiles,list) or not profiles: return self.json({"error":"At least one RBAC profile is required"},400)
            with db() as conn: valid={r[0] for r in conn.execute("SELECT id FROM rbac_profiles WHERE id IN (%s)"%",".join("?"*len(profiles)),profiles).fetchall()}
            if set(profiles)!=valid: return self.json({"error":"Unknown RBAC profile"},400)
            role="admin" if "super_admin" in profiles else "user"
            if len(username)<3 or len(password)<8: return self.json({"error":"Username must be at least 3 characters and password at least 8"},400)
            try:
                uid=str(uuid.uuid4())
                with db() as conn:
                    created=stamp(); conn.execute("INSERT INTO users(id,username,password_hash,role,active,created_at,email,verified_at) VALUES(?,?,?,?,?,?,?,?)",(uid,username,password_hash(password),role,1,created,body.get("email") or None,created))
                    for profile in profiles: conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,profile))
                assigned,_=access_for_user(uid); return self.json({"id":uid,"username":username,"role":role,"active":1,"profiles":assigned},201)
            except sqlite3.IntegrityError: return self.json({"error":"This username already exists"},409)
        if self.path.startswith("/api/users/") and self.path.endswith("/approve"):
            if "users.verify" not in user["permissions"]: return self.deny(403,"Permission required: users.verify")
            uid=self.path.split("/")[-2]; now=stamp()
            with db() as conn:
                changed=conn.execute("UPDATE users SET verified_at=? WHERE id=? AND verified_at IS NULL",(now,uid)).rowcount
                conn.execute("UPDATE email_verification_codes SET used_at=? WHERE user_id=? AND used_at IS NULL",(now,uid))
            return self.json({"verified":True,"changed":bool(changed)})
        if self.path.startswith("/api/users/"):
            uid=self.path.rsplit("/",1)[-1]; fields=[]; values=[]
            with db() as conn: existing=conn.execute("SELECT role,active FROM users WHERE id=?",(uid,)).fetchone()
            if not existing: return self.json({"error":"User not found"},404)
            current_profiles,_=access_for_user(uid); current_ids={p["id"] for p in current_profiles}; requested_profiles=body.get("profiles")
            resulting_profiles=set(requested_profiles) if isinstance(requested_profiles,list) else current_ids
            resulting_active=(1 if body["active"] else 0) if "active" in body else existing["active"]
            if "super_admin" in current_ids and existing["active"] and ("super_admin" not in resulting_profiles or not resulting_active):
                with db() as conn: remaining=conn.execute("SELECT COUNT(DISTINCT u.id) FROM users u JOIN user_profiles up ON up.user_id=u.id WHERE up.profile_id='super_admin' AND u.active=1 AND u.id<>?",(uid,)).fetchone()[0]
                if remaining==0: return self.json({"error":"At least one active Super Administrator is required"},409)
            if requested_profiles is not None:
                if not resulting_profiles: return self.json({"error":"At least one RBAC profile is required"},400)
                with db() as conn: valid={r[0] for r in conn.execute("SELECT id FROM rbac_profiles WHERE id IN (%s)"%",".join("?"*len(resulting_profiles)),tuple(resulting_profiles)).fetchall()}
                if valid!=resulting_profiles: return self.json({"error":"Unknown RBAC profile"},400)
                fields.append("role=?"); values.append("admin" if "super_admin" in resulting_profiles else "user")
            if "active" in body: fields.append("active=?"); values.append(1 if body["active"] else 0)
            if body.get("password"):
                if len(str(body["password"]))<8: return self.json({"error":"Password is too short"},400)
                fields.append("password_hash=?"); values.append(password_hash(str(body["password"])))
            if not fields and requested_profiles is None: return self.json({"error":"No changes supplied"},400)
            with db() as conn:
                if fields: conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?",(*values,uid))
                if requested_profiles is not None:
                    conn.execute("DELETE FROM user_profiles WHERE user_id=?",(uid,))
                    for profile in resulting_profiles: conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,profile))
            return self.json({"ok":True})
        if self.path=="/api/admin/settings":
            allowed=bool(body.get("users_can_choose_execution_mode"))
            with db() as conn: conn.execute("INSERT INTO settings VALUES('users_can_choose_execution_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("true" if allowed else "false",))
            return self.json({"users_can_choose_execution_mode":allowed})
        if self.path=="/api/admin/email":
            host=str(body.get("host","")).strip(); from_address=str(body.get("from_address","")).strip(); security=str(body.get("security","starttls"))
            if not host or not from_address or security not in ("starttls","ssl","plain"): return self.json({"error":"SMTP host, sender address, and valid security mode are required"},400)
            values={"smtp_host":host,"smtp_port":str(int(body.get("port",587))),"smtp_username":str(body.get("username","")).strip(),"smtp_from":from_address,"smtp_security":security}
            if body.get("password"): values["smtp_password"]=protect_secret(str(body["password"]))
            with db() as conn:
                for key,value in values.items(): conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
            return self.json(smtp_configuration())
        if self.path=="/api/admin/email/test":
            recipient=str(body.get("recipient","")).strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",recipient): return self.json({"error":"Valid recipient email required"},400)
            try: send_email(recipient,"Skein SMTP test","Your Skein SMTP configuration is working.")
            except Exception as exc: return self.json({"error":"SMTP test failed","details":str(exc)},502)
            return self.json({"sent":True})
        if self.path=="/api/workflows":
            objective=str(body.get("objective","")).strip()
            if len(objective)<5: return self.json({"error":"Objective is too short"},400)
            if os.getenv("SKEIN_ALLOW_SIMULATION","0") != "1":
                missing=[role for role in ("reasoner","worker") if not endpoint_ready(ACTIVE_ENDPOINTS.get(role,""))]
                if missing: return self.json({"error":"Real models are not loaded","missing_roles":missing,
                  "action":"Use Auto-detect and load local models in Model Plane."},409)
            planning_mode=str(body.get("planning_mode","automatic")).strip().lower(); template_id=body.get("template_id"); specs=None; selection=None
            if planning_mode not in ("template","automatic","generate"): return self.json({"error":"Invalid workflow planning mode"},400)
            if planning_mode=="template":
                if not template_id: return self.json({"error":"A workflow template must be selected"},400)
                row=self.template_row(str(template_id))
                if not self.template_visible(user,row): return self.json({"error":"Workflow template not found"},404)
                template=workflow_template_payload(row,user["id"],"workflow_templates.manage_all" in user["permissions"]); specs=template_task_specs(template["tasks"]); selection={"template_id":template["id"],"template_name":template["name"],"mode":"template"}
            elif planning_mode=="automatic":
                template=select_workflow_template(objective,user["id"],"workflow_templates.manage_all" in user["permissions"])
                if not template: return self.json({"error":"No visible workflow template"},404)
                if template.get("error"): return self.json(template,502)
                template_id=template["id"]; specs=template_task_specs(template["tasks"]); selection={"template_id":template["id"],"template_name":template["name"],"mode":"automatic","selection_method":template["selection"]["method"],"reason":template["selection"]["reason"],"confidence":template["selection"]["confidence"]}
            else:
                generated,status=generate_validated_workflow_template(objective)
                if status!=200: return self.json(generated,status)
                specs=template_task_specs(generated["tasks"]); selection={"template_id":None,"template_name":generated["name"],"mode":"generate","validation":generated["validation"],"generation_mode":generated["mode"]}
            wid=create_workflow(objective,user["id"],user.get("session_id"),specs,template_id,planning_mode); start_workflow(wid); return self.json({"id":wid,"planning":selection},201)
        if self.path=="/api/execution-mode":
            if "settings.manage" not in user["permissions"] and not setting_bool("users_can_choose_execution_mode"): return self.deny(403,"Execution mode selection is disabled by the administrator")
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

    def do_DELETE(self):
        parsed=urlparse(self.path)
        if parsed.path.startswith("/api/workflow-templates/"):
            user=self.authorize_any("workflow_templates.manage_own","workflow_templates.manage_all")
            if not user: return
            template_id=parsed.path.rsplit("/",1)[-1]; row=self.template_row(template_id)
            if not row: return self.json({"error":"Workflow template not found"},404)
            if row["system"]: return self.json({"error":"System workflow templates cannot be deleted"},409)
            if not self.template_editable(user,row): return self.deny(403,"Workflow template delete permission required")
            with db() as conn: conn.execute("DELETE FROM workflow_templates WHERE id=?",(template_id,))
            return self.json({"deleted":True,"id":template_id})
        if parsed.path!="/api/workflows/history": return self.json({"error":"Unknown route"},404)
        scope=dict(part.split("=",1) for part in parsed.query.split("&") if "=" in part).get("scope","own")
        if scope not in ("own","all"): return self.json({"error":"Invalid history deletion scope"},400)
        permission="workflows.delete_all" if scope=="all" else "workflows.delete_own"
        user=self.authorize(permission)
        if not user: return
        result,status=delete_workflow_history(user["id"],scope=="all")
        return self.json(result,status)


if __name__=="__main__":
    init_db(); recover_pending_workflows(); server=ThreadingHTTPServer(("127.0.0.1",int(os.getenv("SKEIN_PORT","8787"))),Handler)
    print(f"Skein disponible sur http://127.0.0.1:{server.server_port}")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
