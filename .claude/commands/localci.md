# Local CI

Run the same checks as the GitHub Actions CI workflow locally.

## Steps

Run all steps sequentially — stop and report on the first failure.

### 1. Lint
```bash
ruff check .
```

### 2. Format
```bash
ruff format --check .
```

### 3. Unit tests
```bash
pytest tests/unit -v --cov=router --cov-report=term-missing -m unit
```
Exit code 5 (no tests collected) is acceptable — treat it as a pass.

### 4. Integration tests
```bash
pytest tests/integration -v -m integration
```
Exit code 5 (no tests collected) is acceptable — treat it as a pass.

## Reporting

After all steps complete (or on first failure), report a summary:
- Which steps passed / failed
- For failures: show the relevant error output

## Fixing

After reporting, fix any issues found:
- Lint errors: apply `ruff check --fix .` where possible, fix remaining issues manually
- Format errors: apply `ruff format .`
- Test failures: investigate and fix the underlying code
- Re-run the failing step after each fix to confirm it passes before moving on
- Once all steps pass, report the final green summary
