# Skein interface architecture

## Product model

Skein is an agentic operations workspace rather than a conventional chat application. Its interface combines the low-friction task entry of modern AI assistants with the inspectability of a local infrastructure console.

The layout uses three stable regions:

1. **Primary sidebar** — Execution, History, and Administration. The available destinations come from server-issued RBAC permissions.
2. **System header** — authenticated user, language, control-plane health, and stack lifecycle controls.
3. **Task canvas** — the active product view, optimized independently for creation, review, or administration.

This separation prevents account, infrastructure, navigation, and workflow actions from competing in one toolbar.

## Reference patterns

The design deliberately adopts established interaction patterns without reproducing any product-specific visual identity:

- ChatGPT, Claude, Gemini, Ollama, and Open WebUI use persistent navigation and make task creation the primary action.
- Claude separates conversational work, longer-running agentic work, and code work into distinct destinations. Skein similarly separates Execution, History, and Administration.
- Gemini and Open WebUI use side panels to keep project/history navigation available without displacing the active content.
- Open WebUI separates administrator capabilities from the normal user workspace and exposes features according to permissions.

## View responsibilities

### Execution

- Objective composer and model-role context first.
- Sandbox/local execution tools immediately below the composer.
- Active workflow, task graph, step results, and deliverables after the creation controls.
- Operational controls remain visible only when the current permission set allows them.

### History

- Recent workflows and event activity appear before the selected workflow details.
- Each row exposes objective, creation date, short reference, and state.
- Keyboard activation is supported with Enter and Space.
- Destructive cleanup remains visually separated and requires confirmation.

### Administration

- User and profile management, execution policy, email, server statistics, hardware, and models are grouped into separate surfaces.
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

Static interface copy uses English canonical markup with explicit `data-i18n`, `data-i18n-placeholder`, or `data-i18n-value` keys. Dynamic components call the same translation service directly. DOM text scanning remains only as a compatibility bridge for older workflow-result templates and should not be used for new interface copy.

Future framework adoption should replace the current incremental DOM scripts only when it brings typed components, reliable state management, testability, and an accessible component system without blocking the working prototype.
