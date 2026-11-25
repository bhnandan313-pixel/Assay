# Release Checklist

Follow these steps to build, verify, and publish a clean release of `assay-runtime` to PyPI.

## 1. Pre-Release Verification

- [ ] Ensure the working tree is clean on `main` and all CI checks pass.
- [ ] Run the full test suite and linter locally:
  ```bash
  ruff check .
  pytest tests/ -v
  ```
- [ ] Confirm `CHANGELOG` / `README.md` / `docs/` reflect all user-facing changes.

## 2. Bump Version

Update the `version` field in `pyproject.toml` following Semantic Versioning (`MAJOR.MINOR.PATCH`):

```toml
[project]
name = "assay-runtime"
version = "0.1.0"
```

Commit the version bump and create an annotated Git tag (e.g., `git tag -a v0.1.0 -m "Release v0.1.0"`).

## 3. Build Distribution Archives

Remove any stale build artifacts and build both the source distribution (`.tar.gz`) and wheel (`.whl`) using `build`:

```bash
rm -rf dist/ build/
python -m pip install --upgrade build twine
python -m build
```

## 4. Validate Package Metadata

Run `twine check` to verify the rendered `README.md` and distribution metadata before uploading:

```bash
twine check dist/*
```

Confirm `PASSED` for every archive in `dist/`.

## 5. Publish to PyPI

Upload the verified archives using a scoped PyPI API token (`__token__` username, or configured `~/.pypirc`):

```bash
twine upload dist/*
```

Push the commit and release tag (`git push origin main --tags`) and verify installation in a fresh virtual environment:

```bash
pip install --no-cache-dir assay-runtime==<version>
assay --help
```
