# Recording Format & Redaction Specification

Assay persists captured HTTP workloads as versioned JSON documents in `.assay/sessions/session_<session_id>.json` using atomic temp-file writes (`os.replace`) guarded by a thread lock.

## Schema (`version: "1.0.0"`)

A `RecordingSession` contains:
- `version`: Schema version string (`"1.0.0"`).
- `session_id`: Unique session identifier.
- `target_url`: Original upstream URL targeted during capture.
- `created_at`: UTC ISO-8601 timestamp.
- `interactions`: Ordered list of `RecordedInteraction` objects.

Each `RecordedInteraction` pairs:
- `interaction_id`: UUIDv4 string.
- `recorded_at`: UTC ISO-8601 timestamp.
- `request` (`RecordedRequest`): `method`, `url`, `path`, `query_params`, `headers` (`[{name, value}]`), `body`, `content_type`.
- `response` (`RecordedResponse`): `status_code`, `headers`, `body`, `content_type`, `elapsed_ms`.

### Binary & Non-UTF-8 Bodies

Empty bodies are stored as `null`. Text payloads are decoded using the response/request charset (defaulting to UTF-8). Non-UTF-8 binary payloads are encoded losslessly as `<binary:hex:<hex_digits>>` and decoded back to raw bytes during replay.

## Redaction Rules

Before any interaction is stored in memory or flushed to disk, `Redactor` scrubs sensitive data using the configured field list in `.assay.yaml` (defaulting to `Authorization`, `Cookie`, `Set-Cookie`, `password`, `secret`, `token`, `credit_card`):

1. **Headers:** Matched case-insensitively; values are replaced with `"[REDACTED]"`.
2. **JSON Bodies (`application/json` / `*+json`):** Parsed and walked recursively across nested objects and arrays; matching keys (case-insensitive) have their values replaced with `"[REDACTED]"`.
3. **Form Bodies (`application/x-www-form-urlencoded`):** Parsed via `parse_qsl`; matching parameter keys have their values replaced with `"[REDACTED]"`.
4. **Fallback:** Malformed or unstructured bodies are returned unchanged so capture never fails on invalid payloads.
