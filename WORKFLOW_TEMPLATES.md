# Workflow template architecture

Workflow templates are reusable DAG definitions. They are separate from workflow runs: a template describes how work should be structured, while a run records one prompt, its task state, results, metrics, and artifacts.

## Template model

Each template stores a name, description, objective hint, search tags, owner, sharing state, and one to twelve tasks. Every task has a stable key, title, role, dependencies, and normalized complexity, risk, and criticality scores.

Every task also has an executable contract:

- `action_type`: `llm`, `command`, or `script`;
- `action_config`: the command text, or the script runtime, path, and complete source;
- `system_prompt`: a task-specific instruction for LLM actions;
- `output_format`: `text`, `markdown`, `json`, `files`, `exit_code`, or `boolean`;
- `output_schema`: a precise description of the expected result.

An optional `action_config.condition` uses the same three action types and must declare a boolean output. A false condition completes the node as skipped without executing its main action. Existing templates that predate typed contracts are hydrated as LLM tasks with role-specific prompts and explicit output contracts when they are read.

Validation requires:

- unique task keys and supported roles;
- existing, unique dependencies with no self-reference;
- scores between zero and one;
- an acyclic dependency graph;
- exactly one terminal task using the `integrator` or `workflow-reporter` role;
- valid action configuration, output format, and output schema for every task and condition.

Skein automatically appends a `workflow-reporter` LLM task to every non-chat template. It depends on every preceding task and receives their results plus workflow events and command/script execution logs. Its system prompt requires a factual Markdown audit covering chronological step outcomes, errors, logs, timing, tokens, power, uncertainty, anomalies, and recommendations. Templates tagged `chat`, including the system Simple chat template, deliberately omit this extra step.

The reporter is operational metadata, not the user's deliverable. The workflow API therefore exposes the last completed non-reporter result as `final_output` and the reporter result separately as `execution_report`. The Execution and History views present both independently.

System templates are seeded for general delivery, software implementation, translation, security-sensitive changes, simple chat, daily assistance, research and synthesis, and specification derived from code. They are shared, immutable, and validated during database initialization.

## Access rules

- `workflow_templates.read` exposes system templates, shared templates, and templates owned by the current user.
- `workflow_templates.manage_own` permits creation, editing, sharing, and deletion of owned templates.
- `workflow_templates.manage_all` permits management of every non-system template.
- System templates cannot be edited or deleted, including by administrators.

Sharing changes visibility but never transfers ownership or edit rights.

## API

- `GET /api/workflow-templates` lists visible templates.
- `GET /api/workflow-templates/{id}` returns one visible template.
- `POST /api/workflow-templates` creates an owned template.
- `POST /api/workflow-templates/select` asks the active reasoner to select the best visible template and returns its concise reason and confidence.
- `POST /api/workflow-templates/generate` asks the active reasoner to propose a template and returns it only after structural validation.
- `POST /api/workflow-templates/{id}` updates an editable template, including its sharing state.
- `DELETE /api/workflow-templates/{id}` deletes an editable non-system template.

## Planning modes

`POST /api/workflows` accepts a `planning_mode`:

- `template` executes the selected visible `template_id`;
- `automatic` gives the active reasoner the visible validated template catalog and asks it to select exactly one template. The server rejects unknown identifiers and never silently substitutes a different template;
- `generate` asks the reasoner for a new DAG, validates it, and executes the validated ephemeral plan without silently saving it as a reusable template.

The response identifies the chosen or generated planning source. Template generation and prompt execution remain separate API operations: generating a proposal never executes the user prompt, and executing in generation mode performs a fresh validated planning task.

## Execution interface

The Execution and Workflows tabs expose the lifecycle without conflating design and runtime actions. Workflow design lives in its dedicated RBAC-protected Workflows view, not in the execution request:

1. Enter the user objective.
2. Choose a saved workflow, automatic selection, or automatic generation.
3. In the Workflows view, use **Generate workflow** to open a dedicated generation request. The validated proposal opens in the editor for review and optional saving; this action never creates a run.
4. Use **Execute prompt** to create a run with the selected planning mode.

The workflow library lists system, private, and shared templates. Permission-aware controls let an owner or authorized administrator use, edit, share or unshare, and delete non-system templates. Every create or update request is validated again by the server, so editing JSON in the browser cannot bypass DAG validation.

The execution form keeps planning controls on their own row and gives the user request a full-width, resizable editor. Selecting a saved workflow therefore never compresses the prompt into the remaining horizontal space.

Every saved template has a **View graph** action with two generated representations:

- an algorithm/activity diagram with start and end terminators, typed action nodes, dependency arrows, conditional diamonds, and true/false branches;
- a UML-style sequence diagram with User, Orchestrator, Reasoner, Worker, and command/script Runtime lifelines, chronological messages, optional condition fragments, and parallel markers.

Both representations support zoom buttons, mouse-wheel zoom, reset, drag-to-pan, and fullscreen display. When the browser denies the native Fullscreen API, the viewer uses an application-level fullscreen fallback. The workflow editor includes the same dual visualization as a live preview: changing the task JSON redraws both diagrams after a short debounce. Unknown dependencies, empty task sets, and cycles are displayed inside the preview instead of leaving stale diagrams on screen. Generated proposals are visualized before they can be saved.

When no real reasoner is loaded, automatic selection and generation return actionable errors. Deterministic selection and generation are available only when `SKEIN_ALLOW_SIMULATION=1`, which is reserved for tests and explicit development mode. Automatic selection runs once when the workflow is launched; it is not invoked on every edit of the user prompt.
