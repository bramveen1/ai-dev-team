"""Pydantic models for the capability configuration schema."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

# Permission vocabulary per capability type — allowlisted verbs.
PERMISSION_VOCABULARY: dict[str, set[str]] = {
    "email": {"read", "send", "draft-create", "draft-update", "draft-delete", "archive", "label"},
    "calendar": {"read", "propose", "book", "update", "update-tentative", "cancel", "delete-tentative"},
    "code-repo": {"read", "comment", "issue-create", "pr-create", "merge", "branch-create"},
    "social": {"read", "draft", "publish", "reply"},
    "analytics": {"read", "annotate", "configure"},
    "docs": {"read", "write", "comment", "organize"},
    "marketing": {"read", "draft", "send", "configure"},
    "web": {"browse", "screenshot", "interact"},
    "design": {"read", "comment", "export"},
    "chat": {"read", "send", "react"},
    "memory": {"read", "write"},
    "slack_io": {"read", "send", "react"},
    "scheduled-tasks": {"create", "read", "update", "delete"},
    "scheduled_tasks": {"create", "read", "update", "delete"},
}

VALID_OWNERSHIP_VALUES = {"self", "delegate", "shared"}


class CapabilityInstance(BaseModel):
    """A single configured capability instance — one account, one set of permissions."""

    instance: str = Field(..., description="Unique name within this agent+capability (e.g. 'mine', 'bram')")
    provider: str = Field(..., description="MCP server identifier from the provider registry")
    account: str = Field(..., description="Account/resource identifier (email, org name, etc.)")
    ownership: str = Field(..., description="Relationship to the account: self | delegate | shared")
    permissions: list[str] = Field(..., description="Allowed actions from the permission vocabulary")
    config: dict[str, Any] = Field(default_factory=dict, description="Provider-specific configuration overrides")

    @model_validator(mode="after")
    def validate_ownership(self) -> CapabilityInstance:
        if self.ownership not in VALID_OWNERSHIP_VALUES:
            raise ValueError(f"Invalid ownership '{self.ownership}'. Must be one of: {sorted(VALID_OWNERSHIP_VALUES)}")
        return self


class AgentCapabilities(BaseModel):
    """All capabilities for a single agent, grouped by capability type."""

    agent: str = Field(..., description="Agent name")
    capabilities: dict[str, list[CapabilityInstance]] = Field(
        default_factory=dict, description="Mapping of capability_type -> list of instances"
    )


class ProviderConfig(BaseModel):
    """Configuration for a single MCP provider."""

    command: str = Field(..., description="Command to run the MCP server")
    args: list[str] = Field(default_factory=list, description="Command arguments")
    capabilities: list[str] = Field(..., description="Capability types this provider supports")
    permission_scopes: dict[str, dict[str, str]] = Field(
        default_factory=dict, description="Mapping of capability_type -> permission -> scope"
    )
    env_template: dict[str, str] = Field(
        default_factory=dict, description="Environment variable template for the MCP server"
    )
    secrets_map: dict[str, str] = Field(
        default_factory=dict,
        description="Mapping of env var name -> 'provider:key' in the secrets store",
    )
    oauth: dict[str, str] = Field(
        default_factory=dict,
        description="OAuth endpoint configuration (authority, token_path, devicecode_path)",
    )


class ProvidersConfig(BaseModel):
    """Top-level provider registry."""

    providers: dict[str, ProviderConfig] = Field(
        default_factory=dict, description="Mapping of provider_name -> ProviderConfig"
    )
