"""Entry point for running the M365 Mail MCP server.

Usage:
    python -m mcps.m365_mail

Environment variables:
    M365_ACCESS_TOKEN: OAuth2 access token (required)
    M365_ACCOUNT: Mailbox owner UPN for delegate access (optional)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys

from mcps.m365_mail.graph_client import GraphMailClient, GraphMailError
from mcps.m365_mail.server import get_tool_definitions, handle_tool_call

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s", stream=sys.stderr)
logger = logging.getLogger(__name__)


async def _process_request(client: GraphMailClient, request: dict) -> dict:
    """Process a single JSON-RPC style MCP request."""
    method = request.get("method", "")
    req_id = request.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "m365-mail", "version": "0.1.0"},
            },
        }

    elif method == "notifications/initialized":
        return None  # No response needed for notifications

    elif method == "tools/list":
        tools = get_tool_definitions()
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "result": {"tools": tools},
        }

    elif method == "tools/call":
        params = request.get("params", {})
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        try:
            result = await handle_tool_call(client, tool_name, arguments)
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                },
            }
        except GraphMailError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }
        except ValueError as e:
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": {
                    "content": [{"type": "text", "text": f"Error: {e}"}],
                    "isError": True,
                },
            }

    else:
        return {
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": -32601, "message": f"Method not found: {method}"},
        }


async def main() -> None:
    """Run the MCP server on stdin/stdout."""
    access_token = os.environ.get("M365_ACCESS_TOKEN", "")
    account = os.environ.get("M365_ACCOUNT")

    if not access_token:
        logger.error("M365_ACCESS_TOKEN environment variable is required")
        sys.exit(1)

    client = GraphMailClient(access_token=access_token, user_id=account)
    logger.info("M365 Mail MCP server started (account=%s)", account or "/me")

    try:
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue

            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON input: %s", line[:100])
                continue

            response = await _process_request(client, request)
            if response is not None:
                sys.stdout.write(json.dumps(response) + "\n")
                sys.stdout.flush()
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
