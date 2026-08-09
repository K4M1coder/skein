# Contributing to Skein

All source code, identifiers, comments, commit messages, and technical documentation must be written in English. User-facing interface copy belongs in the English and French i18next catalogs.

## Local checks

Install the Git hook after cloning the repository:

```powershell
python -m pip install pre-commit
pre-commit install
pre-commit run --all-files
```

The hook validates repository hygiene, YAML and JSON syntax, critical Python errors with Ruff, JavaScript syntax with Node.js, localization parity, smoke behavior, and authentication/RBAC behavior. The Python suites run in separate processes because the prototype owns a process-global workflow executor.

Every development phase must pass the complete hook before it is committed. Do not bypass the hook with `--no-verify`.
