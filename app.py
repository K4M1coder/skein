from __future__ import annotations

import base64, csv, ctypes, io, json, logging, mimetypes, os, re, shlex, shutil, signal, sqlite3, struct, subprocess, sys, tempfile, threading, time, uuid, zipfile, hashlib, hmac, secrets, smtplib, ssl
from collections import deque
from email.message import EmailMessage
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse
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


LOG_DIR = DB_PATH.parent / "logs"


def _install_file_handler(root, log_dir, formatter):
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(log_dir / "skein.log",
            maxBytes=int(os.getenv("SKEIN_LOG_MAX_BYTES", 5_000_000)),
            backupCount=int(os.getenv("SKEIN_LOG_BACKUP_COUNT", 10)), encoding="utf-8")
        file_handler.setLevel(getattr(logging, os.getenv("SKEIN_LOG_LEVEL", "INFO").upper(), logging.INFO))
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except OSError as exc:
        root.warning(f"Rotating log file unavailable ({exc}); logging to console only.")


def configure_logging():
    """One rotating file (exhaustive: every action, error, and workflow/model lifecycle
    event) plus a quieter console echo, built on stdlib logging only per DEPENDENCY_POLICY.md.
    Idempotent so re-importing app.py never stacks duplicate handlers. Logging must never be
    the reason Skein fails to start: a file handler that can't be created (read-only disk,
    permissions) degrades to console-only instead of raising."""
    root = logging.getLogger("skein")
    if root.handlers: return root
    root.setLevel(logging.DEBUG)
    root.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    console = logging.StreamHandler()
    console.setLevel(getattr(logging, os.getenv("SKEIN_LOG_CONSOLE_LEVEL", "WARNING").upper(), logging.WARNING))
    console.setFormatter(formatter)
    root.addHandler(console)
    _install_file_handler(root, LOG_DIR, formatter)
    return root


