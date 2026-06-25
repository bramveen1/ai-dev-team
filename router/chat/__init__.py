"""Transport-agnostic chat adapter package.

Feature flag: ``CHAT_BACKENDS`` (env var). When truthy, the migration layer
routes live Slack traffic through the :class:`~router.chat.interface.ChatAdapter`
abstraction. Defaults to ``False`` — no live routing change until the sibling
migration issue flips this switch.
"""

from __future__ import annotations

import os

# No-op by default. The migration issue (sibling of #122) will flip this.
CHAT_BACKENDS: bool = os.environ.get("CHAT_BACKENDS", "").lower() in ("1", "true", "yes")
