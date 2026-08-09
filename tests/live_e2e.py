"""Real translation + code artifacts check. Run: python -B tests/live_e2e.py"""
import json, os, sys, tempfile, time
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
os.environ["SKEIN_DB_PATH"]=str(Path(tempfile.gettempdir())/f"skein-live-{os.getpid()}.db")
os.environ.pop("SKEIN_ALLOW_SIMULATION",None)
import app

def run(objective,timeout=240):
    wid=app.create_workflow(objective); app.start_workflow(wid); deadline=time.time()+timeout
    while time.time()<deadline:
        data=app.workflow_data(wid)
        if data["workflow"]["status"] in ("COMPLETED","FAILED"): return data
        time.sleep(.5)
    raise RuntimeError("workflow timeout")

def main():
    app.init_db(); ids=[]
    try:
        loaded,status=app.autoload_models()
        if status!=200: raise RuntimeError(loaded)
        ids=[m["id"] for m in loaded["models"]]

        translation=run("Traduis exactement 'Hello world, how are you?' en français.")
        translated=(translation["final_output"] or {}).get("deliverable","")
        if translation["workflow"]["status"]!="COMPLETED" or "bonjour" not in translated.lower():
            raise RuntimeError({"translation":translated,"status":translation["workflow"]["status"]})

        coding=run("Crée un petit module Python hello.py avec une fonction hello(name) qui retourne 'Hello, {name}!', et un fichier test_hello.py avec unittest.")
        artifacts=coding["artifacts"]
        with app.db() as conn: disk=[dict(r) for r in conn.execute("SELECT * FROM artifacts WHERE workflow_id=?",(coding["workflow"]["id"],))]
        contents="\n".join(Path(r["disk_path"]).read_text(encoding="utf-8") for r in disk)
        if coding["workflow"]["status"]!="COMPLETED" or not artifacts or "def hello" not in contents:
            raise RuntimeError({"status":coding["workflow"]["status"],"artifacts":artifacts})
        if any(a["validation"] and a["validation"]["status"]=="FAIL" for a in artifacts):
            raise RuntimeError({"validation_failed":artifacts})

        proof={"autoload":loaded["status"],"translation":{"status":translation["workflow"]["status"],"deliverable":translated,
          "modes":[t["result"]["mode"] for t in translation["tasks"]]},
          "code":{"status":coding["workflow"]["status"],"artifacts":[{"path":a["relative_path"],"validation":a["validation"]} for a in artifacts],
          "modes":[t["result"]["mode"] for t in coding["tasks"]]}}
        print(json.dumps(proof,ensure_ascii=False,indent=2)); print("LIVE_TRANSLATION_AND_CODE_OK")
    finally:
        for mid in ids: app.stop_model(mid)
        app.POOL.shutdown(wait=True)

if __name__=="__main__": main()
