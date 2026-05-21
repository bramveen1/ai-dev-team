# ops-diag pack

Gives agents read-only access to recent router log lines for self-service
incident diagnosis (issue #218).

## Verbs

| Verb | Description |
|---|---|
| `router_logs` | Fetch the most-recent N lines from the router's in-memory ring buffer via its internal `/logs` HTTP endpoint |

## How it works

The router keeps the last 2 000 formatted log lines in an in-memory ring
buffer (see `router/log_buffer.py`).  Lines are redacted before they are
stored — PAT-bearing clone URLs, GitHub tokens, bearer auth headers, and
`user_id=` query params are all replaced with `[REDACTED]` placeholders.

The `/logs` endpoint on the router's existing `aiohttp` server (default
port 8080) serves the buffered lines as JSON.  The handler calls this
endpoint from inside the agent container over the Docker-compose internal
network (`http://router:8080/logs`), so no external aggregator is needed.

## Limits

| Parameter | Default | Hard max |
|---|---|---|
| Lines returned (`tail`) | 200 | 500 |
| Response byte cap (`max_bytes`) | 50 000 | 200 000 |
| Lines kept in buffer | 2 000 | — |

## Granting access

```
grant sam ops-diag
```

No secrets or credentials are required — the grant simply adds `ops-diag`
to the agent's pack list.  Restart the agent container to pick up the change.

## Security notes

- Redaction is applied **before** a line enters the buffer, so redacted data
  is never stored in memory or served to an agent.
- The `/logs` endpoint is unauthenticated within the Docker-compose network.
  Treat the docker-compose network itself as the trust boundary.
- Only agents whose manifest lists `ops-diag` have `handler.py` in scope.
  Agents without the pack cannot invoke `router_logs`.

## Override the router URL

If your deployment uses a non-default router address, set
`ROUTER_LOGS_URL=http://<host>:<port>` in the agent container's environment.
