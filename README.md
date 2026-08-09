# Skein

Skein is a local, GPU-aware multi-agent workflow orchestrator. It separates reasoning and worker roles, routes tasks to real local models, exposes task outputs and project artifacts, and can execute generated code either in isolated Docker sandboxes or directly on the host.

## Highlights

- NVIDIA GPU discovery, utilization, VRAM, temperature, and power telemetry.
- Reasoner, worker, and retrieval pools with explicit GPU assignment.
- Local `llama.cpp` and compatible model profiles.
- Real workflows for code, translation, and general tasks with dependency-aware steps.
- Per-task tokens, output throughput, duration, average/peak GPU power, and estimated Wh.
- End-of-workflow summary, Markdown audit report, individual artifacts, and project ZIP downloads.
- Python, Node.js, Java, PHP, HTML, and CSS sandbox support.
- Markdown viewer with GFM, Mermaid, DOMPurify, and syntax highlighting.
- Persistent users, session authentication, `admin` and `user` roles.
- English/French interface with `Automatic`, `English`, and `Français` selection. Automatic follows a French browser locale and falls back to English.
- Role-aware tab navigation that separates workflow execution, run history, and system administration.

## Navigation

- **Execution** contains workflow creation, the sandbox/local tool plane, the active DAG, step outputs, metrics, and deliverables.
- **History** contains recent runs and the event stream. Selecting a run displays its complete report and artifacts without returning to the workflow launcher.
- **Administration** contains access control, execution policy, GPU pools, telemetry, model selection, and runtime controls. This tab is rendered only for administrators.

## Access control

Administrators can:

- manage users and roles;
- reset passwords and activate/deactivate accounts;
- manage models, pools, GPU assignments, and the full stack;
- choose whether standard users may switch between Sandbox and Local execution.

Standard users can create and inspect only their own workflows. They cannot change models or system settings. Local/Sandbox selection is available only when enabled by an administrator.

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
