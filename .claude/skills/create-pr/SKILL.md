---
name: create-pr
description: >-
  Open (or finalize) the pull request for a dispatched fix the correct,
  deterministic way — base main, READY (never draft), body carrying
  `Closes #<issue>`, and an @sam review mention. Use this whenever a
  dispatched worker is ready to open its PR, or to flip an existing draft
  PR to ready. Prevents the two recurring automation-stallers: PRs left in
  draft (merge queue skips them) and PRs merged without a `Closes` keyword
  (the issue never closes and strands in the auto-dispatch tracker).
---

# create-pr

The one correct way to open the PR for a dispatched fix. Encapsulates the
full incantation so a worker invokes a single thing and cannot get the
draft flag, the `Closes` keyword, or the review mention wrong.

## Why this exists

Two failure modes have repeatedly stalled all automation:

1. **PR left in draft.** A draft PR reports `mergeStateStatus: UNKNOWN`, so
   the merge queue silently skips it — a green, finished PR just sits there
   and the whole pipeline stalls (#825). "Push before you verify" (worker
   contract rule 1) makes drafting a legitimate *mid-flight* survival move,
   but the finished PR must be **ready**.
2. **Merged without `Closes #N`.** If the PR body has no closing keyword,
   merging it never closes the issue; the issue then strands in the
   auto-dispatch awaiting tracker until a 24h age-out, blocking re-dispatch.

This skill makes both correct by construction.

## Inputs

- **Issue number** the PR fixes (e.g. `386`). Required for the `Closes`
  line. Derive it from `$DISPATCH_ISSUE_URL` or the issue you were briefed
  on.
- **A pushed branch** with your commits (the worker contract already had
  you push early).

## Procedure

Run from inside the repo checkout (`$DISPATCH_REPO`), on your feature
branch, after your final commit is pushed.

### 1. Verify preconditions

```bash
# You must be on your feature branch, not main, and it must be pushed.
git rev-parse --abbrev-ref HEAD          # -> your feature branch
git status --short                        # -> clean; commit anything staged first
git push -u origin HEAD                   # ensure the remote branch is current
```

### 2. If a PR already exists (you drafted it earlier)

Flip it to ready and make sure the body closes the issue:

```bash
PR_URL="$(gh pr view --json url -q .url)"   # current branch's PR
gh pr ready "$PR_URL"                        # draft -> ready (no-op if already ready)
# Confirm the body has a closing keyword; if not, append one:
gh pr view --json body -q .body | grep -qi "closes #<ISSUE>" \
  || gh pr edit "$PR_URL" --body "$(gh pr view --json body -q .body)

Closes #<ISSUE>"
```

### 3. If no PR exists yet — create it ready

```bash
gh pr create \
  --base main \
  --head "$(git rev-parse --abbrev-ref HEAD)" \
  --title "<concise imperative summary>" \
  --body "$(cat <<'EOF'
<one-paragraph what/why>

Closes #<ISSUE>

@sam ready for review.
EOF
)"
```

**Never pass `--draft`** to `gh pr create` here. Drafting is only for the
push-before-verify survival step in worker-contract rule 1; by the time you
run this skill the work is done, so the PR is ready by definition.

### 4. Confirm

```bash
gh pr view --json url,isDraft,mergeStateStatus,body \
  -q '{url:.url, isDraft:.isDraft, mergeState:.mergeStateStatus}'
```

Success = `isDraft: false` and the body contains `Closes #<ISSUE>`.

## Rules (non-negotiable)

- **Base is `main`.** One issue per branch; never stack PRs.
- **Ready, not draft.** `isDraft` must end `false`.
- **`Closes #<issue>`** must be in the PR body so the merge closes the issue.
- **@mention `sam`** for review. Do not self-approve; do not merge — the
  merge tap belongs to Bram.
- **Do not** force-flip someone else's PR; only operate on the branch you
  were dispatched to work.
