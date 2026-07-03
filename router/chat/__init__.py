"""Transport-agnostic chat adapter package.

Feature flag: ``CHAT_BACKENDS`` setting (runtime config with env fallback —
see :mod:`router.settings`). When truthy, the migration layer routes live
Slack traffic through the :class:`~router.chat.interface.ChatAdapter`
abstraction. Defaults to ``False`` — no live routing change until the sibling
migration issue flips this switch. Evaluated at import → restart to change.
"""

from __future__ import annotations

from router import settings as _settings

# No-op by default. The migration issue (sibling of #122) will flip this.
CHAT_BACKENDS: bool = bool(_settings.get("CHAT_BACKENDS"))
