"""Tests for the InputRequest structured-input primitive and its scripted fallback.

Covers the new types (``InputField``/``InputRequest``/``InputResponse``/
``InputFieldType``), the ``supports_forms`` capability flag, and
``collect_input_scripted`` — the transport-neutral helper that fulfils an
``InputRequest`` over any adapter's ``send_message``/``read_thread``
primitives. Exercised against a fake/stub ``ChatAdapter``, in the same style
as the #122 parity fixtures — no real transport is touched.
"""

from __future__ import annotations

import re

import pytest

pytestmark = pytest.mark.unit

# Fast enough to keep the suite quick, long enough that the poll loop's
# sleep() reliably yields to the event loop before rechecking read_thread.
_POLL_INTERVAL = 0.01


def _call_scripted(adapter, conversation_ref, request, **kwargs):
    from router.chat.input_collect import collect_input_scripted

    kwargs.setdefault("poll_interval_seconds", _POLL_INTERVAL)
    return collect_input_scripted(adapter, conversation_ref, request, **kwargs)


# ---------------------------------------------------------------------------
# Fake adapter — in-memory ChatAdapter stub
# ---------------------------------------------------------------------------


def _make_fake_adapter():
    from router.chat.interface import ChatAdapter
    from router.chat.types import (
        AdapterCapabilities,
        ConversationRef,
        InboundMessage,
        OutboundMessage,
        PrincipalRef,
        StructuredResponse,
    )

    class FakeAdapter(ChatAdapter):
        """In-memory adapter that echoes its own posts into thread history.

        Real transports (Slack, Discord) append the bot's own outbound
        messages to the very channel history ``read_thread`` returns, so a
        faithful fake must do the same. Echoing posts into ``history`` is what
        exercises the collector's reply-position accounting — an earlier fake
        that appended sends only to ``self.sent`` let a broken collector
        (which mistook its own prompt for the user's reply) pass CI; see the
        PR #746/#748 review.

        Scripted user replies are queued via :meth:`reply` and delivered FIFO:
        a queued reply lands in history the next time the collector polls
        ``read_thread`` while waiting on a posted prompt (i.e. history's last
        entry is a bot post), mirroring "the user answers after seeing the
        prompt." This is deterministic — no reliance on event-loop timing.
        """

        def __init__(self):
            self.history: list = []  # full thread: bot posts + user replies, in order
            self.sent: list = []  # bot posts only, for content assertions
            self._pending: list[str] = []  # queued user replies, FIFO

        @property
        def capabilities(self):
            return AdapterCapabilities()

        async def send_message(self, outbound):
            self.sent.append(outbound)
            self.history.append(outbound)  # a real transport echoes our post into the thread

        async def read_thread(self, conversation_ref):
            # A queued reply arrives once the collector is waiting on a posted
            # prompt (the thread's last entry is one of our own posts).
            if self._pending and self.history and isinstance(self.history[-1], OutboundMessage):
                self.history.append(
                    InboundMessage(
                        conversation_ref=ConversationRef("fake:1"),
                        principal_ref=PrincipalRef("u1"),
                        text=self._pending.pop(0),
                    )
                )
            return list(self.history)

        async def set_status(self, conversation_ref, state):
            pass

        def resolve_principal(self, raw_user_id):
            return PrincipalRef(raw_user_id)

        def parse_mentions(self, text, conversation_ref):
            return []

        async def prompt_for_choice(self, conversation_ref, prompt):
            return StructuredResponse(choice=prompt.choices[0], index=0)

        async def collect_input(self, conversation_ref, request):
            return await _call_scripted(self, conversation_ref, request)

        def reply(self, text: str) -> None:
            """Queue a scripted user reply, delivered FIFO as the collector waits."""
            self._pending.append(text)

    return FakeAdapter()


# ---------------------------------------------------------------------------
# Type definitions
# ---------------------------------------------------------------------------


class TestImports:
    def test_types_importable(self):
        from router.chat.types import (  # noqa: F401
            InputField,
            InputFieldType,
            InputRequest,
            InputResponse,
        )

    def test_helper_importable(self):
        from router.chat.input_collect import collect_input_scripted  # noqa: F401


class TestInputFieldType:
    def test_all_variants_present(self):
        from router.chat.types import InputFieldType

        assert InputFieldType.TEXT == "text"
        assert InputFieldType.SECRET == "secret"
        assert InputFieldType.CHOICE == "choice"
        assert InputFieldType.INT == "int"


