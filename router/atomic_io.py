"""Atomic file writes — the shared temp-file-then-rename idiom.

Consolidates the ~9 hand-rolled ``write .tmp then os.replace`` copies across
the router (refactoring roadmap §8.1). The contract:

- Readers never observe a partial file (same-directory temp + ``os.replace``).
- On failure the temp file is removed and the previous contents stay intact.
- File permissions are deliberate: an explicit ``mode`` wins; otherwise an
  existing target's mode is preserved; a fresh file gets 0o644 (what the
  legacy ``Path.write_text`` sites produced under the default umask). This
  matters because ``config/`` files are mounted into agent containers and
  must stay readable there.

This module must stay a leaf: stdlib imports only.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

_DEFAULT_NEW_FILE_MODE = 0o644


def atomic_write_text(
    path: str | Path,
    text: str,
    *,
    mode: int | None = None,
    mkdir: bool = True,
    encoding: str = "utf-8",
) -> None:
    """Atomically write *text* to *path*.

    Args:
        path: Destination file.
        text: Full new contents.
        mode: chmod applied before the file lands (e.g. ``0o600`` for
            secrets). ``None`` preserves the existing target's mode, or
            0o644 for a fresh file.
        mkdir: Create parent directories (default True).
        encoding: Text encoding (default utf-8).
    """
    dest = Path(path)
    if mkdir:
        dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".tmp")
    try:
        if mode is not None:
            os.chmod(tmp, mode)
        else:
            try:
                os.chmod(tmp, dest.stat().st_mode & 0o7777)
            except FileNotFoundError:
                os.chmod(tmp, _DEFAULT_NEW_FILE_MODE)
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
        os.replace(tmp, dest)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(
    path: str | Path,
    obj: Any,
    *,
    indent: int | None = None,
    sort_keys: bool = False,
    mode: int | None = None,
    mkdir: bool = True,
) -> None:
    """Atomically serialize *obj* as JSON to *path* (see :func:`atomic_write_text`)."""
    atomic_write_text(
        path,
        json.dumps(obj, indent=indent, sort_keys=sort_keys),
        mode=mode,
        mkdir=mkdir,
    )
