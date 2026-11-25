# Architecture Overview

Assay provides end‑to‑end behavioral verification by stitching together **frontend UI actions** and **backend API interactions** through a lightweight proxy. The architecture consists of the following core components:

1. **Assay Proxy** – an HTTP proxy that injects a unique `X‑Assay‑Trace‑ID` header into every request/response pair. This header enables correlation of UI events captured by Playwright with the corresponding backend calls.
2. **Playwright Recorder** – runs a Chromium browser instance, records user interactions (clicks, navigation, form inputs) and forwards the trace data to the proxy.
3. **Recording Store** – persisted JSON files that capture a full session: UI actions, network interactions, timestamps, and the injected trace identifiers.
4. **Differential Engine** – consumes two recordings (baseline & candidate) and performs:
   * **API Diff** – status‑code comparison, header diffing (with ignore filters), and deep JSON structural diff using `deepdiff`.
   * **UI Diff** – route changes, DOM element presence/absence, page title variations, and visual screenshot diffs (optional).
5. **Evidence Generator** – assembles a canonical JSON bundle containing the diff report, execution statistics, and configuration metadata. The bundle is cryptographically signed with Ed25519, producing a tamper‑evident attestation.
6. **CLI (`assay`)** – orchestrates the workflow: `record`, `replay`, `verify`, and `verify‑attestation` commands.

```
+-------------------+   +-------------------+   +-------------------+
|   Baseline App   |   |   Candidate App   |   |   Assay Proxy     |
|  (v1 – healthy)  |   | (v2 – buggy)      |   | (inject Trace‑ID) |
+--------+----------+   +--------+----------+   +--------+----------+
         |                       |                     |
         |  HTTP traffic (with  |  HTTP traffic (with |
         |   Trace‑ID header)   |   Trace‑ID header)  |
         v                       v                     v
   +-------------------+   +-------------------+   +-------------------+
   |   Playwright      |   |   Playwright      |   |   Diff Engine     |
   |   Recorder        |   |   Replayer        |   |   (API + UI)      |
   +-------------------+   +-------------------+   +-------------------+
```

**Data Flow**
1. The **Recorder** launches a browser against the **Baseline App** through the **Proxy**. UI actions are logged and each outgoing HTTP request receives a `X‑Assay‑Trace‑ID`.
2. The **Proxy** forwards traffic to the app and logs the corresponding responses, storing the trace ID alongside the request/response metadata.
3. The complete session is saved as a JSON file (`recordings/*.json`).
4. Replaying the **Candidate App** follows the same steps, generating a second recording.
5. The **Diff Engine** loads both recordings, correlates actions via the trace IDs, and produces a structured diff report.
6. The **Evidence Generator** signs the report, yielding an attestation that can be independently verified.

---
*The architecture is deliberately minimalistic to keep the runtime lightweight while still providing deep end‑to‑end visibility.*