class TestInputField:
    def test_defaults(self):
        from router.chat.types import InputField, InputFieldType

        field = InputField(key="name", label="Name?")
        assert field.type == InputFieldType.TEXT
        assert field.required is True
        assert field.options is None
        assert field.validator is None

    def test_custom_values(self):
        from router.chat.types import InputField, InputFieldType

        field = InputField(
            key="env",
            label="Environment",
            type=InputFieldType.CHOICE,
            required=False,
            options=["prod", "staging"],
        )
        assert field.type == InputFieldType.CHOICE
        assert field.required is False
        assert field.options == ["prod", "staging"]


class TestInputRequest:
    def test_defaults(self):
        from router.chat.types import InputField, InputRequest

        request = InputRequest(title="Setup", fields=[InputField(key="k", label="K?")])
        assert request.timeout_seconds == 300
        assert len(request.fields) == 1

    def test_custom_timeout(self):
        from router.chat.types import InputField, InputRequest

        request = InputRequest(title="Setup", fields=[InputField(key="k", label="K?")], timeout_seconds=30)
        assert request.timeout_seconds == 30


class TestInputResponse:
    def test_fields(self):
        from router.chat.types import InputResponse

        resp = InputResponse(values={"k": "v"}, status="completed")
        assert resp.values == {"k": "v"}
        assert resp.status == "completed"


class TestAdapterCapabilitiesSupportsForms:
    def test_default_false(self):
        from router.chat.types import AdapterCapabilities

        assert AdapterCapabilities().supports_forms is False

    def test_can_be_enabled(self):
        from router.chat.types import AdapterCapabilities

        assert AdapterCapabilities(supports_forms=True).supports_forms is True


class TestChatAdapterCollectInputAbstract:
    def test_collect_input_is_abstract(self):
        from router.chat.interface import ChatAdapter

        assert "collect_input" in ChatAdapter.__abstractmethods__


# ---------------------------------------------------------------------------
# collect_input_scripted — happy path
# ---------------------------------------------------------------------------


class TestScriptedHappyPath:
    @pytest.mark.asyncio
    async def test_text_field_collected(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("Ada")
        request = InputRequest(title="Setup", fields=[InputField(key="name", label="Name?")])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"name": "Ada"}
        # One prompt for the field (title has already been posted separately).
        assert any("Name?" in m.text for m in adapter.sent)

    @pytest.mark.asyncio
    async def test_multi_field_collected_in_order(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("nightly-backup")
        adapter.reply("0 2 * * *")
        request = InputRequest(
            title="Create task",
            fields=[
                InputField(key="name", label="Task name?"),
                InputField(key="cron", label="Cron schedule?", type=InputFieldType.TEXT),
            ],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"name": "nightly-backup", "cron": "0 2 * * *"}

    @pytest.mark.asyncio
    async def test_posts_title_first(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("Ada")
        request = InputRequest(title="Create task", fields=[InputField(key="name", label="Name?")])

        await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert adapter.sent[0].text == "Create task"

    @pytest.mark.asyncio
    async def test_ignores_own_posts_as_replies(self):
        """Regression (PR #748): on a real transport the bot's own title and
        prompt posts land in the same thread history ``read_thread`` returns.
        The collector must count those posts and wait for a genuine user
        reply — never treat its own prompt text as the answer.
        """
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("real-answer")
        request = InputRequest(title="Setup", fields=[InputField(key="name", label="Name?")])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        # The stored value is the user's reply, NOT an echoed title/prompt post.
        assert resp.values == {"name": "real-answer"}
        assert "Setup" not in resp.values.values()
        assert "Name?" not in resp.values.values()

    @pytest.mark.asyncio
    async def test_optional_field_accepts_empty_reply(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("")
        request = InputRequest(title="Setup", fields=[InputField(key="notes", label="Notes?", required=False)])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"notes": ""}


# ---------------------------------------------------------------------------
# collect_input_scripted — validation → reprompt
# ---------------------------------------------------------------------------


class TestScriptedValidationReprompt:
    @pytest.mark.asyncio
    async def test_int_field_reprompts_on_non_numeric(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("not-a-number")
        adapter.reply("42")
        field = InputField(key="timeout", label="Timeout?", type=InputFieldType.INT)
        request = InputRequest(title="Setup", fields=[field])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"timeout": "42"}
        # The reprompt after the bad reply should surface an error.
        assert any(":x:" in m.text for m in adapter.sent)

    @pytest.mark.asyncio
    async def test_required_field_reprompts_on_empty(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("")
        adapter.reply("real-value")
        request = InputRequest(title="Setup", fields=[InputField(key="name", label="Name?")])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"name": "real-value"}

    @pytest.mark.asyncio
    async def test_regex_validator_reprompts_until_match(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        cron_pattern = re.compile(r"(\S+\s+){4}\S+")
        adapter = _make_fake_adapter()
        adapter.reply("not a cron string")
        adapter.reply("0 2 * * *")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="cron", label="Cron?", validator=cron_pattern)],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"cron": "0 2 * * *"}

    @pytest.mark.asyncio
    async def test_callable_validator_reprompts_until_true(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("5")
        adapter.reply("50")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="pct", label="Percent?", validator=lambda v: int(v) >= 10)],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"pct": "50"}

    @pytest.mark.asyncio
    async def test_raising_validator_reprompts_instead_of_crashing(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        def _validator(value: str) -> bool:
            raise RuntimeError("boom")

        adapter = _make_fake_adapter()
        adapter.reply("first")
        adapter.reply("second")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="k", label="K?", validator=_validator)],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request, max_attempts=2)

        # Every attempt fails validation (always raises), so attempts are
        # exhausted rather than the collector crashing.
        assert resp.status == "cancelled"
        assert resp.values == {}

    @pytest.mark.asyncio
    async def test_attempts_exhausted_yields_cancelled(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("x")
        adapter.reply("y")
        adapter.reply("z")
        request = InputRequest(title="Setup", fields=[InputField(key="n", label="N?", type=InputFieldType.INT)])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request, max_attempts=3)

        assert resp.status == "cancelled"
        assert resp.values == {}


