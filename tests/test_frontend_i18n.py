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


if __name__ == "__main__":
    unittest.main()
