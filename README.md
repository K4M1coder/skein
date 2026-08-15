# Skein

Skein is a local, GPU-aware multi-agent workflow orchestrator. It separates reasoning and worker roles, routes tasks to real local models, exposes task outputs and project artifacts, and can execute generated code either in isolated Docker sandboxes or directly on the host.

## Engineering policy

Skein favors mature, actively maintained libraries over custom security, protocol, parsing, and persistence code. Dependencies are selected for maintenance health, tests, documentation, license compatibility, supply-chain cost, and safe upgrade paths. Framework migrations remain incremental and test-backed so that existing users, workflows, and artifacts continue to work. See [DEPENDENCY_POLICY.md](DEPENDENCY_POLICY.md) for the selection checklist and current adoption roadmap.

The repository includes a deterministic CycloneDX SBOM, a generated readable dependency list, and a Syft enrichment workflow. See [SBOM.md](SBOM.md) and verify it with `python scripts/generate_sbom.py --check`.

Development commits are protected by the checks documented in [CONTRIBUTING.md](CONTRIBUTING.md). Install the repository hook with `pre-commit install`; each phase must pass the full hook before commit.

## Highlights

- NVIDIA GPU discovery, utilization, VRAM, temperature, and power telemetry.
- Reasoner, worker, and retrieval pools with explicit GPU assignment.
- Local `llama.cpp` and compatible model profiles, loaded and unloaded per GPU pool from the interface.
- A weight-file library with configurable model roots, Hugging Face search and download, and local upload.
- Dependency-scoped failure handling: a failed step blocks only its own descendants.
- Real workflows for code, translation, and general tasks with dependency-aware steps.
- A reusable workflow-template catalog with validated default, private, and shared DAGs.
- Per-task tokens, output throughput, duration, average/peak GPU power, and estimated Wh.
- A terminal log-analysis task on every non-chat workflow, with the Markdown execution report kept separate from the main deliverable.
- Python, Node.js, Java, PHP, HTML, and CSS sandbox support.
- Markdown viewer with GFM, Mermaid, DOMPurify, and syntax highlighting.
- Persistent users, session authentication, `admin` and `user` roles.
- English/French interface with `Automatic`, `English`, and `Français` selection. Automatic follows a French browser locale and falls back to English.
- Role-aware tab navigation that separates execution, workflow design, run history, and system administration.
- Permission-aware history cleanup for the current user or, when authorized, every user.
- Parallel workflow scheduling with a visible FIFO queue and per-user, per-session workspaces.
- Responsive desktop-style shell with permission-aware navigation and bilingual mobile layouts.

## Navigation

- **Execution** contains workflow creation, the sandbox/local tool plane, the active DAG, step outputs, metrics, and deliverables.
- **Workflows** is a dedicated RBAC-protected library for validated defaults, owned workflows, sharing, editing, deletion, graph previews, and reasoner-generated proposals. Choosing **Use** returns to Execution with that template selected.
- Saved, generated, and currently edited workflows can be visualized as dependency graphs. The editor redraws its DAG live and reports unknown dependencies or cycles directly in the preview.
- **History** contains recent runs and the event stream. Selecting a run displays its complete report and artifacts without returning to the workflow launcher. Use **Continue session** on one of your previous runs to start a follow-up in its existing session workspace; users cannot continue another user's session, including administrators.
- **Administration** contains access control, execution policy, GPU pools, telemetry, model selection, and runtime controls. The hardware control plane retains the latest 15 minutes of measurements and draws power, GPU load, VRAM use, and temperature charts for every configured pool, labelled with its domain. This tab is rendered only for administrators.

The interface uses a persistent sidebar on desktop, an icon rail on tablets, and bottom navigation on phones. See [UI_ARCHITECTURE.md](UI_ARCHITECTURE.md) for the layout rationale, reference patterns, responsive behavior, and visual dependency policy.

## RBAC access control

Skein uses composable RBAC profiles rather than a single hard-coded administrator flag. A user can hold multiple profiles.

| Default profile | Granted capabilities |
|---|---|
| Super Administrator | Every permission, including cross-user workflow reads |
| User Manager | Create/update users, reset passwords, activate accounts, assign profiles |
| Settings Manager | Execution policy, GPU pools, assignments, and stack controls |
| Model Manager | Model registry/runtimes and privacy-safe server statistics |
| Workflow Operator | Execute workflows and read only owned workflows |
| Workflow Runner | Execute workflows and manage personal run history; browse templates without editing them |
| Workflow Designer | Browse templates and create, edit, share, or delete owned templates without workflow execution rights |
| Statistics Auditor | Read privacy-safe operational statistics only |

