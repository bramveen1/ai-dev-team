# Refactoring roadmap

Outcome of a full code-quality review (July 2026). The low-risk quick wins
landed together on one branch (shared `github_api`/`background` helpers,
settings-registry bypass fixes, keyword-only `Setting` registry, compose
default-drift fix, integration-test de-skipping, CI caching + coverage
ratchet, secret-file untracking). This document records what was **deferred**,
why, and how to approach each item when it is picked up.

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

## 2. Structural decomposition (highest-value code change)

**Done (July 2026): the two god modules are split.**

- `router/auto_dispatch.py` (1.6k lines) is now the `router/auto_dispatch/`
  package: `config` / `state` / `triage` / `github` / `notify` / `inflight` /
  `worker` / `loop`, with the package `__init__` re-exporting the full old
  surface (the scheduler still resolves `router.auto_dispatch:tick`).
- `router/app.py` (1.5k lines) is now a composition root. The event pipeline
  lives in `router/slack_events.py`, approved-draft execution in
  `router/approvals/execute.py`, the cleanup/expiration loops in
  `router/session_lifecycle.py`, and the shared bot/app registries in
  `router/runtime.py` (mutated in place, never rebound, so `router.app`'s
  back-compat aliases stay live).
- The `patch("router.app.<name>")` test targets were migrated to the modules
  that now *call* each name; `importlib.reload(router.app)` fixtures reset
  the shared state in `router.slack_events` / `router.runtime` explicitly.
- Note for the Slack→transport-neutral migration
  (`docs/chat-backends-architecture.md`): the Slack-specific inbound body is
  now isolated in `router/slack_events.py`, which is the single module that
  migration needs to absorb into `router/chat/` adapters.

Remaining same-file private-helper extractions (acceptance bar per function:
the existing test file passes **with no test edits**):

1. `router/dispatcher.py` `dispatch()` (~360 lines): extract
   `_resolve_dispatch_args`, `_check_guards`, `_load_thread_and_memory`,
   `_build_cli_command` (absorb the inline `"--max-turns", "50"` pair into a
   `DEFAULT_MAX_TURNS = 50` module constant), `_run_agent_container`,
   `_parse_agent_output`, `_post_results`.
2. `router/slack_events.py` `_handle_event()` (~290 lines): bot-guard /
   dedup / mention-parse / handoff / dispatch / error-post stages.
3. `router/auto_dispatch/loop.py` `_tick_impl()` (~260 lines):
   `_gather_candidates`, `_apply_verdicts`, `_dispatch_one`, `_post_status`.
4. `router/dispatch/supervision.py` `check_dispatch()` (~230 lines) and
   `router/internal_api.py` `_handle_create_draft()` (~200 lines).

## 3. Settings / config layer

- **Break the import cycle structurally.** ~15 call sites lazily import
  `settings` / `config` / `internal_api` inside functions with
  `# noqa: PLC0415 — deferred to avoid import cycle`. The fix is layering
  (nothing `router.settings` imports may import back into router modules),
  which deserves a small ADR, not an opportunistic refactor. Until then, new
  code keeps using the established lazy-import pattern.