# ---------------------------------------------------------------------------
# collect_input_scripted — CHOICE digit-parse
# ---------------------------------------------------------------------------


class TestScriptedChoiceField:
    @pytest.mark.asyncio
    async def test_digit_reply_selects_option(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("2")
        field = InputField(key="color", label="Color?", type=InputFieldType.CHOICE, options=["red", "green", "blue"])
        request = InputRequest(title="Setup", fields=[field])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"color": "green"}

    @pytest.mark.asyncio
    async def test_prompt_renders_numbered_list(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("1")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="color", label="Color?", type=InputFieldType.CHOICE, options=["red", "green"])],
        )

        await _call_scripted(adapter, ConversationRef("fake:1"), request)

        prompt_text = adapter.sent[-1].text
        assert "1. red" in prompt_text
        assert "2. green" in prompt_text

    @pytest.mark.asyncio
    async def test_out_of_range_digit_reprompts(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("99")
        adapter.reply("1")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="color", label="Color?", type=InputFieldType.CHOICE, options=["red", "green"])],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"color": "red"}


# ---------------------------------------------------------------------------
# collect_input_scripted — cancel / timeout
# ---------------------------------------------------------------------------


class TestScriptedCancelTimeout:
    @pytest.mark.asyncio
    async def test_cancel_keyword_ends_form(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("cancel")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="a", label="A?"), InputField(key="b", label="B?")],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "cancelled"
        assert resp.values == {}

    @pytest.mark.asyncio
    async def test_cancel_keyword_is_case_insensitive(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("CaNcEl")
        request = InputRequest(title="Setup", fields=[InputField(key="a", label="A?")])

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "cancelled"

    @pytest.mark.asyncio
    async def test_partial_values_kept_before_cancel(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("Ada")
        adapter.reply("cancel")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="a", label="A?"), InputField(key="b", label="B?")],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "cancelled"
        assert resp.values == {"a": "Ada"}

    @pytest.mark.asyncio
    async def test_no_reply_times_out(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="a", label="A?")],
            timeout_seconds=0.05,
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request, poll_interval_seconds=0.01)

        assert resp.status == "timed_out"
        assert resp.values == {}

    @pytest.mark.asyncio
    async def test_timeout_after_partial_collection(self):
        from router.chat.types import ConversationRef, InputField, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("Ada")
        request = InputRequest(
            title="Setup",
            fields=[InputField(key="a", label="A?"), InputField(key="b", label="B?")],
            timeout_seconds=0.05,
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request, poll_interval_seconds=0.01)

        assert resp.status == "timed_out"
        assert resp.values == {"a": "Ada"}


# ---------------------------------------------------------------------------
# SECRET field visibility warning
# ---------------------------------------------------------------------------


class TestScriptedSecretField:
    @pytest.mark.asyncio
    async def test_secret_field_posts_visibility_warning(self):
        from router.chat.types import ConversationRef, InputField, InputFieldType, InputRequest

        adapter = _make_fake_adapter()
        adapter.reply("ghp_faketoken")
        request = InputRequest(
            title="Grant",
            fields=[InputField(key="pat", label="Personal access token?", type=InputFieldType.SECRET)],
        )

        resp = await _call_scripted(adapter, ConversationRef("fake:1"), request)

        assert resp.status == "completed"
        assert resp.values == {"pat": "ghp_faketoken"}
        prompt_text = adapter.sent[-1].text
        assert "visible in the channel history" in prompt_text