Backend permissions are `users.manage`, `settings.manage`, `models.manage`, `workflows.execute`, `workflows.read_own`, `workflows.read_all`, `workflows.delete_own`, `workflows.delete_all`, and `server_stats.read`. The Workflows menu requires `workflow_templates.read`; its write controls additionally require `workflow_templates.manage_own` or `workflow_templates.manage_all`. UI visibility follows the same server-issued permission list; hiding a control is never the authorization boundary.

Workflow-template permissions are `workflow_templates.read`, `workflow_templates.manage_own`, and `workflow_templates.manage_all`. See [WORKFLOW_TEMPLATES.md](WORKFLOW_TEMPLATES.md) for validation, ownership, sharing, and API rules.

Registration and email verification add `users.verify` and `email.manage` permissions. The default User Manager can manually approve pending registrations; the default Settings Manager can configure and test SMTP delivery.

Administrators can:

- manage users and roles;
- reset passwords and activate/deactivate accounts;
- manage models, pools, GPU assignments, and the full stack;
- choose whether standard users may switch between Sandbox and Local execution.

Standard users can create and inspect only their own workflows. They cannot change models or system settings. Local/Sandbox selection is available only when enabled by an administrator.

The History tab lets Workflow Operators permanently delete their own workflow history and associated deliverables. Only profiles granted `workflows.delete_all` can delete history for every user. Both actions require an explicit browser confirmation, and running workflows block matching history deletion.

The server statistics endpoint (`GET /api/server-stats`) exposes dated request-step metadata: anonymized request reference, model, role, status, token counts, tokens/s, duration, average/peak watts, and estimated Wh. It deliberately excludes objectives, prompts, usernames, user IDs, results, deliverables, and artifacts. The request reference is a truncated SHA-256 digest and cannot be used to retrieve workflow content through the statistics API.

## Registration and email verification

Public registration requires a unique username, unique email address, and a password of at least eight characters. A new account receives the Workflow Operator profile but has no effective permissions until its email is verified.

- Verification codes contain six digits, expire after ten minutes, and are stored only as salted PBKDF2-SHA256 hashes.
- Successful verification consumes the code atomically; it cannot be reused.
- Sending a new code invalidates every previous code immediately.
- Resends are limited to one per minute, invalid verification attempts are limited to five per code, and registration attempts are rate-limited per client address.
- An authorized User Manager can approve a pending account manually. Manual approval invalidates all outstanding codes.
- Pending accounts can only inspect their verification state, submit a code, request a resend, or sign out. Every workflow and administration endpoint rejects them.

Configure outbound mail under **Administration → Outbound email server**. Supported transport modes are STARTTLS, implicit SSL/TLS, and plain SMTP. On Windows, the SMTP password is encrypted with the current account's DPAPI key before being stored; the API never returns it. On other platforms, set `SKEIN_SMTP_PASSWORD` instead of persisting the password. Use **Send test email** to verify the connection before enabling user onboarding.

On the first start, Skein creates a bootstrap administrator. Configure it before that first start:

```powershell
$env:SKEIN_ADMIN_USER = "admin"
$env:SKEIN_ADMIN_PASSWORD = "replace-with-a-long-password"
.\run-skein.cmd
```

If no environment variables are supplied, the development fallback is `admin` / `admin`. Sign in and replace this password immediately from Access Control. Passwords use PBKDF2-SHA256 with per-user salts; session cookies are HttpOnly and SameSite Strict.

## Start and stop

Start the supervised frontend, backend, and model-process stack:

```powershell
.\run-skein.cmd
```