- **Unify SecretStore's schemas.** `router/packs/secret_store.py` carries
  three shapes in one `data/secrets.json`: pack-keyed dict blocks
  (`get`/`set`), top-level scalar strings (`get_str`/`set_str`, #576), and
  nested `agent_credentials.<id>.<backend>` blocks. Unification needs a data
  migration for live deployments; in the meantime the composite shape should
  at least be documented in the module docstring. Also: `set_str("")`
  overloading empty-string as delete deserves an explicit `delete_str`.
- **Render compose fallbacks from the registry.** `scripts/render_compose.py`
  now emits empty fallbacks so registry defaults rule, but nothing stops a
  future non-empty fallback from reintroducing drift. Have the renderer read
  `router.settings.REGISTRY` (or add a governance assertion that all
  registry-known compose vars use `${VAR:-}`).
- `.env.example` still documents defaults for registry keys (e.g.
  `WORKER_MENTION_HANDOFF=1` uncommented); align it with the registry so the
  template can't mislead.

## 4. Test infrastructure

- **Consolidate module loaders**: ~27 pack test files hand-roll
  `importlib.util.spec_from_file_location` loaders (e.g.
  `tests/unit/packs/test_pack_dispatch.py`). One shared fixture/helper in
  `tests/unit/packs/conftest.py` — or plain imports if the packs are
  importable — removes them all.
- **Reuse the shared Slack mock**: ~13 files build local
  `chat_postMessage = AsyncMock(...)` clients duplicating
  `tests/conftest.py::mock_slack_client`; extend the shared fixture rather
  than forking it. Several root-conftest fixtures (`sample_role_md`,
  `env_with_defaults`, `fixtures_dir`) are entirely unused — delete them.
- **Reorganize dispatch-pack tests by capability.** 14
  `test_pack_dispatch_issue*.py` files (plus `d3`/`d7` variants) accreted one
  per bug; the pack's behavior surface is unreadable. Fold them into
  capability-named files lazily, whenever one is next touched.
- **Split the >1k-line test files** the same lazy way (`test_app.py` 3.3k,
  `test_pack_browser_use.py` 2.2k, `test_auto_dispatch.py` 2.0k, …).
- **Fix the pytest-asyncio config.** `[tool.pytest-asyncio]` in
  `pyproject.toml` is not a section pytest-asyncio reads — async tests only
  run because every one carries `@pytest.mark.asyncio`. Either set
  `asyncio_mode = "auto"` under `[tool.pytest.ini_options]` and drop the
  decorators, or delete the dead section.
- **Fix the urlopen seam.** `tests/unit/packs/conftest.py` autouse-patches
  `urllib.request.urlopen` globally because pack handlers reach for a real
  network call when no client is injected (#466 flake). The real fix is
  injecting an HTTP client into the packs' Slack-post helper; the autouse
  patch is a documented stopgap.
- The two `filterwarnings` ignores for never-awaited coroutines
  (`_session_cleanup_loop`, `run_forever`) suppress real async-hygiene
  warnings — fix the tests that leak those coroutines, then drop the ignores.

## 5. Tooling

- **Ruff rule expansion**: `lint.select` is only `E,F,I,W`. Add one family
  per PR — `UP` (pyupgrade), `B` (bugbear), `SIM` (simplify), then `RUF` —
  autofix with `ruff check --fix`, hand-review the rest, and prefer
  `per-file-ignores` over global disables.
- **Type checking**: no mypy/pyright anywhere. Type-hint coverage is already
  strong, so a `mypy --strict`-lite config on `router/` is mostly wiring
  work; treat it as its own initiative.
- **Pre-commit**: add `.pre-commit-config.yaml` running ruff check+format so
  CI is not the first gate.
- `/localci` omits the `docker-build` and `compose-check` CI jobs, so it can
  pass locally while CI fails on compose drift — add
  `python -m scripts.render_compose --check` to the command.
- CI matrix: only Python 3.11 is tested although `requires-python >= 3.11`;
  add 3.12/3.13 to a matrix when dependencies allow.

## 6. Error handling & async hygiene

- ~168 broad `except Exception` sites exist across the router. Most are
  deliberate "never crash a tick / never wedge a notification" guards —
  do **not** sweep them. Enable ruff `BLE001` for new code via
  per-file-ignores, and narrow old ones opportunistically during §2
  extractions.
- `router/dispatch/drain.py` calls blocking
  `subprocess.run(["docker", "compose", "ps", …])`; it is host-side today,
  but if it is ever reachable from the router event loop, wrap it in
  `asyncio.to_thread`.
- Unify the exception taxonomy: `DispatchError`/`ApiError`/`TaskHaltedError`
  (dispatcher) vs `github_api.TokenError` vs ad-hoc raises. A small
  `router/errors.py` with the shared bases would let call sites distinguish
  retryable vs fatal uniformly.

## 7. Coverage scope

The 85% gate (raised from 80; measured 88.2%) covers `router/` only.
`packs/` (~10k LOC, heavily tested under `tests/unit/packs/`) is invisible to
the metric. Either add `--cov=packs` with its own measured threshold, or note
in CLAUDE.md that the Definition-of-Done coverage number is router-scoped.
Branch coverage (`[tool.coverage.run] branch = true`) is the next ratchet
after that.
