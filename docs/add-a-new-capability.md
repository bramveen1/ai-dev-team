# Runbook: Add a New Capability Type

Walk-through for introducing a new capability *type* — a new abstract verb the team can do (e.g. `crm`, `finance`, `support`, `recruiting`).

Budget: **under one hour** if a provider for the capability already exists.

## First: should this be a new capability type?

Add a new capability type when the new work is **semantically distinct** from every existing type. A capability type answers the question "what can the agent *do*?" — not "which service?".

### Add a new type when

- The new work has its **own verb vocabulary** that doesn't fit any existing type. Example: CRM has `create-contact`, `update-deal`, `export` — these aren't `read`/`write`/`send`.
- The new work has **its own trust boundary**. Example: `finance` needs its own approval rules distinct from `docs`.
- A role.md sentence like *"use your X access to ..."* reads naturally — meaning the word is a first-class verb for users.

### Do NOT add a new type when

- It's just a new provider for existing work. Adding a Gmail provider for `email` is [add-a-new-provider](add-a-new-provider.md), not a new type.
- It's just a new permission on an existing type. Adding `reply` to `social` is a permission-vocabulary change, not a new type.
- It's a sub-category of an existing type. Example: "internal wiki" and "public help center" both fit under `docs` with different instances.

When in doubt, start by adding an instance or a permission to an existing type. Promote to a new type only once you feel the existing vocabulary strain.

## Checklist

- [ ] 1. Design the permission vocabulary
- [ ] 2. Document the capability in `docs/capability-framework.md`
- [ ] 3. Add the capability to at least one provider in `config/providers.yaml`
- [ ] 4. Add a capability section to an agent in `config/capabilities.yaml`
- [ ] 5. (If relevant) add a deep-link generator for approval-required actions
- [ ] 6. Update `docs/agents.md` with the new capability for each affected agent
- [ ] 7. Add tests

---

## 1. Design the permission vocabulary

Permissions are **verbs** scoped to a capability. Pick a small allowlist — 3 to 7 verbs is typical. Each verb should answer "what is the smallest action a user could grant independently?"

Guidelines:

- **Prefer action verbs** (`read`, `send`, `publish`, `book`, `export`).
- **Split read from write** — always. If the provider bundles them, that's a Level-2 enforcement problem (see capability framework), not a reason to bundle the permission.
- **Separate "draft" from "send/publish"** for anything outbound. The approval flow depends on this split.
- **Don't encode ownership in the permission.** `delegate-send` is wrong; ownership is a separate field on the instance.
- **Keep names lowercase-hyphenated.** Match the style of existing permissions (`draft-create`, `issue-create`).

### Worked example: `crm`

| Permission | Description |
|---|---|
| `read` | View contacts, deals, pipelines |
| `create` | Create new contacts or deals |
| `update` | Modify existing records |
| `delete` | Remove records |
| `export` | Export data to CSV / external |

Questions to sanity-check:
- Could an agent have `read` without `create`? → yes (common for analysts). ✅
- Could an agent have `update` without `create`? → yes (enrich-only agent). ✅
- Is `delete` distinct from `update`? → yes (destructive). ✅
- Could `create` and `update` ever be granted independently? → yes (draft-only agent). ✅

If any answer is "no", the permission probably shouldn't exist.

## 2. Document the capability in `docs/capability-framework.md`

Open [docs/capability-framework.md](capability-framework.md) and:

1. Add a row to the **Layer 1: Capability type** table with the new name and a one-line "what it covers".
2. Add a new subsection under **Permission vocabulary** with the full table you designed in step 1.
3. If the capability has approval semantics worth calling out (e.g. `publish` always requires approval on delegate accounts), add a note under **Ownership semantics** or in the capability's permission section.

This doc is authoritative. Adding the capability here is what makes it "real" in the framework — the loader doesn't hard-code the list.

## 3. Add the capability to at least one provider

The capability is useless until a provider implements it. In `config/providers.yaml`, either:

- **Add the capability to an existing provider** that supports it (e.g. `hubspot-mcp` gains `crm`), or
- **Add a new provider** — see [add-a-new-provider.md](add-a-new-provider.md).

For each provider, list the capability under `capabilities:` and add a `permission_scopes` subtree:

```yaml
providers:
  hubspot-mcp:
    command: npx
    args: ["-y", "@hubspot/mcp"]
    capabilities: [crm]
    permission_scopes:
      crm:
        read: "crm.objects.contacts.read"
        create: "crm.objects.contacts.write"
        update: "crm.objects.contacts.write"
        delete: "crm.objects.contacts.write"
        export: "crm.export"
    env_template:
      HUBSPOT_TOKEN: "${HUBSPOT_TOKEN}"
      HUBSPOT_PORTAL: "{account}"
```

