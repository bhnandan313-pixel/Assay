# Determinism Roadmap

Assay currently replays recorded sessions using real network calls and a live database. While this provides high‑fidelity verification, true **deterministic replay**—where an execution can be reproduced bit‑for‑bit without external variability—is a longer‑term goal. This roadmap outlines the incremental steps we will take to achieve that vision.

## Phase 1: Capture‑Replay Isolation (v2.x)
- **Record full request/response bodies** (including binary payloads) in the recording format.
- **Snapshot the database state** before each session using a lightweight dump (e.g., SQLite backup or PostgreSQL `pg_dump`).
- **Export environment variables** and system time offsets.
- **Introduce a `--dry-run` mode** that validates the recording structure without invoking the target service.

## Phase 2: Virtual Network Layer (v3.x)
- Implement a **mock HTTP server** that can serve recorded responses based on the `X‑Assay‑Trace‑ID` header.
- Provide a **network shim** that intercepts outbound HTTP calls and routes them to the mock server.
- Add **configurable latency injection** to emulate realistic network timing while remaining deterministic.

## Phase 3: Virtual Time & Scheduler (v4.x)
- Integrate a **virtual clock** (e.g., `freezegun`‑style) that replaces `time.time()` and `datetime.now()` calls throughout the application under test.
- Record **timer and sleep events** during the baseline run and replay them deterministically.
- Support **event‑driven scheduling** where background jobs (Celery, APScheduler) are captured and replayed in a controlled order.

## Phase 4: Full System Snapshot (v5.x)
- Containerize the **entire runtime environment** (OS, Python interpreter, dependencies) using Docker or reproducible builds.
- Store **container images** or **build hashes** alongside the recording to guarantee identical runtime.
- Provide a **single‑command replay** that launches the recorded container with the virtual network and time layers.

## Phase 5: Cryptographic Guarantees (v6.x)
- Sign the **deterministic snapshot** (including container image digest, database dump hash, and network mock manifest) with Ed25519.
- Enable downstream users to **verify the integrity** of the replay environment before running verification.

## Benefits
- **Zero‑flakiness**: Tests no longer depend on external services or timing.
- **Regulatory compliance**: Deterministic runs can be audited and reproduced for compliance checks.
- **Faster CI**: Mocked network reduces test suite runtime dramatically.

---
*This roadmap is intentionally high‑level; detailed design specifications will be added in future documentation as each phase is implemented.*
