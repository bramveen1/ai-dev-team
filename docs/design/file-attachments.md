# File-Attachments Shared Contract — Design 1-pager

_Status: draft, pending Bram sign-off · Owner: Sam · Parent: #325 · Closes #326_

## Goal

Define the shared contract that all file-attachment ingest paths (Slack v1, Notion fast-follow) must implement. The contract covers runtime deps, permission boundaries, storage layout, prompt surface, failure handling, rollback, and the abstraction split between common and source-specific code. Implementation issues (#327–#330 once filed) cite this doc for their contract.

## Non-goals (v1)

- Browser-based upload surface. Attachments arrive via Slack or Notion only.
- Worker-level attachment access. Workers see only what their prompt contains.
- Inline image rendering in Slack replies. Agents reference paths; rendering is out of scope.
- Deduplication across threads. Same file uploaded twice → two scratch-dir entries.

---

## Runtime deps

| Dependency | Required | Notes |
|---|---|---|
| Slack `files:read` OAuth scope | Yes | Per-agent Slack apps only (see *Permission boundaries*). |
| `/var/lib/attachments/` bind mount | Yes | Named Docker volume, router RW, agents RO. |
| Janitor pack hook | Yes | Mtime-based GC; runs on Sam container start (mirrors dispatch janitor). |
| Mimetype allowlist | Yes | `config/attachments/mimetype_allowlist.yaml`. Blocks executable content. |
| `python-magic` | Yes | Mimetype detection from file content, not extension. |
| LibreOffice headless (Office converter) | Yes | Converts `.docx`/`.xlsx`/`.pptx` → PDF for agent read. v1 scope — tracked in #329. |

The scratch-dir bind mount is declared in `docker-compose.yml` as a named volume (`attachments-scratch`) — not a host bind so it survives `docker compose down` for post-mortems.

---

## Permission boundaries

```
Slack workspace

│

├─ workers-bot app → no files:read scope (blocked at OAuth level)

│

└─ per-agent apps → files:read scope
   (sam-app, lisa-app, …)

│

▼

Router (uid 1000)

│  RW on /var/lib/attachments/

│  Downloads file, validates mimetype, writes to scratch dir

▼

Named agent containers (sam, lisa, …)

│  RO bind mount on /var/lib/attachments/

│  Path surfaced in prompt; agent calls Read/image/PDF tools

▼

Dispatch worker subprocesses

✗  No mount — workers inherit only what the prompt contains
```

Key rules:

- `files:read` lives on per-agent Slack apps, **not** on `workers-bot`. This is enforced at the OAuth app configuration level, not in code.
- The router performs all network I/O (download, signed-URL resolution). Agents never call out to Slack's CDN.
- Workers (headless dispatch subprocesses) do **not** see the attachments mount. If an attachment is relevant to a dispatch, the router must include the path explicitly in the dispatch prompt.

---

## Storage layout

```
/var/lib/attachments/          ← named volume mount

└── <thread_id>/               ← one dir per Slack/Notion thread
    ├── <sanitised_filename>   ← downloaded file (flat, no subdirs)
    └── …
```

**`<thread_id>`** is the Slack `thread_ts` (or Notion page ID for Notion paths), URL-encoded if needed, used verbatim from the ingest event.

**`<sanitised_filename>`** rules:

1. Strip everything but `[A-Za-z0-9._-]`.
2. Collapse consecutive dots to one.
3. Truncate to 200 chars before the extension.
4. If a collision exists in the thread dir, append `_<2hex>` before the extension.

**Ownership and permissions:**

- Files written as uid 1000 (router process user), mode `0640`.
- Thread dirs created mode `0750`.
- Agent containers run as uid 1000; the RO bind mount is readable without privilege escalation.

**Caps and quotas:**

| Cap | Value | Scope |
|---|---|---|
| Max file size | 25 MB | Per file |
| Max files per thread | 5 | Per `<thread_id>/` dir |
| Max total size per thread | 100 MB | Sum of files in `<thread_id>/` |

When any cap is breached, the router rejects the file with the corresponding failure-mode reply (below) and does not write to disk. Caps are constants in v1 — no env override surface — so the contract is verifiable from code without consulting runtime config.

**GC contract (mtime-based, single 7-day rule):**

- Janitor runs on the existing dispatch-janitor pack (#159) — no new pack, no orphan staging.
- Any thread dir whose newest mtime is older than 7 days is removed with `shutil.rmtree`.
- "Newest mtime" = `max(mtime of any file in the dir)`. The router `touch`es the thread dir on every new message in the thread, so any active conversation resets the 7-day timer.
- TTL is fixed at 7 days for v1 (configurable later via `config/attachments/janitor.yaml`).
- If the volume is full (ENOSPC on write), the janitor runs an emergency sweep before the router returns a user-visible error.

---

## Prompt contract

### Surface mechanism

When the router injects a message into a named agent's prompt and one or more attachment files are present for that thread, it **prepends** an `[ATTACHMENTS]` block before the user message body:

```
[ATTACHMENTS]
/var/lib/attachments/<thread_id>/<filename1>
/var/lib/attachments/<thread_id>/<filename2>

<original user message>
```

The block lists **absolute paths** — the agent passes them directly to the `Read`, image, or PDF tools without any further resolution. No custom verb; no shell expansion. The model treats them as opaque filesystem paths.

### Rationale for this approach

`read_attachment` verb alternative was considered and rejected for v1: it would require a new tool call round-trip, add latency, and complicate error attribution (tool error vs. file-not-found). Absolute-path injection costs nothing and leverages tools the model already uses.

### Dispatch workers

If a dispatch task requires an attachment, the dispatching verb must copy the relevant paths into the dispatch prompt body explicitly. The worker subprocess does not see the mount; it only sees what the prompt says.

---

## Failure modes

Each failure produces one user-visible Slack reply in the thread where the file was shared. Templates below are exact strings; `<…>` are filled at runtime.

| Failure | Trigger | User-visible response |
|---|---|---|
| **Mimetype blocked** | `python-magic` returns a type not in the allowlist | `Sorry, I can't read files of type <mimetype>. Allowed: <comma-list of top-level types>.` |
| **File size cap exceeded** | File > 25 MB | `That file is <size_mb> MB — above the 25 MB per-file limit. Upload a smaller file or paste the relevant excerpt.` |
| **Thread file-count cap exceeded** | Thread already has 5 files | `This thread already has 5 attachments — the per-thread limit. Start a new thread or remove an older file.` |
| **Thread total-size cap exceeded** | Sum of thread files + new file > 100 MB | `This thread's attachments total <total_mb> MB — above the 100 MB per-thread limit. Start a new thread or share a smaller file.` |
| **External link (not a Slack upload)** | Slack file event has `is_external=true` (Drive, Dropbox, GitHub, …) | `I only handle files uploaded directly to Slack — external links (Drive, Dropbox, …) aren't supported in v1. Re-upload as a Slack file.` |
| **Download timeout** | HTTP GET to Slack CDN exceeds `DOWNLOAD_TIMEOUT_SECONDS` (default 30 s) | `Timed out downloading the file from Slack. Try re-uploading, or check Slack's status page.` |
| **Signed-URL expiry** | Slack CDN returns 403/410 on the download URL | `Slack's download link has expired. Please re-upload the file.` |
| **Channel membership missing** | Slack API returns `not_in_channel` on `files.info` | `I'm not in the channel where that file was shared. Add me to the channel and re-upload.` |
| **Scratch-dir full** | ENOSPC on write after emergency janitor sweep | `Attachment storage is full. Ask an admin to run \`docker volume prune\` or extend the volume.` |
| **Unsupported encoding** | Binary file with no mimetype match and no converter | `I can't extract text from that file format yet. Try PDF, plain text, or an image.` |

No retries on failure. Each failure is logged at `ERROR` level with `thread_id`, `filename`, `mimetype`, and the exception.

---

## Rollback

A single feature flag at the router level controls ingest:

```yaml
# config/router.yaml (or env override)
ATTACHMENTS_ENABLED: true  # set to false to disable all attachment ingest
```

When `ATTACHMENTS_ENABLED=false`:

- The router skips the download step entirely.
- Files shared in Slack produce no `[ATTACHMENTS]` block; the message is forwarded as if no file were attached.
- No mount, no janitor, no mimetype check — the code path is not entered.
- Agents and workers are unaffected; they simply receive prompts without attachment paths.

This flag lives at the router level so the feature can be toggled with a config change + container restart, without `docker compose` rebuild or volume teardown.

---

## Cross-platform abstraction

The following table captures what is **shared** between Slack and Notion ingest paths vs. what is **source-specific**:

| Concern | Common (shared library) | Slack-specific | Notion-specific |
|---|---|---|---|
| Storage layout | `AttachmentStore` class: path construction, sanitisation, collision suffix, mode/ownership | — | — |
| Mimetype validation | `MimetypeGuard`: magic-byte detection + allowlist lookup | — | — |
| Prompt injection | `build_attachments_block(paths) → str` | — | — |
| GC / janitor | `AttachmentJanitor`: 7-day mtime sweep, direct `rmtree` (no orphan staging); reuses dispatch janitor pack | — | — |
| File download | — | `SlackFileIngester`: resolves `files.info`, fetches CDN URL with bot token | `NotionFileIngester`: resolves block's `file.url`, handles Notion signed-URL expiry separately |
| Auth / token | — | Per-agent Slack app token; `files:read` scope | Notion integration token; no extra scope beyond page access |
| URL expiry model | — | Slack CDN URLs expire ~30 min; detect via 403/410 on GET | Notion signed URLs expire per their API docs; refetch on 401 |
| Mimetype detection hint | — | Slack provides `mimetype` in the event; used as fallback if magic detection ambiguous | Notion provides `type` in block; same fallback role |

All shared classes live in `router/attachments/`. Source-specific ingesters live in `router/attachments/slack.py` and `router/attachments/notion.py` and depend on the shared library, not on each other.
