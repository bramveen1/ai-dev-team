# Refactoring roadmap

Living document. Round 1 (early July 2026) produced the quick-win batch
(#694) and the god-module split (#695). Round 2 (2026-07-07) re-reviewed the
whole codebase — five parallel area reviews over the chat layer, the
settings/config layer, the dispatch machinery, the background loops, and
packs/tests/tooling. This revision folds the round-2 findings in: stale
round-1 items are corrected in place, new items are added, and everything
verified still-open stays.

Progress: **wave 0** (dead code + the myth-cycle lazy-import hoist, §3/§6)
landed in #697. **Wave 1** (shared leaf helpers) landed next:
`router/atomic_io.py` (§8.1), `router/slack_post.py` (§8.2),
`router/container_exec.py` (§2b's `_run_in_container` move), and
`router/config_web.py` (§3's shared web helpers).

Priority guide (highest value first):

1. §2a — split `packs/dispatch/handler.py` (3.5k lines, the largest file in
   the repo; round 1 missed it because only `router/` was measured).
2. §2b — extract the shared agent-execution core (the CLI command, envelope
   parse, and error classification are forked verbatim between
   `router/dispatcher.py` and `router/chat/core.py`).
3. §2c — de-duplicate the inbound event pipeline (Slack vs Discord carry two
   near-textual copies of the same ~10-stage orchestration).
4. §8 — remaining shared infrastructure (path construction ×5, SQLite store
   boilerplate ×3, periodic-loop runner, merge_queue/auto_dispatch twin).
5. §3 — remaining settings/config corrections.

## 1. Security follow-ups (operator action required)

`systems/zoho-mail.md` and `systems/outlook.md` were committed before the
`.gitignore` rules covering them existed. They are now untracked, but their
contents — including a plaintext Zoho mail password and a Maton API key —
**remain in git history**.

1. **Rotate the Zoho mail password and the Maton API key now.** Untracking
   does not remove history; treat both credentials as exposed.
2. Optionally scrub history after rotation. This rewrites every commit and
   force-pushes `main` — coordinate with anyone holding a clone, and merge or
   close open PRs first:

   ```bash
   pip install git-filter-repo
   git filter-repo --invert-paths \
     --path systems/zoho-mail.md \
     --path systems/outlook.md
   git push --force --all origin
   git push --force --tags origin
   ```

   Every collaborator must re-clone afterwards. Skipping the scrub is
   defensible for a private repo **only if** step 1 is done.

Also new in round 2: the age-decryption logic in §7 item 2 is
security-sensitive and duplicated — treat that dedup as a hardening item,
not just hygiene.

## 2. Structural decomposition

**Done (July 2026): the two router god modules are split** —
`router/auto_dispatch.py` → the `router/auto_dispatch/` package, and
`router/app.py` → a composition root with the event pipeline in
`router/slack_events.py`, approved-draft execution in
`router/approvals/execute.py`, lifecycle loops in
`router/session_lifecycle.py`, and shared registries in `router/runtime.py`.
Test patch targets were migrated to the calling modules.

### 2a. `packs/dispatch/handler.py` is the real god module (3,508 lines) — NEW

Round 1 only measured `router/`; this pack handler is larger than both
modules §2 split, combined. It mixes slot-pool file locking, Slack posting,
GitHub auth seeding, approval gating, six CLI verbs, and nine argparse
builders. `dispatch_issue` alone spans `handler.py:1515-2020` (~505 lines).

- Split into flat sibling modules (the pack's established pattern —
  `constants`/`quota`/`transport_ref` — rather than a package, since
  handler.py is executed as a script and spec-loaded flat by the ~27 test
  module-loaders), with handler.py re-exporting each moved name so its
  callers and test patch targets hold: **`slots.py` DONE (wave 3a)**;
  **`pr_review.py` DONE (wave 3b)**; **`router_client.py` DONE
  (wave 3c)** — the compose-internal HTTP transport (token resolution,
  request/error ladder, structured error mapping) shared by
  `dispatch_draft`/`dispatch_list_pending_drafts`/the #707 status callback;
  the verbs stay in handler (they resolve the patched
  `_read_router_token`/gate helpers through the handler module) — both verbs, their five helpers, and
  constants (~400 lines), with `_pr_review_settings` kept in handler as a
  thin wrapper forwarding its quota globals for patch-compat. Remaining:
  `dispatch_issue` (~500 lines — hardest: its tests patch handler-module
  helpers it calls, so those patch targets migrate with it),
  `dispatch_draft`/`list_pending`, `dispatch_cancel`, `dispatch_status`,
  the health probes, and the argparse/`run()` table below.
- **DONE (wave 3d):** `run()` is now a `_VERB_RUNNERS` registry of
  per-verb `_run_<verb>(verb, rest)` functions, resolved through
  `globals()` at dispatch time so tests that patch a verb on the handler
  module keep intercepting; `dispatch_health` deliberately stays ahead of
  the janitor gate. `dispatch_status`'s body also moved to
  `slots.pool_status` (pool introspection belongs with the pool
  protocol), leaving a thin handler wrapper.
- **DONE (wave 3a):** `packs/dispatch/slots.py` now owns the lock-file
  protocol (acquire/release/count + FIFO queue + `POOL_SIZE`). handler
  re-imports under the historical private names (callers and test patch
  targets unchanged), babysit's release functions are thin delegates, and
  `router.dispatch.supervision`'s loader targets `slots.py` directly
  instead of executing all of handler.py for one function.
- The byte-identical `_do_post(msg)` closure is defined twice
  (`handler.py:1089` and `:1678`) — lift to a `_make_status_poster(...)`.

### 2b. Shared agent-execution core — NEW (upgrades round-1 item 1)

The round-1 plan to extract `_build_cli_command` etc. as *dispatcher-private*
helpers is too narrow: `router/chat/core.py` holds a second, byte-for-byte
copy of the whole execution sequence (its comments say "identical shape to
dispatcher.py").

- **DONE (wave 2):** `router/agent_cli.py` now owns `build_cli_command`
  (incl. the `CONTAINER_*` constants and `DEFAULT_MAX_TURNS`),
  `parse_cli_result` (the 5-branch classification, taking a
  `record_error` callback so each seam keeps its own guard wiring),
  `extract_last_tool_use`, and `ApiError`. Both `dispatcher.dispatch()`
  and `chat/core.run_agent_turn()` consume it; `dispatcher` re-exports
  the moved names for back-compat.
- The CLI JSON-envelope unwrap (`json.loads(stdout)`, `.get("result")`) is
  reimplemented in six callers (`dispatcher.py:552`, `core.py:349`,
  `auto_dispatch/worker.py:124`, `approvals/execute.py:183`,
  `memory_curator.py:174`, `session_end.py:176-256`) — and only
  `session_end._extract_json` tolerates fenced/preambled JSON, so the others
  are strictly more brittle. Extract `parse_cli_json_envelope(stdout)`
  wrapping the resilient variant.
- **DONE (wave 1):** `_run_in_container` moved to the leaf
  `router/container_exec.py` (as `run_in_container`, with
  `DispatchError`/`DispatchTimeoutError`); `dispatcher` re-exports for
  back-compat and the direct consumers import the leaf. One deliberate
  exception: `auto_dispatch/worker.py` keeps its lazy dispatcher import —
  `test_auto_dispatch` patches `router.dispatcher._run_in_container` for
  the worker path, so switching that seam means migrating those patch
  targets first (fold into §8.8's patch-target migration).
- The `handler.py dispatch_issue` argv is hand-assembled twice
  (`approvals/execute.py:120-165`, `auto_dispatch/worker.py:78-104`) with
  the same comments duplicated — extract `build_dispatch_issue_argv(...)`.
- `run_agent_turn` also re-derives the dispatcher's context assembly
  (history cap, summary split, memory + `build_full_context`, token budget:
  `core.py:206-244`) — the round-1 `_load_thread_and_memory` extraction
  should likewise be shared, not dispatcher-private.

### 2c. Inbound event pipeline: one core, currently two copies — NEW

`slack_events.py:187-478` (`_handle_event`) and
`chat/adapters/discord.py:816-1041` (`_handle_inbound`, which self-documents
as mirroring the Slack path "stage for stage") duplicate the same ~10-stage
orchestration: bot/allowlist guard, Guard-1 skip, dedup, `set_active_agent`,
attachments-mtime bump, session find/create, exit trigger, memory-curation
trigger, attachment ingest, dispatch + handoff + classified error reply —
down to identical log strings and user-facing text.

- **Orchestrator DONE for Discord (wave 5):** `chat/core.handle_inbound`
  + `InboundFacts` now own the transport-neutral stages (thread-state,
  sessions, exit trigger, curation, attachments via an adapter-supplied
  ingest callback, dispatch, handoff, classified error reply). The Discord
  adapter decodes → gates → hands off; its inbound method shrank ~120
  lines. Remaining: cut `slack_events._handle_event` over to the same
  orchestrator (#553 — the actual Slack-on-adapter migration, needs a
  SlackAdapter carrying the client/say plumbing).
- **DONE (wave 4):** `router/chat/inbound_common.py` now owns the four
  helpers that were duplicated —
  dedup cache (same OrderedDict-TTL algorithm and the same
  `_SEEN_EVENTS_MAX/_TTL` constants redeclared in both files), agent-handoff
  detection, attachment-mtime bump, and attachment ingest (identical
  user-facing strings in both copies).
- `_ATTACHMENTS_ROOT = "/var/lib/attachments"` is declared three times
  (`slack_events.py:84`, `discord.py:122`, canonically
  `attachments.py:67`) — import the constant.
- Round-1 item "decompose `_handle_event`" stays, but coordinate: the
  valuable decomposition extracts *shared* stages, not
  `slack_events`-private ones. Add the Discord twin (`_handle_inbound`, 226
  lines, `noqa: PLR0911/0912/0915`) to the same work item.
- After the dedup lands, `discord.py` (1,101 lines) splits cleanly into a
  package: ref codec, outbound/rate-limit/chunking, interactive UI
  (`_ChoiceView` — reserved shape, keep), inbound pipeline, adapter.
  Design `_split_message` + the 429-retry send loop as
  transport-parameterized helpers so the Slack adapter can reuse them at
  #553 migration time.
- Also in `discord.py`: the "resolve channel/thread target" block is
  hand-inlined five times with divergent None-handling (`:355`, `:394`,
  `:459`, `:483`, `:598`) — extract `_resolve_target(ref)`.
- Slack leaks through the transport-neutral seam: `core.py:308-313` builds
  `:alarm_clock:` / `*bold*` notices and calls
  `stuck_guard.format_slack_message` — on Discord these render wrong. Route
  notices through the adapter.

### 2d. Same-file private-helper extractions (round-1 list, updated)

Acceptance bar unchanged: the existing test file passes with no test edits.

1. `router/dispatcher.py` `dispatch()` (~360 lines): still open — but see
   §2b: `_build_cli_command`, `_parse_agent_output`, and
   `_load_thread_and_memory` must land as **shared** helpers, not private.
2. `router/slack_events.py` `_handle_event()` — see §2c.
3. `router/auto_dispatch/loop.py` `_tick_impl()` (~240 lines): **partly
   stale.** `_apply_verdicts` already exists as `_process_awaiting`
   (`loop.py:72-157`), and `_post_status` is not a natural seam (status
   posts are interleaved, not a phase). Remaining: `_gather_candidates`
   (`loop.py:289-325`) and `_dispatch_one` (`loop.py:337-426`).
4. `router/dispatch/supervision.py` `check_dispatch()` (~220 lines,
   `supervision.py:607-827`): seams confirmed — six sequential
   first-match-wins stages, each already ending in `return {...}`. Extract
   `_handle_terminal` / `_handle_halt_marker` / `_handle_budget` /
   `_handle_orphan`. Additionally (NEW): the three kill branches repeat an
   identical 5-step finalize ritual (halt-reason → wait-exitcode →
   synthetic-exitcode → release-slot → post + cleanup; the same invariant
   comment is triplicated at `:715`, `:752`, `:787`) — extract
   `_finalize_kill(...)`.
5. `router/internal_api.py` `_handle_create_draft()` (~200 lines): still
   open, but the higher-value cut is the **duplication** §3 item 5 records,
   not the length.
6. NEW: `router/merge_queue.py` `tick()` (~230 lines) repeats an identical
   `TokenError`/`HTTPError` → post + skip-dict block **eight times**
   (`:360`, `:379`, `:410`, `:432`, `:458`, `:485`, `:501`, `:518`).
   Extract a `_guarded_gh(...)` helper, then split per-PR evaluation from
   merge execution.
7. NEW: `packs/dispatch/handler.py` `dispatch_issue` (~505 lines) — see §2a.

## 3. Settings / config layer

- **The import cycle is mostly a myth — downgrade the round-1 ADR item.**
  Round 2 verified empirically: `router.settings` imports only
  `router.packs.secret_store`, and `router.config`'s top level is
  stdlib+yaml only. Both are leaf modules, so ~20 of the (now 32, up from
  ~15) `# noqa: PLC0415` lazy imports cannot participate in any cycle —
  they are cargo-culted. Mechanically hoist all `settings` / `config` /
  `secret_store` lazy imports to module top and delete the misleading
  comments. The *real* order-sensitive cluster is small:
  `auto_dispatch/worker.py` → `internal_api`/`dispatcher`/`dispatch_hook`,
  and `app.py`'s `merge_queue`/`auto_dispatch` deferrals — only that
  handful needs the layering analysis. A separate handful of noqa sites are
  third-party import deferrals (slack_sdk, markitdown, …) — recomment them
  honestly instead of "avoid import cycle".
- **`config.py` is NOT legacy** (round-2 verification): it owns agent
  discovery + credential loading; `settings.py` owns the runtime registry.
  The split is sound — no merge. But two dead artifacts inside it:
  `config.DEFAULTS` (`config.py:27-36`) restates registry defaults and has
  **zero** code readers (only two stale comments point at it —
  `dispatcher.py:443`, `session_manager.py:18`), and
  `_BACKENDS_SCHEMA_VERSION` (`config.py:40`) gates nothing. Delete both,
  fix the comments.
- **Split the registry data out of `settings.py`.** 373 of 731 lines are
  the `_REGISTRY_ENTRIES` literal. Move it to a pure-data
  `router/settings_registry.py`; machinery stays. This also gives
  `scripts/render_compose.py` a data-only import (serving the existing
  "render compose fallbacks from the registry" item without pulling in
  `SecretStore`).
- **Shared web-layer helpers — mostly DONE (wave 1).**
  `router/config_web.py` now owns `read_json_body` (per-module size caps
  preserved) and `mask`; `agent_admin._SECRET_REF_RE` aliases
  `config._SECRET_REF_RE`. Still open: the auth/503 guard prologue
  repeated across `internal_api.py` handlers (`@require_auth` decorator) —
  fold into the §3 internal_api draft-path dedup, which touches those
  handlers anyway.
- **`internal_api.py` draft-path duplication — DONE (wave 2).**
  `_build_dispatch_payload` + `_persist_dispatch_draft` now serve both the
  HTTP endpoint and the direct-call (auto-dispatch) path; `_build_card`
  serves both the Slack and Discord posters (only the rendering adapter
  and transport post remain per-path).
- **Agent-credential accessors are split across modules.** SecretStore
  shape #3 (`agent_credentials.<id>.<backend>`) is read in
  `config.py:254-272` and written with hand-rolled three-level traversal in
  `agent_admin.py:366-385`. Extract a typed `AgentCredentialStore` so the
  eventual schema migration is a single-module change. (The three-schema
  unification, `delete_str`, and docstring items from round 1 remain open;
  no fourth shape has appeared.)
- **`REPO_ROOT` / mount-or-repo boilerplate ×5.** `settings.py`,
  `config.py`, `config_page.py`, `agent_admin.py`, `secret_store.py` each
  recompute `Path(__file__).resolve().parent…` and the "mounted dir else
  repo fallback" probe — with an inconsistency (`is_dir()` vs `exists()`)
  that is a latent bug. One `router/paths.py` with `REPO_ROOT` and
  `mount_or_repo(...)`.
- `agent_admin._handle_post_agent` (83 lines, `:445-525`) interleaves
  validation, scaffolding, manifest surgery, and credential storage —
  extract `_validate_add_agent_body` and `_wrap_or_template`.
- Still open from round 1: compose fallbacks rendered from the registry;
  `.env.example` drift vs registry defaults.

## 4. Test infrastructure

Round-2 status check: **every round-1 item is still open** (verified
2026-07-07), except marker hygiene, which is effectively done (all 105 unit
files carry `pytestmark`).

- **Consolidate module loaders**: exactly 27 pack test files still hand-roll
  `importlib.util.spec_from_file_location`; the shared fixture never
  happened.
- **Reuse the shared Slack mock**: now 16 files (up from ~13) build local
  `chat_postMessage = AsyncMock(...)` clones of
  `tests/conftest.py::mock_slack_client`. The unused root fixtures
  (`sample_role_md`, `env_with_defaults`, `fixtures_dir`) still have zero
  references — delete them.
- **Reorganize dispatch-pack tests by capability** — unchanged; the
  issue-numbered files keep growing (`test_pack_dispatch_d3.py` 966,
  `issue588` 682, `issue418` 680).
- **Split the >1k-line test files** — the list has grown since round 1;
  new members: `test_discord_adapter.py` (1,612),
  `dispatch/test_supervision.py` (1,571),
  `packs/test_browser_use_credentials.py` (1,543),
  `scheduled_tasks/test_scheduler.py` (1,241), `test_discord_parity.py`
  (1,078). Same lazy policy: split when next touched.
- NEW: `test_discord_adapter.py` defines `_make_adapter(...)` and then
  bypasses it with inline `DiscordAdapter(...)` construction 32 times —
  route construction through the helper before that file is split.
- NEW: `packs/path_to_hired/` ships its own `tests/` + `conftest.py` inside
  the pack, unlike every other pack (tested under `tests/unit/packs/`).
  Decide one location and converge.
- **Fix the pytest-asyncio config** — still open: `[tool.pytest-asyncio]`
  in `pyproject.toml` remains a section pytest-asyncio never reads.
- **Fix the urlopen seam** — still open, and §7 item 3 identifies the root
  cause: `babysit._slack_post` lacks the `_urlopen` injection seam its two
  sibling posters have. Fixing the poster duplication removes the global
  autouse patch.
- The two `filterwarnings` coroutine ignores — still open; §8 item 4's
  `run_periodic` helper (stop-event support) is the natural fix for the two
  `session_lifecycle` loops that leak them.

## 5. Tooling

- **Ruff rule expansion** — still `select = ["E","F","I","W"]`. Unchanged
  plan: one family per PR (`UP`, `B`, `SIM`, `RUF`).
- **Type checking** — still absent; unchanged.
- **Pre-commit** — still absent; unchanged.
- **`/localci` gap is bigger than round 1 recorded**: it omits both the
  `compose-check` **and** the `docker-build` CI jobs. Add
  `python -m scripts.render_compose --check` and (optionally, slower)
  `docker build -f router/Dockerfile .`.
- NEW: 4 of 5 CI jobs repeat the same checkout/setup-python/seed-config/
  install block — a composite action (`.github/actions/setup/action.yml`)
  removes ~40 duplicated lines and makes the future Python-version matrix a
  one-line change.
- NEW (low confidence, maintenance note): `docker/Dockerfile.browser` and
  `docker/Dockerfile.playwright` provision Chromium two different ways with
  two version sources; cross-link or consolidate the Chromium layer.
- CI matrix (3.12/3.13) — unchanged.

## 6. Error handling & async hygiene

- Broad `except Exception` guards (~120 sites in `router/` as of round 2):
  unchanged policy — don't sweep; narrow opportunistically during §2
  extractions; `BLE001` for new code.
- NEW: `MergeQueueError` (`merge_queue.py:75`) is defined but never raised
  or caught anywhere — delete it. The `router/errors.py` shared-taxonomy
  item stays.
- `drain.py`'s blocking `subprocess.run` — unchanged (host-side, low risk).
- NEW: the genuinely on-loop sync IO is elsewhere: `check_dispatch` does
  10+ synchronous `dstate` sidecar-file reads per tick on the router loop,
  and `merge_queue.is_system_idle` stats every dispatch dir before its
  first `await`. Not urgent at current cadence (120 s / 15 min ticks); if
  tick volume grows, batch behind `asyncio.to_thread`.
- NEW: `supervision.py:52-104` does ~50 lines of import-time `sys.path`
  munging + `spec_from_file_location` to load pack modules, with broad
  swallows and module-global availability flags — move to a
  `router/dispatch/_pack_bridge.py` exposing `load_quota()` /
  `load_slot_release()`.

## 7. Packs (NEW section)

1. `packs/dispatch/handler.py` split — see §2a (top priority).
2. **Age-encrypted secret handling is duplicated across packs —
   security-sensitive.** `packs/browser_use/helpers/secrets.py` and
   `packs/path_to_hired/helpers/secrets.py` independently implement
   `resolve_keyfile`, the `0o077` keyfile-permission check, and the
   `age --decrypt` subprocess with identical error mapping;
   `browser_use/helpers/credentials.py` is a third age module (encrypt
   side). A fix in one copy will not propagate. Extract
   `packs/_shared/age.py` (`assert_keyfile_safe`, `decrypt_blob`,
   `resolve_keyfile`); packs keep only schema/paths.
3. **Three hand-rolled HTTP posters**: `babysit.py:88` (`_slack_post`),
   `handler.py:950` (`_post_slack_message`), `transport_ref.py:212`
   (`_post_discord_message`) — same urllib Request/opener/swallow pattern;
   babysit's lacks the `_urlopen` seam, which is the root cause of the §4
   global-autouse-patch stopgap. One
   `_http_post_json(url, token, payload, *, _urlopen=None)`.
4. The `override → env → default` resolution idiom is copied as 8
   `resolve_*` functions across pack helpers — one `env_path` / `env_str`
   helper.
5. Three different HTTP stacks across packs (sync httpx / async httpx /
   urllib) — no action now; lift `sidecar_client._request`'s
   typed-error core if a fourth pack needs a client.

## 8. Shared infrastructure duplication (NEW section — round-2 headline)

Cross-module forks that no single round-1 item captured:

1. **Atomic tmp-write-then-replace ×9 — DONE (wave 1).**
   `router/atomic_io.py::atomic_write_text/json` now owns the idiom
   (mkstemp + `os.replace`, unlink-on-failure, deliberate permission
   handling: explicit `mode` > preserve-existing > 0o644). Adopted by the
   eight text/JSON sites; `memory_index.py`'s sqlite-db build keeps its own
   mkstemp flow (it writes a database, not text).
2. **Best-effort "post to Slack, never raise" ×6 — DONE (wave 1).**
   `router/slack_post.py::best_effort_post` (async, returns ts) and
   `fire_and_forget_post` (sync context) own the contract; the six
   `chat_postMessage` posters are now thin per-module delegates keeping
   their names, signatures, and patch targets. `drain._post_slack_sync`
   was excluded on inspection — it posts to a *webhook URL* via urllib,
   not a chat client; fold it in only if drain ever gets a client.
3. **`merge_queue.py` is an un-deduplicated twin of the `auto_dispatch/`
   package.** Duplicated between them: `_slack_post` (byte-identical),
   `register_*` (same list-guard → payload → `create_system_task` shape,
   already drifting — auto_dispatch gained payload reconciliation,
   merge_queue didn't), the destination triple-fallback
   (`loop.py:211-216` = `merge_queue.py:349-351`), and `_get_pr_details`.
   Either make merge_queue a peer module of the package or extract a shared
   periodic-GitHub-task scaffold.
4. **Periodic-loop runner.** `scheduler.run_forever` is the complete
   implementation (stop-event, interruptible sleep, guard); the two
   `session_lifecycle` loops hand-roll the same shape with no stop-event —
   which is exactly why they leak the never-awaited-coroutine warnings §4
   suppresses. `router/background.py::run_periodic(...)`; migrate the two
   lifecycle loops first.
5. **SQLite store boilerplate ×3.** `approvals/store.py:109`,
   `scheduled_tasks/store.py:144`, `threads/state.py:41` share the same
   mkdir/connect/row_factory/schema-sibling-file lifecycle and per-write
   commit pattern. Extract the connection lifecycle (`SqliteStore` base or
   `open_sqlite(db_path, schema_path)`); keep per-store row mapping.
   `scheduled_tasks/store._row_value` is worth promoting to the shared
   helper.
6. **Agent/container path construction.** The
   `…/agents/<id>/{role,personality,agent.yaml,memory}` scheme is built in
   ≥5 places in two flavors (container-absolute vs host-relative):
   `dispatcher.py:37-41`, `config.py:154`, `memory_loader.py:165`,
   `agent_admin.py:205`, `packs/dispatch_hook.py:43`; the
   `container = manifest.get("container") or agent_id` fallback is
   reimplemented in `config.py:145` and `agent_admin.py:196`; the
   agent-memory dir `Path(agent_base)/agent/"memory"` (+ the
   `/config/agents` default literal) is rebuilt in `memory_writer`,
   `memory_curator`, `memory_retriever`, `memory_loader`. One
   `router/agent_paths.py`; memory half can live in `memory_identity` as
   `agent_memory_dir(...)`.
7. **Dual command implementations (Slack handler + `execute_*` twin) —
   partially done (wave 2), premise corrected.** On inspection the pairs
   are NOT pure transport twins: the Slack surfaces carry emoji/markdown
   formatting the neutral surfaces deliberately lack, `handle_grant` runs
   the interactive `authenticate.py` flow that `execute_grant_command`
   refuses off-Slack (#552), and the two kill surfaces differ in
   agent-resolution *precedence* (legacy: explicit arg wins; verb path:
   resolver wins). Wave 2 extracted the genuinely verbatim pieces —
   `kill_command`'s cross-thread kill loop, halt-marker scoping, and
   summary builder (`_kill_across_threads` / `_mark_halt_markers` /
   `_kill_summary`), and `grants`' who-has scan + pack-description line
   (`_agents_with_pack` / `_first_description_line`). A full collapse
   needs (a) a severity-typed `CommandResult` so transport shims can
   render emoji prefixes mechanically, and (b) a deliberate decision on
   kill precedence — do those with the #553 transport migration, not as
   a mechanical dedup. `scheduled_tasks/handlers.py`'s three-entry-point
   variant is unchanged.
8. **`auto_dispatch/__init__.py` re-exports ~70 private helpers** purely as
   `patch("router.auto_dispatch._foo")` targets — a rename now needs three
   edit sites. Migrate patch targets to the defining modules (as was
   already done for `router.app`) and shrink `__all__` to the real public
   surface.

## 9. Smaller cleanups (opportunistic, do when touching the file)

- `slack_events.handle_app_mention` (:518) is an unused indirection — the
  live handler calls `_handle_event` directly; only the `router.app`
  re-export and its own test keep it alive. Delete alongside the §2c work.
- `slack_events._is_dispatch_bot_sender` carries a documented-but-unused
  `receiving_agent` parameter — drop it.
- `auto_dispatch/inflight.py`: `_get_in_flight_issue_nums` and
  `_has_any_in_flight_dispatch` share a line-for-line iterate/skip/reap
  preamble (:27-33 = :50-56) — one `_iter_live_dispatch_ids` generator.
- `approvals/expiration_worker.run_once`: Phase B re-fetches every pending
  draft (`store.get` N+1, :193) that Phase A already loaded; the
  expired-card blocks (:106-120) are the only Block Kit built outside
  `approvals/block_kit.py` — merge the passes, move the blocks.
- `approvals/block_kit.py`: `_make_button` and `_make_button_from_spec`
  copy-paste the same style/url tail — normalize `BUTTON_CONFIG` to specs.
- `scheduled_tasks/block_kit.py`: schedule-display, destination-display,
  and UTC-format snippets are each written 2-3 times across the three
  builders — extract `_schedule_display` / `_dest_display` / `_fmt_utc`.
- `attachments.ingest_files` (:351-452) mixes name reservation, download,
  conversion, and a copy-pasted double mtime bump — extract
  `_reserve_dest_names` and `_touch_thread_dir`.
- `memory_curator.py` writes the `sorted(dir.glob("*.md"))` +
  `date.fromisoformat(f.stem)` dated-file scan three times (:264, :333-363)
  — one `_iter_dated_md(dir, start, end)` generator; longer-term, a small
  `memory_layout` module for the shared tree literals
  (`daily/`, `decisions/`, `people/`, …).
- `context_builder.py`: `build_context` and `build_full_context` are two
  assemblers for one concept with different header vocabularies — check
  whether `build_context` still has live callers; deprecate or share the
  section-join helper.

## 10. Coverage scope

Unchanged from round 1, still open: the 85% gate covers `router/` only;
`packs/` (~6.5k LOC measured round 2, plus the 3.5k handler) is invisible to
the metric. Add `--cov=packs` with its own threshold or annotate the
Definition-of-Done as router-scoped. Branch coverage is the next ratchet.
