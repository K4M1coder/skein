import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class FrontendLocalizationTest(unittest.TestCase):
    def setUp(self):
        self.catalog_source = (ROOT / "static" / "i18n.js").read_text(encoding="utf-8")
        english, french = self.catalog_source.split("\n    fr: {", 1)
        self.english_keys = set(re.findall(r"^\s{6}([A-Za-z][A-Za-z0-9]*):", english, re.MULTILINE))
        self.french_keys = set(re.findall(r"^\s{6}([A-Za-z][A-Za-z0-9]*):", french, re.MULTILINE))

    def test_catalogs_have_identical_keys(self):
        self.assertEqual(self.english_keys, self.french_keys)
        self.assertGreater(len(self.english_keys), 100)

    def test_static_markup_keys_exist(self):
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        keys = set(re.findall(r'data-i18n(?:-placeholder|-value)?="([A-Za-z][A-Za-z0-9]*)"', markup))
        self.assertTrue(keys)
        self.assertEqual(set(), keys - self.english_keys)

    def test_external_assets_are_versioned_and_integrity_protected(self):
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        external_tags = re.findall(r'<(?:script|link)\b[^>]+https://[^>]+>', markup)
        self.assertGreaterEqual(len(external_tags), 7)
        for tag in external_tags:
            with self.subTest(tag=tag):
                self.assertIn('integrity="sha384-', tag)
                self.assertIn('crossorigin="anonymous"', tag)
                self.assertRegex(tag, r"@?\d+\.\d+\.\d+")

    def test_model_controls_collapse_at_the_tablet_breakpoint(self):
        styles = (ROOT / "static" / "control.css").read_text(encoding="utf-8")
        self.assertIn("@media(max-width:980px)", styles)
        self.assertIn(".model-form,.model-row{grid-template-columns:1fr}", styles)
        self.assertIn(".model-row>*{min-width:0}", styles)

    def test_a_gpu_card_offers_one_checkbox_per_pool_without_losing_a_toggle_to_polling(self):
        """A single GPU commonly serves every pool at once (reasoner, worker, and retrieval
        sharing one card), so the GPU card must not force an exclusive pool choice. It must
        also survive the 2.5s poll: loadHardware() used to overwrite a just-picked assignment
        with stale data mid-request (same class of bug already fixed once for the model list),
        so each (gpu,pool) toggle keeps its own pending draft until the request settles."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("gpu-pool-check", feature_source)
        self.assertIn("data-gpu=\"${encodeURIComponent(gpu.id)}\" data-pool=\"${pool.id}\"", feature_source)
        self.assertIn("assigned:checked", feature_source)
        self.assertNotIn("<select data-gpu=", feature_source)
        self.assertIn("gpuDrafts", feature_source)
        self.assertIn("gpuDrafts.set(key,checked)", feature_source)
        self.assertIn("gpuDrafts.delete(key)", feature_source)
        self.assertIn("catch(error){showError(error,tr('hardwareControlPlane'))}", feature_source)

    def test_pool_telemetry_charts_are_interactive_and_freeze_on_hover(self):
        """Each metric gets gridlines, an end-value label, and a hover crosshair/tooltip
        instead of a bare polyline; a poll must not rebuild a chart the operator is
        actively reading, or the tooltip would flicker away mid-hover."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        for marker in ("telemetry-grid", "telemetry-tooltip", "telemetry-crosshair", "telemetry-end-dot",
                       "telemetryHovered", "pointerenter", "pointermove", "pointerleave"):
            with self.subTest(marker=marker):
                self.assertIn(marker, feature_source)
        self.assertIn("telemetryHovered)return", feature_source)
        styles = (ROOT / "static" / "control.css").read_text(encoding="utf-8")
        self.assertIn(".telemetry-grid-2", styles)
        self.assertIn(".telemetry-tooltip", styles)

    def test_vram_estimate_discloses_its_method(self):
        """Per-model VRAM is an estimate (no per-process nvidia-smi data on this stack), so
        the breakdown must carry its disclosure as a visible affordance, not just a code comment."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("renderVramEstimate", feature_source)
        self.assertIn("gpu.vram_estimation_method", feature_source)
        self.assertIn("estimatedVramByModel", feature_source)
        styles = (ROOT / "static" / "control.css").read_text(encoding="utf-8")
        self.assertIn(".vram-estimate", styles)

    def test_hardware_poll_discards_stale_out_of_order_responses(self):
        """Toggling several pool checkboxes for the same GPU in quick succession fires
        several overlapping /api/hardware fetches; a slow, stale one must not be allowed
        to overwrite a fresher render (regression: an assignment could look reverted even
        though it had actually succeeded, because the last response to *arrive* won
        regardless of which request was actually the newest)."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("hardwareRequestSeq", feature_source)
        self.assertIn("requestId!==hardwareRequestSeq", feature_source)
        self.assertIn("requestId===hardwareRequestSeq", feature_source)

    def test_pool_cards_are_color_coded_and_list_their_models(self):
        """Every pool card showed the same four fixed metric colors, so a shared GPU made
        Reasoner/Workers/Retrieval indistinguishable at a glance; the pool's own color now
        marks the card, and it names which model(s) are configured for it."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("border-left:3px solid ${pool.color}", feature_source)
        self.assertIn("pool-models", feature_source)
        self.assertIn("noModelConfigured", feature_source)
        styles = (ROOT / "static" / "control.css").read_text(encoding="utf-8")
        self.assertIn(".pool-models", styles)

    def test_execution_page_echoes_runtime_telemetry_next_to_the_model_nodes(self):
        """The Hardware admin page is not the only place an operator watches GPU health;
        the reasoner/worker runtime cards already on the Execution page now carry a
        compact echo of the same telemetry instead of requiring a tab switch."""
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn('data-runtime-chart="reasoner"', markup)
        self.assertIn('data-runtime-chart="workers"', markup)
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("data-runtime-chart", feature_source)
        self.assertIn("runtimeCharts.forEach", feature_source)

    def test_a_reasoner_model_pointed_at_a_worker_pool_is_flagged(self):
        """role and pool_id are independent fields (workflow tier vs. GPU pool), so a
        mismatch (a reasoner-role model assigned to a worker-domain pool) is never
        rejected — it can be exactly what an operator wants — but it is easy to create by
        accident and was previously invisible, so it must be flagged for review."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("poolRoleMismatch", feature_source)
        self.assertIn("ROLE_EXPECTED_DOMAIN", feature_source)
        self.assertIn("modelPoolMismatch", feature_source)
        styles = (ROOT / "static" / "model-manager.css").read_text(encoding="utf-8")
        self.assertIn(".model-warning", styles)

    def test_model_pool_select_survives_the_hardware_fetch_race(self):
        """loadModels() and loadHardware() start together, and /api/models usually answers
        first since /api/hardware waits on nvidia-smi — so the pool <option> list is empty
        on renderModels()'s very first call. The render-skip signature must track the pools
        array too, or a model's pool select renders permanently blank (regression:
        confirmed live — a correctly-persisted pool_id still showed as "Unassigned" because
        no other change ever bumped the signature afterward)."""
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("JSON.stringify(pools.map(p=>p.id))", feature_source)

    def test_legacy_dom_translation_scanner_is_removed(self):
        self.assertNotIn("TreeWalker", self.catalog_source)
        self.assertNotIn("MutationObserver", self.catalog_source)
        self.assertNotIn("const pairs", self.catalog_source)

    def test_feature_logic_does_not_embed_french_interface_copy(self):
        feature_source = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
        forbidden = ["Aucun workflow", "Planification…", "Exécuter localement", "Visualiser en Markdown"]
        for phrase in forbidden:
            with self.subTest(phrase=phrase):
                self.assertNotIn(phrase, feature_source)

    def test_workflow_generation_and_execution_are_distinct_actions(self):
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        manager = (ROOT / "static" / "workflow-templates.js").read_text(encoding="utf-8")
        self.assertRegex(markup, r'id="generate-workflow"[^>]+type="submit"|type="submit"[^>]+id="generate-workflow"')
        self.assertIn('button type="submit" data-i18n="executePrompt"', markup)
        execution_form = markup.split('<form id="create">', 1)[1].split('</form>', 1)[0]
        catalog = markup.split('<section class="workflow-catalog workspace">', 1)[1].split('<section class="runtime-gate">', 1)[0]
        self.assertNotIn('id="generate-workflow"', execution_form)
        self.assertIn('id="workflow-generator"', catalog)
        self.assertIn('id="generate-workflow"', catalog)
        self.assertIn('/api/workflow-templates/generate', manager)
        self.assertIn('executionPayload()', manager)
        self.assertIn('planning_mode', manager)

    def test_administration_is_grouped_into_domain_submenu(self):
        navigation = (ROOT / "static" / "navigation.js").read_text(encoding="utf-8")
        auth = (ROOT / "static" / "auth.js").read_text(encoding="utf-8")
        shell = (ROOT / "static" / "shell.css").read_text(encoding="utf-8")
        for domain in ("access", "policy", "email", "stats", "hardware", "models"):
            with self.subTest(domain=domain):
                self.assertIn(f'key: "{domain}"', navigation)
        self.assertIn("data-admin-view", navigation)
        self.assertIn("sub-navigation", navigation)
        for cls in ("admin-access", "admin-policy", "admin-email", "admin-stats"):
            with self.subTest(cls=cls):
                self.assertIn(f'"{cls}"', auth)
        self.assertNotIn('id="rbac-users"></div><div id="rbac-settings">', auth)
        self.assertIn(".sub-navigation", shell)

    def test_admin_panels_escape_user_controlled_html(self):
        """Usernames and emails are registered free text rendered into the access-control
        panel: unescaped interpolation is stored XSS running in every user manager's session."""
        auth = (ROOT / "static" / "auth.js").read_text(encoding="utf-8")
        self.assertIn("const esc", auth)
        self.assertIn("${esc(user.username)}", auth)
        self.assertIn('${esc(user.email)||t("noEmail")}', auth)
        self.assertIn("${esc(session.user.username)}", auth)
        self.assertIn("${esc(user.permissions", auth)
        self.assertNotIn("<b>${user.username}</b>", auth)

    def test_browser_history_buttons_drive_the_active_view(self):
        """activate() mirrors the view into location.hash, so every navigation creates a
        browser history entry. Without a hashchange listener, Back and Forward only rewrote
        the URL and left the interface on the view it was already showing."""
        navigation = (ROOT / "static" / "navigation.js").read_text(encoding="utf-8")
        self.assertIn('addEventListener("hashchange"', navigation)
        self.assertIn("requested !== activeView", navigation)

    def test_the_language_selector_is_scoped_not_duplicated_by_id(self):
        """The sign-in overlay can now appear while the signed-in user bar is on the page,
        and both carry a language selector: sharing one id would wire only the first."""
        auth = (ROOT / "static" / "auth.js").read_text(encoding="utf-8")
        self.assertIn('select class="language-choice"', auth)
        self.assertNotIn('select id="language-choice"', auth)
        self.assertIn("const wireLanguage = root =>", auth)
        self.assertNotIn('document.querySelector("#language-choice")', auth)

    def test_a_401_reopens_the_sign_in_overlay_over_an_opaque_backdrop(self):
        """A session that ends mid-use used to surface as a generic incident panel over a
        stale page. Every fetch wrapper must route 401 to the sign-in overlay instead, and
        the overlay must hide the page behind it rather than dim it."""
        auth = (ROOT / "static" / "auth.js").read_text(encoding="utf-8")
        styles = (ROOT / "static" / "auth.css").read_text(encoding="utf-8")
        self.assertIn("window.skeinAuth = { requireSignIn }", auth)
        self.assertIn('showLogin(t("sessionExpired"))', auth)
        self.assertIn('document.querySelector("#viewer-dialog")?.close()', auth)
        self.assertIn(".auth-overlay{position:fixed;inset:0;z-index:10000;background:var(--bg)", styles)
        self.assertIn(".auth-locked{overflow:hidden}", styles)
        for name in ("app.js", "workflow-templates.js", "model-manager.js"):
            with self.subTest(script=name):
                source = (ROOT / "static" / name).read_text(encoding="utf-8")
                self.assertIn("skeinAuth?.requireSignIn()", source)
        # auth.js owns requireSignIn, so it calls it directly — but only outside /api/auth,
        # where a 401 is the expected answer the sign-in form renders inline.
        self.assertIn('response.status === 401 && !path.startsWith("/api/auth/")', auth)
        self.assertIn("requireSignIn();", auth)
        self.assertIn("if(error?.silent)return", (ROOT / "static" / "app.js").read_text(encoding="utf-8"))

    def test_workflow_editor_has_live_graph_preview(self):
        markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
        manager = (ROOT / "static" / "workflow-templates.js").read_text(encoding="utf-8")
        self.assertIn('id="workflow-editor-graph"', markup)
        self.assertIn('id="workflow-graph-panel"', markup)
        # One renderer owns graph drawing and validation: workflow-diagrams.js. The editor
        # and the catalog both call it, so no second copy can drift out of sync.
        diagrams = (ROOT / "static" / "workflow-diagrams.js").read_text(encoding="utf-8")
        self.assertIn("graphCycleDetected", diagrams)
        self.assertNotIn("const drawGraph=", manager)
        self.assertIn('window.skeinWorkflowDiagrams.render($("#workflow-editor-graph")', manager)
        self.assertIn('addEventListener("input",updateEditorGraph)', manager)


if __name__ == "__main__":
    unittest.main()
