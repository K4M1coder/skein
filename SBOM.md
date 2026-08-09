# Software Bill of Materials

Skein maintains a deterministic, committable CycloneDX JSON SBOM at [`sbom/skein.cdx.json`](sbom/skein.cdx.json). The generated human-readable inventory is [`sbom/DEPENDENCIES.md`](sbom/DEPENDENCIES.md), and operator-supplied runtime requirements are declared in [`sbom/dependency-inventory.json`](sbom/dependency-inventory.json).

## Generate and verify

Regenerate the canonical project SBOM after changing a dependency, CDN URL, sandbox image, runtime requirement, or project version:

```powershell
python scripts/generate_sbom.py
python scripts/generate_sbom.py --check
```

The check runs in the pre-commit suite and fails when either committed generated file is stale. It also fails when a Python source imports a third-party module that has not been introduced through the dependency process. Frontend dependencies are discovered from `static/index.html`; every remote asset must have a pinned version, SHA-384 SRI digest, and anonymous CORS. Sandbox images are discovered from `app.py`. The manual inventory supplies host/runtime components that cannot be derived reliably from source code.

The generator is deterministic: it omits timestamps and random serial numbers, sorts components and dependency references, and produces stable UTF-8 JSON. `VERSION` controls the root application version.

## Syft enrichment

[Syft](https://github.com/anchore/syft) is the selected maintained discovery engine. The project pins the expected generator version in the inventory. On Windows, the installer downloads the pinned official release, verifies its SHA-256 digest against the official release checksum file, and installs it under `%LOCALAPPDATA%\Skein\tools` rather than the repository:

```powershell
.\scripts\install-syft.ps1
```

Then enrich a deployment-specific SBOM with packages discovered in the source directory:

```powershell
python scripts/generate_sbom.py --syft auto --output sbom/skein.deployment.cdx.json --summary sbom/DEPLOYMENT-DEPENDENCIES.md
```

To merge packages from locally available sandbox images as well:

```powershell
python scripts/generate_sbom.py --syft auto --scan-images --output sbom/skein.deployment.cdx.json --summary sbom/DEPLOYMENT-DEPENDENCIES.md
```

Image scanning can be slow and requires Docker plus locally available images, so it is not part of pre-commit. Deployment SBOMs must resolve components marked `operator-provided` to the exact deployed versions. The canonical project SBOM records the project-level dependency contract; the enriched deployment SBOM records observed transitive packages.

## Maintenance rules

- Update `VERSION` for a release and regenerate the SBOM.
- Never hand-edit generated SBOM files.
- Add host tools and non-discoverable components to `sbom/dependency-inventory.json`.
- Keep package URLs and component scopes accurate.
- Review licenses and security advisories before dependency updates.
- Generate a Syft-enriched SBOM for every distributed deployment or image set.
