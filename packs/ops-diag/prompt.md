# Ops-diagnostic pack

You have read-only access to recent router log lines for self-service
incident diagnosis.

## Verb

```
python /config/packs/ops-diag/handler.py router_logs [--tail N] [--max-bytes N]
```

| Flag | Default | Max | Description |
|---|---|---|---|
| `--tail` | 200 | 500 | Number of most-recent lines to retrieve |
| `--max-bytes` | 50 000 | 200 000 | Byte cap on the returned log block |

## Output format

```json
{
  "status": "ok",
  "lines": ["2026-05-20 ...", "..."],
  "line_count": 42
}
```

When the router's log buffer is not yet initialised (e.g. just restarted):

```json
{"status": "unavailable", "lines": [], "line_count": 0}
```

On network / router error:

```json
{"status": "error", "error": "...", "lines": [], "line_count": 0}
```

## Usage guidance

- Prefer small `--tail` values (50–100) first; increase only if needed.
- All sensitive values (tokens, user IDs) are redacted server-side before
  the lines reach you.  You will never see raw credentials.
- Use this for **read-only** diagnosis only.  You cannot modify logs.
- Report errors back to the operator if `status` is not `"ok"`.
