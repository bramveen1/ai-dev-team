"""Capability framework — config loading, MCP namespacing, and prompt rendering."""

from src.capabilities.loader import get_agent_capabilities, load_config
from src.capabilities.mcp_namespacer import generate_mcp_config
from src.capabilities.prompt_renderer import render_capability_summary

__all__ = [
    "load_config",
    "get_agent_capabilities",
    "generate_mcp_config",
    "render_capability_summary",
]
