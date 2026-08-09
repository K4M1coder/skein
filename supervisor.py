"""Skein stack supervisor: keeps the web app and its model child processes together."""
import json, os, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT=Path(__file__).resolve().parent
LOCK=threading.Lock(); APP=None

def alive(): return APP is not None and APP.poll() is None
def start_stack():
    global APP
    with LOCK:
        if alive(): return {"status":"RUNNING","pid":APP.pid}
        APP=subprocess.Popen([sys.executable,"-B","app.py"],cwd=ROOT,
          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        return {"status":"STARTING","pid":APP.pid}
def stop_stack():
    global APP
    with LOCK:
        if not alive(): APP=None; return {"status":"STOPPED"}
        pid=APP.pid
        if os.name=="nt": subprocess.run(["taskkill","/PID",str(pid),"/T","/F"],capture_output=True,
          creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
        else: APP.terminate(); APP.wait(timeout=10)
        APP=None; return {"status":"STOPPED","previous_pid":pid}
def delayed(action):
    def run(): time.sleep(.4); stop_stack(); action=="restart" and start_stack()
    threading.Thread(target=run,daemon=True).start()

class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send(self,data,status=200):
        raw=json.dumps(data).encode(); self.send_response(status); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(raw))); self.end_headers(); self.wfile.write(raw)
    def do_GET(self):
        if self.path=="/status": return self.send({"status":"RUNNING" if alive() else "STOPPED","pid":APP.pid if alive() else None})
        self.send({"error":"route inconnue"},404)
    def do_POST(self):
        if self.path=="/start": return self.send(start_stack())
        if self.path=="/stop": delayed("stop"); return self.send({"status":"STOPPING"})
        if self.path=="/restart": delayed("restart"); return self.send({"status":"RESTARTING"})
        self.send({"error":"route inconnue"},404)

if __name__=="__main__":
    start_stack(); server=ThreadingHTTPServer(("127.0.0.1",8777),Handler)
    print("Skein supervisor: http://127.0.0.1:8777 · app: http://127.0.0.1:8787")
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: stop_stack()
