# Skein interface architecture

## Product model

Skein is an agentic operations workspace rather than a conventional chat application. Its interface combines the low-friction task entry of modern AI assistants with the inspectability of a local infrastructure console.

The layout uses three stable regions:

1. **Primary sidebar** — Execution, Workflows, History, and Administration. The available destinations come from server-issued RBAC permissions.
2. **System header** — authenticated user, language, control-plane health, and stack lifecycle controls.
3. **Task canvas** — the active product view, optimized independently for creation, review, or administration.

This separation prevents account, infrastructure, navigation, and workflow actions from competing in one toolbar.

## Reference patterns

The design deliberately adopts established interaction patterns without reproducing any product-specific visual identity:

- ChatGPT, Claude, Gemini, Ollama Desktop, and Open WebUI use persistent navigation and make task creation the primary action.
- Claude separates conversational work, longer-running agentic work, and code work into distinct destinations. Skein similarly separates Execution, workflow design, History, and Administration.
- Gemini and Open WebUI use side panels to keep project/history navigation available without displacing the active content.
- Open WebUI separates administrator capabilities from the normal user workspace and exposes features according to permissions.
- Colibri.ai is a task-focused meeting assistant embedded in the active work context; Skein applies the same focus principle by showing only the controls relevant to the selected Execution, Workflows, History, or Administration context.

Reference documentation consulted during the audit: [Claude Desktop](https://docs.anthropic.com/en/docs/claude-code/desktop), [Open WebUI workspace](https://docs.openwebui.com/features/workspace/), [Open WebUI administration](https://docs.openwebui.com/features/administration/), [Colibri.ai feature overview](https://support.colibri.ai/hc/en-us/categories/360003014040-Feature-overview), [Lucide](https://lucide.dev/), and [i18next fallback behavior](https://www.i18next.com/principles/fallback).

## View responsibilities

### Execution

- Objective composer and model-role context first.
- Sandbox/local execution tools immediately below the composer.
- Active workflow, task graph, step results, and deliverables after the creation controls.
- Operational controls remain visible only when the current permission set allows them.

### Workflows

- The complete template library, generator, editor, sharing controls, and DAG previews live in their own view.
- Each DAG preview offers an algorithm/activity view and a sequence view, with fullscreen, zoom, reset, and drag-to-pan controls.
- `workflow_templates.read` controls menu visibility and read access.
- `workflow_templates.manage_own` and `workflow_templates.manage_all` independently control mutation actions.
- Selecting a template transfers it to the Execution composer without starting a run.

### History

- Recent workflows and event activity appear before the selected workflow details.
- Each row exposes objective, creation date, short reference, and state.
- Keyboard activation is supported with Enter and Space.
- Destructive cleanup remains visually separated and requires confirmation.

### Administration

- User and profile management, execution policy, email, server statistics, hardware, and models are grouped into separate surfaces.
- The model surfaces are ordered by intent: the registry and its runtime controls, then the local weight-file library, then remote acquisition from Hugging Face.
- The model registry refreshes only when the server state actually changes, so periodic polling never discards a role or pool selection in progress. An edited row is marked until it is saved, and every load, unload, save, and removal reports its outcome in the incident panel rather than failing silently.
- Forms use responsive grids on desktop and a single-column layout on narrow screens.
- Administrative buttons use a consistent primary action treatment instead of browser defaults.

## Responsive behavior

- At wide desktop sizes the sidebar is 248 px and content is centered within a 1600 px canvas.
- At tablet sizes the sidebar collapses to icons and the content becomes a single column.
- At phone sizes navigation becomes a fixed bottom bar, the header is simplified, and forms become single-column.

## Design tokens

The shell defines shared colors, spacing, radii, borders, and shadows in `static/shell.css`. The neutral dark surfaces keep long results readable; lime is reserved for selection, readiness, and primary actions. Amber, blue, and red communicate running, queued, and failed/destructive states.

## Maintained UI dependencies

- **Lucide 1.31.0** supplies consistent navigation icons under the ISC license.
- **i18next 26.3.6** provides language resolution and explicit English fallback while translations remain bundled for local use.
- Existing Markdown output continues to use pinned Marked, DOMPurify, Highlight.js, and Mermaid builds.

All CDN assets declare SHA-384 Subresource Integrity and anonymous CORS attributes. A compromised or unexpectedly changed CDN response is rejected by the browser instead of executing with application privileges.

Static interface copy uses English canonical markup with explicit `data-i18n`, `data-i18n-placeholder`, or `data-i18n-value` keys. Dynamic components and navigation call the same translation service directly. The localization layer never scans or rewrites arbitrary DOM text, which protects user input and keeps translations deterministic.

Workflow history dates follow the selected locale. Dynamic workflow metrics, GPU/model controls, result summaries, artifact actions, execution warnings, error contexts, and Markdown viewer controls use catalog keys rather than language-specific strings in feature code. Result grids set explicit minimum-width and wrapping constraints so long model output cannot expand the application canvas.

`tests/test_frontend_i18n.py` enforces English/French key parity, validates static markup keys, rejects the removed DOM-scanning mechanism, and guards feature logic against embedded French interface copy.

Future framework adoption should replace the current incremental DOM scripts only when it brings typed components, reliable state management, testability, and an accessible component system without blocking the working prototype.
