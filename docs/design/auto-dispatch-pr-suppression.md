# auto-dispatch: PR-suppression semantics

_Status: implemented · Owner: dev · Closes #573 · Prereq: #563 (approval gate, merged)_

## Problem

`auto_dispatch.tick()` was suppressing dispatch whenever **any** open PR existed in the
repo.  A docs change, a config bump, or a human hotfix would stall the autonomous
bug-backlog loop until it merged or closed.  This is too broad: the brake should track
*in-flight dev-worker PRs*, not "is the PR queue literally empty."

A latent bug made it worse: `_get_open_dev_prs` was documented to return only PRs whose
head branch starts with a dev-worker prefix, but the implementation returned
`resp.json()` unfiltered — every open PR, regardless of source.

## Chosen semantics

**The suppression gate is: "zero open dev-worker PRs."**

A PR counts as a dev-worker PR if and only if its head branch starts with the prefix
`issue-` (e.g. `issue-573-fix-suppression`).  Branches that do not match — including
`docs/*`, `fix/*`, `feat/*`, and any human-authored branch — are invisible to the gate.

This is the simplest correct choice given:
- Dev-worker dispatches follow the `issue-<number>-<description>` branch convention
  established by the dispatch pack's CLAUDE.md instructions.
- Docs, config, and hotfix PRs use other prefixes and must never block autonomous work.
- A concurrency limit (`≤N in-flight`) or per-issue gating would be future work if
  needed; for now one-in-flight is the design (issue #535).

## Implementation

- **Constant:** `DEV_WORKER_BRANCH_PREFIX = "issue-"` in `router/auto_dispatch.py`.
- **Filter:** `_get_open_dev_prs` now filters `resp.json()` to PRs whose
  `head.ref` starts with the prefix, matching its existing docstring.
- **Suppression check:** unchanged — tick() suppresses when `_get_open_dev_prs`
  returns a non-empty list.

## Safety note

This change narrows the suppression gate.  The `shadow_mode` guard (#562) and the
human-approval gate on the auto path (#563) remain in place.  Removing or further
loosening this check requires re-evaluating those safety layers.

## Covered by

`tests/unit/test_auto_dispatch.py::TestGetOpenDevPrs`
