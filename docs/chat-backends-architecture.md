# Architecture: Pluggable Chat Backends (multi-transport epic)

> **Epic:** #121 (`epic:chat-backends`) · **PRD:** Dave's Notion PRD
> **Status (2026-06-27):** Phase 1.1 (#122) landed — contract + stubs in tree.
> Phase 2 (#125, terminal) draft dispatched, awaiting approval.
> **Sequencing decision (2026-06-27):** Discord (#126) now lands *before* the
> live Slack migration (#553). Slack is the lifeline, so it is the **last**
> transport we operate on — and only once Discord is a proven, independent
> *remote* fallback. See §5.
> **Audience:** Bram. This is the "understand it at a detailed level" doc —
> the design rationale behind the code already in `router/chat/`.

---

## 1. The one-sentence problem

Today Slack is not *a* transport — it *is* the chat layer. `slack_sdk` calls,
`channel:thread` addressing, `<@U…>` mention parsing, reaction-emoji status, and
Block Kit approval cards are spread through the router, dispatcher, and several
packs. Adding any second surface (terminal, Discord, WebUI) means duplicating
all of that coupling. This epic makes the chat layer **pluggable**: one core,
N transports, where the core never knows which transport it's talking through.

The target invariant:

> Adding a new backend touches only adapter code + a tiny amount of wiring —
> **zero** changes to router, dispatcher, or pack handlers.

---

## 2. The shape: one core, N transports

```
                       ┌─────────────────────────────────────┐
                       │                CORE                  │
                       │  (router, dispatcher, agent loop)    │
                       │                                      │
                       │  topology-blind · identity-blind     │
                       │  depends ONLY on ChatAdapter + types │
                       └───────────────┬──────────────────────┘
                                       │  ChatAdapter contract
                                       │  (interface.py + types.py)
              ┌────────────────────────┼────────────────────────┐
              │                        │                        │
      ┌───────▼────────┐      ┌────────▼────────┐      ┌────────▼────────┐
      │  SlackAdapter  │      │ TerminalAdapter │      │ DiscordAdapter  │
      │  channel:thread│      │ terminal:<sid>  │      │   (Phase 3)     │
      │  <@U…> mentions│      │ local:<user>    │      │                 │
      │  reaction emoji│      │ numbered text   │      │                 │
      │  Block Kit     │      │ stdout          │      │                 │
      └───────┬────────┘      └────────┬────────┘      └────────┬────────┘
              │                        │                        │
          Slack API                stdin/stdout             Discord gateway
```

The contract boundary is a hard wall. Everything Slack-shaped lives to the
*left* of an adapter; everything to the *right* (core) sees only opaque tokens
and capability flags. Any Slack concept that leaks across that wall is a future
contract rewrite — which is the exact cost this epic exists to stop paying.

---

## 3. The contract (`router/chat/`)

The contract is deliberately tiny — **four primitives + a handful of types** —
because every method here is expensive to reverse once a second adapter depends
on it. Source of truth: `router/chat/interface.py` and `router/chat/types.py`.

### 3.1 The two opaque reference types

These are the load-bearing idea of the whole epic.

```python
ConversationRef = NewType("ConversationRef", str)   # "where"  — addressing
PrincipalRef    = NewType("PrincipalRef", str)      # "who"    — identity
```

Rules enforced by convention (and grep-guards in #122's ACs):

- **Core may store and echo them; it must never construct or parse them.**
  Only adapter code in `router/chat/adapters/` calls the constructors.
- They are **persistable plain strings**, not just echo-on-reply handles. This
  is what lets the core emit *proactively* — a cron tick or "PR merged" event
  stores a ref and sends to it later, with no inbound to reply to.

What they decode to is the adapter's private business:

| Transport | `ConversationRef` encoding | `PrincipalRef` encoding |
|-----------|----------------------------|--------------------------|
| Slack     | `"<channel_id>:<thread_ts>"` | Slack user ID (`U01234ABC`) |
| Terminal  | `"terminal:<session_id>"`    | `"local:<username>"` |
| Discord   | guild/channel snowflake (TBD)| Discord snowflake (TBD) |

The terminal encoding being structurally *unlike* `channel:thread` is the whole
point of Phase 2 — see §5.

### 3.2 The four behavioural primitives

From `ChatAdapter` (interface.py). Core depends on these signatures and nothing
below them:

1. **Outbound — `send_message(outbound)`** *(first-class, not request/response)*
   The core can speak unprompted. `OutboundMessage.conversation_ref` may be
   `None`, in which case the adapter delivers to its configured **default/home**
   destination. This is the cron / dispatch-result / "PR merged" path and the
   reason refs must be persistable.

2. **Inbound — `read_thread(conversation_ref)`**
   Returns conversation history in chronological order. Core passes the opaque
   ref straight back; the adapter decodes it.

3. **Status — `set_status(conversation_ref, AdapterStatus)`**
   A generic enum (`THINKING / WORKING / DONE / ERROR`), *not* Slack emoji
   names. Rich transports render a reaction; threadless ones print `[...]` or
   no-op gracefully. Using an enum is what lets a non-reaction backend map each
   state sensibly instead of inheriting Slack's vocabulary.

4. **Interactive — `prompt_for_choice(conversation_ref, PromptChoice)`**
   Gated by the `supports_interactive` capability flag. Rich transports render
   buttons / Block Kit; threadless transports render a **numbered text list** and
   parse a digit reply into a `StructuredResponse`. The de-Slacking of the
   *implementation* is its own phase — this method reserves the *shape* now so
   that phase has a stable target.

Plus two synchronous resolution helpers that keep identity and mention-parsing
out of core: `resolve_principal(raw_user_id)` and
`parse_mentions(text, conversation_ref)`.

### 3.3 Capability flags — the anti-`if transport ==` mechanism

```python
@dataclass(frozen=True)
class AdapterCapabilities:
    supports_threads: bool = False
    supports_channels: bool = False
    supports_interactive: bool = False
```

This is the single most important design rule in the epic:

> **Core behaviour degrades on capability flags — never on transport names.**
> There is no `if transport == "slack"` anywhere in core, and a grep-guard in
> CI keeps it that way.

`SlackAdapter` declares all three `True`; `TerminalAdapter` declares all three
`False`. Core checks `adapter.capabilities.supports_interactive` before reaching
for `prompt_for_choice` — it never asks "are you Slack?". Add Discord later and
core needs zero new branches; Discord just declares its own flags.

---

## 4. Current state of the tree (what's real vs. stubbed)

Everything from #122 is committed and type-checks. Important: it is **inert** —
no live behaviour change yet.

| File | What it is today |
|------|------------------|
| `router/chat/interface.py` | The `ChatAdapter` ABC — the contract. Real. |
| `router/chat/types.py` | The opaque refs, `AdapterCapabilities`, message/prompt shapes. Real. |
| `router/chat/adapters/slack.py` | **Stub.** Implements the interface, logs, returns safe defaults. Makes **no live Slack calls.** Proves the contract compiles against a Slack-flavoured shape. |
| `router/chat/adapters/terminal.py` | **Working** terminal adapter — real stdin/stdout, in-process, non-Slack refs. |
| `router/chat/terminal_driver.py` | **E2E harness (#555).** Drives all four primitives at a prompt with no Slack creds. **Today its reply is still `f"Echo: {text}"`** — it proves the *adapter loop* works, not that it talks to Sam. |
| `router/chat/__init__.py` | `CHAT_BACKENDS` env flag — **defaults `False`**. Until #553 flips it, nothing live routes through the abstraction. |

So the codebase already has the contract and a structurally-opposite second
adapter. What it does **not** yet have is anything wiring the adapter loop to the
actual agent — which is exactly the gap Phase 2 (#125) closes.

---

## 5. The phase sequence — and *why* this order

```
 #122 ─────► #555 ─────► #125 ─────► #126 ───────► #553 ────────► (#123/#124/
 contract   E2E proof   terminal    Discord       migrate         #551/#552
 + stub     (2nd        consumes    (3rd xport,   LIVE Slack      fold in as
            transport)  the seam    1st REMOTE    onto adapter    each area
                                    fallback)     — LAST          de-Slacks)
 ────────────────────── Phase 1/2 ── Phase 3 ──── Phase 1.4 ────
 PURE ADD   PROOF       LOW-RISK     NEW REMOTE    RISKIEST:
                        can't        SURFACE       rewires the
                        break prod   (design-      lifeline; run
                                     first)        while Discord up
```

> **Note:** "Phase 3" is Discord's *epic* name; in execution order it now runs
> **before** the Phase 1.4 Slack migration. The de-Slack leftovers
> (#123/#124/#551/#552) are folded in as each area is needed — and Discord pulls
> some forward (see below).

The non-obvious sequencing decision, and the reason it matters:

- **#122 is split from #553 by *layer*, not by feature.** #122 is pure addition
  (define the interface + a stub that type-checks — trivial to revert). #553 is
  the one refactor that can break prod (rewire the live Slack path). You never
  want "new abstraction" and "rewire prod" in the same diff with the same review
  lens. #122 is reviewed for design (opacity, no `if transport ==`); #553 is
  reviewed for **parity** (byte-identical Slack behaviour).

- **#555 (terminal E2E) gates #553 deliberately.** An abstraction with one
  implementation hasn't been proven transport-agnostic — it has only ever seen
  Slack's shape. The terminal is Slack's structural opposite (threadless,
  synchronous, in-process, no Block Kit, no network, no tokens), so it forces
  every primitive we claimed was expensive-to-reverse. This is our own rule —
  *"second instance of a pattern is when the contract has to exist"* — pointed
  back at ourselves. Prove the contract on a transport that **can't break prod**,
  *then* let the risky Slack migration ride the proven seam.

- **#125 makes the terminal the *first real consumer*.** #555 proved the adapter
  loop; #125 connects that loop to Sam. See §6.

- **Discord (#126) is sequenced *before* the live Slack migration (#553) — Slack
  is the lifeline.** The terminal (#125) proves the contract is transport-agnostic,
  but it only helps when you're at a terminal with host access (`docker exec`); it
  is **not a remote fallback**. Discord is — a real remote transport reachable from
  a phone, the same class as Slack. So the order that actually protects us is:
  (1) terminal confirms the contract holds; (2) Discord becomes our first
  *independent remote* channel, proven live; (3) **only then** migrate the live
  Slack path onto the adapter, behind the `CHAT_BACKENDS` flag with the legacy path
  kept revertable — done **while Discord is up**, so if Slack breaks mid-cutover we
  talk over Discord and roll back. We are never mute. Operating on the lifeline
  before an independent remote fallback exists is the one ordering we refuse.

- **Cost of this order (tracked, temporary).** Until #553 lands we carry two
  Slack-ish paths — the legacy live Slack handlers *and* the adapter-based
  Discord/terminal path. Acceptable and bounded; tracked as tech debt so it does
  not become permanent.

- **Discord is a Design-First Trigger.** New external dependency (gateway client) +
  new bot token (secret) + new outbound network surface ⇒ its own 1-pager (runtime
  deps, permission boundary, smoke probe, failure modes, rollback) **before any
  code**. It is also the "third real instance" that stress-tests whether the
  contract actually holds. Likely prerequisite to flag now: Discord's approval
  cards need the ApprovalCard renderer (#123) and per-bot identity (#124), so those
  two may pull forward from "leftovers" into Discord's critical path.

---

## 6. Phase 2 in detail: the `run_agent_turn` seam (#125)

This is the piece awaiting approval, and the one most worth understanding,
because it's the seam #553 later reuses for Slack.

**Problem it solves:** today `terminal_driver.run_once` builds a real inbound,
exercises all four primitives, and replies `Echo: {text}`. There is no path from
"adapter received text" to "Sam's container produced an answer." That path is
core, and it must not be Slack-specific.

**What #125 adds:**

```python
# core seam — transport-agnostic, no Slack `client` anywhere in this path
async def run_agent_turn(adapter: ChatAdapter, inbound: InboundMessage) -> None:
    # text in → run Sam's container → text out
    # builds OutboundMessage, emits via adapter.send_message(...)
    ...
```

```
  docker exec -it <router> python -m router.chat.terminal_console
        │
        ▼
  read stdin ──► TerminalAdapter builds inbound (terminal:<sid> ref)
        │
        ▼
  run_agent_turn(adapter, inbound)        ◄── the new core seam
        │           │
        │           └─► runs Sam's container, captures real output
        ▼
  adapter.send_message(OutboundMessage(real text))  ──► stdout
```

**Deliberate constraints (the design-first guardrails on the issue):**

- **No `if transport ==` / no Slack `client` reference in the seam.** It takes a
  `ChatAdapter`, not a Slack handle. This is enforced by review, because the
  whole value of the seam is that #553 can hand it a `SlackAdapter` unchanged.
- **Runs inside the *existing* router container** via `docker exec`. **No new
  dependency, no new network port, no new secret.** Moves with a one-directory
  copy — the portability hard-constraint holds.
- **Reach-Sam-only, prod Slack untouched.** `CHAT_BACKENDS` stays `False`; the
  diff touches no live Slack handler behaviour. Blast radius on prod Slack is
  zero; rollback is one `git revert`.
- **Logging trap (carried from the original issue):** the console is a separate
  `docker exec` process, so its stdout must not collide with router logging. If
  router log lines leak into the TUI, apply file-only logging while the console
  is active. Called out as an explicit AC so the worker doesn't rediscover it.

**Why it's not throwaway:** `run_agent_turn` is the same consume-loop #553 needs
for Slack. Phase 2 validates it on a transport that physically cannot reach prod
Slack; Phase 1.4 then rewires the live path onto an already-proven seam.

---

## 7. Phase 1.4 in detail: the live Slack migration (#553)

The risky half — quarantined on purpose, and now sequenced **last of the
transports**: run only after Discord is live as an independent remote fallback,
so a broken cutover never leaves us mute.

- **Goal:** route the live Slack flow through `ChatAdapter`, **byte-for-byte
  identical** to current `main`, proven by parity tests. `CHAT_BACKENDS=slack`
  becomes the default and must reproduce today's behaviour exactly.
- **Mechanic:** move every `slack_sdk` call site out of core and into
  `SlackAdapter`; inbound resolves `conversation_ref` + `principal_ref`; outbound
  (including cron ticks, "PR merged", dispatch results) emits via the adapter's
  first-class outbound to a stored ref with a default fallback.
- **Parity harness:** capture golden outputs from *current* behaviour first for
  the top ~10 message shapes (mentions, bold/links/emoji, code blocks, threaded
  replies, reactions/status), then refactor against the goldens. This is a CI
  matrix job — without CI enforcement, parity drifts.
- **Out of scope, kicked to siblings:** contract/type changes (back to #122),
  ApprovalCard de-Slacking (#123), `agents.yaml`/identity (#124), command grammar
  (#551), structured input/modals (#552). If migration reveals a contract gap, it
  goes *back* to #122 rather than getting patched in the risky diff.

---

## 8. Open questions carried from the epic (still unresolved)

These are the known-hard problems the contract reserves shape for but hasn't
fully solved. Flagging them so they don't surprise us in Phase 1.x / Phase 3:

1. **ApprovalCard is not an "attachment."** It's interaction state with a
   callback — the most platform-coupled thing in the system. It needs its own
   normalized type and a per-backend renderer (#123). `prompt_for_choice` is the
   first thin slice of this; the full card model is bigger.
2. **Status signals** beyond the four-state enum — Slack uses reactions today;
   anything richer needs to stay on the interface, not leak.
3. **Agent-initiated messages** — when the nightly curator or a scheduled task
   posts, *which* backend + *which* conversation? Each adapter needs a
   `default_channel` / home ref. The `conversation_ref=None` outbound path is the
   mechanism; the routing policy (which transport) is still open.
4. **Secrets sprawl** — 5 agents × N backends = a lot of env vars. The standing
   recommendation is a structured `agents.yaml` referencing a secrets store
   (#124) rather than N flat env vars. Not yet decided.
5. **Same-process multi-backend** — Slack + Terminal both active must not
   interfere. Success criterion in the epic; exercised first by running the
   terminal console alongside the live router.

---

## 9. Risk & rollback posture

- **Phase 1.1 (#122) — landed:** pure addition, inert behind `CHAT_BACKENDS=False`.
  Revert = delete `router/chat/`. No prod surface.
- **Phase 2 (#125):** no new dep/port/secret; prod Slack untouched; runs in the
  existing container. Revert = one `git revert`. This is the lowest-risk way to
  prove the consume-loop.
- **Phase 3 (#126, Discord):** the first phase that breaks the
  "no new dep/secret/port" streak — gateway client + bot token + outbound network.
  This is the **explicit portability exception**, gated on its own design-first
  1-pager and a smoke probe before any code. Sequenced before #553 precisely so a
  proven remote fallback exists when we touch the lifeline.
- **Phase 1.4 (#553) — now last of the transports:** the real risk lives here,
  fenced behind parity tests and a parity-specific review lens, and executed
  **while Discord is live** as the fallback channel. The flag defaults to `slack`
  only once parity is green in CI; flipping back is an env-var change.
- **Portability invariant** holds through Phase 2. **Phase 3 (Discord) is the
  first deliberate exception** (new external service + secret), gated on its own
  1-pager. No *other* phase introduces a new service, port, or secret; the system
  still moves with one directory copy everywhere except Discord's documented
  token/config.

---

## 10. Glossary

- **Transport** — one channel Sam is reachable on (Slack, Terminal, Discord, …).
- **Adapter** — the per-transport implementation of `ChatAdapter`; the only code
  allowed to touch transport SDKs and construct/parse refs.
- **Core** — router + dispatcher + agent loop; depends only on `ChatAdapter` and
  the opaque types. Topology-blind and identity-blind.
- **`ConversationRef` / `PrincipalRef`** — opaque, persistable "where" / "who"
  tokens. Adapter-constructed, core-stored-and-echoed.
- **Seam (`run_agent_turn`)** — the transport-agnostic core entry point: inbound
  → run agent → outbound, with no transport SDK in sight.
- **Parity gate** — golden-output tests proving the Slack migration is
  byte-identical to current `main`.

---

*Maintainer note: this doc tracks the design, not the line numbers. If the
contract in `router/chat/interface.py` changes, update §3. Source issues:
#121 (epic), #122, #555, #125, #553, #123, #124, #551, #552.*
