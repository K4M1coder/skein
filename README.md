# Skein

Skein is a local, GPU-aware multi-agent workflow orchestrator. It separates reasoning and worker roles, routes tasks to real local models, exposes task outputs and project artifacts, and can execute generated code either in isolated Docker sandboxes or directly on the host.

## Engineering policy

Skein favors mature, actively maintained libraries over custom security, protocol, parsing, and persistence code. Dependencies are selected for maintenance health, tests, documentation, license compatibility, supply-chain cost, and safe upgrade paths. Framework migrations remain incremental and test-backed so that existing users, workflows, and artifacts continue to work. See [DEPENDENCY_POLICY.md](DEPENDENCY_POLICY.md) for the selection checklist and current adoption roadmap.

Development commits are protected by the checks documented in [CONTRIBUTING.md](CONTRIBUTING.md). Install the repository hook with `pre-commit install`; each phase must pass the full hook before commit.

## Highlights

- NVIDIA GPU discovery, utilization, VRAM, temperature, and power telemetry.
- Reasoner, worker, and retrieval pools with explicit GPU assignment.
- Local `llama.cpp` and compatible model profiles.
- Real workflows for code, translation, and general tasks with dependency-aware steps.
- A reusable workflow-template catalog with validated default, private, and shared DAGs.
- Per-task tokens, output throughput, duration, average/peak GPU power, and estimated Wh.
- End-of-workflow summary, Markdown audit report, individual artifacts, and project ZIP downloads.
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
- **History** contains recent runs and the event stream. Selecting a run displays its complete report and artifacts without returning to the workflow launcher.
- **Administration** contains access control, execution policy, GPU pools, telemetry, model selection, and runtime controls. This tab is rendered only for administrators.

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

The Execution view reports live runtime state instead of static domain labels. For both reasoner and worker it shows endpoint health, active tasks versus shared task capacity, queued tasks, recent average tokens per second, and recent average execution time. The recap also shows active and queued workflows, current GPU power from `nvidia-smi`, CPU utilization, and used/total RAM. CPU package and RAM watts are read from LibreHardwareMonitor/OpenHardwareMonitor WMI sensors when available. Without those sensors, Skein displays clearly marked estimates: CPU load applied to a logical-core power envelope and used RAM at 0.375 W/GB.

Command and script executions record a resource window in their result. Local mode is labeled `local_machine`; sandbox execution is labeled `docker_container_host_window`. The Docker value is an attribution estimate from host load during that container's execution window, not a direct container power measurement. Reports include GPU watts, estimated CPU/RAM watts, CPU utilization, RAM usage, execution time, and the attribution scope.

```powershell
$env:SKEIN_ALLOW_SIMULATION = "1"
python app.py
```

## Execution modes

- **Sandbox** (default): Docker, no network, limited CPU/RAM/PIDs, read-only root filesystem, copied workflow workspace, and a timeout.
- **Local**: native Windows runtimes and PowerShell with real host filesystem access from the workflow artifact directory. The UI requires confirmation before local execution.

Supported sandbox images include Python 3.12, Node.js 22, Java 21, PHP 8.4, and Alpine for shell/HTML workflows.

## Metrics

Inference token counts come from the model server response. Tokens/s uses generated tokens over inference time. GPU power is sampled through `nvidia-smi` during each task; Wh is an estimate derived from average sampled watts and task duration. Shared simultaneous GPU activity may be attributed to more than one task. A physical wattmeter is required for certified whole-system energy measurement.

## Data

The default SQLite database and artifacts live under `%LOCALAPPDATA%\Skein`. Override the database path with `SKEIN_DB_PATH`. Generated models, databases, workflow artifacts, logs, and local secrets are excluded from version control.

Workflow deliverables are isolated under `users/<user-id>/sessions/<session-id>/workflows/<workflow-id>/artifacts`. Opaque identifiers keep paths stable when usernames change and prevent collisions between concurrent sessions. Existing workflows without session metadata remain readable through the `legacy` session namespace.

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
python -B tests/sandbox_e2e.py
python -B tests/live_e2e.py
```

The live test requires loaded reasoner and worker endpoints. Sandbox tests require Docker and the runtime images listed above.

## License

MIT — see [LICENSE](LICENSE).
