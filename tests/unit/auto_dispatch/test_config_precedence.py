"""Unit tests for AUTO_DISPATCH_FILE_THRESHOLD precedence: portal > yaml > code default (#726)."""

from __future__ import annotations

import pytest
import yaml

from router import settings as settings_mod
from router.auto_dispatch.config import load_auto_dispatch_config
from router.packs.secret_store import SecretStore
from router.settings import RuntimeSettings

pytestmark = pytest.mark.unit


@pytest.fixture
def rs(tmp_path):
    """A RuntimeSettings on tmp paths, wired in as the module singleton."""
    instance = RuntimeSettings(
        path=tmp_path / "runtime.json",
        ttl=0.0,
        secret_store=SecretStore(path=tmp_path / "secrets.json"),
    )
    settings_mod.reset_settings_for_tests(instance)
    yield instance
    settings_mod.reset_settings_for_tests(None)


@pytest.fixture
def yaml_config(tmp_path):
    path = tmp_path / "dispatch.yaml"
    path.write_text(yaml.dump({"auto_dispatch": {"multi_file_threshold": 3}}))
    return str(path)


class TestFileThresholdPrecedence:
    def test_code_default_when_nothing_set(self, rs, tmp_path):
        cfg = load_auto_dispatch_config(str(tmp_path / "missing.yaml"))
        assert cfg["multi_file_threshold"] == 1

    def test_yaml_overrides_code_default(self, rs, yaml_config):
        cfg = load_auto_dispatch_config(yaml_config)
        assert cfg["multi_file_threshold"] == 3

    def test_portal_overrides_yaml(self, rs, yaml_config):
        rs.set("AUTO_DISPATCH_FILE_THRESHOLD", 5)
        cfg = load_auto_dispatch_config(yaml_config)
        assert cfg["multi_file_threshold"] == 5

    def test_portal_overrides_code_default_with_no_yaml_file(self, rs, tmp_path):
        rs.set("AUTO_DISPATCH_FILE_THRESHOLD", 2)
        cfg = load_auto_dispatch_config(str(tmp_path / "missing.yaml"))
        assert cfg["multi_file_threshold"] == 2

    def test_portal_value_rejected_above_max(self, rs, yaml_config):
        with pytest.raises(ValueError, match="<= 5"):
            rs.set("AUTO_DISPATCH_FILE_THRESHOLD", 6)
        # Rejected write must not have taken effect — yaml value still applies.
        cfg = load_auto_dispatch_config(yaml_config)
        assert cfg["multi_file_threshold"] == 3

    def test_portal_value_rejected_below_min(self, rs, yaml_config):
        with pytest.raises(ValueError, match=">= 1"):
            rs.set("AUTO_DISPATCH_FILE_THRESHOLD", 0)
