#!/usr/bin/env python3
"""Run or recover the complete Skein workflow matrix and analyze its outputs."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEVELS = (
    ("simple", "Produce a short, directly usable result with one clear requirement."),
    ("medium", "Produce a complete result with multiple constraints, verification, and concise usage guidance."),
    ("complex", "Handle interacting requirements, edge cases, verification evidence, and explicit trade-offs."),
    ("very-complex", "Handle a production-scale scenario with conflicting constraints, failure modes, rigorous verification, and a maintainable deliverable."),
)


class SkeinClient:
    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def _open(self, path: str, body: dict | None = None, method: str | None = None):
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(self.base_url + path, raw, {"Content-Type": "application/json", "Accept": "application/json"}, method=method)
        try:
            return self.opener.open(request, timeout=180)
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {path}: {details}") from exc
        except URLError as exc:
            raise RuntimeError(f"Cannot reach {self.base_url}: {exc.reason}") from exc

    def request(self, path: str, body: dict | None = None, method: str | None = None):
        with self._open(path, body, method) as response:
            return json.load(response)

    def download(self, path: str) -> bytes:
        with self._open(path) as response:
            return response.read()

    def login(self, username: str, password: str):
        return self.request("/api/auth/login", {"username": username, "password": password}, "POST")

    def wait_for_workflow(self, workflow_id: str, timeout: int, poll_interval: float):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.request(f"/api/workflows/{workflow_id}")
            if result["workflow"]["status"] in {"COMPLETED", "FAILED"}:
                return result
            time.sleep(poll_interval)
        raise TimeoutError(f"Workflow {workflow_id} did not finish within {timeout} seconds")


def objective_for(template: dict, level: str, instruction: str) -> str:
    objective = template.get("objective_template") or template.get("description") or template["name"]
    return f"{objective}. Test difficulty: {level}. {instruction}"


def safe_name(value: str) -> str:
    return "".join(character if character.isalnum() or character in "-_" else "-" for character in value).strip("-")[:80] or "workflow"


def prepare_output_dir(requested: Path) -> Path:
    candidate = requested if requested.is_absolute() else PROJECT_ROOT / requested
    session = candidate / ("workflow-matrix-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
    try:
        session.mkdir(parents=True, exist_ok=False)
    except OSError as exc:
        fallback = Path(tempfile.gettempdir()) / "skein-manual-test-results" / session.name
        fallback.mkdir(parents=True, exist_ok=True)
        print(f"Warning: output directory unavailable ({exc}). Using {fallback}", file=sys.stderr)
        session = fallback
    return session.resolve()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def collect_run(client: SkeinClient, template: dict, level: str, objective: str, workflow: dict, elapsed: float, run_dir: Path) -> dict:
    run_dir.mkdir(parents=True, exist_ok=True)
    workflow_id = workflow["workflow"]["id"]
    errors = []
    atomic_json(run_dir / "workflow.json", workflow)
    try:
        executions = client.request(f"/api/workflows/{workflow_id}/executions")
        atomic_json(run_dir / "executions.json", executions)
    except Exception as exc:
        executions = []
        errors.append(f"Execution log collection failed: {exc}")
    try:
        (run_dir / "skein-report.md").write_bytes(client.download(f"/api/workflows/{workflow_id}/report"))
    except Exception as exc:
        errors.append(f"Report download failed: {exc}")
    artifacts = workflow.get("artifacts") or []
    if artifacts:
        try:
            (run_dir / "deliverables.zip").write_bytes(client.download(f"/api/workflows/{workflow_id}/deliverable.zip"))
        except Exception as exc:
            errors.append(f"Deliverable download failed: {exc}")
    final_output = workflow.get("final_output") or {}
    execution_report = workflow.get("execution_report") or {}
    failed_tasks = [task["title"] for task in workflow.get("tasks", []) if task["status"] != "COMPLETED"]
    return {
        "template_id": template["id"], "template_name": template["name"], "difficulty": level,
        "objective": objective, "workflow_id": workflow_id, "status": workflow["workflow"]["status"],
        "elapsed_seconds": round(elapsed, 3), "summary": workflow.get("summary", {}),
        "final_deliverable": final_output.get("deliverable") or final_output.get("summary"),
        "execution_report": execution_report.get("deliverable") or execution_report.get("summary"),
        "artifact_count": len(artifacts), "execution_log_count": len(executions), "failed_tasks": failed_tasks,
        "task_statuses": [{"title": task["title"], "role": task["role"], "status": task["status"], "mode": (task.get("result") or {}).get("mode")} for task in workflow.get("tasks", [])],
        "collection_errors": errors, "files_directory": str(run_dir),
    }


def analyze_results(results: list[dict]) -> dict:
    completed = [item for item in results if item.get("status") == "COMPLETED"]
    failed = [item for item in results if item.get("status") != "COMPLETED"]
    summaries = [item.get("summary") or {} for item in results]
    total_seconds = sum(float(summary.get("wall_clock_seconds") or 0) for summary in summaries)
    completion_tokens = sum(int(summary.get("completion_tokens") or 0) for summary in summaries)
    return {
        "cases": len(results), "completed": len(completed), "failed": len(failed),
        "completion_rate_percent": round(100 * len(completed) / len(results), 2) if results else 0,
        "total_tokens": sum(int(summary.get("total_tokens") or 0) for summary in summaries),
        "completion_tokens": completion_tokens, "wall_clock_seconds": round(total_seconds, 3),
        "average_tokens_per_second": round(completion_tokens / total_seconds, 2) if total_seconds else 0,
        "estimated_energy_wh": round(sum(float(summary.get("energy_wh") or 0) for summary in summaries), 4),
        "artifacts": sum(int(item.get("artifact_count") or 0) for item in results),
        "execution_logs": sum(int(item.get("execution_log_count") or 0) for item in results),
        "reports_present": sum(bool(item.get("execution_report")) for item in results),
        "collection_error_count": sum(len(item.get("collection_errors") or []) for item in results),
        "failures": [{"workflow": item.get("template_name"), "difficulty": item.get("difficulty"), "status": item.get("status"), "error": item.get("error"), "failed_tasks": item.get("failed_tasks", [])} for item in failed],
    }


def write_final(session_dir: Path, results: list[dict]) -> tuple[Path, Path]:
    analysis = analyze_results(results)
    json_path = session_dir / "results.json"
    markdown_path = session_dir / "analysis.md"
    atomic_json(json_path, {"analysis": analysis, "results": results})
    lines = ["# Manual workflow matrix analysis", "", f"- Cases: **{analysis['cases']}**", f"- Completed: **{analysis['completed']}**", f"- Failed or harness errors: **{analysis['failed']}**", f"- Completion rate: **{analysis['completion_rate_percent']}%**", f"- Total tokens: **{analysis['total_tokens']}**", f"- Average throughput: **{analysis['average_tokens_per_second']} tokens/s**", f"- Wall-clock sum: **{analysis['wall_clock_seconds']} s**", f"- Estimated energy: **{analysis['estimated_energy_wh']} Wh**", f"- Reports present: **{analysis['reports_present']}/{analysis['cases']}**", f"- Artifacts: **{analysis['artifacts']}**", f"- Command/script logs: **{analysis['execution_logs']}**", f"- Collection errors: **{analysis['collection_error_count']}**", "", "## Case matrix", "", "| Workflow | Difficulty | Status | Duration | Tokens | Report | Artifacts | Logs |", "|---|---|---:|---:|---:|---:|---:|---:|"]
    for item in results:
        summary = item.get("summary") or {}
        lines.append(f"| {item['template_name']} | {item['difficulty']} | {item['status']} | {item.get('elapsed_seconds',0):.3f}s | {summary.get('total_tokens',0)} | {'yes' if item.get('execution_report') else 'no'} | {item.get('artifact_count',0)} | {item.get('execution_log_count',0)} |")
    lines.extend(["", "## Failures and anomalies", ""])
    if analysis["failures"]:
        for failure in analysis["failures"]:
            lines.append(f"- **{failure['workflow']} / {failure['difficulty']}**: {failure['status']} — {failure.get('error') or ', '.join(failure.get('failed_tasks') or []) or 'See the captured workflow logs.'}")
    else:
        lines.append("- No workflow failure was detected.")
    lines.extend(["", "Each `runs/` directory contains the complete workflow response, execution logs, Skein report, and deliverable archive when files were produced."])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--run", action="store_true", help="Execute every matrix case.")
    mode.add_argument("--recover-existing", action="store_true", help="Recover the newest matching matrix runs from Skein history without re-executing them.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--username", default=os.getenv("SKEIN_TEST_USERNAME", "admin"))
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path, default=Path("manual-test-results"))
    parser.add_argument("--stop-on-failure", action="store_true", help="Optional; failures continue by default.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    session_dir = prepare_output_dir(args.output_dir)
    print(f"Results will be saved incrementally in {session_dir}")
    password = os.getenv("SKEIN_TEST_PASSWORD") or getpass.getpass("Skein password: ")
    client = SkeinClient(args.base_url)
    client.login(args.username, password)
    templates = client.request("/api/workflow-templates")
    if not templates:
        raise RuntimeError("No visible workflow templates were returned.")
    history = client.request("/api/workflows?limit=1000") if args.recover_existing else []
    history_by_objective = {}
    for workflow in history:
        history_by_objective.setdefault(workflow["objective"], workflow)
    results = []
    total = len(templates) * len(LEVELS)
    print(("Recovering" if args.recover_existing else "Running") + f" {len(templates)} workflows x {len(LEVELS)} difficulty levels sequentially.")
    for template in templates:
        for level, instruction in LEVELS:
            objective = objective_for(template, level, instruction)
            index = len(results) + 1
            print(f"[{index}/{total}] {template['name']} / {level}", flush=True)
            started = time.monotonic()
            try:
                if args.recover_existing:
                    historical = history_by_objective.get(objective)
                    if not historical:
                        raise RuntimeError("No matching workflow was found in history")
                    workflow = client.request(f"/api/workflows/{historical['id']}")
                else:
                    created = client.request("/api/workflows", {"objective": objective, "planning_mode": "template", "template_id": template["id"]}, "POST")
                    workflow = client.wait_for_workflow(created["id"], args.timeout, args.poll_interval)
                run_dir = session_dir / "runs" / f"{index:02d}-{safe_name(template['name'])}-{level}"
                item = collect_run(client, template, level, objective, workflow, time.monotonic() - started, run_dir)
            except Exception as exc:
                item = {"template_id": template["id"], "template_name": template["name"], "difficulty": level, "objective": objective, "status": "HARNESS_ERROR", "elapsed_seconds": round(time.monotonic() - started, 3), "error": str(exc), "collection_errors": []}
            results.append(item)
            atomic_json(session_dir / "results.partial.json", results)
            if args.stop_on_failure and item["status"] != "COMPLETED":
                break
        if args.stop_on_failure and results[-1]["status"] != "COMPLETED":
            break
    json_path, markdown_path = write_final(session_dir, results)
    failures = sum(item["status"] != "COMPLETED" for item in results)
    print(f"Completed analysis with {failures} failure(s). Results: {json_path} and {markdown_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
