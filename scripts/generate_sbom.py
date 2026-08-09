#!/usr/bin/env python3
"""Generate and verify Skein's deterministic CycloneDX project SBOM."""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INVENTORY = ROOT / "sbom" / "dependency-inventory.json"
DEFAULT_OUTPUT = ROOT / "sbom" / "skein.cdx.json"
DEFAULT_SUMMARY = ROOT / "sbom" / "DEPENDENCIES.md"


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def npm_component(url: str, integrity: str) -> dict:
    if "/npm/" in url:
        remainder = url.split("/npm/", 1)[1]
        package_token = "/".join(remainder.split("/")[:2]) if remainder.startswith("@") else remainder.split("/", 1)[0]
        name, version = package_token.rsplit("@", 1)
    else:
        match = re.search(r"/ajax/libs/([^/]+)/([^/]+)/", url)
        if not match:
            raise ValueError(f"Unsupported pinned frontend dependency URL: {url}")
        name, version = match.group(1), match.group(2)
    digest_type, digest_value = integrity.split("-", 1)
    digest_hex = base64.b64decode(digest_value).hex()
    return {
        "type": "library", "name": name, "version": version, "scope": "required",
        "purl": f"pkg:npm/{name.replace('/', '%2F')}@{version}",
        "bom-ref": f"pkg:npm/{name.replace('/', '%2F')}@{version}",
        "hashes": [{"alg": digest_type.upper().replace("SHA", "SHA-"), "content": digest_hex}],
        "externalReferences": [{"type": "distribution", "url": url}],
        "properties": [{"name": "skein:source", "value": "static/index.html"}, {"name": "skein:sri", "value": integrity}],
    }


def frontend_components() -> list[dict]:
    markup = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    tags = re.findall(r"<(?:script|link)\b[^>]+>", markup, re.I)
    components = []
    for tag in tags:
        url_match = re.search(r"(?:src|href)=\"(https://[^\"]+)\"", tag)
        if not url_match:
            continue
        integrity_match = re.search(r"integrity=\"([^\"]+)\"", tag)
        crossorigin = re.search(r"crossorigin=\"anonymous\"", tag)
        if not integrity_match or not crossorigin:
            raise ValueError(f"External frontend dependency lacks SRI or anonymous CORS: {url_match.group(1)}")
        components.append(npm_component(url_match.group(1), integrity_match.group(1)))
    return components


def container_components() -> list[dict]:
    source = (ROOT / "app.py").read_text(encoding="utf-8")
    images = sorted(set(re.findall(r'"image"\s*:\s*"([^\"]+)"', source) + re.findall(r'image\s*=\s*"([^\"]+:[^\"]+)"', source)))
    components = []
    for image in images:
        repository, version = image.rsplit(":", 1)
        purl = f"pkg:docker/{repository}@{version}"
        components.append({
            "type": "container", "name": repository, "version": version, "scope": "optional", "purl": purl, "bom-ref": purl,
            "properties": [{"name": "skein:source", "value": "app.py"}, {"name": "skein:purpose", "value": "Sandbox execution runtime"}],
        })
    return components


def declared_components(inventory: dict) -> list[dict]:
    result = []
    for item in inventory["runtime_components"]:
        component = {key: item[key] for key in ("type", "name", "version", "scope", "purl")}
        component["bom-ref"] = component["purl"]
        component["properties"] = [{"name": "skein:purpose", "value": item["purpose"]}, {"name": "skein:source", "value": "sbom/dependency-inventory.json"}]
        result.append(component)
    return result


def syft_components(syft: str | None, scan_images: bool) -> list[dict]:
    if not syft:
        return []
    targets = [f"dir:{ROOT}"] + ([component["name"] + ":" + component["version"] for component in container_components()] if scan_images else [])
    result = []
    for target in targets:
        command = [syft, "scan", target, "-o", "cyclonedx-json"]
        completed = subprocess.run(command, capture_output=True, text=True, timeout=600, check=False)
        if completed.returncode:
            raise RuntimeError(f"Syft failed for {target}: {completed.stderr.strip()}")
        result.extend(json.loads(completed.stdout).get("components", []))
    return result


def component_key(component: dict) -> str:
    return component.get("purl") or component.get("bom-ref") or f"{component.get('type')}:{component.get('name')}@{component.get('version')}"


