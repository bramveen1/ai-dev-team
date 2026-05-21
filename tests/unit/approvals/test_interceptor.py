"""Tests for the approval flow response interceptor.

Verifies that:
- draft-approval code-fence blocks are correctly parsed from agent responses
- Malformed or incomplete blocks are silently stripped
- Drafts must specify exactly one of {pack, target}
- Approval messages are posted with pack-aware buttons
- Draft records persist with correct fields
"""

from __future__ import annotations

import json
import textwrap
from unittest.mock import AsyncMock

import pytest

from router.approvals.block_kit import (
    ACTION_APPROVE_SEND,
    ACTION_DISCARD,
    ACTION_OPEN_IN_APP,
    ACTION_REQUEST_EDIT,
)
from router.approvals.interceptor import (
    DraftRequest,
    detect_unbacked_claim,
    parse_response,
    post_approval_message,
)
from router.approvals.store import DraftStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    db_path = str(tmp_path / "test_interceptor.db")
    s = DraftStore(db_path)
    yield s
    s.close()


@pytest.fixture
def slack_client():
    client = AsyncMock()
    client.chat_postMessage.return_value = {"ts": "1705700000.000200"}
    return client


@pytest.fixture
def packs_dir(tmp_path):
    """Build a tiny packs/ tree with two packs: github (approve:[merge])
    and zoho-mail (approve:[]). Mirrors production shape."""
    base = tmp_path / "packs"
    base.mkdir()

    gh = base / "github"
    gh.mkdir()
    (gh / "pack.yaml").write_text(
        textwrap.dedent("""\
            name: github
            description: GitHub via gh CLI
            needs: [GITHUB_TOKEN]
            approve: [merge]
        """)
    )

    zoho = base / "zoho-mail"
    zoho.mkdir()
    (zoho / "pack.yaml").write_text(
        textwrap.dedent("""\
            name: zoho-mail
            description: Zoho Mail via Maton gateway
            needs: [ZOHO_API_KEY]
            approve: []
        """)
    )

    return base


def _make_draft_request(**overrides) -> DraftRequest:
    defaults = {
        "draft_id": "AAMkAGI2TG93AAA=",
        "action_verb": "send",
        "payload": {"to": "sam@example.com", "subject": "Test", "body": "Hello"},
        "target": "outlook",
    }
    defaults.update(overrides)
    return DraftRequest(**defaults)


# ---------------------------------------------------------------------------
# parse_response: schema variations
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestParseResponseNoBlocks:
    def test_no_draft_blocks(self):
        text = "Done! I've sent the email."
        result = parse_response(text)

        assert result.cleaned_text == text
        assert result.has_drafts is False
        assert result.draft_requests == []

    def test_regular_code_blocks_are_not_matched(self):
        text = "Here's some code:\n\n```python\nprint('hello')\n```\n\nDone."
        result = parse_response(text)

        assert result.has_drafts is False
        assert "```python" in result.cleaned_text


@pytest.mark.unit
class TestParseResponseNewSchema:
    def test_pack_backed_draft(self):
        payload = {
            "draft_id": "PR-42",
            "pack": "github",
            "action_verb": "merge",
            "payload": {"pr_number": 42, "title": "Fix bug"},
        }
        text = f"Drafted the merge.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        assert len(result.draft_requests) == 1
        req = result.draft_requests[0]
        assert req.draft_id == "PR-42"
        assert req.pack == "github"
        assert req.target is None
        assert req.action_verb == "merge"
        assert req.payload == {"pr_number": 42, "title": "Fix bug"}
        assert result.cleaned_text == "Drafted the merge."

    def test_connector_backed_draft(self):
        payload = {
            "draft_id": "AAMkAGI=",
            "target": "outlook",
            "action_verb": "send",
            "payload": {"to": "a@b.com", "subject": "Hi"},
        }
        text = f"Created the draft.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        req = result.draft_requests[0]
        assert req.target == "outlook"
        assert req.pack is None
        assert req.action_verb == "send"

    def test_multiple_blocks_mixed_schemas(self):
        pack_payload = {
            "draft_id": "pr1",
            "pack": "github",
            "action_verb": "merge",
            "payload": {"pr_number": 1},
        }
        target_payload = {
            "draft_id": "AAMk",
            "target": "outlook",
            "action_verb": "send",
            "payload": {"to": "x@y.com"},
        }
        text = (
            f"Two drafts.\n\n"
            f"```draft-approval\n{json.dumps(pack_payload)}\n```\n\n"
            f"```draft-approval\n{json.dumps(target_payload)}\n```"
        )

        result = parse_response(text)

        assert len(result.draft_requests) == 2
        assert result.draft_requests[0].pack == "github"
        assert result.draft_requests[1].target == "outlook"


