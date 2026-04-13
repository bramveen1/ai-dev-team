# Commit Current Work

Commit all current changes with a well-crafted commit message.

## Steps

### 1. Review changes
Run `git status` and `git diff` (both staged and unstaged) to understand all current changes.

### 2. Check for uncommittable files
Look for files that should NOT be committed:
- `.env` files or credentials
- Large binary files
- Debug/temp files
- `node_modules` or build artifacts

Warn the user if any such files are staged or untracked.

### 3. Stage relevant files
Stage all modified and new files that are part of the current work. Use `git add <specific-files>` rather than `git add -A` to avoid accidentally including sensitive or irrelevant files. Use your judgement to determine which untracked files are relevant to the current work.

### 4. Draft commit message
Analyse the staged changes and write a commit message following these rules:
- First line: concise summary (max 72 chars) in imperative mood ("Add feature", "Fix bug", not "Added" or "Fixes")
- Summarize the nature: new feature, enhancement, bug fix, refactor, test, docs, etc.
- Use "Add" for wholly new features, "Update" for enhancements, "Fix" for bug fixes
- If the changes are substantial, add a blank line followed by a short body explaining the "why"
- Check `git log --oneline -10` to match the repository's existing commit style

### 5. Commit
Create the commit. Do NOT amend previous commits — always create a new commit. Do NOT use `--no-verify`.

### 5. Push 
Push the commit. `git push origin HEAD` if the branch does not exist in Github create it. 

### 6. Confirm
Show the user the commit hash and summary of what was committed.
