import ast
import sys
import unittest
from pathlib import Path

from scripts import generate_sbom


ROOT = Path(__file__).resolve().parents[1]


class SbomTest(unittest.TestCase):
    def test_committed_sbom_is_current_and_has_a_complete_dependency_graph(self):
        generated = generate_sbom.generate()
        committed = generate_sbom.load_json(generate_sbom.DEFAULT_OUTPUT)
        self.assertEqual(committed, generated)
        references = {component["bom-ref"] for component in committed["components"]}
        root_dependency = committed["dependencies"][0]
        self.assertEqual(set(root_dependency["dependsOn"]), references)
        self.assertEqual(len(references), len(committed["components"]))

    def test_frontend_dependencies_are_pinned_and_integrity_protected(self):
        components = generate_sbom.frontend_components()
        self.assertEqual({component["name"] for component in components}, {"dompurify", "highlight.js", "i18next", "lucide", "marked", "mermaid"})
        self.assertTrue(all(component["version"] and component["hashes"] for component in components))
        self.assertTrue(all(component["hashes"][0]["alg"] == "SHA-384" for component in components))

    def test_all_sandbox_images_are_declared(self):
        components = generate_sbom.container_components()
        self.assertEqual({component["purl"] for component in components}, {
            "pkg:docker/alpine@3.20", "pkg:docker/eclipse-temurin@21-jdk-alpine",
            "pkg:docker/node@22-alpine", "pkg:docker/php@8.4-cli-alpine", "pkg:docker/python@3.12-alpine",
        })

    def test_python_sources_use_only_the_standard_library_and_local_modules(self):
        local_modules = {path.stem for path in ROOT.glob("*.py")} | {"scripts", "tests"}
        third_party = set()
        for path in list(ROOT.glob("*.py")) + list((ROOT / "scripts").glob("*.py")) + list((ROOT / "tests").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [alias.name.split(".")[0] for alias in node.names]
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names = [node.module.split(".")[0]]
                else:
                    continue
                third_party.update(name for name in names if name not in sys.stdlib_module_names and name not in local_modules)
        self.assertEqual(third_party, set(), f"Declare new Python dependencies before use: {sorted(third_party)}")


if __name__ == "__main__":
    unittest.main()
