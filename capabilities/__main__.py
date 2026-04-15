"""CLI entry points for the capability framework.

Usage:
    python -m capabilities render <agent_name>     — render system prompt summary
    python -m capabilities mcp_config <agent_name> — output .mcp.json for agent
"""

from __future__ import annotations

import json
import sys

from capabilities.loader import ConfigError
from capabilities.mcp_namespacer import generate_mcp_config
from capabilities.prompt_renderer import render_capability_summary


def main() -> None:
    if len(sys.argv) < 3:
        print("Usage: python -m capabilities <command> <agent_name>", file=sys.stderr)
        print("Commands: render, mcp_config", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]
    agent_name = sys.argv[2]

    try:
        if command == "render":
            print(render_capability_summary(agent_name))
        elif command == "mcp_config":
            config = generate_mcp_config(agent_name)
            print(json.dumps(config, indent=2))
        else:
            print(f"Unknown command: {command}", file=sys.stderr)
            print("Commands: render, mcp_config", file=sys.stderr)
            sys.exit(1)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
