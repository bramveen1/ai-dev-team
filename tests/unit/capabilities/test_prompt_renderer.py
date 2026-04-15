"""Unit tests for src.capabilities.prompt_renderer — system prompt rendering."""

import textwrap

import pytest

from src.capabilities.prompt_renderer import render_capability_summary

pytestmark = pytest.mark.unit


@pytest.fixture
def providers_yaml(tmp_path):
    """Write a providers.yaml and return its path."""
    content = textwrap.dedent("""\
        providers:
          zoho-mcp:
            command: npx
            args: ["-y", "@zoho/zoho-mcp"]
            capabilities: [email]
            permission_scopes:
              email:
                read: "Mail.Read"
                send: "Mail.Send"
                draft-create: "Mail.Draft"
                draft-update: "Mail.Draft"
                draft-delete: "Mail.Draft"
                archive: "Mail.Archive"
            env_template:
              ZOHO_ACCOUNT: "{account}"
              ZOHO_API_KEY: "${ZOHO_API_KEY}"
          m365-mcp:
            command: npx
            args: ["-y", "@microsoft/m365-mcp"]
            capabilities: [email, calendar]
            permission_scopes:
              email:
                read: "Mail.Read"
                draft-create: "Mail.ReadWrite"
                draft-update: "Mail.ReadWrite"
                draft-delete: "Mail.ReadWrite"
              calendar:
                read: "Calendars.Read"
                propose: "Calendars.ReadWrite"
            env_template:
              M365_ACCOUNT: "{account}"
              M365_ACCESS_TOKEN: "${M365_ACCESS_TOKEN}"
              M365_SCOPES: "{computed_scopes}"
    """)
    p = tmp_path / "providers.yaml"
    p.write_text(content)
    return p


@pytest.fixture
def capabilities_yaml(tmp_path, providers_yaml):
    """Write a capabilities.yaml and return its path."""
    content = textwrap.dedent("""\
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
                    - archive
                    - draft-create
                    - draft-update
                    - draft-delete
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - draft-create
                    - draft-update
                    - draft-delete
              calendar:
                - instance: bram
                  provider: m365-mcp
                  account: bram@pathtohired.com
                  ownership: delegate
                  permissions:
                    - read
                    - propose
    """)
    p = tmp_path / "capabilities.yaml"
    p.write_text(content)
    return p


class TestRenderCapabilitySummary:
    """Tests for the prompt renderer."""

    def test_starts_with_header(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert summary.startswith("## Your Capabilities\n")

    def test_contains_capability_sections(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "### calendar" in summary
        assert "### email" in summary

    def test_contains_namespace_references(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "**email_mine**" in summary
        assert "**email_bram**" in summary
        assert "**calendar_bram**" in summary

    def test_contains_ownership_labels(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "(self)" in summary
        assert "(delegate)" in summary

    def test_contains_account_info(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "lisa@pathtohired.com" in summary
        assert "bram@pathtohired.com" in summary

    def test_contains_provider_info(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "via zoho-mcp" in summary
        assert "via m365-mcp" in summary

    def test_contains_permissions(self, capabilities_yaml):
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "Permissions: read, send, archive, draft-create, draft-update, draft-delete" in summary
        assert "Permissions: read, draft-create, draft-update, draft-delete" in summary

    def test_delegate_email_note_no_send(self, capabilities_yaml):
        """Delegate email without send permission should have a note about drafts."""
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "no send permission" in summary.lower() or "draft" in summary.lower()

    def test_delegate_calendar_note(self, capabilities_yaml):
        """Delegate calendar should mention approval requirements."""
        summary = render_capability_summary("lisa", capabilities_yaml)
        assert "booking requires approval" in summary.lower()

    def test_self_account_no_delegate_note(self, capabilities_yaml):
        """Self-owned accounts should not have delegate/approval notes."""
        summary = render_capability_summary("lisa", capabilities_yaml)
        lines = summary.split("\n")
        # Find the email_mine block — it should not have a Note line
        mine_idx = next(i for i, line in enumerate(lines) if "email_mine" in line)
        # The next lines are Permissions and possibly Note
        mine_block = []
        for line in lines[mine_idx + 1 :]:
            if line.startswith("  "):
                mine_block.append(line)
            else:
                break
        note_lines = [line for line in mine_block if line.strip().startswith("Note:")]
        assert len(note_lines) == 0, f"Self account should have no Note, got: {note_lines}"

    def test_deterministic_output(self, capabilities_yaml):
        """Same config should always produce the same summary."""
        summary1 = render_capability_summary("lisa", capabilities_yaml)
        summary2 = render_capability_summary("lisa", capabilities_yaml)
        assert summary1 == summary2

    def test_golden_file_lisa_summary(self, capabilities_yaml):
        """Golden test — verify Lisa's full rendered summary matches expected output."""
        summary = render_capability_summary("lisa", capabilities_yaml)
        expected = textwrap.dedent("""\
            ## Your Capabilities

            ### calendar
            - **calendar_bram** (delegate) — bram@pathtohired.com via m365-mcp
              Permissions: read, propose
              Note: delegate account — booking requires approval.

            ### email
            - **email_mine** (self) — lisa@pathtohired.com via zoho-mcp
              Permissions: read, send, archive, draft-create, draft-update, draft-delete
            - **email_bram** (delegate) — bram@pathtohired.com via m365-mcp
              Permissions: read, draft-create, draft-update, draft-delete
              Note: delegate account — no send permission. Create drafts and notify bram for review.
        """)
        assert summary == expected


class TestRealPromptRenderer:
    """Tests using the actual config files."""

    def test_real_lisa_summary(self):
        """The checked-in config should produce a valid summary for Lisa."""
        summary = render_capability_summary("lisa")
        assert "## Your Capabilities" in summary
        assert "email_mine" in summary
        assert "email_bram" in summary
        assert "calendar_bram" in summary
