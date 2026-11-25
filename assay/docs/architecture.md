# Assay Architecture

Assay is structured around four decoupled, black-box verification subsystems connected by versioned Pydantic v2 data contracts.

## Subsystem Overview

```text
RecordingSession ──► ReplayRunner ──► ReplaySessionReport (Baseline & Candidate)
                                                │
                                                ▼
EvidenceBundle ◄── EvidenceGenerator ◄── SessionComparisonRunner
```

### 1. Recorder Proxy (`src/assay/recorder/`)
- **Implementation:** `RecordingProxyServer` (`aiohttp.web` + `aiohttp.ClientSession`).
- **Behavior:** Accepts inbound HTTP traffic on a local port (`127.0.0.1:9000` by default), forwards requests to the upstream target without hop-by-hop headers, measures `elapsed_ms` via `time.perf_counter()`, and passes captured metadata through `Redactor` before appending to `RecordingSession`.
- **Resilience:** Client responses are always returned unredacted, even if background recording encounters an unexpected exception.

### 2. Replay Client & Runner (`src/assay/replay/`)
- **Implementation:** `ReplayClient` (`httpx.AsyncClient`) and `ReplayRunner`.
- **Behavior:** Loads a `RecordingSession` via `SessionStore`, strips conflicting hop-by-hop headers (`Host`, `Content-Length`, `Transfer-Encoding`), applies optional dynamic header overrides, and dispatches requests either sequentially (`concurrency=1`) or concurrently (`asyncio.Semaphore` + `asyncio.gather`).
- **Data Contract:** Produces `ReplayExecutionPair` items aggregated into a `ReplaySessionReport` saved under `.assay/replays/`. Network timeouts and connection failures set `is_success=False` with `error_message` without aborting the queue.

### 3. Differential Comparator (`src/assay/diff/`)
- **Implementation:** `DifferentialComparator`, `SessionComparisonRunner`, and `normalizer.py`.
- **Behavior:** Compares baseline and candidate `ReplayExecutionPair`s:
  - Evaluates HTTP status codes and configured headers (`Content-Type`, `Cache-Control`).
  - Performs structural JSON diffing via `deepdiff.DeepDiff`, normalizing paths (`root['user']['age']` → `$.user.age`) and filtering out dynamic fields matching `ignore_paths`.
  - Classifies each interaction as `UNCHANGED`, `CHANGED`, `FAILED`, or `INCONCLUSIVE` in a `SessionDiffReport` saved under `.assay/verifications/`.

### 4. Attestation Signer & Evidence Packager (`src/assay/attest/`)
- **Implementation:** `KeyStore`, `sign_manifest`, `verify_manifest`, and `EvidenceGenerator`.
- **Behavior:** Computes a deterministic SHA-256 digest (`deltas_hash_sha256`) over sorted, canonicalized `InteractionDiff` objects, constructs an `EvidenceManifest`, signs its canonical UTF-8 JSON bytes with Ed25519, and persists `evidence_<verification_id>.json` under `.assay/evidence/`.
