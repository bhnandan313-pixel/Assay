# Recording Format (v2.0.0)

Assay stores each verification run as a **single JSON document** that captures both frontend UI actions and backend API interactions. The format is versioned (currently `2.0.0`) to allow forward‑compatible extensions.

## Top‑Level Structure
```json
{
  "schema_version": "2.0.0",
  "metadata": {
    "recorded_at": "2026-11-25T15:32:10Z",
    "environment": {
      "os": "windows",
      "python_version": "3.12",
      "playwright_version": "1.42.0",
      "assay_version": "0.9.0"
    },
    "trace_id": "a1b2c3d4-e5f6-7g8h-9i10-j11k12l13m14"
  },
  "ui_actions": [
    {
      "action_id": "ui-1",
      "type": "click",
      "selector": "#login-button",
      "timestamp": "2026-11-25T15:32:12.345Z",
      "trace_id": "a1b2c3d4-e5f6-7g8h-9i10-j11k12l13m14"
    },
    {
      "action_id": "ui-2",
      "type": "navigate",
      "url": "http://localhost:8000/dashboard",
      "timestamp": "2026-11-25T15:32:13.210Z",
      "trace_id": "d4e5f6g7-h8i9-j10k-11l12-m13n14o15p16"
    }
  ],
  "network_interactions": [
    {
      "request_id": "req-1",
      "method": "GET",
      "url": "http://localhost:8000/api/user",
      "status_code": 200,
      "request_headers": {
        "Accept": "application/json",
        "X-Assay-Trace-ID": "a1b2c3d4-e5f6-7g8h-9i10-j11k12l13m14"
      },
      "response_headers": {
        "Content-Type": "application/json",
        "Cache-Control": "no-store"
      },
      "response_body": {
        "id": 42,
        "username": "alice",
        "created_at": "2026-11-25T15:30:00Z"
      },
      "timestamp": "2026-11-25T15:32:12.560Z"
    }
  ]
}
```

## Fields Description
| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Must be `2.0.0` for this release. |
| `metadata` | object | Capture of run‑time environment and a top‑level `trace_id` that groups the whole session. |
| `ui_actions` | array | Ordered list of UI interactions recorded by Playwright. Each entry includes a `trace_id` that matches the corresponding network request(s). |
| `network_interactions` | array | Detailed HTTP request/response pairs observed by the Assay proxy. The `X‑Assay‑Trace‑ID` header links back to UI actions. |

### Ignorable Dynamic Fields
When diffing recordings, these fields are typically **ignored** because they are expected to change on each run:
- `metadata.recorded_at`
- `ui_actions.timestamp`
- `network_interactions.timestamp`
- Any UUID‑style identifiers (`trace_id`, `action_id`, `request_id`).

The diff engine accepts a **JSONPath filter list** to automatically drop these fields before comparison.

---
*The format is deliberately extensible – future versions may add `visual_snapshots`, `performance_metrics`, or `custom_metrics` without breaking existing tools.*
