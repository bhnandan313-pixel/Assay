# Contributing to Assay

We welcome contributions from the community! This guide walks you through setting up a development environment, running tests, and submitting pull requests.

## Table of Contents
- [Prerequisites](#prerequisites)
- [Setup](#setup)
- [Development Workflow](#development-workflow)
- [Testing](#testing)
- [Code Style & Linting](#code-style--linting)
- [Documentation](#documentation)
- [Submitting a Pull Request](#submitting-a-pull-request)
- [Additional Resources](#additional-resources)

## Prerequisites
- **Python** ≥ 3.11 (tested on 3.11 and 3.12)
- **Git**
- **Node.js** (optional, only needed for frontend examples)
- **Playwright browsers** – installed via `playwright install chromium`

## Setup
```bash
# Clone the repository
git clone https://github.com/your-org/assay.git
cd assay

# Create a virtual environment
python -m venv .venv
.\.venv\Scripts\activate  # on Windows
# source .venv/bin/activate  # on Unix/macOS

# Upgrade pip and install the package with development extras
python -m pip install --upgrade pip
pip install -e "[dev]"

# Install Playwright browsers (required for UI recording/replay)
playwright install chromium
```

## Development Workflow
1. **Create a branch** for your feature or bug fix:
   ```bash
   git checkout -b feature/awesome-feature
   ```
2. **Make changes** following the project's coding standards (see *Code Style & Linting*).
3. **Run the test suite** frequently to ensure you haven't introduced regressions.
4. **Commit** your changes with clear, prefixed messages (e.g., `feat: add UI recorder` or `fix: correct diff logic`).
5. **Push** the branch and open a Pull Request against `main`.

## Testing
```bash
# Run the full test suite with verbose output
pytest -vv

# Run a subset of tests (optional)
pytest tests/unit/test_diff.py
```
All tests must pass on **both** Python 3.11 and 3.12. The CI workflow enforces this.

## Code Style & Linting
The project uses **Ruff** for linting and **Black** for formatting (optional). Before committing, run:
```bash
ruff check .          # Linting
ruff check . --fix   # Auto‑fix simple issues
black .               # Optional: format with Black
```
The CI pipeline will reject any PR that fails linting.

## Documentation
Documentation lives under the `docs/` directory and is written in plain Markdown. When you add a new feature, please:
- Update the relevant `docs/*.md` file with an overview and usage examples.
- Keep diagrams ASCII‑based for easy rendering on GitHub.
- Ensure any new public API is reflected in the generated reference documentation (if a docs generator is added in the future).

## Submitting a Pull Request
- **Title**: Use a concise, descriptive title prefixed with the appropriate tag (`feat:`, `fix:`, `docs:`).
- **Description**: Explain the problem being solved, the approach taken, and any relevant screenshots or examples.
- **Linked Issues**: Reference the Issue ID (e.g., `Closes #123`).
- **Tests**: Include unit or integration tests that cover the new behavior.
- **CI**: Ensure the GitHub Actions checks pass before requesting a review.

## Additional Resources
- [Project Roadmap](README.md#roadmap)
- [Architecture Overview](docs/architecture.md)
- [Recording Format Specification](docs/recording-format.md)

Thank you for helping make Assay better! 🚀
