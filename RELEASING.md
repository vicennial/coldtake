# Releasing `coldtake`

Releases are tag-driven via `.github/workflows/release.yml` using PyPI Trusted Publishing (OIDC, no long-lived API tokens).

## Checklist

1. Ensure `main` is clean and all checks pass:
   ```bash
   uv run --extra dev pytest -q
   uv run --extra dev ruff check src tests
   gitleaks detect --source .
   ```
2. Update the version in:
   - `src/coldtake/__init__.py` (`__version__`, source of truth)
   - `server.json` (`version` and `packages[0].version`)
   - `integrations/omnigent/pyproject.toml` (`version`, when releasing `coldtake-omnigent`)
3. Move entries under `## [Unreleased]` in `CHANGELOG.md` into `## [X.Y.Z] - YYYY-MM-DD`.
4. Commit the release prep on `main` and push.
5. Tag and push `vX.Y.Z`:
   ```bash
   git tag -a vX.Y.Z -m "coldtake X.Y.Z"
   git push origin vX.Y.Z
   ```
6. Wait for `.github/workflows/release.yml` to publish to PyPI and create the GitHub Release, then smoke-test from an empty folder:
   ```bash
   uvx --refresh coldtake --version
   uvx coldtake try
   ```
