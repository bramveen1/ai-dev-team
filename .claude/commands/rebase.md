# Rebase Current Branch

Rebase the current branch onto another branch. If no branch is specified, rebase onto `main`.

**Target branch:** $ARGUMENTS (default: `main`)

## Steps

### 1. Determine target branch
If `$ARGUMENTS` is provided and non-empty, use that as the target branch. Otherwise, default to `main`.

### 2. Pre-flight checks
- Run `git status` to check for uncommitted changes
- If there are uncommitted changes, warn the user and ask whether to stash them before continuing or abort

### 3. Fetch latest
Run `git fetch origin <target-branch>` to ensure we have the latest remote state.

### 4. Rebase
Run `git rebase origin/<target-branch>`.

### 5. Handle conflicts
If the rebase produces conflicts:
- Run `git diff --name-only --diff-filter=U` to list conflicted files
- Show the user which files have conflicts
- For each conflicted file, read it and resolve the conflict by choosing the most sensible resolution
- After resolving, run `git add <file>` and `git rebase --continue`
- If the conflicts are too complex or ambiguous, ask the user how to proceed rather than guessing

### 6. Restore stashed changes
If changes were stashed in step 2, run `git stash pop` and warn the user if there are any conflicts from the pop.

### 7. Report
Show the user:
- How many commits were replayed
- The current HEAD commit
- Whether any conflicts were resolved
