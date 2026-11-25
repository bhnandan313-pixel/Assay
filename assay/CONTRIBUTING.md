# Contributing to Assay

Thank you for your interest in contributing to **Assay — The Verification Runtime for Agent-Written Code**!

## Development Environment Setup

Assay requires **Python 3.11+**.

1. Clone the repository and enter the project directory:
   ```bash
   git clone https://github.com/assay-runtime/assay.git
   cd assay
   ```

2. Create and activate a virtual environment:
   ```bash
   python -m venv .venv
   # Linux / macOS:
   source .venv/bin/activate
   # Windows PowerShell:
   .\.venv\Scripts\Activate.ps1
   ```

3. Install Assay in editable mode with all development and demo dependencies:
   ```bash
   pip install -e ".[dev]"
   ```

## Running Tests

Run the full unit and integration test suite using `pytest`:

```bash
pytest tests/ -v
```

To run only unit tests or only integration tests:

```bash
pytest tests/unit/ -v
pytest tests/integration/ -v
```

## Code Quality & Formatting

We use `ruff` for linting and import/style checks:

```bash
ruff check .
```

Please ensure all new modules include:
- Strict Python type annotations (`from __future__ import annotations`).
- Concise docstrings on public classes, functions, and Pydantic models.
- Unit tests covering both happy paths and failure/edge cases.

## Pull Request Checklist

Before opening a pull request, verify that:
- [ ] `pytest tests/ -v` passes with zero failures or skipped assertions.
- [ ] `ruff check .` reports no errors.
- [ ] New CLI options or configuration fields are documented in `README.md` or `docs/`.
- [ ] No sensitive credentials, private keys, or `.assay/` artifacts are committed.