Open [http://127.0.0.1:8787/](http://127.0.0.1:8787/). The header controls restart or stop the full stack. Direct `python app.py` startup is supported for development but cannot control the supervisor.

## Real models

Use **Auto-detect and load local models** to discover a local `llama-server.exe` and GGUF model, or register profiles manually. Normal workflows require active reasoner and worker endpoints. Simulation is disabled unless explicitly enabled for development:

The Model Plane also offers **Discover available models**. It scans the configured model roots, stores discovered GGUF entries in the persistent registry, and lets a Model Manager assign a role and GPU pool before saving, loading, or stopping a runtime. Discovering a model does not load it; loading and stopping always update the persistent runtime state. File discovery never depends on finding a runtime: weights are registered even when no `llama-server` executable is present, and the report states which roots were scanned, how many files were seen, and what was skipped.

### Loading and unloading models manually

Every registered model exposes **Save**, **Load**, **Unload**, **Runtime log**, and **Unregister**. Choose a runnable role (`reasoner`, `worker`, `embedding`, `reranker`) and, optionally, a GPU pool; a model kept as `available` stays in the registry without reserving a port. Leaving the pool on *Unassigned* keeps the stored assignment instead of clearing it. When a pool is selected, the GPUs assigned to it are passed to the runtime through `CUDA_VISIBLE_DEVICES`, which is how a model is pinned to specific GPU nodes.

Runtime output is captured to a per-model log instead of being discarded, so a runtime that fails to start reports the real reason in the interface. Loading returns success only when the process actually started; a saved assignment whose runtime could not start is reported as an error rather than as a load.

Unloading terminates the runtime even when it outlived a previous Skein process: the recorded PID is verified against the running image name before being terminated, so a recycled PID is never killed by mistake. On startup Skein re-attaches to runtimes that are still answering, instead of reporting that no model is loaded.

### Model library, downloads, and uploads

**Available weight files** lists every `.gguf` file under the configured roots with its size, quantization, and registration state, and registers any of them in one action. Model roots are configurable from the interface, or with `SKEIN_MODEL_ROOTS` (path-separated). `SKEIN_RUNTIME_PATHS` adds runtime executables, and `SKEIN_MIN_MODEL_MB` (default 64) sets the size floor below which a file is ignored.

**Download from Hugging Face** searches GGUF repositories, lists the weight files of one repository with sizes and quantization, and downloads a selected file with live progress, cancellation, and resume of an interrupted transfer. Set `SKEIN_HF_TOKEN` (or `HF_TOKEN`) to reach gated repositories. Uploading a local `.gguf` file streams it to disk without buffering it in memory; `SKEIN_MAX_UPLOAD_GB` (default 80) bounds the accepted size. Downloaded and uploaded weights land in Skein's own model library, are registered automatically, and are the only files the **Unregister** action can delete from disk.

Model endpoints are `GET/POST /api/models`, `POST /api/models/discover`, `GET /api/models/files`, `POST /api/models/files/register`, `GET/POST /api/models/roots`, `POST /api/models/{id}/configure`, `POST /api/models/{id}/activate`, `POST /api/models/{id}/stop`, `GET /api/models/{id}/logs`, `DELETE /api/models/{id}`, `GET /api/models/huggingface/search`, `GET /api/models/huggingface/files`, `POST /api/models/huggingface/download`, `GET /api/models/downloads`, `POST /api/models/downloads/{id}/cancel`, and `POST /api/models/upload`. All of them require `models.manage`.

The Execution view reports live runtime state instead of static domain labels. For both reasoner and worker it shows endpoint health, active tasks versus shared task capacity, queued tasks, recent average tokens per second, and recent average execution time. The recap also shows active and queued workflows, current GPU power from `nvidia-smi`, CPU utilization, and used/total RAM. CPU package and RAM watts are read from LibreHardwareMonitor/OpenHardwareMonitor WMI sensors when available. Without those sensors, Skein displays clearly marked estimates: CPU load applied to a logical-core power envelope and used RAM at 0.375 W/GB. Pool charts only contain GPU telemetry after one or more GPUs have been assigned to that pool; unavailable readings are labelled explicitly instead of being fabricated.

Command and script executions record a resource window in their result. Local mode is labeled `local_machine`; sandbox execution is labeled `docker_container_host_window`. The Docker value is an attribution estimate from host load during that container's execution window, not a direct container power measurement. Reports include GPU watts, estimated CPU/RAM watts, CPU utilization, RAM usage, execution time, and the attribution scope.

```powershell
$env:SKEIN_ALLOW_SIMULATION = "1"
python app.py
```

## Execution modes

- **Sandbox** (default): Docker, no network, limited CPU/RAM/PIDs, read-only root filesystem, copied workflow workspace, and a timeout.
- **Local**: native Windows runtimes and PowerShell with real host filesystem access from the workflow artifact directory. The UI requires confirmation before local execution.

Supported sandbox images include Python 3.12, Node.js 22, Java 21, PHP 8.4, and Alpine for shell/HTML workflows.

## Workflow execution and failure handling

A task succeeds when it produced usable content. Self-reported confidence is reporting metadata, never a success criterion: a model that omits `confidence`, returns it as text such as `high`, or reports a low value still completes its task. Values are normalised from numbers, percentages, and common wordings; anything unrecognised is stored as unknown rather than as zero. A low or unknown confidence on a worker task escalates it to the reasoner instead of failing it.

A task fails only when it returned nothing usable — a backend error, an unparsable answer, an empty result, or a failing command or script. Failure is then contained to the branch that depends on it:

- an LLM task that failed is retried; `SKEIN_MAX_TASK_ATTEMPTS` (default 2) bounds the attempts;
- once attempts are exhausted, only the transitive descendants of the failed task become `BLOCKED`, with the unmet dependency named in their result;
- independent branches keep running to completion;
- the `workflow-reporter` always runs last, so the audit covers what succeeded as well as what did not;
- a task that raises is recorded as a failed task instead of stopping the orchestrator, and an unresolvable dependency blocks its task rather than aborting the run.

A workflow ends `COMPLETED` when every task completed, and `FAILED` when any task failed or was blocked. `summary.failed_tasks` and `summary.blocked_tasks` report the split.

## Metrics

Inference token counts come from the model server response. Tokens/s uses generated tokens over inference time. GPU power is sampled through `nvidia-smi` during each task; Wh is an estimate derived from average sampled watts and task duration. Shared simultaneous GPU activity may be attributed to more than one task. A physical wattmeter is required for certified whole-system energy measurement.

## Data

The default SQLite database and artifacts live under `%LOCALAPPDATA%\Skein`. Override the database path with `SKEIN_DB_PATH`. Generated models, databases, workflow artifacts, logs, and local secrets are excluded from version control.

Workflow deliverables are isolated under `users/<user-id>/sessions/<session-id>/workflows/<workflow-id>/artifacts`. Opaque identifiers keep paths stable when usernames change and prevent collisions between concurrent sessions. Existing workflows without session metadata remain readable through the `legacy` session namespace.

Continuing an owned workflow creates a separate workflow record and artifact directory, but preserves its parent session ID so related work remains in the same per-user session tree. The continuation source is recorded for auditability; it does not expose prior objectives or results to other users.

Skein runs up to two workflows concurrently by default and keeps additional submissions in a FIFO queue. Configure workflow and task concurrency independently before startup:

```powershell
$env:SKEIN_MAX_PARALLEL_WORKFLOWS = "2"
$env:SKEIN_TASK_WORKERS = "4"
.\run-skein.cmd
```

Queued workflows expose their queue position through the workflow API and History/Execution interface. A workflow moves from `QUEUED` to `RUNNING` only when a workflow slot is available.

## Tests

```powershell
python -B -m unittest tests.test_smoke -v
python -B -m unittest tests.test_auth -v
python -B -m unittest tests.test_workflow_resilience -v
python -B -m unittest tests.test_model_manager -v
python -B tests/sandbox_e2e.py
python -B tests/live_e2e.py
```

Each suite sets its own database path at import time, so suites must be run as separate processes. `python -B tests/run_precommit_tests.py` does this for the full pre-commit set.

The live test requires loaded reasoner and worker endpoints. Sandbox tests require Docker and the runtime images listed above.

### Manual full workflow matrix

The manual matrix runs every workflow visible to the chosen account, one after another, with simple, medium, complex, and very complex cases. It creates `4 × workflow count` real runs, so it is intentionally excluded from pre-commit hooks and automated CI. The script refuses to start without the explicit `--run` switch.

```powershell
$env:SKEIN_TEST_USERNAME = "admin"
$env:SKEIN_TEST_PASSWORD = "your-password"
python scripts/manual_workflow_matrix.py --run --base-url http://127.0.0.1:8787
```

Each workflow must finish before the next begins. Results are written as timestamped JSON and Markdown files under `manual-test-results` by default. Use `--timeout`, `--output-dir`, or `--stop-on-failure` when needed. The JSON retains each objective, task status, metrics, final deliverable, and separate execution report. Do not commit result files, as they may contain user-visible generated content.

The output directory is resolved from the Skein project root and created before the first run. `results.partial.json` is replaced atomically after every case, so a later workflow or collection failure does not discard earlier results. Failures continue to the next case by default. Every case receives a `runs/<case>/` directory containing `workflow.json`, `executions.json`, `skein-report.md`, and `deliverables.zip` when artifacts exist. The final `analysis.md` and `results.json` aggregate completion, failures, tokens, throughput, duration, energy, reports, artifacts, logs, and collection anomalies.

If execution completed but a previous script version failed during final export, recover the newest matching runs from Skein history without executing them again:

```powershell
python scripts/manual_workflow_matrix.py --recover-existing
```

## License

MIT — see [LICENSE](LICENSE).
