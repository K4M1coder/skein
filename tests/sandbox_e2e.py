"""Explicit Docker + local execution smoke test."""
import os,subprocess,sys,tempfile,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
os.environ["SKEIN_DB_PATH"]=str(Path(tempfile.gettempdir())/f"skein-sandbox-{os.getpid()}.db")
import app

def main():
    app.init_db();wid=app.create_workflow("sandbox test");tid=app.workflow_data(wid)["tasks"][0]["id"]
    files=[
      {"path":"main.py","content":"print('python-ok')\n"},
      {"path":"main.js","content":"console.log('node-ok');\n"},
      {"path":"Main.java","content":"public class Main { public static void main(String[] a){ System.out.println(\"java-ok\"); } }\n"},
      {"path":"main.php","content":"<?php echo \"php-ok\\n\"; ?>\n"},
      {"path":"index.html","content":"<!doctype html><html><head></head><body><h1>preview-ok</h1></body></html>"},
      {"path":"style.css","content":"h1{color:lime}"},
      {"path":"README.md","content":"# Markdown\n\n```mermaid\ngraph TD; A-->B;\n```\n\n```python\nprint('highlight')\n```"},
    ]
    saved=app.persist_artifacts(wid,tid,files);by_path={x["path"]:x for x in saved}; proof={}
    for path,expected in (("main.py","python-ok"),("main.js","node-ok"),("Main.java","java-ok"),("main.php","php-ok")):
        result,status=app.execute_in_sandbox(by_path[path]["id"],20,"sandbox")
        if status!=200 or result["status"]!="PASS" or expected not in result["stdout"]: raise RuntimeError({path:result})
        proof[path]=result
    html,_=app.execute_in_sandbox(by_path["index.html"]["id"],20,"sandbox")
    preview=app.artifact_preview(by_path["index.html"]["id"])
    if html["status"]!="PREVIEW_READY" or "h1{color:lime}" not in preview["content"]: raise RuntimeError("html preview failed")
    shell,_=app.execute_command(wid,"echo sandbox-command-ok",20,"sandbox")
    if shell["status"]!="PASS" or "sandbox-command-ok" not in shell["stdout"]: raise RuntimeError(shell)
    local,_=app.execute_in_sandbox(by_path["main.py"]["id"],20,"local")
    local_shell,_=app.execute_command(wid,"Write-Output local-command-ok",20,"local")
    if local["status"]!="PASS" or "python-ok" not in local["stdout"] or "local-command-ok" not in local_shell["stdout"]: raise RuntimeError({"local":local,"shell":local_shell})

    # A local command that outlives its timeout must take everything it spawned with it:
    # terminating only the PowerShell child leaves grandchildren running on the host
    # forever, and one holding the inherited pipes can block the caller past the timeout.
    def running_pings():
        listing=subprocess.run(["tasklist","/FI","IMAGENAME eq PING.EXE"],capture_output=True,text=True,errors="replace").stdout
        return listing.lower().count("ping.exe")
    pings_before=running_pings()
    timed_out,_=app.execute_command(wid,"Start-Process -FilePath 'ping' -ArgumentList '-t','127.0.0.1' -WindowStyle Hidden; Start-Sleep -Seconds 30",3,"local")
    if timed_out["status"]!="TIMEOUT": raise RuntimeError({"expected":"TIMEOUT","got":timed_out})
    time.sleep(1.5)
    pings_after=running_pings()
    if pings_after>pings_before: raise RuntimeError({"orphaned_processes":pings_after-pings_before,"result":timed_out})

    print({"sandbox_runtimes":list(proof),"html":html["status"],"sandbox_shell":shell["status"],"local_python":local["status"],"local_shell":local_shell["status"],"local_timeout_process_tree":"killed"})
    print("SANDBOX_AND_LOCAL_E2E_OK")

if __name__=="__main__":main()
