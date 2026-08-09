# Workflow template architecture

Workflow templates are reusable DAG definitions. They are separate from workflow runs: a template describes how work should be structured, while a run records one prompt, its task state, results, metrics, and artifacts.

## Template model

Each template stores a name, description, objective hint, search tags, owner, sharing state, and one to twelve tasks. Every task has a stable key, title, role, dependencies, and normalized complexity, risk, and criticality scores.

Validation requires:

- unique task keys and supported roles;
- existing, unique dependencies with no self-reference;
- scores between zero and one;
- an acyclic dependency graph;
- exactly one terminal task using the `integrator` role.

System templates are seeded for general delivery, software implementation, translation, and security-sensitive changes. They are shared, immutable, and validated during database initialization.

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
- `POST /api/workflow-templates/select` selects the best visible template for an objective and returns the score and matched terms.
- `POST /api/workflow-templates/generate` asks the active reasoner to propose a template and returns it only after structural validation.
- `POST /api/workflow-templates/{id}` updates an editable template, including its sharing state.
- `DELETE /api/workflow-templates/{id}` deletes an editable non-system template.

## Planning modes

`POST /api/workflows` accepts a `planning_mode`:

- `template` executes the selected visible `template_id`;
- `automatic` scores visible templates using objective terms, tags, names, descriptions, and objective hints;
- `generate` asks the reasoner for a new DAG, validates it, and executes the validated ephemeral plan without silently saving it as a reusable template.

The response identifies the chosen or generated planning source. Template generation and prompt execution remain separate API operations: generating a proposal never executes the user prompt, and executing in generation mode performs a fresh validated planning task.

## Execution interface

The Execution tab exposes the same lifecycle without conflating design and runtime actions. Workflow design lives in the workflow library, not in the execution request:

1. Enter the user objective.
2. Choose a saved workflow, automatic selection, or automatic generation.
3. In the workflow library, use **Generate workflow** to open a dedicated generation request. The validated proposal opens in the editor for review and optional saving; this action never creates a run.
4. Use **Execute prompt** to create a run with the selected planning mode.

The workflow library lists system, private, and shared templates. Permission-aware controls let an owner or authorized administrator use, edit, share or unshare, and delete non-system templates. Every create or update request is validated again by the server, so editing JSON in the browser cannot bypass DAG validation.

Every saved template has a **View graph** action. The graph lays tasks out by dependency depth and draws directed edges between them. The workflow editor includes the same visualization as a live preview: changing the task JSON redraws the DAG after a short debounce. Unknown dependencies, empty task sets, and cycles are displayed inside the preview instead of leaving a stale graph on screen. Generated proposals are visualized before they can be saved.

When no real reasoner is loaded, generation returns an actionable error. Deterministic generation is available only when `SKEIN_ALLOW_SIMULATION=1`, which is reserved for tests and explicit development mode.