def generate(inventory_path: Path = DEFAULT_INVENTORY, syft: str | None = None, scan_images: bool = False) -> dict:
    inventory = load_json(inventory_path)
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    repository = inventory["project"]["repository"]
    root_ref = f"pkg:github/{urlparse(repository).path.strip('/')}@{version}"
    components = frontend_components() + container_components() + declared_components(inventory) + syft_components(syft, scan_images)
    unique = {}
    for component in components:
        key = component_key(component)
        if key not in unique:
            unique[key] = component
            continue
        for field in ("hashes", "externalReferences", "properties"):
            existing = unique[key].setdefault(field, [])
            for value in component.get(field, []):
                if value not in existing:
                    existing.append(value)
    ordered = [unique[key] for key in sorted(unique, key=str.lower)]
    tool = inventory["sbom_tool"]
    tools = [{"type": "application", "group": inventory["project"]["supplier"], "name": "Skein SBOM generator", "version": version}]
    if syft:
        tools.append({"type": "application", "group": tool["vendor"], "name": tool["name"], "version": tool["version"], "purl": tool["purl"]})
    return {
        "bomFormat": "CycloneDX", "specVersion": "1.6", "version": 1,
        "metadata": {
            "tools": {"components": tools},
            "component": {
                "type": "application", "name": inventory["project"]["name"], "version": version, "bom-ref": root_ref,
                "purl": root_ref, "supplier": {"name": inventory["project"]["supplier"]},
                "licenses": [{"license": {"id": inventory["project"]["license"]}}],
                "externalReferences": [{"type": "vcs", "url": repository}],
            },
            "properties": [{"name": "skein:deterministic", "value": "true"}, {"name": "skein:inventory-schema", "value": str(inventory["schema_version"])}],
        },
        "components": ordered,
        "dependencies": [{"ref": root_ref, "dependsOn": [component_key(component) for component in ordered]}] + [{"ref": component_key(component), "dependsOn": []} for component in ordered],
    }


def summary_markdown(sbom: dict) -> str:
    lines = ["# Skein dependency inventory", "", "This file is generated by `python scripts/generate_sbom.py`. Do not edit it manually.", "", "| Component | Version | Type | Scope | Package URL |", "|---|---|---|---|---|"]
    for component in sbom["components"]:
        lines.append(f"| {component['name']} | {component.get('version','')} | {component['type']} | {component.get('scope','')} | `{component.get('purl','')}` |")
    lines.extend(["", "The canonical machine-readable inventory is [`skein.cdx.json`](skein.cdx.json). Runtime components marked `operator-provided` must be resolved to deployed versions in release/deployment SBOMs. Use Syft enrichment to include discovered transitive packages.", ""])
    return "\n".join(lines)


def canonical_json(value: dict) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
        return
    except OSError as original_error:
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=path.suffix) as temporary:
                temporary.write(content)
                temporary_path = Path(temporary.name)
            os.replace(temporary_path, path)
            return
        except OSError:
            if temporary_path:
                temporary_path.unlink(missing_ok=True)
        if sys.platform != "win32" or not shutil.which("powershell"):
            raise
        command = "[IO.File]::WriteAllText($env:SKEIN_SBOM_WRITE_TARGET,[Console]::In.ReadToEnd(),[Text.UTF8Encoding]::new($false))"
        environment = {**os.environ, "SKEIN_SBOM_WRITE_TARGET": str(path)}
        completed = subprocess.run(["powershell", "-NoProfile", "-Command", command], input=content, text=True, capture_output=True, check=False, env=environment)
        if completed.returncode:
            raise OSError(f"Cannot write {path}: {completed.stderr.strip()}") from original_error


def resolve_syft(value: str | None) -> str | None:
    if value == "none":
        return None
    if value and value != "auto":
        path = shutil.which(value) or (value if Path(value).is_file() else None)
        if not path:
            raise FileNotFoundError(f"Syft executable not found: {value}")
        return str(path)
    system_syft = shutil.which("syft")
    if system_syft:
        return system_syft
    version = load_json(DEFAULT_INVENTORY)["sbom_tool"]["version"]
    local_app_data = os.getenv("LOCALAPPDATA")
    managed = Path(local_app_data) / "Skein" / "tools" / "syft" / version / "syft.exe" if local_app_data else None
    if managed and managed.is_file():
        return str(managed)
    if value == "auto":
        raise FileNotFoundError("Syft was requested but is not installed. Run scripts/install-syft.ps1 first.")
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, default=DEFAULT_INVENTORY)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--summary", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--syft", default="none", help="Syft executable, auto, or none. Use Syft only for enriched deployment SBOMs.")
    parser.add_argument("--scan-images", action="store_true", help="Ask Syft to merge packages from locally available sandbox images.")
    parser.add_argument("--check", action="store_true", help="Fail when committed deterministic SBOM files are stale.")
    args = parser.parse_args()
    if args.scan_images and args.syft == "none":
        parser.error("--scan-images requires --syft auto or an explicit Syft executable")
    sbom = generate(args.inventory.resolve(), resolve_syft(args.syft), args.scan_images)
    rendered, summary = canonical_json(sbom), summary_markdown(sbom)
    if args.check:
        stale = []
        if not args.output.is_file() or args.output.read_text(encoding="utf-8") != rendered:
            stale.append(str(args.output))
        if not args.summary.is_file() or args.summary.read_text(encoding="utf-8") != summary:
            stale.append(str(args.summary))
        if stale:
            print("SBOM is stale: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"SBOM is current: {len(sbom['components'])} components")
        return 0
    write_text(args.output, rendered)
    write_text(args.summary, summary)
    print(f"Generated {args.output} with {len(sbom['components'])} components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
