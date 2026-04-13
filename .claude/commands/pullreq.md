# Commit and Create Pull Request

Commit all open work and create a pull request on GitHub.

## Steps

### 1. Commit outstanding changes
If there are uncommitted changes (staged, unstaged, or untracked files that are part of the current work):
- Stage relevant files using `git add <specific-files>` (avoid `git add -A`)
- Do NOT stage `.env` files, credentials, large binaries, or temp files
- Write a clear commit message following the repo's style (`git log --oneline -10`)
- Create a new commit (never amend)

If the working tree is already clean, skip this step.

### 2. Determine base branch
The base branch is `main`. Run `git log main..HEAD --oneline` and `git diff main...HEAD --stat` to understand all changes included in this PR.

### 3. Push to remote
Push the current branch to origin: `git push -u origin <current-branch-name>`

### 4. Analyse all changes for PR description
Review ALL commits from `git log main..HEAD` — not just the latest commit. Understand the full scope of changes: what was added, modified, fixed, and why.

### 5. Create the Pull Request
Use `gh pr create` with:

**Title:** Short, descriptive (under 72 chars). Use imperative mood.

**Body format:**
```
## Summary
- Bullet points describing what changed and why (1-3 bullets)

## Changes
- List of specific changes across all commits

## Test plan
- [ ] How to verify this works
- [ ] What tests were added or updated
```

### 6. Start CI monitoring
After the PR is created, call the `start_monitoring` MCP tool to watch for CI results:
- Extract the PR number from the created PR
- Use the current branch name
- Owner: `bramveen1`, repo: `job-application-tracker`

This enables the ci-monitor channel to automatically notify you of CI failures so you can self-heal.

### 7. Report back
Show the user the PR URL when done, and confirm that CI monitoring has started.
