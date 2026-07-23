"""Regression test for issue #788.

``playwright_runner`` used to parse three timeout env vars with a bare
``int(os.environ.get(...))`` at module import time. A non-numeric
override (e.g. ``BROWSER_USE_VERB_TIMEOUT_MS=30s``) raised ``ValueError``
during import, which took down ``server.py`` (it imports ``run_verb``
at load time) before uvicorn could bind — the sidecar crash-looped
instead of falling back to the documented default.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
PACK_DIR = REPO_ROOT / "packs" / "browser_use"

if str(PACK_DIR) not in sys.path:
    sys.path.insert(0, str(PACK_DIR))


@pytest.fixture
def runner_mod():
    module = importlib.import_module("browser_use_sidecar.playwright_runner")
    yield importlib.reload(module)
    # Leave the module in its default state for any tests that import it later.
    import os

    for name in (
        "BROWSER_USE_VERB_TIMEOUT_MS",
        "BROWSER_USE_EXTRACT_TIMEOUT_MS",
        "BROWSER_USE_LOGIN_TIMEOUT_MS",
    ):
        os.environ.pop(name, None)
    importlib.reload(module)


def test_non_numeric_timeout_env_falls_back_to_default(monkeypatch, runner_mod, caplog):
    monkeypatch.setenv("BROWSER_USE_VERB_TIMEOUT_MS", "30s")

    module = importlib.reload(runner_mod)

    assert module._DEFAULT_TIMEOUT_MS == 30000


def test_valid_timeout_env_is_honoured(monkeypatch, runner_mod):
    monkeypatch.setenv("BROWSER_USE_VERB_TIMEOUT_MS", "45000")

    module = importlib.reload(runner_mod)

    assert module._DEFAULT_TIMEOUT_MS == 45000
