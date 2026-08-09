#!/usr/bin/env python3
"""Run every visible Skein workflow sequentially across four difficulty levels.

This destructive, potentially expensive integration benchmark is intentionally
manual. It never runs unless --run is supplied.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from datetime import datetime, timezone
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


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

    def request(self, path: str, body: dict | None = None, method: str | None = None):
        raw = json.dumps(body).encode("utf-8") if body is not None else None
        request = Request(
            self.base_url + path,
            raw,
            {"Content-Type": "application/json", "Accept": "application/json"},
            method=method,
        )
        try:
            with self.opener.open(request, timeout=180) as response:
                return json.load(response)
        except HTTPError as exc:
            details = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"HTTP {exc.code} for {path}: {details}") from exc
        except URLError as exc:
            raise RuntimeError(f"Cannot reach {self.base_url}: {exc.reason}") from exc

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


def compact_result(template: dict, level: str, objective: str, workflow: dict, elapsed: float) -> dict:
    final_output = workflow.get("final_output") or {}
    execution_report = workflow.get("execution_report") or {}
    return {
        "template_id": template["id"],
        "template_name": template["name"],
        "difficulty": level,
        "objective": objective,
        "workflow_id": workflow["workflow"]["id"],
        "status": workflow["workflow"]["status"],
        "elapsed_seconds": round(elapsed, 3),
        "summary": workflow.get("summary", {}),
        "final_deliverable": final_output.get("deliverable") or final_output.get("summary"),
        "execution_report": execution_report.get("deliverable") or execution_report.get("summary"),
        "task_statuses": [
            {"title": task["title"], "role": task["role"], "status": task["status"], "mode": (task.get("result") or {}).get("mode")}
            for task in workflow.get("tasks", [])
        ],
    }


def write_results(output_dir: Path, results: list[dict]) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = output_dir / f"workflow-matrix-{timestamp}.json"
    markdown_path = output_dir / f"workflow-matrix-{timestamp}.md"
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# Manual workflow matrix", "", f"Generated: {timestamp}", "", "| Workflow | Difficulty | Status | Duration | Tokens | Report |", "|---|---|---:|---:|---:|---:|"]
    for item in results:
        summary = item.get("summary") or {}
        lines.append(
            f"| {item['template_name']} | {item['difficulty']} | {item['status']} | "
            f"{item['elapsed_seconds']:.3f}s | {summary.get('total_tokens', 0)} | {'yes' if item.get('execution_report') else 'no'} |"
        )
    lines.extend(["", "The JSON companion contains objectives, task statuses, final deliverables, metrics, and execution reports."])
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true", help="Required explicit confirmation to execute the matrix.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    parser.add_argument("--username", default=os.getenv("SKEIN_TEST_USERNAME", "admin"))
    parser.add_argument("--timeout", type=int, default=1800, help="Maximum seconds per workflow execution.")
    parser.add_argument("--poll-interval", type=float, default=1.0)
    parser.add_argument("--output-dir", type=Path, default=Path("manual-test-results"))
    parser.add_argument("--stop-on-failure", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.run:
        print("Refusing to execute: pass --run explicitly. This test invokes every workflow four times.", file=sys.stderr)
        return 2
    password = os.getenv("SKEIN_TEST_PASSWORD") or getpass.getpass("Skein password: ")
    client = SkeinClient(args.base_url)
    client.login(args.username, password)
    templates = client.request("/api/workflow-templates")
    if not templates:
        raise RuntimeError("No visible workflow templates were returned.")
    results = []
    print(f"Running {len(templates)} workflows x {len(LEVELS)} difficulty levels sequentially.")
    for template in templates:
        for level, instruction in LEVELS:
            objective = objective_for(template, level, instruction)
            print(f"[{len(results) + 1}/{len(templates) * len(LEVELS)}] {template['name']} / {level}", flush=True)
            started = time.monotonic()
            try:
                created = client.request("/api/workflows", {"objective": objective, "planning_mode": "template", "template_id": template["id"]}, "POST")
                workflow = client.wait_for_workflow(created["id"], args.timeout, args.poll_interval)
                item = compact_result(template, level, objective, workflow, time.monotonic() - started)
            except Exception as exc:  # Continue by default so the matrix exposes every failing workflow.
                item = {"template_id": template["id"], "template_name": template["name"], "difficulty": level, "objective": objective, "status": "HARNESS_ERROR", "elapsed_seconds": round(time.monotonic() - started, 3), "error": str(exc)}
            results.append(item)
            if args.stop_on_failure and item["status"] != "COMPLETED":
                json_path, markdown_path = write_results(args.output_dir, results)
                print(f"Stopped after failure. Results: {json_path} and {markdown_path}")
                return 1
    json_path, markdown_path = write_results(args.output_dir, results)
    failures = sum(item["status"] != "COMPLETED" for item in results)
    print(f"Completed with {failures} failure(s). Results: {json_path} and {markdown_path}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