def relocate_log_directory(new_db_dir):
    """Re-point the rotating file handler at <new_db_dir>/logs. init_db() calls this when the
    default database location turns out to be unwritable and falls back to another directory;
    LOG_DIR was already fixed at import time (before init_db can run), so without this the log
    file would keep writing to a directory that no longer matches where the database lives."""
    global LOG_DIR, LOG_FILE
    LOG_DIR = new_db_dir / "logs"; LOG_FILE = LOG_DIR / "skein.log"
    root = logging.getLogger("skein")
    for handler in [h for h in root.handlers if isinstance(h, RotatingFileHandler)]:
        root.removeHandler(handler); handler.close()
    formatter = logging.Formatter("%(asctime)s %(levelname)-8s %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    _install_file_handler(root, LOG_DIR, formatter)


logger = configure_logging()
logger_http = logger.getChild("http")
logger_auth = logger.getChild("auth")
logger_models = logger.getChild("models")
logger_workflow = logger.getChild("workflow")
logger_settings = logger.getChild("settings")
logger_system = logger.getChild("system")

LOG_FILE = LOG_DIR / "skein.log"
LOG_LEVEL_NAMES = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
LOG_LINE_PATTERN = re.compile(r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) (?P<level>[A-Z]+)\s+(?P<logger>\S+): (?P<message>.*)$")


def parse_log_records(text):
    """One record per logged call; a traceback's continuation lines (from logger.exception)
    have no timestamp of their own, so they fold into the message of the record above them
    instead of appearing as unreadable orphan rows in the viewer."""
    records = []
    for raw_line in text.splitlines():
        match = LOG_LINE_PATTERN.match(raw_line)
        if match: records.append(match.groupdict())
        elif records: records[-1]["message"] += "\n" + raw_line
        else: records.append({"timestamp": None, "level": None, "logger": None, "message": raw_line})
    return records


def read_log_records(limit=500, level=None, search=None):
    try: text = LOG_FILE.read_text("utf-8", "replace")
    except OSError: return []
    records = parse_log_records(text)
    if level: records = [r for r in records if r["level"] == level]
    if search:
        needle = search.lower()
        records = [r for r in records if needle in r["message"].lower() or needle in (r["logger"] or "").lower()]
    return records[-limit:]


def log_files_summary():
    """The active file plus any RotatingFileHandler backups (skein.log.1, .2, ...), oldest
    last, so the viewer can show how much history exists and offer each one for download."""
    if not LOG_DIR.exists(): return []
    files = [LOG_FILE] + sorted(LOG_DIR.glob("skein.log.*"), key=lambda p: int(p.suffix[1:]) if p.suffix[1:].isdigit() else 0)
    summary = []
    for path in files:
        try: stat = path.stat()
        except OSError: continue
        summary.append({"name": path.name, "size_bytes": stat.st_size, "modified_at": round(stat.st_mtime, 3)})
    return summary


def log_file_path(name):
    """Confine downloads to the log directory's own rotated files; reject anything else,
    the same traversal posture used for weight-file deletion elsewhere in this file."""
    if not re.fullmatch(r"skein\.log(\.\d+)?", str(name)): return None
    path = LOG_DIR / name
    return path if path.is_file() else None


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
          started_at REAL, finished_at REAL, action_type TEXT NOT NULL DEFAULT 'llm',
          action_config TEXT NOT NULL DEFAULT '{}', system_prompt TEXT NOT NULL DEFAULT '',
          output_format TEXT NOT NULL DEFAULT 'markdown', output_schema TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS events(
          id INTEGER PRIMARY KEY AUTOINCREMENT, workflow_id TEXT NOT NULL,
          task_id TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL, created_at REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS pools(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, domain TEXT NOT NULL, color TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS gpu_assignments(
          gpu_id TEXT NOT NULL, pool_id TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(gpu_id,pool_id));
        CREATE TABLE IF NOT EXISTS pool_telemetry(
          id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, pool_id TEXT NOT NULL, domain TEXT NOT NULL,
          assigned_gpus INTEGER NOT NULL, utilization REAL, power_w REAL, memory_used_mb REAL, memory_total_mb REAL, temperature_c REAL);
        CREATE TABLE IF NOT EXISTS models(
          id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL, backend TEXT NOT NULL,
          model_path TEXT NOT NULL, runtime_path TEXT NOT NULL, context_size INTEGER NOT NULL,
          port INTEGER NOT NULL, pool_id TEXT, status TEXT NOT NULL, pid INTEGER,
          endpoint TEXT, last_error TEXT, updated_at REAL NOT NULL,
          architecture TEXT, total_params INTEGER, active_params INTEGER,
          trained_context_length INTEGER, expert_count INTEGER, expert_used_count INTEGER,
          gguf_parsed_at REAL);
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
        relocate_log_directory(DB_PATH.parent)
        with db() as conn: conn.executescript(schema)
    with db() as conn:
        columns={r[1] for r in conn.execute("PRAGMA table_info(workflows)")}
        if "owner_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN owner_id TEXT")
        if "session_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN session_id TEXT")
        if "template_id" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN template_id TEXT")
        if "planning_mode" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN planning_mode TEXT DEFAULT 'legacy'")
        if "continued_from" not in columns: conn.execute("ALTER TABLE workflows ADD COLUMN continued_from TEXT")
        user_columns={r[1] for r in conn.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns: conn.execute("ALTER TABLE users ADD COLUMN email TEXT")
        if "verified_at" not in user_columns: conn.execute("ALTER TABLE users ADD COLUMN verified_at REAL")
        session_columns={r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
        task_columns={r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for column,definition in (("action_type","TEXT NOT NULL DEFAULT 'llm'"),("action_config","TEXT NOT NULL DEFAULT '{}'"),("system_prompt","TEXT NOT NULL DEFAULT ''"),("output_format","TEXT NOT NULL DEFAULT 'markdown'"),("output_schema","TEXT NOT NULL DEFAULT ''")):
            if column not in task_columns: conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
        if "storage_id" not in session_columns: conn.execute("ALTER TABLE sessions ADD COLUMN storage_id TEXT")
        model_columns={r[1] for r in conn.execute("PRAGMA table_info(models)")}
        for column,definition in (("architecture","TEXT"),("total_params","INTEGER"),("active_params","INTEGER"),
                                   ("trained_context_length","INTEGER"),("expert_count","INTEGER"),
                                   ("expert_used_count","INTEGER"),("gguf_parsed_at","REAL")):
            if column not in model_columns: conn.execute(f"ALTER TABLE models ADD COLUMN {column} {definition}")
        gpu_assignment_pk=[row["name"] for row in conn.execute("PRAGMA table_info(gpu_assignments)") if row["pk"]]
        if gpu_assignment_pk==["gpu_id"]:
            # A single physical GPU commonly serves every pool at once (reasoner, worker, and
            # retrieval sharing the only card); the old schema only ever let it belong to one.
            conn.execute("ALTER TABLE gpu_assignments RENAME TO gpu_assignments_legacy")
            conn.execute("CREATE TABLE gpu_assignments(gpu_id TEXT NOT NULL, pool_id TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(gpu_id,pool_id))")
            conn.execute("INSERT INTO gpu_assignments(gpu_id,pool_id,updated_at) SELECT gpu_id,pool_id,updated_at FROM gpu_assignments_legacy WHERE pool_id IS NOT NULL")
            conn.execute("DROP TABLE gpu_assignments_legacy")
        for session in conn.execute("SELECT token FROM sessions WHERE storage_id IS NULL OR storage_id='' ").fetchall():
            conn.execute("UPDATE sessions SET storage_id=? WHERE token=?",(str(uuid.uuid4()),session["token"]))
        conn.execute("UPDATE users SET verified_at=created_at WHERE verified_at IS NULL AND email IS NULL")
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_email_unique ON users(email) WHERE email IS NOT NULL")
        # Login matches usernames case-insensitively, so uniqueness must be case-insensitive
        # too: 'Admin' next to 'admin' would make one of the two accounts unreachable.
        try: conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS users_username_nocase ON users(username COLLATE NOCASE)")
        except sqlite3.IntegrityError: pass  # legacy rows already collide by case; the registration-time checks still guard new accounts
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
          "workflow_runner":("Workflow Runner","Execute workflows and manage personal run history",["workflows.execute","workflows.read_own","workflows.delete_own","workflow_templates.read"]),
          "workflow_designer":("Workflow Designer","Read workflow templates and manage owned templates",["workflow_templates.read","workflow_templates.manage_own"]),
          "stats_auditor":("Statistics Auditor","Read anonymized operational statistics only",["server_stats.read"]),
        }
        for profile_id,(name,description,grants) in profiles.items():
            conn.execute("INSERT OR IGNORE INTO rbac_profiles VALUES(?,?,?,1)",(profile_id,name,description))
            for permission in grants: conn.execute("INSERT OR IGNORE INTO rbac_profile_permissions VALUES(?,?)",(profile_id,permission))
        for user in conn.execute("SELECT id,role FROM users").fetchall():
            if not conn.execute("SELECT 1 FROM user_profiles WHERE user_id=?",(user["id"],)).fetchone():
                conn.execute("INSERT OR IGNORE INTO user_profiles VALUES(?,?)",(user["id"],"super_admin" if user["role"]=="admin" else "workflow_operator"))
        seed_default_workflow_templates(conn)
        conn.execute("DELETE FROM pool_telemetry WHERE created_at<?", (stamp()-7*24*3600,))
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


# Login verifies against this hash when the username does not exist, so the 401 takes the
# same ~100 ms of PBKDF2 work either way and response timing stops revealing which
# usernames are real.
DUMMY_PASSWORD_HASH=password_hash(secrets.token_hex(16))


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


class _ResendThrottled(Exception): pass


def issue_verification_code(user_id,language="en",force=False):
    now=stamp()
    code=f"{secrets.randbelow(1_000_000):06d}"; code_id=str(uuid.uuid4()); code_hash=password_hash(code)
    try:
        with db() as conn:
            user=conn.execute("SELECT id,username,email,verified_at,active FROM users WHERE id=?",(user_id,)).fetchone()
            if not user or not user["active"]: return {"error":"Account not found or inactive"},404
            if user["verified_at"]: return {"error":"Account is already verified"},409
            if not user["email"]: return {"error":"No email address is associated with this account"},400
            # The invalidation write takes the transaction's write lock, so concurrent
            # resends serialize here and the throttle check below cannot race; a throttled
            # request raises so the rollback restores the codes it just invalidated.
            conn.execute("UPDATE email_verification_codes SET used_at=? WHERE user_id=? AND used_at IS NULL",(now,user_id))
            latest=conn.execute("SELECT created_at FROM email_verification_codes WHERE user_id=? ORDER BY created_at DESC LIMIT 1",(user_id,)).fetchone()
            if latest and not force and now-latest["created_at"]<60: raise _ResendThrottled(latest["created_at"])
            conn.execute("INSERT INTO email_verification_codes VALUES(?,?,?,?,?,?,?)",(code_id,user_id,code_hash,now+600,None,0,now))
    except _ResendThrottled as throttled:
        return {"error":"Please wait before requesting another code","retry_after_seconds":round(60-(now-throttled.args[0]))},429
    french=str(language).lower().startswith("fr")
    subject="Votre code de vérification Skein" if french else "Your Skein verification code"
    body=(f"Bonjour {user['username']},\n\nVotre code Skein est : {code}\n\nIl expire dans 10 minutes et ne peut être utilisé qu'une seule fois."
      if french else f"Hello {user['username']},\n\nYour Skein code is: {code}\n\nIt expires in 10 minutes and can be used only once.")
    try: send_email(user["email"],subject,body)
    except Exception as exc:
        # SMTP internals (relay host, auth failures) are email.manage material; an
        # unverified session only learns that delivery failed.
        logger_auth.warning(f"verification email delivery failed for user {user_id}: {exc}")
        return {"error":"Verification email could not be sent","registration_pending":True},503
    return {"sent":True,"expires_in_seconds":600,"resend_after_seconds":60,"email_hint":user["email"][:2]+"***@"+user["email"].split("@")[-1]},200


def verify_user_code(user_id,code):
    now=stamp()
    with db() as conn: rows=conn.execute("SELECT * FROM email_verification_codes WHERE user_id=? AND used_at IS NULL ORDER BY created_at DESC",(user_id,)).fetchall()
    if not rows: return {"error":"No active verification code"},400
    current=rows[0]
    if current["expires_at"]<now:
        with db() as conn: conn.execute("UPDATE email_verification_codes SET used_at=? WHERE id=?",(now,current["id"]))
        return {"error":"Verification code expired"},410
    # Claim an attempt slot atomically before checking the code: a read-then-increment
    # would let N concurrent requests each see attempts<5 and overshoot the cap together.
    with db() as conn:
        claimed=conn.execute("UPDATE email_verification_codes SET attempts=attempts+1 WHERE id=? AND attempts<5 AND used_at IS NULL",(current["id"],)).rowcount
    if not claimed: return {"error":"Too many invalid attempts; request a new code"},429
    if not password_valid(str(code).strip(),current["code_hash"]):
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


MODEL_LIBRARY = DB_PATH.parent / "models"
DISCOVERY_ROOTS = [Path.home()/".cache/huggingface/hub", Path.home()/".lmstudio/models"]
DEFAULT_MODEL_ROOTS = DISCOVERY_ROOTS + [MODEL_LIBRARY]
MIN_MODEL_MB = max(1, int(os.getenv("SKEIN_MIN_MODEL_MB", "64")))
MAX_SCANNED_MODEL_FILES = 2000
QUANTIZATION_PATTERN = re.compile(r"(?<![A-Za-z0-9])(IQ\d+(?:_[A-Z0-9]+)*|Q\d+(?:_[A-Z0-9]+)*|BF16|F16|F32|MXFP\d+)(?![A-Za-z0-9])", re.IGNORECASE)


def setting_text(key, default=None):
    with db() as conn: row=conn.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    with db() as conn:
        conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,str(value)))


def unique_paths(paths):
    seen, ordered = set(), []
    for path in paths:
        try: key=str(Path(path).expanduser().resolve(strict=False)).lower()
        except (OSError, ValueError): continue
        if key in seen: continue
        seen.add(key); ordered.append(Path(path).expanduser())
    return ordered


def configured_model_roots():
    """Model directories to scan: environment first, then operator-managed, then defaults."""
    roots=[Path(raw.strip()) for raw in (os.getenv("SKEIN_MODEL_ROOTS","") or "").split(os.pathsep) if raw.strip()]
    try: roots += [Path(raw) for raw in json.loads(setting_text("model_roots") or "[]") if str(raw).strip()]
    except (json.JSONDecodeError, TypeError): pass
    # The library is resolved through model_library_dir so it exists and never warns as missing.
    return unique_paths(roots + DISCOVERY_ROOTS + [model_library_dir()])


def save_model_roots(roots):
    cleaned=[str(path) for path in unique_paths(Path(str(raw)) for raw in roots if str(raw).strip())]
    set_setting("model_roots", json.dumps(cleaned, ensure_ascii=False))
    return cleaned


def model_library_dir():
    """Writable directory that receives downloaded and uploaded weights."""
    target=Path(os.getenv("SKEIN_MODEL_LIBRARY","") or MODEL_LIBRARY).expanduser()
    target.mkdir(parents=True, exist_ok=True)
    return target


def find_llama_runtime():
    home=Path.home()
    candidates=[Path(raw.strip()) for raw in (os.getenv("SKEIN_RUNTIME_PATHS","") or "").split(os.pathsep) if raw.strip()]
    candidates.append(home/".unsloth/llama.cpp/build/bin/Release/llama-server.exe")
    backends=home/".lmstudio/extensions/backends"
    # Prefer the proven standalone build; LM Studio entries can be tiny launcher shims.
    if backends.exists(): candidates += sorted(backends.glob("llama.cpp-win-*-nvidia-*/llama-server.exe"))
    discovered=shutil.which("llama-server") or shutil.which("llama-server.exe")
    if discovered: candidates.append(Path(discovered))
    return next((path for path in candidates if path.is_file()), None)


def detect_quantization(filename):
    matches=QUANTIZATION_PATTERN.findall(Path(filename).stem)
    return matches[-1].upper() if matches else None


GGUF_MAX_HEADER_BYTES=64*1024*1024
GGUF_SCALAR_FORMATS={0:'<B',1:'<b',2:'<H',3:'<h',4:'<I',5:'<i',6:'<f',7:'<?',10:'<Q',11:'<q',12:'<d'}
GGUF_STRING_TYPE,GGUF_ARRAY_TYPE=8,9


def _read_gguf_string(buf,offset):
    (length,)=struct.unpack_from('<Q',buf,offset); offset+=8
    end=offset+length
    if end>len(buf): raise EOFError("string exceeds header buffer")
    return buf[offset:end].decode("utf-8",errors="replace"),end


def _read_gguf_value(buf,offset,value_type):
    if value_type==GGUF_STRING_TYPE: return _read_gguf_string(buf,offset)
    if value_type==GGUF_ARRAY_TYPE:
        (elem_type,)=struct.unpack_from('<I',buf,offset); offset+=4
        (count,)=struct.unpack_from('<Q',buf,offset); offset+=8
        values=[]
        for _ in range(count):
            value,offset=_read_gguf_value(buf,offset,elem_type); values.append(value)
        return values,offset
    fmt=GGUF_SCALAR_FORMATS.get(value_type)
    if not fmt: raise ValueError(f"unsupported gguf value type {value_type}")
    size=struct.calcsize(fmt)
    if offset+size>len(buf): raise EOFError("scalar exceeds header buffer")
    (value,)=struct.unpack_from(fmt,buf,offset)
    return value,offset+size


def parse_gguf_metadata(path):
    """Read a GGUF file's own header: architecture, trained context length, MoE expert
    counts, and an exact parameter count summed from tensor shapes (most GGUF files never
    carry general.parameter_count, so the metadata alone cannot give a total). Reads only
    a bounded prefix of the file, never the weight data, and degrades to partial results
    instead of raising if a huge tokenizer vocabulary runs past that prefix."""
    try:
        with open(path,"rb") as handle: buf=handle.read(GGUF_MAX_HEADER_BYTES)
    except OSError: return None
    if buf[:4]!=b"GGUF": return None
    offset=4
    try:
        (_version,)=struct.unpack_from('<I',buf,offset); offset+=4
        (tensor_count,)=struct.unpack_from('<Q',buf,offset); offset+=8
        (kv_count,)=struct.unpack_from('<Q',buf,offset); offset+=8
    except struct.error:
        return None  # valid magic but the fixed header itself is truncated
    metadata={}
    try:
        for _ in range(kv_count):
            key,offset=_read_gguf_string(buf,offset)
            (value_type,)=struct.unpack_from('<I',buf,offset); offset+=4
            value,offset=_read_gguf_value(buf,offset,value_type)
            if not key.startswith("tokenizer."): metadata[key]=value  # skip huge vocab arrays
    except (EOFError,struct.error,IndexError,UnicodeError,ValueError):
        pass  # truncation or an unknown value type: keep whatever metadata was already read
    architecture=metadata.get("general.architecture")
    total_params=0; expert_params=0
    try:
        for _ in range(tensor_count):
            name,offset=_read_gguf_string(buf,offset)
            (n_dims,)=struct.unpack_from('<I',buf,offset); offset+=4
            dims=struct.unpack_from(f'<{n_dims}Q',buf,offset); offset+=8*n_dims
            offset+=4+8  # ggml_type, tensor data offset
            count=1
            for dim in dims: count*=dim
            total_params+=count
            if "exps" in name or "experts" in name: expert_params+=count
    except (EOFError,struct.error,IndexError):
        total_params=None  # tensor section ran past the header prefix; size is unknowable
    result={
        "architecture":architecture,
        "trained_context_length":metadata.get(f"{architecture}.context_length") if architecture else None,
        "total_params":total_params,
        "active_params":None,
    }
    expert_count=metadata.get(f"{architecture}.expert_count") if architecture else None
    expert_used=metadata.get(f"{architecture}.expert_used_count") if architecture else None
    result["expert_count"]=expert_count or None
    result["expert_used_count"]=expert_used or None
    if total_params and expert_count and expert_used and expert_params:
        shared=total_params-expert_params
        result["active_params"]=round(shared+expert_params*(expert_used/expert_count))
    return result


def model_gguf_metadata(model_id,model_path,parsed_at):
    """Parse-once-and-cache: GGUF header parsing reads real file bytes, so it happens at
    registration time and lazily for pre-existing rows, never on every /api/models poll."""
    if parsed_at: return None  # already cached, nothing to do
    info=parse_gguf_metadata(model_path) or {}
    with db() as conn:
        conn.execute("UPDATE models SET architecture=?,total_params=?,active_params=?,trained_context_length=?,"
                     "expert_count=?,expert_used_count=?,gguf_parsed_at=? WHERE id=?",
                     (info.get("architecture"),info.get("total_params"),info.get("active_params"),
                      info.get("trained_context_length"),info.get("expert_count"),info.get("expert_used_count"),
                      stamp(),model_id))
    return info


def model_file_entries(limit=MAX_SCANNED_MODEL_FILES):
    """Every GGUF file visible under the configured roots, with registration state."""
    entries, warnings, truncated = [], [], False
    with db() as conn: registered={str(row[0]).lower() for row in conn.execute("SELECT model_path FROM models")}
    for root in configured_model_roots():
        if not root.exists():
            warnings.append(f"Model root not found: {root}"); continue
        try: found=sorted(root.rglob("*.gguf"))
        except OSError as exc:
            warnings.append(f"Cannot read {root}: {exc}"); continue
        for path in found:
            if "mmproj" in path.name.lower(): continue
            if len(entries)>=limit: truncated=True; break
            try: info=path.stat()
            except OSError: continue
            entries.append({"path":str(path),"name":path.stem,"root":str(root),"size_bytes":info.st_size,
              "size_gb":round(info.st_size/1073741824,2),"quantization":detect_quantization(path.name),
              "modified_at":round(info.st_mtime,3),"registered":str(path).lower() in registered,
              "too_small":info.st_size < MIN_MODEL_MB*1048576})
        if truncated:
            warnings.append(f"Scan stopped at {limit} files; narrow the configured model roots."); break
    return entries, warnings


def preferred_auto_model(entries):
    """Largest weight file whose two concurrent runtimes plausibly fit detected VRAM, else
    the smallest available. The auto model is registered for the reasoner AND the worker,
    so two instances of this same file load at once: each may claim at most half of the
    usable budget, or autoload is guaranteed to over-commit the card."""
    usable=[entry for entry in entries if not entry["too_small"]]
    if not usable: return None
    vram_mb=sum(float(gpu.get("memory_total_mb") or 0) for gpu in nvidia_gpus())
    if vram_mb:
        budget=vram_mb*1048576*.8/2
        fitting=[entry for entry in usable if entry["size_bytes"]<=budget]
        if fitting: return max(fitting, key=lambda entry: entry["size_bytes"])
    return min(usable, key=lambda entry: entry["size_bytes"])


def register_model_file(path, role="available", pool_id=None, name=None, context_size=8192, runtime_path=None):
    resolved=Path(str(path)).expanduser()
    if resolved.suffix.lower()!=".gguf": return {"error":"Only .gguf weight files can be registered"},400
    if not resolved.is_file(): return {"error":"Model file not found","details":str(resolved)},404
    role=str(role or "available").strip().lower()
    if role not in MODEL_ROLES: return {"error":f"Unsupported model role '{role}'"},400
    with db() as conn:
        existing=conn.execute("SELECT id FROM models WHERE model_path=? COLLATE NOCASE",(str(resolved),)).fetchone()
        if existing: return {"error":"This weight file is already registered","id":existing["id"]},409
    runtime=Path(runtime_path) if runtime_path else find_llama_runtime()
    mid=str(uuid.uuid4())
    with db() as conn:
        conn.execute("INSERT INTO models(id,name,role,backend,model_path,runtime_path,context_size,port,pool_id,status,pid,endpoint,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
          (mid,str(name or resolved.stem),role,"llama.cpp",str(resolved),str(runtime or ""),
           max(512,int(context_size or 8192)),0,None,"STOPPED",None,None,None,stamp()))
    result,status=configure_model(mid,role,pool_id)
    if status!=200: return result,status
    logger_models.info(f"registered id={mid} name={name or resolved.stem!r} role={role} path={resolved}")
    return {"id":mid,"name":str(name or resolved.stem),"model_path":str(resolved),"role":role,
            "quantization":detect_quantization(resolved.name),"runtime_path":str(runtime or "")},201


def discover_local_models(include_available=False):
    """Scan the configured roots. File discovery never depends on finding a runtime."""
    entries, warnings = model_file_entries()
    runtime = find_llama_runtime()
    if not runtime:
        warnings.append("No llama-server executable was found. Weights are still registered; set SKEIN_RUNTIME_PATHS or edit the runtime path before loading.")
    created=[]
    auto=preferred_auto_model(entries) if runtime else None
    with db() as conn:
        if auto:
            for role,port in (("reasoner",8001),("worker",8002)):
                if conn.execute("SELECT 1 FROM models WHERE role=?",(role,)).fetchone(): continue
                mid=str(uuid.uuid4())
                conn.execute("INSERT INTO models(id,name,role,backend,model_path,runtime_path,context_size,port,pool_id,status,pid,endpoint,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (mid,f"Auto {role} · {auto['name']}",role,"llama.cpp",auto["path"],str(runtime),8192,port,None,"STOPPED",None,None,None,stamp()))
                created.append({"id":mid,"name":auto["name"],"model_path":auto["path"],"role":role})
        if include_available:
            for entry in entries:
                if entry["too_small"] or entry["registered"]: continue
                if conn.execute("SELECT 1 FROM models WHERE model_path=? COLLATE NOCASE",(entry["path"],)).fetchone(): continue
                mid=str(uuid.uuid4())
                conn.execute("INSERT INTO models(id,name,role,backend,model_path,runtime_path,context_size,port,pool_id,status,pid,endpoint,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (mid,f"Available · {entry['name']}","available","llama.cpp",entry["path"],str(runtime or ""),8192,0,None,"STOPPED",None,None,None,stamp()))
                created.append({"id":mid,"name":entry["name"],"model_path":entry["path"],"role":"available","quantization":entry["quantization"]})
    skipped=[entry for entry in entries if entry["too_small"]]
    if skipped: warnings.append(f"{len(skipped)} file(s) below {MIN_MODEL_MB} MB were ignored; lower SKEIN_MIN_MODEL_MB to include them.")
    return {"discovered":created,"count":len(created),"scanned_files":len(entries),
            "roots":[str(root) for root in configured_model_roots()],
            "runtime":str(runtime) if runtime else None,"warnings":warnings}


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


KV_CACHE_FRACTION_PER_4K_CONTEXT=0.03
MODEL_COMPUTE_BUFFER_MB=350
VRAM_ESTIMATION_METHOD=(
  "Per-model VRAM is not measurable on this system: nvidia-smi reports no per-process "
  "utilization or memory on Windows GeForce (WDDM) drivers, unlike Linux datacenter cards "
  "in TCC mode. This is an estimate — GGUF file size (the weights fully offloaded with "
  "-ngl 999) plus ~3% of that size per 4096 tokens of configured context (a rough KV-cache "
  "proxy) plus a fixed 350 MB compute-buffer allowance. Actual usage varies by architecture, "
  "batch size, and quantization; when a pool spans several GPUs the same running model is "
  "not split between them.")


def estimate_model_vram_mb(model):
    """Rough per-model VRAM footprint from GGUF file size and context; see VRAM_ESTIMATION_METHOD."""
    try: file_mb = Path(model["model_path"]).stat().st_size/1048576
    except OSError: return None
    context = int(model["context_size"] or 4096)
    kv_estimate_mb = (context/4096) * (file_mb * KV_CACHE_FRACTION_PER_4K_CONTEXT)
    return round(file_mb + kv_estimate_mb + MODEL_COMPUTE_BUFFER_MB, 1)


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
        # A GPU can serve several pools at once (one card running reasoner, worker, and
        # retrieval together is the common single-GPU case), so this is many-to-many.
        assignments = {}
        for row in conn.execute("SELECT gpu_id, pool_id FROM gpu_assignments"):
            assignments.setdefault(row["gpu_id"], []).append(row["pool_id"])
        pools = [dict(r) for r in conn.execute("SELECT * FROM pools ORDER BY rowid")]
        all_models = [dict(r) for r in conn.execute(
          "SELECT id,name,role,pool_id,model_path,context_size,status,pid,runtime_path FROM models ORDER BY name")]
    # The DB status column only changes on an explicit activate/stop; reconcile it against
    # the live process here too, so a crashed runtime never shows RUNNING on this page while
    # /api/models (which already does this same check) has already moved on to STOPPED.
    for model in all_models: model["status"] = reconciled_model_status(model)
    running_models = [model for model in all_models if model["status"]=="RUNNING"]
    running_by_pool = {}
    for model in running_models:
        if model["pool_id"]: running_by_pool[model["pool_id"]] = running_by_pool.get(model["pool_id"], 0) + 1
    models_by_pool = {}
    for model in all_models:
        if model["pool_id"]:
            models_by_pool.setdefault(model["pool_id"], []).append({"name":model["name"],"role":model["role"],"status":model["status"]})
    for gpu in gpus: gpu["pool_ids"] = assignments.get(gpu["id"], [])
    gpus_by_pool = {}
    for gpu in gpus:
        for pool_id in gpu["pool_ids"]: gpus_by_pool.setdefault(pool_id, []).append(gpu)
    estimated_by_gpu = {gpu["id"]: [] for gpu in gpus}
    # A model can be running without a pool assignment at all (loaded before ever picking
    # one); with exactly one physical GPU that is unambiguous, so it still gets attributed
    # there — but only once that lone GPU actually belongs to a pool. A GPU checked into no
    # pool at all is "not part of this monitoring", full stop; showing an estimate on it
    # anyway would contradict that operator action.
    lone_assigned_gpu = [gpus[0]] if len(gpus)==1 and gpus[0]["pool_ids"] else []
    for model in running_models:
        estimate_mb = estimate_model_vram_mb(model)
        if estimate_mb is None: continue
        target_gpus = gpus_by_pool.get(model["pool_id"]) if model["pool_id"] else lone_assigned_gpu
        for gpu in target_gpus or []:
            estimated_by_gpu[gpu["id"]].append({"model_id":model["id"],"name":model["name"],"role":model["role"],"estimated_vram_mb":estimate_mb})
    for gpu in gpus:
        gpu["estimated_models"] = estimated_by_gpu.get(gpu["id"], [])
        gpu["vram_estimation_method"] = VRAM_ESTIMATION_METHOD if gpu["estimated_models"] else None
    cpu_script = "(Get-CimInstance Win32_Processor | Measure-Object LoadPercentage -Average).Average"
    try: cpu = float(run_text(["powershell","-NoProfile","-Command",cpu_script],6) or 0)
    except ValueError: cpu = None
    pool_metrics=[]
    for pool in pools:
        # Shared-GPU attribution: a card serving three pools reports its full load under
        # each one, matching the same "shared activity may be attributed more than once"
        # principle already used for per-task energy estimates.
        members=[gpu for gpu in gpus if pool["id"] in gpu.get("pool_ids",())]
        values=lambda field:[float(gpu[field]) for gpu in members if gpu.get(field) is not None]
        utilization=values("utilization"); temperatures=values("temperature_c")
        pool_metrics.append({"pool_id":pool["id"],"name":pool["name"],"domain":pool["domain"],"color":pool["color"],"assigned_gpus":len(members),
          "utilization":round(sum(utilization)/len(utilization),2) if utilization else None,
          "power_w":round(sum(values("power_w")),2) if members else 0,
          "memory_used_mb":round(sum(values("memory_used_mb")),2),"memory_total_mb":round(sum(values("memory_total_mb")),2),
          "temperature_c":round(sum(temperatures)/len(temperatures),2) if temperatures else None,
          "gpu_ids":[gpu["id"] for gpu in members],
          # A model can run in this pool (its process is live, using whatever GPU the driver
          # picks) even though no GPU is assigned here; surface that gap instead of a silent 0.
          "running_models":running_by_pool.get(pool["id"],0),
          "models":models_by_pool.get(pool["id"],[])})
    return {"node":{"name":os.environ.get("COMPUTERNAME","local-node"),"cpu_utilization":cpu,
      "gpu_power_w":round(sum(g["power_w"] or 0 for g in gpus),1),"gpu_count":len(gpus)},
      "gpus":gpus,"pools":pools,"pool_metrics":pool_metrics,"timestamp":stamp()}


def pool_telemetry(window_seconds=900):
    snapshot=hardware_snapshot(); now=snapshot["timestamp"]
    with db() as conn:
        latest=conn.execute("SELECT MAX(created_at) FROM pool_telemetry").fetchone()[0] or 0
        if now-latest>=4:
            conn.executemany("INSERT INTO pool_telemetry(created_at,pool_id,domain,assigned_gpus,utilization,power_w,memory_used_mb,memory_total_mb,temperature_c) VALUES(?,?,?,?,?,?,?,?,?)",
              [(now,item["pool_id"],item["domain"],item["assigned_gpus"],item["utilization"],item["power_w"],item["memory_used_mb"],item["memory_total_mb"],item["temperature_c"]) for item in snapshot["pool_metrics"]])
        rows=conn.execute("SELECT created_at,pool_id,domain,assigned_gpus,utilization,power_w,memory_used_mb,memory_total_mb,temperature_c FROM pool_telemetry WHERE created_at>=? ORDER BY created_at",(now-max(60,min(window_seconds,86400)),)).fetchall()
    return {"current":snapshot,"history":[dict(row) for row in rows],"window_seconds":window_seconds}


def host_power_sensors():
    script=("$rows=@(); foreach($ns in @('root/LibreHardwareMonitor','root/OpenHardwareMonitor')) { "
      "try { $rows += Get-CimInstance -Namespace $ns -ClassName Sensor -ErrorAction Stop | Where-Object SensorType -eq 'Power' | Select-Object Name,Value } catch {} }; "
      "$rows | ConvertTo-Json -Compress")
    output=run_text(["powershell","-NoProfile","-Command",script],5)
    try: rows=json.loads(output) if output else []
    except json.JSONDecodeError: rows=[]
    if isinstance(rows,dict): rows=[rows]
    cpu_values=[float(row["Value"]) for row in rows if row.get("Value") is not None and re.search(r"cpu package|package|cpu cores",str(row.get("Name","")),re.I)]
    ram_values=[float(row["Value"]) for row in rows if row.get("Value") is not None and re.search(r"dram|memory|ram",str(row.get("Name","")),re.I)]
    return {"cpu_w":round(sum(cpu_values),2) if cpu_values else None,"ram_w":round(sum(ram_values),2) if ram_values else None,
      "source":"LibreHardwareMonitor/OpenHardwareMonitor WMI" if rows else None}


def system_resource_snapshot():
    cached=getattr(system_resource_snapshot,"_cache",None)
    if cached and time.monotonic()-cached[0]<1: return dict(cached[1])
    script=("$cpu=Get-CimInstance Win32_Processor; $os=Get-CimInstance Win32_OperatingSystem; "
      "[pscustomobject]@{cpu_load=($cpu|Measure-Object LoadPercentage -Average).Average; cores=($cpu|Measure-Object NumberOfLogicalProcessors -Sum).Sum; "
      "ram_total_kb=[double]$os.TotalVisibleMemorySize; ram_free_kb=[double]$os.FreePhysicalMemory} | ConvertTo-Json -Compress")
    output=run_text(["powershell","-NoProfile","-Command",script],6)
    try: data=json.loads(output) if output else {}
    except json.JSONDecodeError: data={}
    cpu=float(data.get("cpu_load") or 0); cores=max(1,float(data.get("cores") or 1)); total=float(data.get("ram_total_kb") or 0); used=max(0,total-float(data.get("ram_free_kb") or total))
    used_gb=used/1048576; total_gb=total/1048576
    result={"cpu_utilization":round(cpu,2),"ram_used_gb":round(used_gb,2),"ram_total_gb":round(total_gb,2),
      "estimated_cpu_w":round(max(35,cores*8)*(.15+.85*cpu/100),2),"estimated_ram_w":round(used_gb*.375,2),
      "estimation_method":"Host CPU load with logical-core power envelope; used RAM at 0.375 W/GB"}
    system_resource_snapshot._cache=(time.monotonic(),result); return dict(result)


def resource_window(start,end,duration,scope):
    average={key:round((float(start.get(key) or 0)+float(end.get(key) or 0))/2,2) for key in ("cpu_utilization","ram_used_gb","estimated_cpu_w","estimated_ram_w")}
    average.update({"ram_total_gb":end.get("ram_total_gb"),"estimated_cpu_energy_wh":round(average["estimated_cpu_w"]*duration/3600,4),
      "estimated_ram_energy_wh":round(average["estimated_ram_w"]*duration/3600,4),"resource_scope":scope,
      "resource_estimation_method":end.get("estimation_method")})
    return average


RUNTIMES, ACTIVE_ENDPOINTS = {}, {}


MODEL_ROLES=("available","reasoner","worker","embedding","reranker")
RUNNABLE_MODEL_ROLES=("reasoner","worker","embedding","reranker")
ROLE_PORTS={"reasoner":8001,"worker":8002,"embedding":8003,"reranker":8004}
RUNTIME_LOG_DIR = DB_PATH.parent / "runtime-logs"
UNCHANGED = object()


def running_pids(max_age=3.0):
    """Cached pid -> image name map, so polling the model list stays cheap."""
    cached=getattr(running_pids,"_cache",None)
    if cached and time.monotonic()-cached[0]<max_age: return cached[1]
    mapping={}
    if os.name=="nt":
        for row in csv.reader(io.StringIO(run_text(["tasklist","/FO","CSV","/NH"],8))):
            if len(row)>=2:
                try: mapping[int(row[1])]=row[0]
                except ValueError: continue
    running_pids._cache=(time.monotonic(),mapping)
    return mapping


def process_alive(pid, expected_name=None):
    """Verify the image name too: a recycled pid must never be mistaken for our runtime."""
    try: pid=int(pid)
    except (TypeError, ValueError): return False
    if pid<=0: return False
    if os.name=="nt":
        image=running_pids().get(pid)
        if not image: return False
        return expected_name.lower() in image.lower() if expected_name else True
    try: os.kill(pid, 0)
    except (OSError, ValueError): return False
    return True


def model_running(model):
    proc=RUNTIMES.get(model["id"])
    if proc and proc.poll() is None: return True
    runtime_name=Path(model["runtime_path"]).name if model["runtime_path"] else None
    return process_alive(model["pid"], runtime_name)


def reconciled_model_status(model):
    """The DB status column only changes on an explicit action (activate/stop); a runtime
    that crashed or was killed outside Skein must still be reported as stopped everywhere
    the status is shown, not just wherever happens to call model_running() itself."""
    running=model_running(model)
    status=model["status"]
    if running and status in ("STARTING","CONFIGURED","STOPPED"): return "RUNNING"
    if not running and status in ("STARTING","RUNNING"): return "STOPPED"
    return status


def terminate_pid(pid, expected_name=None):
    if not process_alive(pid, expected_name): return False
    if os.name=="nt":
        run_text(["taskkill","/PID",str(int(pid)),"/T","/F"],15); return True
    try:
        os.kill(int(pid), signal.SIGTERM); return True
    except OSError: return False


def runtime_log_path(model_id):
    return RUNTIME_LOG_DIR / f"{model_id}.log"


def runtime_log_tail(model_id, limit=4000):
    path=runtime_log_path(model_id)
    try: return path.read_text("utf-8","replace")[-limit:].strip()
    except OSError: return ""


def configure_model(model_id, role=None, pool_id=UNCHANGED):
    with db() as conn:
        model=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
        if not model: return {"error":"Model not found"},404
        role=str(role or model["role"]).strip().lower()
        if role not in MODEL_ROLES: return {"error":f"Unsupported model role '{role}'","action":f"Choose one of: {', '.join(MODEL_ROLES)}."},400
        if pool_id is UNCHANGED: effective_pool=model["pool_id"]
        else:
            effective_pool=str(pool_id).strip() or None if pool_id else None
            if effective_pool and not conn.execute("SELECT 1 FROM pools WHERE id=?",(effective_pool,)).fetchone():
                return {"error":"Pool not found"},404
        port=int(model["port"] or 0)
        if role not in RUNNABLE_MODEL_ROLES: port=0
        elif not port or role!=model["role"]:
            port=ROLE_PORTS[role]
            # Never drift onto another role's default port when this one is taken.
            occupied={row[0] for row in conn.execute("SELECT port FROM models WHERE id<>? AND port>0",(model_id,))}
            occupied|={value for key,value in ROLE_PORTS.items() if key!=role}
            while port in occupied: port+=1
        conn.execute("UPDATE models SET role=?,pool_id=?,port=?,updated_at=? WHERE id=?",(role,effective_pool,port,stamp(),model_id))
    return {"id":model_id,"role":role,"pool_id":effective_pool,"port":port},200


def activate_model(model_id, pool_id=None, role=None):
    """Start the runtime for one model. An empty pool keeps the stored assignment."""
    if role:
        configured,status=configure_model(model_id,role,pool_id if pool_id else UNCHANGED)
        if status!=200: return configured,status
    with db() as conn:
        model = conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    if not model: return {"error":"Model not found"}, 404
    if model["role"] not in RUNNABLE_MODEL_ROLES:
        return {"error":"Choose a model role before loading",
                "action":f"Select {', '.join(RUNNABLE_MODEL_ROLES)} for this model, then load it again."}, 400
    effective_pool = (pool_id or None) or model["pool_id"]
    with db() as conn:
        if effective_pool and not conn.execute("SELECT 1 FROM pools WHERE id=?",(effective_pool,)).fetchone():
            return {"error":"Pool not found"}, 404
        gpu_rows = conn.execute("SELECT gpu_id FROM gpu_assignments WHERE pool_id=?",(effective_pool,)).fetchall() if effective_pool else []
    if model_running(model):
        return {"error":"Model already active","action":"Stop this runtime before loading it again.","pid":model["pid"]}, 409
    runtime, model_path = Path(model["runtime_path"] or ""), Path(model["model_path"])
    error, pid, status = None, None, "CONFIGURED"
    if not model["runtime_path"]:
        error="No runtime executable is configured for this model; set its runtime path first."
    elif not runtime.is_file():
        error=f"Runtime executable not found: {runtime}"
    elif not (model_path.is_file() or model["backend"]=="vllm"):
        error=f"Weight file not found: {model_path}"
    else:
        env = os.environ.copy(); gpu_ids = [r["gpu_id"] for r in gpu_rows]
        indices = [str(g["index"]) for g in nvidia_gpus() if g["id"] in gpu_ids]
        if indices: env["CUDA_VISIBLE_DEVICES"] = ",".join(indices)
        if model["backend"] == "vllm":
            args = [str(runtime), "-m", "vllm.entrypoints.openai.api_server", "--model", model["model_path"],
                    "--host", "127.0.0.1", "--port", str(model["port"]), "--max-model-len", str(model["context_size"])]
        else:
            args = [str(runtime), "-m", str(model_path), "--host", "127.0.0.1", "--port", str(model["port"]), "-c", str(model["context_size"]), "-ngl", "999"]
        RUNTIME_LOG_DIR.mkdir(parents=True, exist_ok=True)
        try:
            # Keep the runtime output: a crashed llama-server must be diagnosable from the UI.
            with open(runtime_log_path(model_id),"wb") as handle:
                proc = subprocess.Popen(args, cwd=str(runtime.parent), env=env, stdout=handle,
                  stderr=subprocess.STDOUT, creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            RUNTIMES[model_id]=proc; pid=proc.pid; status="STARTING"
            try: proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired: pass
            if proc.poll() is not None:
                status, pid = "ERROR", None
                error=runtime_log_tail(model_id,2000) or f"The runtime exited immediately with code {proc.returncode}."
                RUNTIMES.pop(model_id,None)
        except OSError as exc: error=str(exc); status="ERROR"
    endpoint=f"http://127.0.0.1:{model['port']}/v1/chat/completions"
    if status == "STARTING": ACTIVE_ENDPOINTS[model["role"]] = endpoint
    with db() as conn:
        conn.execute("UPDATE models SET pool_id=?,status=?,pid=?,endpoint=?,last_error=?,updated_at=? WHERE id=?",
          (effective_pool,status,pid,endpoint,error,stamp(),model_id))
    payload={"id":model_id,"status":status,"pid":pid,"endpoint":endpoint,"pool_id":effective_pool,
             "gpu_indices":indices if status=="STARTING" else [],"error":error}
    # The role and pool are saved either way, but a runtime that did not start must never look like a success.
    if status=="STARTING":
        logger_models.info(f"activated id={model_id} name={model['name']!r} role={model['role']} pid={pid} pool={effective_pool}")
        return payload,200
    payload["action"]="The role and pool assignment was saved. Correct the runtime or weight path, then load again."
    logger_models.error(f"activation failed id={model_id} name={model['name']!r}: {error}")
    return payload,502


def stop_model(model_id):
    """Unload a runtime, including one that outlived a previous Skein process."""
    with db() as conn: model=conn.execute("SELECT * FROM models WHERE id=?",(model_id,)).fetchone()
    if not model: return {"error":"Model not found"},404
    terminated=False
    proc=RUNTIMES.pop(model_id,None)
    if proc and proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=10)
        except subprocess.TimeoutExpired: proc.kill()
        terminated=True
    elif model["pid"]:
        terminated=terminate_pid(model["pid"], Path(model["runtime_path"]).name if model["runtime_path"] else None)
    if ACTIVE_ENDPOINTS.get(model["role"])==model["endpoint"]: ACTIVE_ENDPOINTS.pop(model["role"],None)
    logger_models.info(f"stopped id={model_id} name={model['name']!r} terminated={terminated}")
    with db() as conn: conn.execute("UPDATE models SET status='STOPPED',pid=NULL,updated_at=? WHERE id=?",(stamp(),model_id))
    return {"id":model_id,"status":"STOPPED","terminated":terminated},200


def restore_active_endpoints():
    """Re-attach to runtimes that survived a Skein restart instead of reporting no model."""
    restored=[]
    with db() as conn:
        rows=conn.execute("SELECT * FROM models WHERE endpoint IS NOT NULL AND endpoint<>'' AND role<>'available'").fetchall()
    for row in rows:
        if row["role"] not in RUNNABLE_MODEL_ROLES: continue
        if endpoint_ready(row["endpoint"]):
            ACTIVE_ENDPOINTS.setdefault(row["role"], row["endpoint"])
            with db() as conn: conn.execute("UPDATE models SET status='RUNNING',updated_at=? WHERE id=?",(stamp(),row["id"]))
            restored.append({"id":row["id"],"role":row["role"],"endpoint":row["endpoint"]})
        elif row["status"] in ("STARTING","RUNNING"):
            with db() as conn: conn.execute("UPDATE models SET status='STOPPED',pid=NULL,updated_at=? WHERE id=?",(stamp(),row["id"]))
    return restored


HF_HOST = os.getenv("SKEIN_HF_ENDPOINT", "https://huggingface.co").rstrip("/")
HF_REPO_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*$")
WEIGHT_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\.gguf$", re.IGNORECASE)
MAX_UPLOAD_BYTES = int(float(os.getenv("SKEIN_MAX_UPLOAD_GB", "80")) * 1073741824)
DOWNLOAD_CHUNK = 1048576
DOWNLOADS, DOWNLOADS_LOCK = {}, threading.Lock()


def huggingface_token():
    return (os.getenv("SKEIN_HF_TOKEN") or os.getenv("HF_TOKEN") or os.getenv("HUGGING_FACE_HUB_TOKEN") or "").strip()


def huggingface_headers():
    headers={"User-Agent":"skein-model-manager"}
    token=huggingface_token()
    if token: headers["Authorization"]=f"Bearer {token}"
    return headers


def huggingface_api(path, query=None):
    url=f"{HF_HOST}{path}"+(f"?{urlencode(query)}" if query else "")
    try:
        with urlopen(Request(url, headers=huggingface_headers()), timeout=25) as response: return json.load(response),200
    except HTTPError as exc:
        if exc.code in (401,403):
            return {"error":"Hugging Face refused access to this repository",
                    "action":"Set SKEIN_HF_TOKEN (or HF_TOKEN) with an account that accepted the model licence.","details":f"HTTP {exc.code}"},exc.code
        if exc.code==404: return {"error":"Repository or file not found on Hugging Face","details":f"HTTP {exc.code}"},404
        return {"error":"Hugging Face returned an error","details":f"HTTP {exc.code}"},502
    except (URLError,TimeoutError,json.JSONDecodeError,ValueError) as exc:
        return {"error":"Hugging Face is unreachable","details":str(exc),
                "action":"Check network access or proxy configuration; Skein never downloads weights without an explicit request."},503


def huggingface_search(query, limit=20):
    query=str(query or "").strip()
    if not query: return {"error":"Enter a search term"},400
    payload,status=huggingface_api("/api/models",{"search":query,"filter":"gguf","sort":"downloads","direction":-1,
                                                 "limit":max(1,min(int(limit or 20),50))})
    if status!=200: return payload,status
    results=[{"repo":item.get("modelId") or item.get("id"),"downloads":item.get("downloads"),"likes":item.get("likes"),
              "updated_at":item.get("lastModified"),"gated":bool(item.get("gated")),
              "tags":[tag for tag in (item.get("tags") or []) if isinstance(tag,str)][:8]}
             for item in payload if isinstance(item,dict) and (item.get("modelId") or item.get("id"))]
    return {"query":query,"results":results,"authenticated":bool(huggingface_token())},200


def huggingface_repo_files(repo):
    repo=str(repo or "").strip().strip("/")
    if not HF_REPO_PATTERN.fullmatch(repo): return {"error":"Invalid repository identifier","action":"Use the owner/name form."},400
    payload,status=huggingface_api(f"/api/models/{repo}",{"blobs":"true"})
    if status!=200: return payload,status
    files=[]
    for sibling in payload.get("siblings") or []:
        name=str(sibling.get("rfilename") or "")
        if not name.lower().endswith(".gguf"): continue
        size=sibling.get("size") or (sibling.get("lfs") or {}).get("size")
        files.append({"filename":name,"size_bytes":size,"size_gb":round(size/1073741824,2) if size else None,
                      "quantization":detect_quantization(name),"downloadable":bool(WEIGHT_NAME_PATTERN.fullmatch(Path(name).name))})
    files.sort(key=lambda item:item["filename"])
    return {"repo":repo,"gated":bool(payload.get("gated")),"files":files,"authenticated":bool(huggingface_token())},200


def safe_weight_filename(name):
    """Reduce any client-supplied name to a bare .gguf file name inside the model library."""
    candidate=Path(str(name or "").replace("\\","/")).name
    return candidate if WEIGHT_NAME_PATTERN.fullmatch(candidate) else None


def safe_remote_weight_path(value):
    """Validate a repository-relative path segment by segment; `..` never reaches the request URL."""
    parts=[part for part in str(value or "").replace("\\","/").split("/") if part and part!="."]
    if not parts or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*",part) or part==".." for part in parts): return None
    return "/".join(parts) if WEIGHT_NAME_PATTERN.fullmatch(parts[-1]) else None


def download_snapshot(job):
    data={key:value for key,value in job.items() if key!="cancel"}
    total, received = data.get("total_bytes"), data.get("received_bytes") or 0
    data["progress"]=round(received/total,4) if total else None
    return data


def update_download(job_id, **fields):
    with DOWNLOADS_LOCK:
        job=DOWNLOADS.get(job_id)
        if job: job.update(fields); job["updated_at"]=stamp()


def list_downloads():
    with DOWNLOADS_LOCK: return [download_snapshot(job) for job in DOWNLOADS.values()]


def run_huggingface_download(job_id, repo, filename, target, cancel):
    partial=target.with_name(target.name+".part")
    received=partial.stat().st_size if partial.exists() else 0
    headers=huggingface_headers()
    if received: headers["Range"]=f"bytes={received}-"
    url=f"{HF_HOST}/{repo}/resolve/main/{quote(filename)}"
    try:
        with urlopen(Request(url, headers=headers), timeout=60) as response:
            if response.status!=206: received=0  # the server ignored our resume request
            declared=response.headers.get("Content-Length")
            total=(int(declared)+received) if declared and declared.isdigit() else None
            update_download(job_id,total_bytes=total,received_bytes=received)
            with open(partial,"ab" if received else "wb") as handle:
                while True:
                    if cancel.is_set():
                        update_download(job_id,status="CANCELLED"); return
                    chunk=response.read(DOWNLOAD_CHUNK)
                    if not chunk: break
                    handle.write(chunk); received+=len(chunk)
                    update_download(job_id,received_bytes=received)
        partial.replace(target)
        registered,status=register_model_file(target,"available")
        update_download(job_id,status="COMPLETED",received_bytes=received,
                        model_id=registered.get("id") if status==201 else None,
                        error=None if status==201 else registered.get("error"))
        logger_models.info(f"huggingface download completed job={job_id} repo={repo} file={filename} bytes={received}")
    except (OSError,URLError,TimeoutError,ValueError) as exc:
        update_download(job_id,status="FAILED",error=f"{type(exc).__name__}: {exc}")
        logger_models.error(f"huggingface download failed job={job_id} repo={repo} file={filename}: {exc}")


def start_huggingface_download(repo, filename):
    repo=str(repo or "").strip().strip("/")
    if not HF_REPO_PATTERN.fullmatch(repo): return {"error":"Invalid repository identifier","action":"Use the owner/name form."},400
    remote=safe_remote_weight_path(filename)
    if not remote: return {"error":"Invalid weight filename","action":"Only a .gguf path inside the repository can be downloaded."},400
    safe=safe_weight_filename(remote)
    target=model_library_dir()/f"{repo.replace('/','__')}__{safe}"
    if target.exists(): return {"error":"This weight file is already present locally","path":str(target)},409
    with DOWNLOADS_LOCK:
        if any(job["path"]==str(target) and job["status"]=="RUNNING" for job in DOWNLOADS.values()):
            return {"error":"This download is already running"},409
        job_id=str(uuid.uuid4()); cancel=threading.Event()
        DOWNLOADS[job_id]={"id":job_id,"kind":"huggingface","repo":repo,"filename":safe,"remote_path":remote,
          "path":str(target),"status":"RUNNING","received_bytes":0,"total_bytes":None,"error":None,
          "model_id":None,"started_at":stamp(),"updated_at":stamp(),"cancel":cancel}
        snapshot=download_snapshot(DOWNLOADS[job_id])
    logger_models.info(f"huggingface download started job={job_id} repo={repo} file={safe}")
    threading.Thread(target=run_huggingface_download,args=(job_id,repo,remote,target,cancel),daemon=True).start()
    return snapshot,202


def cancel_download(job_id):
    with DOWNLOADS_LOCK:
        job=DOWNLOADS.get(job_id)
        if not job: return {"error":"Download not found"},404
        if job["status"]!="RUNNING": return download_snapshot(job),200
        job["cancel"].set()
        return {**download_snapshot(job),"status":"CANCELLING"},202


def store_uploaded_weight(filename, stream, length):
    safe=safe_weight_filename(filename)
    if not safe: return {"error":"Invalid weight filename","action":"Send a bare .gguf file name with no path separator."},400
    if not length or length<=0: return {"error":"A Content-Length header is required for uploads"},411
    if length>MAX_UPLOAD_BYTES: return {"error":"Upload exceeds the configured size limit",
      "limit_bytes":MAX_UPLOAD_BYTES,"action":"Raise SKEIN_MAX_UPLOAD_GB or copy the file into a configured model root."},413
    target=model_library_dir()/safe
    if target.exists(): return {"error":"A weight file with this name already exists","path":str(target)},409
    partial=target.with_name(target.name+".part"); received=0
    try:
        with open(partial,"wb") as handle:
            while received<length:
                chunk=stream.read(min(DOWNLOAD_CHUNK,length-received))
                if not chunk: break
                handle.write(chunk); received+=len(chunk)
        if received!=length:
            partial.unlink(missing_ok=True)
            return {"error":"Upload was interrupted before completion","received_bytes":received,"expected_bytes":length},400
        partial.replace(target)
    except OSError as exc:
        partial.unlink(missing_ok=True)
        return {"error":"Could not store the uploaded weight file","details":str(exc)},500
    return register_model_file(target,"available")


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
    discovery=discover_local_models()
    with db() as conn:
        rows=conn.execute("SELECT * FROM models WHERE role IN ('reasoner','worker') ORDER BY updated_at DESC").fetchall()
    selected={}
    for row in rows: selected.setdefault(row["role"],row)
    missing=[r for r in ("reasoner","worker") if r not in selected]
    if missing: return {"error":"Local profiles not found","missing":missing,
      "action":"Use Discover models, or register a runtime and weight file manually.",
      "details":"\n".join(discovery.get("warnings") or [])},400
    started=[]
    def rollback():
        # A failure response must not leave half-loaded multi-GB runtimes running with no
        # owner: stop what this call started (409 = it was already running before us).
        for item in started:
            if item["fresh"]: stop_model(item["id"])
    for role,row in selected.items():
        result,status=activate_model(row["id"])
        if status not in (200,409):
            rollback()
            return result,status
        started.append({"role":role,"id":row["id"],"endpoint":row["endpoint"] or result.get("endpoint"),"fresh":status==200})
    deadline=time.time()+90
    while time.time()<deadline:
        if all(endpoint_ready(item["endpoint"]) for item in started): return {"status":"READY","models":started},200
        time.sleep(1)
    rollback()
    return {"error":"Runtimes did not reach the READY state","models":started,
            "action":"Check the runtime logs; very large weights can exceed the startup window — load them manually from the Models plane."},503


EVENT_LOG_LEVELS = {"task.failed": logging.ERROR, "workflow.failed": logging.ERROR,
                    "task.blocked": logging.WARNING, "task.retried": logging.WARNING, "task.escalated": logging.WARNING}


def emit(wid, kind, payload=None, tid=None):
    """Every workflow/task lifecycle transition already lands here for the events table the
    UI reads; log it too (in a finally, so a DB hiccup on the insert still leaves a record)
    rather than re-instrumenting orchestrate()/run_task() at each of their call sites."""
    payload = payload or {}
    try:
        with db() as conn:
            conn.execute("INSERT INTO events(workflow_id,task_id,kind,payload,created_at) VALUES(?,?,?,?,?)",
                         (wid, tid, kind, json.dumps(payload, ensure_ascii=False), stamp()))
    finally:
        detail = f" {json.dumps(payload, ensure_ascii=False)}" if payload else ""
        logger_workflow.log(EVENT_LOG_LEVELS.get(kind, logging.INFO),
                            f"{kind} workflow={wid}" + (f" task={tid}" if tid else "") + detail)


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
    "reviewer", "security-reviewer", "tester", "translator", "workflow-reporter",
}
WORKFLOW_ACTION_TYPES = {"llm", "command", "script"}
WORKFLOW_OUTPUT_FORMATS = {"text", "markdown", "json", "files", "exit_code", "boolean"}


def default_system_prompt(role):
    return f"You are the {role} for this workflow step. Complete only the assigned micro-task, use dependency results as evidence, and satisfy the declared output contract."


def normalize_task_contract(task):
    return {**task,
      "action_type":task.get("action_type","llm"),"action_config":task.get("action_config",{}),
      "system_prompt":task.get("system_prompt") or default_system_prompt(task.get("role","executor")),
      "output_format":task.get("output_format","markdown" if task.get("role")=="integrator" else "json"),
      "output_schema":task.get("output_schema") or ("A complete usable final answer" if task.get("role")=="integrator" else "Structured findings for dependent steps")}


def ensure_workflow_report_task(tasks,is_chat=False):
    normalized=[normalize_task_contract(task) for task in tasks]
    if is_chat or any(task.get("role")=="workflow-reporter" or task.get("key")=="workflow_report" for task in normalized): return normalized
    keys=[task["key"] for task in normalized]
    normalized.append({"key":"workflow_report","title":"Analyze execution logs and produce the workflow report","role":"workflow-reporter","dependencies":keys,
      "complexity":.35,"risk":.15,"criticality":.85,"action_type":"llm","action_config":{},
      "system_prompt":"You are the workflow execution auditor. Analyze every task result, event, command or script log, error, timing, token metric, and power metric. Produce a factual execution report. Distinguish measured data from estimates, identify failures and uncertainty, and never replace the user's main deliverable.",
      "output_format":"markdown","output_schema":"Markdown execution report with status, chronological step analysis, log and error analysis, performance and energy metrics, anomalies, and recommendations"})
    return normalized


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
            "action_type": "llm",
            "action_config": {},
            "system_prompt": default_system_prompt(role),
            "output_format": "markdown" if role == "integrator" else "json",
            "output_schema": "A complete usable final answer" if role == "integrator" else "Structured findings for dependent steps",
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
        action_type = str(task.get("action_type", "llm")).strip()
        output_format = str(task.get("output_format", "markdown")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_-]{1,63}", key): errors.append(f"Task {index + 1} has an invalid key")
        if not 3 <= len(title) <= 200: errors.append(f"Task {index + 1} title must contain 3 to 200 characters")
        if role not in WORKFLOW_ROLES: errors.append(f"Task {index + 1} has an unsupported role")
        if action_type not in WORKFLOW_ACTION_TYPES: errors.append(f"Task {index + 1} has an unsupported action type")
        if output_format not in WORKFLOW_OUTPUT_FORMATS: errors.append(f"Task {index + 1} has an unsupported output format")
        if not 3 <= len(str(task.get("output_schema", "")).strip()) <= 1000: errors.append(f"Task {index + 1} output schema must contain 3 to 1000 characters")
        config=task.get("action_config",{})
        if not isinstance(config,dict): errors.append(f"Task {index + 1} action config must be an object")
        elif action_type=="command" and not 1 <= len(str(config.get("command","")).strip()) <= 4000: errors.append(f"Task {index + 1} command is required")
        elif action_type=="script":
            if str(config.get("runtime","")) not in {"python","node","java","php"}: errors.append(f"Task {index + 1} script runtime is unsupported")
            if not 1 <= len(str(config.get("content",""))) <= 100000: errors.append(f"Task {index + 1} script content is required")
        if isinstance(config,dict) and config.get("condition") is not None:
            condition=config["condition"]
            if not isinstance(condition,dict): errors.append(f"Task {index + 1} condition must be an object")
            else:
                condition_type=str(condition.get("action_type",""))
                if condition_type not in WORKFLOW_ACTION_TYPES: errors.append(f"Task {index + 1} condition has an unsupported action type")
                if condition.get("output_format")!="boolean": errors.append(f"Task {index + 1} condition output format must be boolean")
                condition_config=condition.get("action_config",{})
                if not isinstance(condition_config,dict): errors.append(f"Task {index + 1} condition action config must be an object")
                elif condition_type=="command" and not str(condition_config.get("command","")).strip(): errors.append(f"Task {index + 1} condition command is required")
                elif condition_type=="script" and (condition_config.get("runtime") not in {"python","node","java","php"} or not str(condition_config.get("content","")).strip()): errors.append(f"Task {index + 1} condition script is incomplete")
                if condition_type=="llm" and len(str(condition.get("system_prompt","")).strip())<10: errors.append(f"Task {index + 1} condition LLM system prompt is required")
        if action_type=="llm" and not 10 <= len(str(task.get("system_prompt","")).strip()) <= 4000: errors.append(f"Task {index + 1} LLM system prompt must contain 10 to 4000 characters")
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
    elif tasks[keys.index(terminals[0])].get("role") not in ("integrator", "workflow-reporter"):
        errors.append("The terminal task must use the integrator or workflow-reporter role")
    # The orchestrator holds the workflow-reporter back until every other step is terminal,
    # so a task depending on it would wait forever and livelock the whole scheduler slot.
    reporter_keys = [task.get("key") for task in tasks if task.get("role") == "workflow-reporter"]
    if len(reporter_keys) > 1: errors.append("A workflow can hold at most one workflow-reporter task")
    elif reporter_keys and reporter_keys[0] in referenced:
        errors.append("No task may depend on the workflow-reporter: it always runs last")
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
    item = dict(row); item["tags"] = [tag for tag in item["tags"].split(",") if tag]; is_chat=item["id"]=="default-chat" or "chat" in item["tags"]
    item["tasks"] = ensure_workflow_report_task(json.loads(item["tasks"]),is_chat)
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
    raw_tasks = body.get("tasks")
    raw_tags = body.get("tags", [])
    chat_hint="chat" in ([tag.strip().lower() for tag in raw_tags.split(",")] if isinstance(raw_tags,str) else [str(tag).strip().lower() for tag in raw_tags])
    tasks=[] if not isinstance(raw_tasks,list) else ensure_workflow_report_task(raw_tasks,chat_hint)
    validation = validate_workflow_tasks(tasks)
    if not validation["valid"]: raise ValueError("; ".join(validation["errors"]))
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
    return [{**normalize_task_contract(task),"dependencies":[positions[key] for key in task["dependencies"]]} for task in tasks]


def create_workflow(objective,owner_id=None,session_id=None,specs=None,template_id=None,planning_mode="automatic",continued_from=None):
    wid, created = str(uuid.uuid4()), stamp()
    specs = specs or plan_for(objective)
    ids = [str(uuid.uuid4()) for _ in specs]
    with db() as conn:
        conn.execute("INSERT INTO workflows(id,objective,status,created_at,updated_at,owner_id,session_id,template_id,planning_mode,continued_from) VALUES(?,?,?,?,?,?,?,?,?,?)", (wid, objective, "READY", created, created,owner_id,session_id,template_id,planning_mode,continued_from))
        for pos, spec in enumerate(specs):
            if isinstance(spec,dict):
                title,role,deps=spec["title"],spec["role"],spec["dependencies"]
                complexity,risk,criticality=float(spec.get("complexity",.5)),float(spec.get("risk",.5)),float(spec.get("criticality",.5))
                contract=normalize_task_contract(spec)
            else:
                title, role, deps, complexity, risk, criticality = spec; contract=normalize_task_contract({"role":role})
            conn.execute("""INSERT INTO tasks(id,workflow_id,position,title,role,dependencies,
              complexity,risk,criticality,status,action_type,action_config,system_prompt,output_format,output_schema) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
               (ids[pos], wid, pos, title, role, json.dumps([ids[i] for i in deps]),
               complexity, risk, criticality, "READY",contract["action_type"],json.dumps(contract["action_config"],ensure_ascii=False),contract["system_prompt"],contract["output_format"],contract["output_schema"]))
    workflow_storage_root(wid,owner_id,session_id).mkdir(parents=True,exist_ok=True)
    emit(wid, "workflow.created", {"objective": objective, "tasks": len(specs), "template_id": template_id, "planning_mode": planning_mode})
    return wid


def route(task):
    score = round(.40*task["complexity"] + .30*task["risk"] + .30*task["criticality"], 3)
    if task["role"] in ("coder","tester","translator","executor") and task["risk"] < .90:
        return "worker-general", score
    if score >= .68 or task["role"] in ("architect", "security-reviewer", "integrator", "workflow-reporter"):
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
        context_limit=48000 if task["role"]=="workflow-reporter" else 12000
        previous=json.dumps(dependency_results,ensure_ascii=False)[:context_limit]
        role_instruction = {
          "translator":"Perform the translation. deliverable must contain the final translated text.",
          "coder":"Write the actual code. files must contain every complete file with path and content.",
          "tester":"Verify previous files and include only required test files or corrections in files.",
          "integrator":"Produce the final usable answer in deliverable. Do not replace the result with a plan.",
          "executor":"Execute the request and place the complete result in deliverable.",
          "reviewer":"Verify the previous result, correct it, and place the corrected version in deliverable.",
          "workflow-reporter":"Analyze the supplied task results and execution logs. Produce only the factual Markdown execution report in deliverable; do not replace or rewrite the user's main deliverable.",
        }.get(task["role"],"Complete the task concretely instead of only explaining how to do it.")
        system_prompt=str(task.get("system_prompt") or default_system_prompt(task["role"]))+"\nExpected output format: "+str(task.get("output_format") or "markdown")+". Output contract: "+str(task.get("output_schema") or "Complete the assigned task.")
        prompt = ("/no_think\n"+("REPAIR ATTEMPT: the previous JSON was invalid. Verify every quote and escape sequence.\n" if retry else "")+role_instruction+"\nReturn only one JSON object with exactly these fields: "
          "summary (short), deliverable (complete usable result), files (list of {path, content}), "
          "confidence (0..1), assumptions, evidence, next_actions. Lists other than files contain at most 3 items. "
          "Do not wrap JSON in Markdown. Never claim that a file exists: include its complete content in files.\n"
          "USER OBJECTIVE:\n"+objective+"\nCURRENT TASK:\n"+task["title"]+"\nDEPENDENCY RESULTS:\n"+previous)
        body = json.dumps({"model": self.model, "messages": [{"role":"system","content":system_prompt},{"role":"user","content":prompt}],
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
        except (OSError, TimeoutError, KeyError, IndexError, json.JSONDecodeError) as exc:
            # OSError covers URLError plus the raw socket errors (ConnectionResetError…) a
            # dying runtime produces mid-read; IndexError covers an empty choices array.
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
          "tasks contains 1 to 12 objects with key, title, role, dependencies, complexity, risk, criticality, action_type, action_config, system_prompt, output_format, and output_schema. "
          "Every action_type is llm, command, or script. LLM steps require a task-specific system_prompt. Command steps require action_config.command. Script steps require action_config.runtime and action_config.content. "
          "Every step declares output_format (text, markdown, json, files, exit_code, or boolean) and a precise output_schema. An optional action_config.condition uses the same action fields and must declare boolean output. "
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


TASK_WORKER_LIMIT=max(1,int(os.getenv("SKEIN_TASK_WORKERS","4")))
POOL = ThreadPoolExecutor(max_workers=TASK_WORKER_LIMIT, thread_name_prefix="skein-worker")
MAX_PARALLEL_WORKFLOWS=max(1,int(os.getenv("SKEIN_MAX_PARALLEL_WORKFLOWS","2")))
ACTIVE, WORKFLOW_QUEUE, ACTIVE_LOCK = set(), deque(), threading.Lock()


class PowerSampler:
    """Best-effort GPU power attribution for one task; shared GPU load remains an estimate."""
    def __init__(self): self.samples=[]; self.stop_event=threading.Event(); self.thread=None; self.resources=None
    def start(self):
        self.resources=system_resource_snapshot()
        def sample():
            while not self.stop_event.is_set():
                watts=sum(float(g.get("power_w") or 0) for g in nvidia_gpus())
                if watts>0: self.samples.append((time.monotonic(),watts))
                self.stop_event.wait(.5)
        self.thread=threading.Thread(target=sample,daemon=True); self.thread.start(); return self
    def stop(self,duration,scope="model_runtime"):
        self.stop_event.set()
        if self.thread: self.thread.join(2)
        values=[x[1] for x in self.samples]
        avg=sum(values)/len(values) if values else 0
        return {"average_power_w":round(avg,2),"average_gpu_power_w":round(avg,2),"peak_power_w":round(max(values,default=0),2),
          "energy_wh":round(avg*duration/3600,4),"power_samples":len(values),
          "energy_method":"measured_nvidia_smi_task_window",**resource_window(self.resources,system_resource_snapshot(),duration,scope)}


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


def run_local_captured(args,cwd,timeout_s):
    """Local-mode execution with a whole-tree timeout. subprocess.run() only terminates the
    direct child on timeout, so anything the command itself spawned (javac/java behind the
    PowerShell wrapper, servers, ping -t) would keep running on the host — and a grandchild
    holding the inherited pipes would even keep the caller blocked past the timeout."""
    proc=subprocess.Popen(args,cwd=cwd,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
      creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
    try:
        stdout,stderr=proc.communicate(timeout=timeout_s)
        return proc.returncode,stdout,stderr,False
    except subprocess.TimeoutExpired:
        if os.name=="nt":
            subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],capture_output=True,
              creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        else: proc.kill()
        try: stdout,stderr=proc.communicate(timeout=5)
        except Exception: stdout,stderr="",""
        return None,stdout or "",stderr or "",True


def execute_in_sandbox(artifact_id,timeout=20,mode=None):
    mode=mode or EXECUTION_MODE
    with db() as conn: artifact=conn.execute("SELECT * FROM artifacts WHERE id=?",(artifact_id,)).fetchone()
    if not artifact: return {"error":"artifact introuvable"},404
    runtime=runtime_for(artifact["relative_path"])
    if not runtime: return {"error":"runtime non supporté","extension":Path(artifact["relative_path"]).suffix},400
    eid=str(uuid.uuid4()); cfg=SANDBOXES[runtime]; started=time.time(); resource_start=system_resource_snapshot()
    if runtime=="html":
        result={"id":eid,"status":"PREVIEW_READY","runtime":runtime,"exit_code":0,"stdout":"Aperçu isolé disponible","stderr":"","duration":0}
    elif mode=="local":
        target=Path(artifact["disk_path"]); command=local_runtime_command(runtime,target)
        if not command: result={"id":eid,"status":"UNAVAILABLE","runtime":runtime,"exit_code":None,"stdout":"","stderr":f"Runtime local indisponible: {runtime}","duration":0}
        else:
            code,stdout,stderr,timed_out=run_local_captured(command,target.parent,max(1,min(int(timeout),60)))
            if timed_out: result={"id":eid,"status":"TIMEOUT","runtime":runtime,"exit_code":None,
              "stdout":stdout[-20000:],"stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
            else: result={"id":eid,"status":"PASS" if code==0 else "FAIL","runtime":runtime,"exit_code":code,
              "stdout":stdout[-20000:],"stderr":stderr[-20000:],"duration":round(time.time()-started,3)}
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
    result["resources"]=resource_window(resource_start,system_resource_snapshot(),float(result.get("duration") or 0),"local_machine" if mode=="local" else "docker_container_host_window")
    with db() as conn: conn.execute("INSERT INTO executions VALUES(?,?,?,?,?,?,?,?,?,?,?)",
      (eid,artifact["workflow_id"],artifact_id,runtime,cfg["image"] if mode=="sandbox" else "LOCAL",result["status"],result["exit_code"],result["stdout"],result["stderr"],result["duration"],stamp()))
    return result,200


def execute_command(wid,command,timeout=20,mode=None):
    mode=mode or EXECUTION_MODE; root=artifact_root(wid); eid=str(uuid.uuid4()); started=time.time(); resource_start=system_resource_snapshot()
    if not command or len(command)>4000: return {"error":"commande vide ou trop longue"},400
    if mode=="local":
        image="LOCAL"
        code,stdout,stderr,timed_out=run_local_captured(["powershell","-NoProfile","-Command",command],root,max(1,min(int(timeout),60)))
        if timed_out: result={"id":eid,"status":"TIMEOUT","mode":mode,"exit_code":None,
          "stdout":stdout[-20000:],"stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
        else: result={"id":eid,"status":"PASS" if code==0 else "FAIL","mode":mode,"exit_code":code,
          "stdout":stdout[-20000:],"stderr":stderr[-20000:],"duration":round(time.time()-started,3)}
    else:
        image="alpine:3.20"
        if not docker_image_ready(image): return {"error":"image de terminal sandbox absente","image":image},503
        scratch=Path(tempfile.mkdtemp(prefix="skein-shell-")); shutil.copytree(root,scratch/"workspace",dirs_exist_ok=True)
        name="skein-shell-"+eid[:10]; args=[shutil.which("docker"),"run","--rm","--name",name,"--network","none","--cpus","1","--memory","256m",
          "--pids-limit","64","--read-only","--tmpfs","/tmp:rw,nosuid,size=64m","-v",f"{scratch/'workspace'}:/workspace:rw","-w","/workspace",image,"sh","-lc",command]
        try:
            proc=subprocess.run(args,cwd=ROOT,capture_output=True,text=True,timeout=max(1,min(int(timeout),60)),creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result={"id":eid,"status":"PASS" if proc.returncode==0 else "FAIL","mode":mode,"exit_code":proc.returncode,
              "stdout":proc.stdout[-20000:],"stderr":proc.stderr[-20000:],"duration":round(time.time()-started,3)}
        except subprocess.TimeoutExpired:
            subprocess.run([shutil.which("docker"),"rm","-f",name],capture_output=True,creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            result={"id":eid,"status":"TIMEOUT","mode":mode,"exit_code":None,"stdout":"","stderr":"Limite de temps dépassée","duration":round(time.time()-started,3)}
        finally:
            shutil.rmtree(scratch,ignore_errors=True)
    result["resources"]=resource_window(resource_start,system_resource_snapshot(),float(result.get("duration") or 0),"local_machine" if mode=="local" else "docker_container_host_window")
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


def evaluate_task_condition(wid,task,objective,dependency_results):
    condition=json.loads(task["action_config"] or "{}").get("condition")
    if not condition: return True,None
    action_type=condition["action_type"]
    if action_type=="command":
        execution,_=execute_command(wid,condition["action_config"]["command"],60,EXECUTION_MODE)
        return execution.get("status")=="PASS",execution
    if action_type=="script":
        config=condition["action_config"]; extensions={"python":"py","node":"js","java":"java","php":"php"}; runtime=config["runtime"]
        artifacts=persist_artifacts(wid,task["id"],[{"path":f"workflow-conditions/{task['position']+1:02d}.{extensions[runtime]}","content":config["content"]}])
        if not artifacts: return False,{"status":"FAIL","stderr":"Condition script artifact could not be created"}
        execution,_=execute_in_sandbox(artifacts[0]["id"],60,EXECUTION_MODE)
        return execution.get("status")=="PASS",execution
    condition_task={**task,"role":"analyst","system_prompt":condition["system_prompt"],"output_format":"boolean","output_schema":condition.get("output_schema","Return true when the step should run, otherwise false")}
    result=ModelClient("reasoner-large").generate(condition_task,objective,dependency_results)
    value=str(result.get("deliverable") or result.get("summary") or "").strip().lower()
    return value.startswith(("true","yes","oui","1")),result


def format_execution_report(auditor_result,dependency_results):
    task_results=[item for item in dependency_results if item.get("task")!="Workflow events and execution logs"]
    log_context=next((item.get("result",{}) for item in dependency_results if item.get("task")=="Workflow events and execution logs"),{})
    failed=[]; lines=["# Workflow Execution Report","","## Status and step analysis",""]
    total_tokens=0; total_seconds=0.0; energy_wh=0.0
    for index,item in enumerate(task_results,1):
        result=item.get("result") or {}; metrics=result.get("metrics") or {}; execution=result.get("execution") or {}
        status="FAILED" if result.get("error") or execution.get("status") in ("FAIL","TIMEOUT") or result.get("mode")=="blocked" else "COMPLETED"
        if status=="FAILED": failed.append(item.get("task","Unknown task"))
        total_tokens+=int(metrics.get("total_tokens") or 0); total_seconds+=float(metrics.get("execution_seconds") or 0); energy_wh+=float(metrics.get("energy_wh") or 0)
        detail=result.get("summary") or result.get("error") or execution.get("stderr") or "No summary was returned."
        lines.extend([f"### {index}. {item.get('task','Unknown task')}",f"- Status: {status}",f"- Mode: {result.get('mode','unknown')}",f"- Summary: {detail}"])
        if execution:
            lines.append(f"- Execution: {execution.get('status','unknown')}; exit code {execution.get('exit_code')}; {execution.get('duration',0)} seconds")
        lines.append("")
    events=log_context.get("events") or []; executions=log_context.get("executions") or []
    lines.extend(["## Log and error analysis","",f"- Events analyzed: {len(events)}",f"- Command or script executions analyzed: {len(executions)}",f"- Failed or blocked tasks: {', '.join(failed) if failed else 'none'}"])
    for execution in executions:
        if execution.get("stderr"):
            lines.append(f"- `{execution.get('runtime','runtime')}` stderr: {str(execution['stderr'])[-1000:]}")
    lines.extend(["","## Performance and energy metrics","",f"- Total tokens before reporting: {total_tokens}",f"- Aggregated task execution time: {round(total_seconds,3)} seconds",f"- Estimated energy before reporting: {round(energy_wh,4)} Wh","- GPU power is measured when nvidia-smi is available; CPU and RAM power may be host-window estimates.","","## Auditor analysis","",str(auditor_result.get("deliverable") or auditor_result.get("summary") or "The auditor model returned no narrative analysis."),"","## Anomalies and recommendations",""])
    recommendations=auditor_result.get("next_actions") or (["Inspect failed task output and retry after correction."] if failed else ["No blocking anomaly was detected."])
    lines.extend(f"- {recommendation}" for recommendation in recommendations)
    return "\n".join(lines)


CONFIDENCE_WORDS={"certain":1.0,"very high":.95,"high":.85,"good":.8,"medium":.6,"moderate":.6,"average":.6,"fair":.55,"low":.35,"very low":.2,"none":0.0}
MAX_TASK_ATTEMPTS=max(1,int(os.getenv("SKEIN_MAX_TASK_ATTEMPTS","2")))
TERMINAL_TASK_STATES=("COMPLETED","FAILED","BLOCKED")


def parse_confidence(value):
    """Return a 0..1 self-reported confidence, or None when the model gave none.

    Confidence is reporting metadata, never a success criterion: an absent or
    unparsable value means "unknown" and must not be collapsed to zero.
    """
    if isinstance(value,bool): return 1.0 if value else 0.0
    if isinstance(value,(int,float)): number=float(value)
    elif isinstance(value,str):
        text=value.strip().lower()
        if not text: return None
        try: number=float(text.rstrip("%").replace(",",".").strip())
        except ValueError: return CONFIDENCE_WORDS.get(text)
        if text.endswith("%"): number/=100
    else: return None
    if number!=number or number in (float("inf"),float("-inf")): return None
    if number>1: number/=100
    return round(min(1.0,max(0.0,number)),3)


def llm_result_usable(result):
    """A task succeeded when it produced content, independently of its confidence."""
    if not isinstance(result,dict): return False
    if result.get("error") or result.get("mode")=="error": return False
    return bool(str(result.get("deliverable") or result.get("summary") or "").strip() or result.get("files"))


def execute_task(wid, tid, objective):
    """Run one task. Never raises: a crash is recorded as a failed task so that
    the orchestrator keeps scheduling the independent branches of the DAG."""
    try:
        return run_task(wid, tid, objective)
    except Exception as exc:
        detail=f"{type(exc).__name__}: {exc}"
        result={"summary":"Task crashed during execution","deliverable":f"The orchestrator could not complete this task. {detail}","files":[],
          "confidence":None,"assumptions":[],"evidence":[detail],"next_actions":["Inspect the Skein server log, then relaunch the workflow"],"mode":"error","error":detail}
        with db() as conn:
            # A crash before run_task claimed the task left the counter untouched; advance it here
            # so a task that always crashes cannot be retried forever.
            conn.execute("UPDATE tasks SET attempts=attempts+1 WHERE id=? AND status='READY'",(tid,))
            conn.execute("UPDATE tasks SET status='FAILED',confidence=NULL,result=?,finished_at=? WHERE id=?",
                         (json.dumps(result,ensure_ascii=False),stamp(),tid))
        emit(wid,"task.failed",{"error":detail},tid)


def run_task(wid, tid, objective):
    with db() as conn:
        task = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        dependency_results=[]
        for dep in json.loads(task["dependencies"]):
            row=conn.execute("SELECT title,result FROM tasks WHERE id=?",(dep,)).fetchone()
            if row and row["result"]: dependency_results.append({"task":row["title"],"result":json.loads(row["result"])})
        if task["role"]=="workflow-reporter":
            event_rows=conn.execute("SELECT task_id,kind,payload,created_at FROM events WHERE workflow_id=? ORDER BY id LIMIT 200",(wid,)).fetchall()
            execution_rows=conn.execute("SELECT artifact_id,runtime,status,exit_code,stdout,stderr,duration,created_at FROM executions WHERE workflow_id=? ORDER BY created_at LIMIT 100",(wid,)).fetchall()
            dependency_results.append({"task":"Workflow events and execution logs","result":{
              "events":[{**dict(row),"payload":json.loads(row["payload"])} for row in event_rows],
              "executions":[{**dict(row),"stdout":str(row["stdout"] or "")[-4000:],"stderr":str(row["stderr"] or "")[-4000:]} for row in execution_rows]}})
        model, score = route(task) if task["action_type"]=="llm" else (task["action_type"],0)
        conn.execute("UPDATE tasks SET status='RUNNING',model=?,routing_score=?,attempts=attempts+1,started_at=? WHERE id=?",
                     (model, score, stamp(), tid))
    emit(wid, "task.started", {"model": model, "routing_score": score}, tid)
    task_started=time.perf_counter(); power=PowerSampler().start()
    scope="local_machine" if task["action_type"] in ("command","script") and EXECUTION_MODE=="local" else ("docker_container_host_window" if task["action_type"] in ("command","script") else "model_runtime")
    try:
        condition_passed,condition_result=evaluate_task_condition(wid,task,objective,dependency_results)
        if not condition_passed:
            result={"summary":"Task skipped because its condition evaluated to false","deliverable":"Condition false; the task action was not executed.","files":[],"confidence":1.0,"assumptions":[],"evidence":[condition_result],"next_actions":[],"mode":"condition"}
            usable=True
        elif task["action_type"]=="command":
            execution,_=execute_command(wid,json.loads(task["action_config"]).get("command",""),60,EXECUTION_MODE)
            passed=execution.get("status")=="PASS"
            result={"summary":f"Command {execution.get('status','FAILED')}","deliverable":execution.get("stdout") or execution.get("stderr") or "No output","files":[],"confidence":1.0 if passed else 0.0,"assumptions":[],"evidence":[f"Exit code: {execution.get('exit_code')}"],"next_actions":[] if passed else ["Inspect stderr and correct the command"],"mode":"command","execution":execution}
            usable=passed
        elif task["action_type"]=="script":
            config=json.loads(task["action_config"]); extensions={"python":"py","node":"js","java":"java","php":"php"}; runtime=config.get("runtime"); path=config.get("path") or f"workflow-scripts/{task['position']+1:02d}-{task['id'][:8]}.{extensions.get(runtime,'txt')}"
            artifacts=persist_artifacts(wid,tid,[{"path":path,"content":config.get("content","")}]); execution={"status":"FAIL","stderr":"Script artifact could not be created"}
            if artifacts: execution,_=execute_in_sandbox(artifacts[0]["id"],60,EXECUTION_MODE)
            passed=execution.get("status") in ("PASS","PREVIEW_READY")
            result={"summary":f"{runtime} script {execution.get('status','FAILED')}","deliverable":execution.get("stdout") or execution.get("stderr") or "No output","files":[],"confidence":1.0 if passed else 0.0,"assumptions":[],"evidence":[f"Exit code: {execution.get('exit_code')}"],"next_actions":[] if passed else ["Inspect stderr and correct the script"],"mode":"script","execution":execution,"artifacts":artifacts}
            usable=passed
        else:
            result = ModelClient(model).generate(task, objective, dependency_results)
            if task["role"]=="workflow-reporter" and not result.get("error"):
                result["deliverable"]=format_execution_report(result,dependency_results)
                result["summary"]="Workflow execution report generated from task results, events, and execution logs"
            usable=llm_result_usable(result)
        confidence=parse_confidence(result.get("confidence"))
        # A low or unknown confidence escalates to the reasoner; it never fails the task on its own.
        if condition_passed and task["action_type"]=="llm" and model=="worker-general" and (not usable or (confidence is not None and confidence<.65)):
            emit(wid, "task.escalated", {"from": model, "confidence": confidence, "usable": usable}, tid)
            retried=ModelClient("reasoner-large").generate(task, objective, dependency_results)
            if task["role"]=="workflow-reporter" and not retried.get("error"):
                retried["deliverable"]=format_execution_report(retried,dependency_results)
                retried["summary"]="Workflow execution report generated from task results, events, and execution logs"
            retried_usable=llm_result_usable(retried)
            if retried_usable or not usable:
                model,result,usable,confidence="reasoner-large",retried,retried_usable,parse_confidence(retried.get("confidence"))
        duration=max(.001,time.perf_counter()-task_started)
    finally:
        # every exit, including a crashing action, must stop the sampler thread — it
        # would otherwise keep spawning an nvidia-smi subprocess twice a second forever
        power_metrics=power.stop(max(.001,time.perf_counter()-task_started),scope)
    metrics=result.setdefault("metrics",{})
    metrics.update({"execution_seconds":round(duration,3),**power_metrics})
    if task["action_type"]!="script": result["artifacts"]=persist_artifacts(wid,tid,result.get("files",[])) if result.get("files") else []
    result["confidence"]=confidence
    status = "COMPLETED" if usable else "FAILED"
    with db() as conn:
        conn.execute("UPDATE tasks SET status=?,model=?,confidence=?,result=?,finished_at=? WHERE id=?",
                     (status, model, confidence, json.dumps(result, ensure_ascii=False), stamp(), tid))
    emit(wid, "task.completed" if status == "COMPLETED" else "task.failed",
         {"model": model, "confidence": confidence, "error": result.get("error")}, tid)


def blocked_task_result(reasons):
    return {"summary":"Task blocked by an unmet dependency","deliverable":"Not executed because a dependency of this task did not complete.","files":[],
      "confidence":None,"assumptions":[],"evidence":reasons,"next_actions":["Inspect the execution report and the failed dependency logs"],"mode":"blocked"}


def orchestrate(wid):
    """Drive one workflow DAG to completion.

    A failed task only propagates to its own descendants; independent branches
    keep running, and the workflow-reporter always runs last so the audit trail
    covers what succeeded as well as what did not.
    """
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
            reporter = next((t for t in tasks if t["role"]=="workflow-reporter"), None)
            if reporter and any(reporter["id"] in json.loads(t["dependencies"]) for t in tasks):
                # Rows predating the no-dependents-on-reporter validation: schedule the
                # reporter as a plain step, or its dependents would wait forever.
                reporter = None
            steps = [t for t in tasks if not (reporter and t["id"]==reporter["id"])]

            # Over tasks, not steps: the reporter is an llm task facing the same transient
            # backend errors as any other, and deserves the same second attempt.
            retryable = [t for t in tasks if t["status"]=="FAILED" and t["action_type"]=="llm" and t["attempts"]<MAX_TASK_ATTEMPTS]
            if retryable:
                with db() as conn:
                    conn.executemany("UPDATE tasks SET status='READY',finished_at=NULL WHERE id=?", [(t["id"],) for t in retryable])
                for t in retryable: emit(wid,"task.retried",{"attempt":t["attempts"]+1,"max_attempts":MAX_TASK_ATTEMPTS},t["id"])
                continue

            broken = {t["id"] for t in steps if t["status"] in ("FAILED","BLOCKED")}
            newly_blocked = []
            for t in steps:
                if t["status"]!="READY": continue
                deps = json.loads(t["dependencies"])
                reasons = [f"Unknown dependency {d}" for d in deps if d not in state]
                reasons += [f"Dependency '{next((o['title'] for o in tasks if o['id']==d), d)}' did not complete" for d in deps if d in broken]
                if reasons: newly_blocked.append((t["id"], reasons))
            if newly_blocked:
                with db() as conn:
                    conn.executemany("UPDATE tasks SET status='BLOCKED',confidence=NULL,result=?,finished_at=? WHERE id=?",
                                     [(json.dumps(blocked_task_result(reasons),ensure_ascii=False),stamp(),tid) for tid,reasons in newly_blocked])
                for tid,reasons in newly_blocked: emit(wid,"task.blocked",{"reasons":reasons},tid)
                continue

            ready = [t for t in steps if t["status"]=="READY" and all(state.get(d)=="COMPLETED" for d in json.loads(t["dependencies"]))]
            if ready:
                futures = [POOL.submit(execute_task, wid, t["id"], wf["objective"]) for t in ready]
                for future in futures: future.result()
                continue
            if any(t["status"] not in TERMINAL_TASK_STATES for t in steps):
                time.sleep(.15); continue
            if reporter and reporter["status"]=="READY":
                execute_task(wid, reporter["id"], wf["objective"]); continue
            if reporter and reporter["status"] not in TERMINAL_TASK_STATES:
                time.sleep(.15); continue

            unfinished = [t for t in steps if t["status"] in ("FAILED","BLOCKED")]
            status = "FAILED" if unfinished else "COMPLETED"
            emit(wid, "workflow.completed" if status=="COMPLETED" else "workflow.failed",
                 {"completed_tasks":sum(1 for t in steps if t["status"]=="COMPLETED"),"unfinished_tasks":len(unfinished),
                  "reporter_failed":bool(reporter and reporter["status"]=="FAILED")})
            with db() as conn: conn.execute("UPDATE workflows SET status=?,updated_at=? WHERE id=?", (status,stamp(),wid))
            return
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
        # QUEUED must be written before the id becomes visible in the queue: once appended,
        # a concurrent dispatch (another start, or an orchestrate winding down) can start
        # orchestrate, and its RUNNING write must not be overwritten by a late QUEUED.
        with db() as conn: conn.execute("UPDATE workflows SET status='QUEUED',updated_at=? WHERE id=?",(stamp(),wid))
        WORKFLOW_QUEUE.append(wid); position=len(WORKFLOW_QUEUE)
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
    report_task=next((task for task in reversed(completed) if task["role"]=="workflow-reporter"),None)
    deliverable_tasks=[task for task in completed if task["role"]!="workflow-reporter"]
    final=deliverable_tasks[-1]["result"] if deliverable_tasks else None
    execution_report=report_task["result"] if report_task else None
    task_metrics=[t["result"].get("metrics",{}) for t in out if t.get("result")]
    total_tokens=sum(int(m.get("total_tokens") or 0) for m in task_metrics)
    completion_tokens=sum(int(m.get("completion_tokens") or 0) for m in task_metrics)
    execution_seconds=sum(float(m.get("execution_seconds") or 0) for m in task_metrics)
    workflow_seconds=max(0,float(wf["updated_at"])-(float(wf["created_at"])))
    summary={"task_count":len(out),"completed_tasks":len(completed),
      "failed_tasks":sum(1 for t in out if t["status"]=="FAILED"),
      "blocked_tasks":sum(1 for t in out if t["status"]=="BLOCKED"),"total_tokens":total_tokens,
      "prompt_tokens":sum(int(m.get("prompt_tokens") or 0) for m in task_metrics),
      "completion_tokens":completion_tokens,"execution_seconds":round(execution_seconds,3),
      "wall_clock_seconds":round(workflow_seconds,3),
      "average_tokens_per_second":round(completion_tokens/execution_seconds,2) if execution_seconds else 0,
      "average_power_w":round(sum(float(m.get("average_power_w") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "estimated_cpu_power_w":round(sum(float(m.get("estimated_cpu_w") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "estimated_ram_power_w":round(sum(float(m.get("estimated_ram_w") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "average_cpu_utilization":round(sum(float(m.get("cpu_utilization") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "average_ram_used_gb":round(sum(float(m.get("ram_used_gb") or 0)*float(m.get("execution_seconds") or 0) for m in task_metrics)/execution_seconds,2) if execution_seconds else 0,
      "peak_power_w":round(max([float(m.get("peak_power_w") or 0) for m in task_metrics] or [0]),2),
      "energy_wh":round(sum(float(m.get("energy_wh") or 0) for m in task_metrics),4),
      "energy_note":"GPU power is measured from nvidia-smi. CPU and RAM watts are host-window estimates; Docker attribution is not a direct container power measurement."}
    workflow=dict(wf)
    with ACTIVE_LOCK:
        workflow["queue_position"]=(list(WORKFLOW_QUEUE).index(wid)+1) if wid in WORKFLOW_QUEUE else None
        workflow["parallel_limit"]=MAX_PARALLEL_WORKFLOWS
    return {"workflow":workflow,"tasks":out,"events":ev,"final_output":final,"execution_report":execution_report,"artifacts":artifacts,"summary":summary,
            "deliverable":{"kind":"none" if not artifacts else ("file" if len(artifacts)==1 else "project"),"file_count":len(artifacts),"archive_url":f"/api/workflows/{wid}/deliverable.zip" if artifacts else None},
            "artifact_notice":f"{len(artifacts)} file(s) produced and validated." if artifacts else "No file was required or produced for this request."}


def runtime_overview():
    endpoints={"reasoner":os.getenv("SKEIN_REASONER_URL") or ACTIVE_ENDPOINTS.get("reasoner", ""),"worker":os.getenv("SKEIN_WORKER_URL") or ACTIVE_ENDPOINTS.get("worker", "")}
    with db() as conn:
        running=[dict(row) for row in conn.execute("SELECT role,model FROM tasks WHERE status='RUNNING'")]
        waiting=[dict(row) for row in conn.execute("SELECT t.* FROM tasks t JOIN workflows w ON w.id=t.workflow_id WHERE t.status='READY' AND w.status IN ('QUEUED','RUNNING')")]
        completed=conn.execute("SELECT model,result,started_at,finished_at FROM tasks WHERE status='COMPLETED' AND result IS NOT NULL ORDER BY finished_at DESC LIMIT 100").fetchall()
    roles={"reasoner":{"active":0,"waiting":0},"worker":{"active":0,"waiting":0}}
    for task in running:
        tier="reasoner" if task.get("model")=="reasoner-large" else "worker"; roles[tier]["active"]+=1
    for task in waiting:
        tier="reasoner" if route(task)[0]=="reasoner-large" else "worker"; roles[tier]["waiting"]+=1
    aggregates={"reasoner":[],"worker":[]}
    for row in completed:
        tier="reasoner" if row["model"]=="reasoner-large" else "worker"
        try: metrics=json.loads(row["result"]).get("metrics",{})
        except (json.JSONDecodeError,TypeError): metrics={}
        aggregates[tier].append(metrics)
    for tier,state in roles.items():
        metrics=aggregates[tier]; seconds=sum(float(item.get("execution_seconds") or 0) for item in metrics); completion=sum(int(item.get("completion_tokens") or 0) for item in metrics)
        state.update({"connected":bool(endpoints[tier]) and endpoint_ready(endpoints[tier]),"endpoint":endpoints[tier] or None,"parallel_capacity":TASK_WORKER_LIMIT,
          "recent_tasks":len(metrics),"average_tokens_per_second":round(completion/seconds,2) if seconds else 0,
          "average_execution_seconds":round(seconds/len(metrics),3) if metrics else 0})
    gpus=nvidia_gpus(); gpu_watts=round(sum(float(gpu.get("power_w") or 0) for gpu in gpus),2) if gpus else None; host_power=host_power_sensors(); resources=system_resource_snapshot()
    with ACTIVE_LOCK: workflow_state={"active":len(ACTIVE),"queued":len(WORKFLOW_QUEUE),"parallel_capacity":MAX_PARALLEL_WORKFLOWS}
    return {"roles":roles,"workflows":workflow_state,"power":{
      "gpu_w":gpu_watts,"gpu_source":"nvidia-smi" if gpus else "No supported GPU power sensor",
      "cpu_w":host_power["cpu_w"],"cpu_source":host_power["source"] or "No reliable CPU package power sensor is available",
      "ram_w":host_power["ram_w"],"ram_source":host_power["source"] or "No reliable RAM power sensor is available",
      "cpu_utilization":resources["cpu_utilization"],"ram_used_gb":resources["ram_used_gb"],"ram_total_gb":resources["ram_total_gb"],
      "estimated_cpu_w":resources["estimated_cpu_w"],"estimated_ram_w":resources["estimated_ram_w"],"estimation_method":resources["estimation_method"]},"timestamp":stamp()}


def workflow_report(wid):
    data=workflow_data(wid)
    if not data: return None
    w=data["workflow"]; s=data["summary"]; lines=[f"# Skein Report — {w['objective']}","",f"Status: **{w['status']}**","",data["artifact_notice"],"",
      "## Execution summary","",f"- Tokens: **{s['total_tokens']}** ({s['prompt_tokens']} input, {s['completion_tokens']} output)",
      f"- Average throughput: **{s['average_tokens_per_second']} tokens/s**",f"- Cumulative task time: **{s['execution_seconds']} s**",
      f"- Workflow wall time: **{s['wall_clock_seconds']} s**",f"- Average / peak GPU power: **{s['average_power_w']} W / {s['peak_power_w']} W**",
      f"- Estimated CPU / RAM power: **{s['estimated_cpu_power_w']} W / {s['estimated_ram_power_w']} W**",
      f"- Average CPU utilization / RAM used: **{s['average_cpu_utilization']}% / {s['average_ram_used_gb']} GB**",
      f"- Estimated GPU energy: **{s['energy_wh']} Wh**","",f"> {s['energy_note']}",""]
    for i,task in enumerate(data["tasks"],1):
        result=task["result"] or {}
        lines += [f"## Step {i} — {task['title']}","",f"- Role: `{task['role']}`",f"- Model: `{task['model'] or 'not executed'}`",
          f"- Status: `{task['status']}`",f"- Confidence: {task['confidence'] if task['confidence'] is not None else 'N/A'}"]
        metrics=result.get("metrics",{})
        if metrics: lines += [f"- Tokens: {metrics.get('total_tokens',0)} ({metrics.get('tokens_per_second',0)} tokens/s)",
          f"- Duration: {metrics.get('execution_seconds',0)} s",f"- Average GPU power: {metrics.get('average_gpu_power_w',metrics.get('average_power_w',0))} W",
          f"- Estimated CPU / RAM power: {metrics.get('estimated_cpu_w',0)} W / {metrics.get('estimated_ram_w',0)} W",
          f"- Resource scope: {metrics.get('resource_scope','unknown')}",
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


# Weight uploads stream to disk through their own handler; every other POST body is JSON
# and small, so anything larger is either a mistake or a pre-auth memory-exhaustion attempt.
MAX_JSON_BODY_BYTES=2*1024*1024


class Handler(SimpleHTTPRequestHandler):
    def __init__(self,*args,**kwargs): super().__init__(*args,directory=str(STATIC),**kwargs)
    def log_message(self,fmt,*args): pass
    def dispatch(self,method,handler):
        """Every GET/POST/DELETE funnels through here: one place to time the request, catch
        anything the ~60 route branches below don't handle themselves, and log the outcome.
        This is the exhaustive layer — it logs every action and error without needing bespoke
        instrumentation at each route, and turns an unhandled exception into a clean 500 JSON
        response instead of a dead connection (previously nothing wrapped do_GET/POST/DELETE,
        so a bug there crashed the connection with a bare traceback on stderr)."""
        start=time.perf_counter(); status_box={"code":200}; disconnected=False
        self._current_user_cache=UNCHANGED  # one lookup per request; dispatch's own logging reuses it below
        if not hasattr(self,"_original_send_response"): self._original_send_response=self.send_response
        def send_response(code,*a,**kw):
            status_box["code"]=code; return self._original_send_response(code,*a,**kw)
        self.send_response=send_response
        try:
            handler()
        except (ConnectionAbortedError,ConnectionResetError,BrokenPipeError):
            # The client went away mid-response (closed tab, navigated on, a network blip) --
            # routine, not a bug: keep it out of ERROR and don't try writing to a dead socket.
            disconnected=True
        except Exception:
            logger_http.exception(f"{method} {self.path} raised an unhandled exception")
            status_box["code"]=500
            try: self.json({"error":"Internal server error"},500)
            except Exception: pass
        finally:
            duration_ms=(time.perf_counter()-start)*1000
            if disconnected:
                logger_http.info(f"{method} {self.path}: client disconnected after {duration_ms:.1f}ms")
            else:
                code=status_box["code"]
                try: user=self.current_user()
                except Exception: user=None
                who=user["username"] if user else "anonymous"
                line=f"{method} {self.path} {code} {duration_ms:.1f}ms user={who}"
                if code>=500: logger_http.error(line)
                elif code>=400: logger_http.warning(line)
                elif method=="GET": logger_http.debug(line)  # frequent UI polling; keep default INFO quiet
                else: logger_http.info(line)
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
        """Memoized per request: dispatch()'s own logging in its finally block calls this
        again purely to name the acting user, and without caching that repeated the same
        sessions/users query on every single request, including hot UI-polling endpoints
        that already call this once via authorize()."""
        cached=getattr(self,"_current_user_cache",UNCHANGED)
        if cached is not UNCHANGED: return cached
        user=self._load_current_user()
        self._current_user_cache=user
        return user
    def _load_current_user(self):
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
    def do_GET(self): self.dispatch("GET",self._handle_get)
    def _handle_get(self):
        if self.path=="/api/auth/me":
            user=self.current_user()
            if not user: return self.deny()
            return self.json({"user":user,"policy":{"users_can_choose_execution_mode":setting_bool("users_can_choose_execution_mode")}})
        if self.path.startswith("/api/"):
            if self.path in ("/api/users","/api/rbac/profiles"): user=self.authorize("users.manage")
            elif self.path=="/api/admin/email": user=self.authorize("email.manage")
            elif self.path=="/api/admin/settings": user=self.authorize("settings.manage")
            elif urlparse(self.path).path.startswith("/api/admin/logs"): user=self.authorize("settings.manage")
            elif urlparse(self.path).path.startswith("/api/models"): user=self.authorize("models.manage")
            elif self.path.startswith("/api/workflow-templates"): user=self.authorize("workflow_templates.read")
            elif self.path=="/api/hardware" or urlparse(self.path).path=="/api/hardware/telemetry": user=self.authorize_any("server_stats.read","settings.manage","models.manage")
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
        # Authenticated like every other /api route: these expose the database path, model
        # endpoints, queue depths, and power draw — not anonymous-probe material.
        if self.path=="/api/health": return self.json({"status":"ok","active":len(ACTIVE),"queued":len(WORKFLOW_QUEUE),"parallel_workflow_limit":MAX_PARALLEL_WORKFLOWS,"database":str(DB_PATH),"execution_mode":EXECUTION_MODE,"active_models":ACTIVE_ENDPOINTS,"backends":{"worker":bool(os.getenv("SKEIN_WORKER_URL") or ACTIVE_ENDPOINTS.get("worker")),"reasoner":bool(os.getenv("SKEIN_REASONER_URL") or ACTIVE_ENDPOINTS.get("reasoner"))}})
        if self.path=="/api/runtime-overview": return self.json(runtime_overview())
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
        if urlparse(self.path).path=="/api/admin/logs":
            query=parse_qs(urlparse(self.path).query)
            level=(query.get("level") or [""])[0].strip().upper() or None
            search=(query.get("q") or [""])[0].strip() or None
            try: limit=max(1,min(5000,int((query.get("limit") or [500])[0])))
            except ValueError: return self.json({"error":"limit must be an integer"},400)
            if level and level not in LOG_LEVEL_NAMES: return self.json({"error":f"Unknown level '{level}'","action":f"Choose one of: {', '.join(LOG_LEVEL_NAMES)}."},400)
            return self.json({"records":read_log_records(limit,level,search),"level_options":LOG_LEVEL_NAMES,
                              "files":log_files_summary(),"path":str(LOG_FILE)})
        if urlparse(self.path).path=="/api/admin/logs/download":
            name=(parse_qs(urlparse(self.path).query).get("file") or ["skein.log"])[0]
            path=log_file_path(name)
            if not path: return self.json({"error":"Unknown log file"},404)
            return self.file(path,path.name)
        if self.path=="/api/server-stats": return self.json(privacy_safe_server_stats())
        if self.path=="/api/sandbox/capabilities": return self.json({"mode":EXECUTION_MODE,"runtimes":sandbox_capabilities()})
        if urlparse(self.path).path=="/api/hardware/telemetry":
            try: window=int((parse_qs(urlparse(self.path).query).get("window") or [900])[0])
            except ValueError: return self.json({"error":"Telemetry window must be an integer"},400)
            return self.json(pool_telemetry(window))
        if self.path=="/api/hardware": return self.json(hardware_snapshot())
        if self.path=="/api/models":
            with db() as conn: rows=conn.execute("SELECT * FROM models ORDER BY updated_at DESC").fetchall()
            models=[]
            for row in rows:
                item=dict(row); item["running"]=model_running(row); item["status"]=reconciled_model_status(row)
                item["runnable"]=item["role"] in RUNNABLE_MODEL_ROLES
                item["quantization"]=detect_quantization(item["model_path"])
                item["log_available"]=runtime_log_path(item["id"]).exists()
                try: item["size_bytes"]=Path(item["model_path"]).stat().st_size
                except OSError: item["size_bytes"]=None
                fresh=model_gguf_metadata(item["id"],item["model_path"],item["gguf_parsed_at"])
                if fresh: item.update(fresh)
                models.append(item)
            return self.json(models)
        if self.path=="/api/models/files":
            entries,warnings=model_file_entries()
            return self.json({"files":entries,"warnings":warnings,"roots":[str(root) for root in configured_model_roots()],
                              "runtime":str(find_llama_runtime() or ""),"library":str(model_library_dir()),
                              "minimum_size_mb":MIN_MODEL_MB})
        if self.path=="/api/models/roots":
            return self.json({"roots":[str(root) for root in configured_model_roots()],
                              "managed":json.loads(setting_text("model_roots") or "[]"),
                              "environment":[raw.strip() for raw in (os.getenv("SKEIN_MODEL_ROOTS","") or "").split(os.pathsep) if raw.strip()],
                              "defaults":[str(root) for root in DEFAULT_MODEL_ROOTS],"library":str(model_library_dir())})
        if self.path=="/api/models/downloads": return self.json({"downloads":list_downloads()})
        if urlparse(self.path).path=="/api/models/huggingface/search":
            query=parse_qs(urlparse(self.path).query)
            result,status=huggingface_search((query.get("q") or [""])[0],(query.get("limit") or [20])[0])
            return self.json(result,status)
        if urlparse(self.path).path=="/api/models/huggingface/files":
            query=parse_qs(urlparse(self.path).query)
            result,status=huggingface_repo_files((query.get("repo") or [""])[0])
            return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/logs"):
            mid=self.path.split("/")[-2]
            with db() as conn: row=conn.execute("SELECT id,name,last_error FROM models WHERE id=?",(mid,)).fetchone()
            if not row: return self.json({"error":"Model not found"},404)
            return self.json({"id":mid,"name":row["name"],"last_error":row["last_error"],"log":runtime_log_tail(mid,20000)})
        if self.path=="/api/workflow-templates":
            return self.json(visible_workflow_templates(user["id"],"workflow_templates.manage_all" in user["permissions"]))
        if self.path.startswith("/api/workflow-templates/"):
            template_id=self.path.rsplit("/",1)[-1]; row=self.template_row(template_id)
            if not self.template_visible(user,row): return self.json({"error":"Workflow template not found"},404)
            return self.json(workflow_template_payload(row,user["id"],"workflow_templates.manage_all" in user["permissions"]))
        if urlparse(self.path).path=="/api/workflows":
            if not any(p in user["permissions"] for p in ("workflows.read_own","workflows.read_all")): return self.deny(403,"Workflow read permission required")
            query=parse_qs(urlparse(self.path).query); requested_limit=int((query.get("limit") or [30])[0]); limit=max(1,min(requested_limit,1000))
            with db() as conn:
                rows=conn.execute("SELECT * FROM workflows ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall() if "workflows.read_all" in user["permissions"] else conn.execute("SELECT * FROM workflows WHERE owner_id=? ORDER BY created_at DESC LIMIT ?",(user["id"],limit)).fetchall()
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
    def upload_model_weight(self):
        """Stream a .gguf upload straight to disk; never buffer multi-gigabyte weights in memory."""
        user=self.authorize("models.manage")
        if not user: return
        try: length=int(self.headers.get("Content-Length",0))
        except ValueError: return self.json({"error":"Invalid Content-Length header"},400)
        filename=self.headers.get("X-Skein-Filename") or (parse_qs(urlparse(self.path).query).get("filename") or [""])[0]
        result,status=store_uploaded_weight(unquote(filename),self.rfile,length)
        return self.json(result,status)

    def do_POST(self): self.dispatch("POST",self._handle_post)
    def _handle_post(self):
        global EXECUTION_MODE
        if urlparse(self.path).path=="/api/models/upload": return self.upload_model_weight()
        try: length=int(self.headers.get("Content-Length",0))
        except ValueError: return self.json({"error":"Invalid Content-Length header"},400)
        # Weight uploads stream above; every other POST is JSON and small. Without a cap an
        # anonymous client could make this pre-auth read allocate gigabytes of RAM.
        if length<0 or length>MAX_JSON_BODY_BYTES: return self.json({"error":"Request body exceeds the JSON size limit"},413)
        try: body=json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError: return self.json({"error":"JSON invalide"},400)
        if self.path=="/api/auth/register":
            remote=self.client_address[0]
            if not consume_rate_limit("register",remote,5,600):
                logger_auth.warning(f"registration rate-limited for {remote}")
                return self.json({"error":"Too many registration attempts; try again later"},429)
            username=str(body.get("username","")).strip(); email=str(body.get("email","")).strip().lower(); password=str(body.get("password","")); language=str(body.get("language","en"))
            if len(username)<3 or len(password)<8 or not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return self.json({"error":"Valid email, username of 3+ characters, and password of 8+ characters are required"},400)
            uid=str(uuid.uuid4()); created=stamp()
            try:
                with db() as conn:
                    # The UNIQUE indexes are BINARY on legacy databases, but login matches
                    # NOCASE: a case-variant duplicate would shadow one of the two accounts.
                    if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE OR (email IS NOT NULL AND email=? COLLATE NOCASE)",(username,email)).fetchone():
                        raise sqlite3.IntegrityError("case-insensitive duplicate")
                    conn.execute("INSERT INTO users(id,username,password_hash,role,active,created_at,email,verified_at) VALUES(?,?,?,?,?,?,?,NULL)",(uid,username,password_hash(password),"user",1,created,email))
                    conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,"workflow_operator"))
            except sqlite3.IntegrityError:
                logger_auth.info(f"registration rejected, already exists: username={username!r}")
                return self.json({"error":"Username or email already registered"},409)
            delivery,status=issue_verification_code(uid,language,True)
            response={"registered":True,"verification_required":True,"email_sent":status==200,"expires_in_seconds":600}
            if status==200: response.update(delivery)
            else: response["message"]="Account created, but email delivery failed. Retry later or ask an authorized user manager for manual approval."
            logger_auth.info(f"registered id={uid} username={username!r} email_sent={status==200}")
            return self.json(response,201)
        if self.path=="/api/auth/login":
            username=str(body.get("username","")).strip(); password=str(body.get("password",""))
            throttle_subject=f"{self.client_address[0]}|{username.lower()}"; now=stamp()
            with db() as conn:
                failures=conn.execute("SELECT COUNT(*) FROM auth_rate_limits WHERE action='login-failure' AND subject=? AND created_at>?",(throttle_subject,now-300)).fetchone()[0]
            if failures>=5:
                logger_auth.warning(f"login throttled username={username!r} from {self.client_address[0]}")
                return self.json({"error":"Too many failed login attempts; try again in a few minutes","retry_after_seconds":300},429)
            with db() as conn: row=conn.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",(username,)).fetchone()
            # The dummy comparison keeps the 401 at the same PBKDF2 cost whether or not the
            # username exists; only failures consume throttle quota, so normal logins never lock out.
            password_ok=password_valid(password,row["password_hash"] if row else DUMMY_PASSWORD_HASH)
            if not row or not row["active"] or not password_ok:
                with db() as conn:
                    conn.execute("DELETE FROM auth_rate_limits WHERE created_at<?",(now-86400,))
                    conn.execute("INSERT INTO auth_rate_limits(action,subject,created_at) VALUES('login-failure',?,?)",(throttle_subject,now))
                logger_auth.warning(f"login failed username={username!r} from {self.client_address[0]}")
                return self.deny(401,"Invalid credentials")
            token=secrets.token_urlsafe(32)
            with db() as conn:
                conn.execute("DELETE FROM sessions WHERE expires_at<=?",(stamp(),)); conn.execute("INSERT INTO sessions(token,user_id,expires_at,created_at,storage_id) VALUES(?,?,?,?,?)",(token,row["id"],stamp()+43200,stamp(),str(uuid.uuid4())))
            profiles,permissions=access_for_user(row["id"])
            verified=bool(row["verified_at"])
            logger_auth.info(f"login succeeded id={row['id']} username={row['username']!r}")
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
                logger_auth.info("logout: session invalidated")
            raw=b'{"ok":true}'; self.send_response(200); self.send_header("Content-Type","application/json"); self.send_header("Set-Cookie","skein_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); return self.wfile.write(raw)
        if self.path.startswith("/api/models"): user=self.authorize("models.manage")
        elif self.path.startswith("/api/workflow-templates"): user=self.authorize_any("workflow_templates.manage_own","workflow_templates.manage_all")
        elif self.path.startswith("/api/users") or self.path.startswith("/api/rbac"): user=self.authorize("users.manage")
        elif self.path.startswith("/api/admin/email"): user=self.authorize("email.manage")
        elif self.path.startswith(("/api/pools","/api/gpus","/api/stack","/api/admin")): user=self.authorize("settings.manage")
        elif self.path=="/api/workflows" or self.path.endswith("/execute") or self.path.endswith("/command"): user=self.authorize("workflows.execute")
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
            # Same normalization and format rule as public registration, or the email
            # uniqueness invariant the verification flow relies on breaks by case variation.
            email=str(body.get("email") or "").strip().lower() or None
            if email and not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",email): return self.json({"error":"Invalid email address"},400)
            try:
                uid=str(uuid.uuid4())
                with db() as conn:
                    if conn.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE OR (email IS NOT NULL AND ? IS NOT NULL AND email=? COLLATE NOCASE)",(username,email,email)).fetchone():
                        raise sqlite3.IntegrityError("case-insensitive duplicate")
                    created=stamp(); conn.execute("INSERT INTO users(id,username,password_hash,role,active,created_at,email,verified_at) VALUES(?,?,?,?,?,?,?,?)",(uid,username,password_hash(password),role,1,created,email,created))
                    for profile in profiles: conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,profile))
                assigned,_=access_for_user(uid); return self.json({"id":uid,"username":username,"role":role,"active":1,"profiles":assigned},201)
            except sqlite3.IntegrityError: return self.json({"error":"This username or email already exists"},409)
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
            guard_last_admin="super_admin" in current_ids and existing["active"] and ("super_admin" not in resulting_profiles or not resulting_active)
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
                if guard_last_admin:
                    # The no-op write takes this transaction's write lock before counting, so
                    # two concurrent demotions serialize instead of both seeing one survivor.
                    conn.execute("UPDATE users SET active=active WHERE id=?",(uid,))
                    remaining=conn.execute("SELECT COUNT(DISTINCT u.id) FROM users u JOIN user_profiles up ON up.user_id=u.id WHERE up.profile_id='super_admin' AND u.active=1 AND u.id<>?",(uid,)).fetchone()[0]
                    if remaining==0: return self.json({"error":"At least one active Super Administrator is required"},409)
                if fields: conn.execute(f"UPDATE users SET {','.join(fields)} WHERE id=?",(*values,uid))
                if body.get("password"):
                    # A password reset is usually meant to evict whoever holds the old
                    # credentials: stolen cookies must die with the password. The caller's
                    # own session survives so self-service resets do not log the admin out.
                    conn.execute("DELETE FROM sessions WHERE user_id=? AND token<>?",(uid,self.cookie("skein_session") or ""))
                if requested_profiles is not None:
                    conn.execute("DELETE FROM user_profiles WHERE user_id=?",(uid,))
                    for profile in resulting_profiles: conn.execute("INSERT INTO user_profiles VALUES(?,?)",(uid,profile))
            return self.json({"ok":True})
        if self.path=="/api/admin/settings":
            allowed=bool(body.get("users_can_choose_execution_mode"))
            with db() as conn: conn.execute("INSERT INTO settings VALUES('users_can_choose_execution_mode',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("true" if allowed else "false",))
            logger_settings.info(f"users_can_choose_execution_mode set to {allowed} by {user['username']}")
            return self.json({"users_can_choose_execution_mode":allowed})
        if self.path=="/api/admin/email":
            host=str(body.get("host","")).strip(); from_address=str(body.get("from_address","")).strip(); security=str(body.get("security","starttls"))
            if not host or not from_address or security not in ("starttls","ssl","plain"): return self.json({"error":"SMTP host, sender address, and valid security mode are required"},400)
            values={"smtp_host":host,"smtp_port":str(int(body.get("port",587))),"smtp_username":str(body.get("username","")).strip(),"smtp_from":from_address,"smtp_security":security}
            if body.get("password"): values["smtp_password"]=protect_secret(str(body["password"]))
            with db() as conn:
                for key,value in values.items(): conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(key,value))
            logger_settings.info(f"SMTP configuration updated by {user['username']}: host={host} from={from_address} security={security}")
            return self.json(smtp_configuration())
        if self.path=="/api/admin/email/test":
            recipient=str(body.get("recipient","")).strip()
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+",recipient): return self.json({"error":"Valid recipient email required"},400)
            try: send_email(recipient,"Skein SMTP test","Your Skein SMTP configuration is working.")
            except Exception as exc:
                logger_settings.warning(f"SMTP test failed for {recipient}: {exc}")
                return self.json({"error":"SMTP test failed","details":str(exc)},502)
            logger_settings.info(f"SMTP test succeeded, sent to {recipient}")
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
            continued_from=str(body.get("continue_workflow_id") or "").strip() or None
            session_id=user.get("session_id")
            if continued_from:
                with db() as conn: source=conn.execute("SELECT id,owner_id,session_id FROM workflows WHERE id=?",(continued_from,)).fetchone()
                if not source: return self.json({"error":"Source workflow not found"},404)
                if source["owner_id"]!=user["id"]: return self.deny(403,"Only the workflow owner can continue its session")
                session_id=source["session_id"] or session_id
            wid=create_workflow(objective,user["id"],session_id,specs,template_id,planning_mode,continued_from); start_workflow(wid); return self.json({"id":wid,"planning":selection,"continued_from":continued_from,"session_id":session_id},201)
        if self.path=="/api/execution-mode":
            if "settings.manage" not in user["permissions"] and not setting_bool("users_can_choose_execution_mode"): return self.deny(403,"Execution mode selection is disabled by the administrator")
            mode=str(body.get("mode","")).lower()
            if mode not in ("sandbox","local"): return self.json({"error":"Invalid mode"},400)
            EXECUTION_MODE=mode
            logger_settings.info(f"execution mode set to {mode} by {user['username']}")
            return self.json({"mode":mode,"warning":"Unisolated local execution" if mode=="local" else None})
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
            logger_settings.info(f"pool created id={pid} name={name!r} domain={domain}")
            return self.json({"id":pid,"name":name,"domain":domain,"color":color},201)
        if self.path.startswith("/api/gpus/") and self.path.endswith("/assign"):
            # A GPU may belong to several pools at once (one card serving reasoner, worker,
            # and retrieval together), so this toggles a single (gpu, pool) membership rather
            # than replacing the whole assignment.
            gpu_id=unquote(self.path[len("/api/gpus/"):-len("/assign")].rstrip("/"))
            pool_id=body.get("pool_id")
            assigned=bool(body.get("assigned",True))
            if not pool_id: return self.json({"error":"pool_id requis"},400)
            with db() as conn:
                if not conn.execute("SELECT 1 FROM pools WHERE id=?",(pool_id,)).fetchone(): return self.json({"error":"pool introuvable"},404)
                if assigned:
                    conn.execute("INSERT INTO gpu_assignments(gpu_id,pool_id,updated_at) VALUES(?,?,?) ON CONFLICT(gpu_id,pool_id) DO UPDATE SET updated_at=excluded.updated_at",(gpu_id,pool_id,stamp()))
                else:
                    conn.execute("DELETE FROM gpu_assignments WHERE gpu_id=? AND pool_id=?",(gpu_id,pool_id))
                pool_ids=[row[0] for row in conn.execute("SELECT pool_id FROM gpu_assignments WHERE gpu_id=? ORDER BY pool_id",(gpu_id,))]
            logger_settings.info(f"gpu={gpu_id} {'assigned to' if assigned else 'unassigned from'} pool={pool_id} by {user['username']}")
            return self.json({"gpu_id":gpu_id,"pool_ids":pool_ids})
        if self.path=="/api/models":
            required=("name","role","backend","model_path","runtime_path")
            if any(not str(body.get(k,"")).strip() for k in required): return self.json({"error":"nom, rôle, backend et chemins requis"},400)
            mid=str(uuid.uuid4()); port=int(body.get("port",8001)); context=int(body.get("context_size",32768))
            with db() as conn: conn.execute("INSERT INTO models(id,name,role,backend,model_path,runtime_path,context_size,port,pool_id,status,pid,endpoint,last_error,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
              (mid,body["name"],body["role"],body["backend"],body["model_path"],body["runtime_path"],context,port,None,"STOPPED",None,None,None,stamp()))
            return self.json({"id":mid},201)
        if self.path=="/api/models/autoload":
            result,status=autoload_models(); return self.json(result,status)
        if self.path=="/api/models/discover":
            return self.json(discover_local_models(True))
        if self.path=="/api/models/roots":
            roots=body.get("roots")
            if not isinstance(roots,list): return self.json({"error":"Send roots as a list of directories"},400)
            saved=save_model_roots(roots)
            return self.json({"managed":saved,"roots":[str(root) for root in configured_model_roots()]})
        if self.path=="/api/models/files/register":
            result,status=register_model_file(body.get("path"),body.get("role","available"),body.get("pool_id"),
                                              body.get("name"),body.get("context_size",8192),body.get("runtime_path"))
            return self.json(result,status)
        if self.path=="/api/models/huggingface/download":
            result,status=start_huggingface_download(body.get("repo"),body.get("filename")); return self.json(result,status)
        if self.path.startswith("/api/models/downloads/") and self.path.endswith("/cancel"):
            result,status=cancel_download(self.path.split("/")[-2]); return self.json(result,status)
        if self.path in ("/api/stack/start","/api/stack/stop","/api/stack/restart"):
            action=self.path.rsplit("/",1)[-1]
            logger_settings.warning(f"stack {action} requested by {user['username']}")
            result,status=supervisor_call(action,"POST"); return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/activate"):
            mid=self.path.split("/")[-2]
            result,status=activate_model(mid,(body.get("pool_id") or None),(body.get("role") or None))
            return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/configure"):
            mid=self.path.split("/")[-2]
            result,status=configure_model(mid,body.get("role"),body["pool_id"] if "pool_id" in body else UNCHANGED)
            return self.json(result,status)
        if self.path.startswith("/api/models/") and self.path.endswith("/stop"):
            result,status=stop_model(self.path.split("/")[-2]); return self.json(result,status)
        return self.json({"error":"route inconnue"},404)

    def do_DELETE(self): self.dispatch("DELETE",self._handle_delete)
    def _handle_delete(self):
        parsed=urlparse(self.path)
        if parsed.path.startswith("/api/models/"):
            user=self.authorize("models.manage")
            if not user: return
            mid=parsed.path.rsplit("/",1)[-1]
            with db() as conn: row=conn.execute("SELECT * FROM models WHERE id=?",(mid,)).fetchone()
            if not row: return self.json({"error":"Model not found"},404)
            if model_running(row): return self.json({"error":"Stop this runtime before unregistering it"},409)
            delete_weights=(parse_qs(parsed.query).get("delete_file") or ["false"])[0].lower()=="true"
            removed=False
            if delete_weights:
                weights=Path(row["model_path"])
                # Only ever remove a file Skein itself downloaded or received.
                if weights.is_file() and str(weights.parent).lower()==str(model_library_dir()).lower():
                    try: weights.unlink(); removed=True
                    except OSError as exc: return self.json({"error":"Could not delete the weight file","details":str(exc)},500)
                else:
                    return self.json({"error":"Skein only deletes weight files it stores in its own model library",
                                      "library":str(model_library_dir())},403)
            with db() as conn: conn.execute("DELETE FROM models WHERE id=?",(mid,))
            runtime_log_path(mid).unlink(missing_ok=True)
            logger_models.info(f"unregistered id={mid} name={row['name']!r} weight_file_removed={removed}")
            return self.json({"deleted":True,"id":mid,"weight_file_removed":removed})
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


def log_periodic_stats(interval_seconds):
    """A periodic milestone distinct from per-action logging: how busy the process is right
    now, so an operator tailing the rotating file sees load trends without wading through
    every individual request."""
    while True:
        time.sleep(interval_seconds)
        try:
            with db() as conn:
                active_models=conn.execute("SELECT COUNT(*) FROM models WHERE status IN ('RUNNING','STARTING')").fetchone()[0]
        except Exception: active_models=None
        logger_system.info(f"stats: active_workflows={len(ACTIVE)} queued_workflows={len(WORKFLOW_QUEUE)} "
                           f"active_models={active_models} endpoints={sorted(ACTIVE_ENDPOINTS)} execution_mode={EXECUTION_MODE}")


if __name__=="__main__":
    init_db()
    restored=restore_active_endpoints(); recover_pending_workflows()
    if restored: logger_system.info(f"restored {len(restored)} runtime endpoint(s) from a previous run: {[r['role'] for r in restored]}")
    server=ThreadingHTTPServer(("127.0.0.1",int(os.getenv("SKEIN_PORT","8787"))),Handler)
    logger_system.info(f"Skein starting on http://127.0.0.1:{server.server_port} (db={DB_PATH}, logs={LOG_DIR})")
    print(f"Skein disponible sur http://127.0.0.1:{server.server_port}")
    threading.Thread(target=log_periodic_stats,args=(int(os.getenv("SKEIN_STATS_LOG_INTERVAL_SECONDS","300")),),daemon=True).start()
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: logger_system.info("Skein shutting down")
