"""Strong-reference bookkeeping for fire-and-forget asyncio tasks.

asyncio only keeps weak references to tasks, so a ``create_task(...)`` whose
return value is discarded can be garbage collected mid-flight — silently
killing scheduler loops and other "fire and forget" workers. Anything that
must outlive the call stack that started it is parked in ``background_tasks``
until it completes.

Previously duplicated verbatim in ``router/app.py`` and
``router/dispatcher.py``; both re-export these under their old private names.
"""

from __future__ import annotations

import asyncio
from typing import Any

background_tasks: set[asyncio.Task] = set()


def spawn_background_task(coro: Any, *, name: str | None = None) -> asyncio.Task:
    """Schedule *coro* and keep a strong reference until it completes."""
    task = asyncio.create_task(coro, name=name)
    background_tasks.add(task)
    task.add_done_callback(background_tasks.discard)
    return task
