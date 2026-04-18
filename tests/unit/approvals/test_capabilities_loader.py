"""Tests for the capability configuration loader."""

from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from router.approvals.capabilities_loader import (
    get_capability_instance,
    load_capabilities,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    """Reset the module-level cache before each test."""
    reset_cache()
    yield
    reset_cache()


@pytest.fixture
def caps_yaml(tmp_path) -> Path:
    """Write a minimal capabilities.yaml for testing."""
    content = dedent("""\
        agents:
          lisa:
            agent: lisa
            capabilities:
              email:
                - instance: mine
                  provider: zoho-mcp
                  account: lisa@pathtohired.com
                  ownership: self
                  permissions:
                    - read
                    - send
                    - draft-create
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - draft-create
                    - draft-update
              calendar:
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - propose
    """)
    path = tmp_path / "capabilities.yaml"
    path.write_text(content)
    return path


@pytest.mark.unit
class TestLoadCapabilities:
    def test_loads_agent_capabilities(self, caps_yaml):
        caps = load_capabilities(caps_yaml)
        assert "lisa" in caps
        assert caps["lisa"].agent == "lisa"
        assert "email" in caps["lisa"].capabilities

    def test_email_instances(self, caps_yaml):
        caps = load_capabilities(caps_yaml)
        email_instances = caps["lisa"].capabilities["email"]
        assert len(email_instances) == 2
        names = [i.instance for i in email_instances]
        assert "mine" in names
        assert "bram" in names

    def test_permissions_loaded(self, caps_yaml):
        caps = load_capabilities(caps_yaml)
        email_instances = caps["lisa"].capabilities["email"]
        mine = [i for i in email_instances if i.instance == "mine"][0]
        bram = [i for i in email_instances if i.instance == "bram"][0]
        assert "send" in mine.permissions
        assert "send" not in bram.permissions


@pytest.mark.unit
class TestGetCapabilityInstance:
    def test_finds_existing_instance(self, caps_yaml):
        inst = get_capability_instance("lisa", "email", "bram", path=caps_yaml)
        assert inst is not None
        assert inst.provider == "m365-mcp"
        assert inst.ownership == "delegate"
        assert "send" not in inst.permissions

    def test_returns_none_for_unknown_agent(self, caps_yaml):
        assert get_capability_instance("unknown", "email", "bram", path=caps_yaml) is None

    def test_returns_none_for_unknown_capability(self, caps_yaml):
        assert get_capability_instance("lisa", "social", "mine", path=caps_yaml) is None

    def test_returns_none_for_unknown_instance(self, caps_yaml):
        assert get_capability_instance("lisa", "email", "unknown", path=caps_yaml) is None

    def test_finds_calendar_instance(self, caps_yaml):
        inst = get_capability_instance("lisa", "calendar", "bram", path=caps_yaml)
        assert inst is not None
        assert "propose" in inst.permissions
        assert "book" not in inst.permissions
