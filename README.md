# Assay – The Verification Runtime for Agent‑Written Code

![Assay Logo](https://raw.githubusercontent.com/your-org/assay/main/assets/logo.png)

## 🎯 Overview
Assay is a **full‑stack behavioral verification platform** that records, replays, and validates the runtime behavior of applications – from backend API responses to frontend UI interactions. It enables you to catch regressions early by comparing a **baseline** (known‑good) version against a **candidate** (new) version, generating a cryptographic attestation of the results.

Key capabilities:
- **Browser recording** with Playwright, capturing UI actions and trace IDs.
- **Unified diff engine** that compares JSON payloads, HTTP status codes, headers, and DOM states.
- **Evidence generation** with Ed25519 signing for tamper‑evident attestations.
- **Extensible CLI** (`assay`) for recording, replaying, verifying, and publishing results.

## 🏗️ Architecture
```
+-------------------+      +-------------------+      +-------------------+
|   Baseline App   | ---> |   Assay Proxy     | ---> |   Recorder (Play- |
|   (v1)           |      |   (captures UI ↔  |      |   wright)         |
+-------------------+      |   backend traffic)|      +-------------------+
                            +-------------------+                |
                                   |                           |
                                   v                           v
                     +-------------------+      +-------------------+
                     |   Candidate App   | ---> |   Assay Proxy     |
                     |   (v2)            |      |   (replay)        |
                     +-------------------+      +-------------------+
```
*The proxy injects `X‑Assay‑Trace‑ID` headers to correlate UI actions with API calls.*

## ⚡ Quick‑Start
```bash
# Install the package (including dev extras for testing)
pip install "assay[dev]"

# Install Playwright browsers (required for UI recording)
playwright install chromium

# Record a baseline run (environment variable selects mode)
ASSAY_MODE=baseline assay record --target http://localhost:8000

# Record a candidate run (introduce regressions)
ASSAY_MODE=candidate assay record --target http://localhost:8001

# Verify and generate an attestation
assay verify --baseline recordings/baseline.json \
            --candidate recordings/candidate.json \
            --output evidence.json

# Verify the attestation (anyone with the public key can do this)
assay verify-attestation --evidence evidence.json --pubkey pubkey.pem
```

## 📚 Documentation
- **Architecture details:** `docs/architecture.md`
- **Recording format (v2.0.0):** `docs/recording-format.md`
- **Determinism roadmap:** `docs/determinism-roadmap.md`
- **Contribution guide:** `CONTRIBUTING.md`
- **Code of conduct:** `CODE_OF_CONDUCT.md`

## 🚀 Roadmap
| Milestone | Description |
|-----------|-------------|
| M0‑M3 | Foundations, differential engine, evidence generation |
| **M4** | **Unified Behavioral Diffing** (UI + API) |
| **M5** | **Release Automation & Documentation** (this release) |
| M6‑M11 | Advanced deterministic replay, distributed tracing, ecosystem integrations |

## 🤝 Contributing
We welcome contributions! Please read our [CONTRIBUTING.md](CONTRIBUTING.md) for setup, testing, and PR guidelines.

---
© 2026 Assay Project. All rights reserved.
