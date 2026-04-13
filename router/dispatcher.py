"""Router dispatcher — routes messages to the correct agent container.

Currently implements a placeholder echo dispatch. This will be replaced
with Docker exec / subprocess / HTTP calls to agent containers.
"""

import logging

from router.config import get_agent_map

logger = logging.getLogger(__name__)


async def dispatch(
    agent_name: str,
    message: str,
    channel: str,
    thread_ts: str,
    client,
    timeout: int | None = None,
) -> dict:
    """Dispatch a message to the named agent and return the response.

    Args:
        agent_name: Logical name of the target agent (e.g. "lisa").
        message: The user's message text.
        channel: Slack channel ID.
        thread_ts: Slack thread timestamp for threading replies.
        client: Slack WebClient instance (used by future implementations).
        timeout: Optional timeout in seconds for the dispatch call.

    Returns:
        A dict with keys:
            - agent: The agent name that handled the request.
            - status: "ok" on success.
            - response: The agent's response text.

    Raises:
        ValueError: If agent_name is not in the agent map or message is empty.
    """
    if not message or not message.strip():
        raise ValueError("Message must not be empty")

    agent_map = get_agent_map()
    if agent_name not in agent_map:
        raise ValueError(f"Unknown agent: {agent_name}")

    agent_config = agent_map[agent_name]
    logger.info(
        "Dispatching to agent=%s container=%s channel=%s thread_ts=%s timeout=%s",
        agent_name,
        agent_config["container"],
        channel,
        thread_ts,
        timeout,
    )

    # TODO: Replace this placeholder with actual agent container invocation.
    # Options under consideration:
    #   - docker exec into the agent container
    #   - HTTP call to an agent HTTP server
    #   - subprocess call to Claude Code CLI
    response_text = f"[{agent_config['name']}] Echo: {message}"

    logger.debug("Agent %s responded: %s", agent_name, response_text[:100])

    return {
        "agent": agent_name,
        "status": "ok",
        "response": response_text,
    }