The `permission_scopes` map is used for two things: documentation, and (for command-transport providers) computing the minimal set of API scopes to request at MCP start.

## 4. Add a capability section to an agent

In `config/capabilities.yaml`, under the agent using the new capability:

```yaml
agents:
  alex:
    agent: alex
    capabilities:
      crm:
        - instance: pathtohired
          provider: hubspot-mcp
          account: pathtohired
          ownership: shared
          permissions: [read, create, update]
```

Verify the config loads and renders:

```bash
python -m capabilities render <agent>
python -m capabilities mcp_config <agent>
```

The render output should include a new `### crm` section listing the instance with its permissions.

## 5. (If relevant) add a deep-link generator

If the capability has actions that require human approval (draft → open-in-app), add a deep-link generator so the approval card can link to the native app.

Edit `router/approvals/deep_links.py`:

```python
def hubspot_contact(contact_id: str) -> str:
    """Generate a deep link to a HubSpot contact record."""
    return f"https://app.hubspot.com/contacts/{quote(contact_id, safe='')}"


DEEP_LINK_GENERATORS: dict[tuple[str, str], callable] = {
    # ...existing entries...
    ("crm", "hubspot-mcp"): hubspot_contact,
}
```

The key is `(capability_type, provider)`. The approval renderer calls `get_deep_link(capability_type, provider, resource_id)` when a draft needs an "Open in <App>" button.

## 6. Docs pattern

For capabilities backed by a command-transport MCP with non-trivial auth, create a page under `docs/providers/<provider>.md` following the pattern of [docs/providers/m365.md](providers/m365.md):

1. **Overview** — what the provider gives, what it deliberately doesn't.
2. **App registration / OAuth setup** — step-by-step with screenshots or exact UI strings.
3. **Authentication** — device code / OAuth flow, token storage path.
4. **MCP server** — where the code lives, tools exposed, env vars.
5. **Approval flow** — what happens when the agent wants to do an approval-gated action.
6. **Capability config** — YAML snippet showing a typical agent entry.
7. **Provider config** — YAML snippet for `config/providers.yaml`.

For connector-transport capabilities, append a row to the table in [docs/providers/claude-ai-connectors.md](providers/claude-ai-connectors.md) and note the available scopes.

## 7. Update `docs/agents.md`

For each agent that got the new capability, update their section in [docs/agents.md](agents.md) to list the new capability row. Keep that file synced to reality.

## 8. Tests

### Required

- **`tests/unit/capabilities/test_loader.py`** — add a fixture `providers.yaml` with the new capability listed, assert it loads without error, and a fixture `capabilities.yaml` that uses it. Assert invalid permissions (verbs outside the vocabulary) raise `ConfigError`.
- **`tests/unit/capabilities/test_prompt_renderer.py`** — assert the rendered summary groups the new capability correctly and lists all granted permissions.
- **`tests/unit/capabilities/test_mcp_namespacer.py`** — assert the generated `.mcp.json` has an entry named `<capability>_<instance>` and the env vars include only scopes for granted permissions.

### Recommended

- **`tests/unit/approvals/test_deep_links.py`** — if you added a deep-link generator, assert it produces the expected URL shape for known resource IDs, and that `get_deep_link(capability, provider, id)` returns `None` for unregistered pairs.
- **Provider integration test** — if the new capability has a real provider, add `tests/integration/test_<provider>.py` that mocks the API and exercises each permission verb (see `tests/unit/mcps/test_server.py` as a template).

### Test names document the contract

Use test names that read as specs. Examples:
- `test_crm_capability_requires_explicit_permissions`
- `test_crm_delete_not_registered_when_permission_missing`
- `test_render_includes_crm_section_for_alex`

### Run before opening a PR

```bash
.venv/bin/pytest tests/unit/capabilities -v
.venv/bin/pytest tests/unit/approvals -v
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

---

## Permission-vocabulary reference

Use these when adding rows to the capability framework doc. Pattern: each row is one verb.

| Column | Content |
|---|---|
| Permission | Lowercase-hyphenated verb |
| Description | One sentence. States the action, not the API call. |

See the existing tables in [capability-framework.md](capability-framework.md#permission-vocabulary) for examples (`email`, `calendar`, `code-repo`, `social`, `analytics`, `docs`, `marketing`, `web`, `design`, `chat`).
