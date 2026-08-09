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
- `POST /api/workflow-templates/{id}` updates an editable template, including its sharing state.
- `DELETE /api/workflow-templates/{id}` deletes an editable non-system template.

Automatic selection, AI generation, and execution integration are implemented as separate layers so template persistence and authorization remain independently testable.