@pytest.mark.unit
class TestParseResponseTolerantShapes:
    """The parser tolerates two common agent mistakes that previously
    produced silent failures (no card posted):

    1. Using ``dispatch-approval`` as the fence info string instead of
       the canonical ``draft-approval``.  Agents pattern-match the
       handler's verb name and emit that instead.
    2. Splatting the dispatch handler's raw
       ``{status: approval_required, draft_id, preview}`` response into
       the fence verbatim instead of re-formatting it into canonical
       ``{draft_id, action_verb, payload, pack}`` shape.

    Both routes must produce a valid ``DraftRequest`` so the card lands
    in Slack.  See ``router/approvals/interceptor.py`` for the
    translation layer."""

    def test_dispatch_approval_fence_is_accepted(self):
        payload = {
            "draft_id": "abc123",
            "pack": "dispatch",
            "action_verb": "dispatch_issue",
            "payload": {"issue_url": "https://github.com/a/b/issues/1"},
        }
        text = f"Drafted.\n\n```dispatch-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        assert len(result.draft_requests) == 1
        req = result.draft_requests[0]
        assert req.draft_id == "abc123"
        assert req.pack == "dispatch"
        assert req.action_verb == "dispatch_issue"
        assert result.cleaned_text == "Drafted."

    def test_handler_native_shape_is_translated(self):
        # The exact shape the dispatch handler returns when gated.
        handler_output = {
            "status": "approval_required",
            "draft_id": "6be86979",
            "preview": {
                "repo": "bramveen1/ai-dev-team",
                "issue_url": "https://github.com/bramveen1/ai-dev-team/issues/241",
                "branch_target": "main",
                "model": "sonnet",
                "est_workspace_path": "/var/lib/dispatch/dispatch-x",
                "gate_reason": "always",
            },
        }
        text = f"Queued.\n\n```draft-approval\n{json.dumps(handler_output)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        req = result.draft_requests[0]
        assert req.draft_id == "6be86979"
        assert req.pack == "dispatch"
        assert req.action_verb == "dispatch_issue"
        assert req.payload["issue_url"].endswith("/241")
        assert req.payload["gate_reason"] == "always"

    def test_handler_native_shape_via_dispatch_fence(self):
        # The full belt-and-suspenders failure mode that prompted the fix:
        # wrong fence string AND wrong JSON schema, both happening at once.
        handler_output = {
            "status": "approval_required",
            "draft_id": "cdaea46d",
            "preview": {
                "repo": "bramveen1/ai-dev-team",
                "issue_url": "https://github.com/bramveen1/ai-dev-team/issues/241",
                "model": "sonnet",
                "gate_reason": "always",
            },
        }
        text = f"Live.\n\n```dispatch-approval\n{json.dumps(handler_output)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        req = result.draft_requests[0]
        assert req.draft_id == "cdaea46d"
        assert req.pack == "dispatch"
        assert req.action_verb == "dispatch_issue"

    def test_canonical_shape_passes_through_unchanged(self):
        # The translation must not corrupt the canonical schema.  An
        # explicit ``action_verb`` or ``payload`` should short-circuit the
        # handler-native translator even when ``status`` is present.
        payload = {
            "draft_id": "PR-99",
            "pack": "github",
            "action_verb": "merge",
            "payload": {"pr_number": 99},
            "status": "approval_required",  # noise; canonical wins
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        req = result.draft_requests[0]
        assert req.pack == "github"
        assert req.action_verb == "merge"
        assert req.payload == {"pr_number": 99}

    def test_handler_native_without_preview_is_rejected(self):
        # ``approval_required`` status without a ``preview`` dict isn't
        # the dispatch handler's shape — don't auto-translate.
        payload = {
            "status": "approval_required",
            "draft_id": "abc",
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False


@pytest.mark.unit
class TestParseResponseLegacyRejected:
    """Drafts that only set the old ``capability_type`` / ``capability_instance``
    fields (no pack, no target) are rejected — the legacy fallback was
    removed in PR 8."""

    def test_legacy_only_block_is_stripped(self):
        payload = {
            "draft_id": "leg-1",
            "capability_type": "email",
            "capability_instance": "bram",
            "action_verb": "send",
            "payload": {"to": "x@y.com"},
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False
        assert result.cleaned_text == "Done."


@pytest.mark.unit
class TestParseResponseInvalidBlocks:
    def test_malformed_json_is_stripped(self):
        text = "Here you go.\n\n```draft-approval\n{this is not json}\n```"
        result = parse_response(text)

        assert result.has_drafts is False
        assert result.cleaned_text == "Here you go."

    def test_missing_always_required_fields_is_stripped(self):
        # missing payload + action_verb
        incomplete = {"draft_id": "x", "pack": "github"}
        text = f"Done.\n\n```draft-approval\n{json.dumps(incomplete)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False
        assert result.cleaned_text == "Done."

    def test_no_pack_no_target_is_stripped(self):
        """Block with required base fields but no routing info is rejected."""
        payload = {
            "draft_id": "x",
            "action_verb": "send",
            "payload": {"to": "a@b.com"},
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False

    def test_pack_and_target_together_is_stripped(self):
        """Setting both pack and target is ambiguous — reject the block."""
        payload = {
            "draft_id": "x",
            "action_verb": "send",
            "payload": {"to": "a@b.com"},
            "pack": "github",
            "target": "outlook",
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is False

    def test_payload_must_be_dict(self):
        payload = {
            "draft_id": "x",
            "pack": "github",
            "action_verb": "merge",
            "payload": "not a dict",
        }
        text = f"Done.\n\n```draft-approval\n{json.dumps(payload)}\n```"

        result = parse_response(text)

        assert result.has_drafts is True
        assert result.draft_requests[0].payload == {}


# ---------------------------------------------------------------------------
# post_approval_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestPostApprovalMessagePackBacked:
    @pytest.mark.asyncio
    async def test_github_merge_renders_direct_action_button(self, store, slack_client, packs_dir):
        draft_req = _make_draft_request(
            draft_id="pr-42",
            target=None,
            pack="github",
            action_verb="merge",
            payload={"pr_number": 42, "title": "Fix bug"},
        )

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="sam",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        assert draft.draft_type == "direct"
        assert draft.external_id is None
        assert draft.capability_type == "pack"
        assert draft.capability_instance == "github"

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        actions = [b for b in call_kwargs["blocks"] if b.get("type") == "actions"][0]
        action_ids = [e["action_id"] for e in actions["elements"]]
        assert ACTION_APPROVE_SEND in action_ids  # merge maps to APPROVE_SEND
        assert ACTION_REQUEST_EDIT in action_ids
        assert ACTION_DISCARD in action_ids
        assert ACTION_OPEN_IN_APP not in action_ids

    @pytest.mark.asyncio
    async def test_pack_verb_not_in_approve_renders_discard_only(self, store, slack_client, packs_dir):
        """If the agent drafts a verb that isn't approval-gated for this
        pack, render discard-only — the agent shouldn't have drafted."""
        draft_req = _make_draft_request(
            draft_id="x",
            target=None,
            pack="zoho-mail",  # approve:[]
            action_verb="send",
        )

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        actions = [b for b in call_kwargs["blocks"] if b.get("type") == "actions"][0]
        action_ids = [e["action_id"] for e in actions["elements"]]
        assert action_ids == [ACTION_DISCARD]

    @pytest.mark.asyncio
    async def test_unknown_pack_falls_back_to_discard(self, store, slack_client, packs_dir):
        """A draft naming a pack that doesn't exist on disk renders
        discard-only — the user can't act on a pack the router can't find."""
        draft_req = _make_draft_request(
            draft_id="x",
            target=None,
            pack="ghost-pack",
            action_verb="send",
        )

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        actions = [b for b in call_kwargs["blocks"] if b.get("type") == "actions"][0]
        action_ids = [e["action_id"] for e in actions["elements"]]
        assert action_ids == [ACTION_DISCARD]


@pytest.mark.unit
class TestPostApprovalMessageConnectorBacked:
    @pytest.mark.asyncio
    async def test_outlook_target_renders_open_in_outlook_with_deep_link(self, store, slack_client, packs_dir):
        draft_req = _make_draft_request(
            draft_id="AAMkTest123",
            target="outlook",
        )

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        assert draft.draft_type == "native"
        assert draft.external_id == "AAMkTest123"
        assert draft.capability_type == "connector"
        assert draft.capability_instance == "outlook"

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        actions = [b for b in call_kwargs["blocks"] if b.get("type") == "actions"][0]
        elements = actions["elements"]
        action_ids = [e["action_id"] for e in elements]

        assert ACTION_OPEN_IN_APP in action_ids
        assert ACTION_APPROVE_SEND not in action_ids

        open_button = [e for e in elements if e["action_id"] == ACTION_OPEN_IN_APP][0]
        assert "outlook.office.com" in open_button["url"]
        assert "AAMkTest123" in open_button["url"]


@pytest.mark.unit
class TestPostApprovalMessagePersistence:
    @pytest.mark.asyncio
    async def test_draft_persisted_with_slack_message_ts(self, store, slack_client, packs_dir):
        slack_client.chat_postMessage.return_value = {"ts": "1705700099.000300"}
        draft_req = _make_draft_request()

        draft = await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C99",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        persisted = store.get(draft.draft_id)
        assert persisted is not None
        assert persisted.slack_message_ts == "1705700099.000300"
        assert persisted.slack_channel == "C99"

    @pytest.mark.asyncio
    async def test_payload_preview_in_approval_message(self, store, slack_client, packs_dir):
        draft_req = _make_draft_request(
            payload={"to": "sam@example.com", "subject": "Hello Sam", "body": "How are you?"},
        )

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C12345",
            thread_ts="1705700000.000100",
            client=slack_client,
            store=store,
            packs_dir=packs_dir,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        section_blocks = [b for b in call_kwargs["blocks"] if b.get("type") == "section"]
        section_text = " ".join(b["text"]["text"] for b in section_blocks)
        assert "sam@example.com" in section_text
        assert "Hello Sam" in section_text
        assert "How are you?" in section_text


@pytest.mark.unit
class TestPostApprovalMessageNoPacksDir:
    """When packs_dir is None we fall back to the repo's real packs/ tree.
    This test just confirms a missing pack doesn't crash."""

    @pytest.mark.asyncio
    async def test_unknown_pack_with_default_packs_dir(self, store, slack_client, tmp_path):
        # tmp_path/packs is empty — no packs at all
        empty_packs = tmp_path / "no-packs"
        empty_packs.mkdir()

        draft_req = _make_draft_request(target=None, pack="anything")

        await post_approval_message(
            draft_request=draft_req,
            agent_name="lisa",
            channel="C",
            thread_ts="t",
            client=slack_client,
            store=store,
            packs_dir=empty_packs,
        )

        call_kwargs = slack_client.chat_postMessage.call_args.kwargs
        actions = [b for b in call_kwargs["blocks"] if b.get("type") == "actions"][0]
        action_ids = [e["action_id"] for e in actions["elements"]]
        assert action_ids == [ACTION_DISCARD]


# ---------------------------------------------------------------------------
# detect_unbacked_claim: safety-net guard against fabricated drafts
# ---------------------------------------------------------------------------


@pytest.mark.unit
class TestDetectUnbackedClaim:
    """The guard catches agent prose that claims a draft/dispatch but
    omits the ``draft-approval`` fenced block — the fabrication pattern
    seen with sam queueing phantom dispatches (draft_ids 88847db2,
    7bd39ef6, 60110859, fad268bc) without invoking the handler."""

    def test_draft_id_with_hex_is_flagged(self):
        text = "Drafted: draft_id: fad268bc — sending the card."
        match = detect_unbacked_claim(text, has_drafts=False)
        assert match is not None
        assert "draft_id" in match.lower()
        assert "fad268bc" in match.lower()

    def test_approval_card_incoming_is_flagged(self):
        text = "Done — approval card incoming."
        assert detect_unbacked_claim(text, has_drafts=False) is not None

    def test_queued_the_dispatch_is_flagged(self):
        text = "Queued the dispatch on sonnet/dev/30min."
        assert detect_unbacked_claim(text, has_drafts=False) is not None

    def test_rocket_dispatch_emoji_is_flagged(self):
        text = ":rocket: Dispatch `abc123def` is en route."
        assert detect_unbacked_claim(text, has_drafts=False) is not None

    def test_has_drafts_short_circuits(self):
        # When a real block was parsed, the guard never fires — even on text
        # that would otherwise match.
        text = "draft_id: fad268bc — approval card incoming."
        assert detect_unbacked_claim(text, has_drafts=True) is None

    def test_innocent_prose_is_not_flagged(self):
        text = "Reviewed PR #224. Approving for squash-merge."
        assert detect_unbacked_claim(text, has_drafts=False) is None

    def test_bare_draft_id_word_is_not_flagged(self):
        # Without a hex suffix, bare "draft_id" mentions (e.g. when
        # discussing the schema field name) don't trip the guard.
        text = "The block needs a draft_id field set to the external id."
        assert detect_unbacked_claim(text, has_drafts=False) is None

    def test_discussing_drafts_abstractly_is_not_flagged(self):
        text = "When we queue dispatches, the approval card lands in Slack."
        assert detect_unbacked_claim(text, has_drafts=False) is None
