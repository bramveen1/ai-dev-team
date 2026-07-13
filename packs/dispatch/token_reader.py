"""Shared secrets-file token reader (#718).

Canonical implementation for every "read a GitHub PAT from a file" call
site in the project: ``handler.py`` (dispatch identity), ``pr_review.py``
(reviewer identity), and ``router/github_api.py`` (merge identity, via
``read_pat``). Three independent copies of this parsing used to drift —
notably ``read_pat`` did not skip comment/blank lines, so a placeholder
token file with a ``#``-comment header would hand GitHub the comment text
as a bearer token.

Lives in the pack dir rather than under ``router/`` because the pack dir
is the one location mounted into *both* the worker containers
(``/config/packs`` in ``docker-compose.yml`` — no ``router`` package
present there) and the router container (``/app/packs``).
``router/github_api.py`` reaches this module via the same
sys.path-injection pattern already used for ``quota.py`` (see
``router/dispatch/supervision.py``).
"""

from __future__ import annotations

from pathlib import Path


class TokenError(Exception):
    """The token file is missing, unreadable, or empty."""


def read_token_file(path: str | Path, *, skip_comments: bool = True, raise_on_empty: bool = False) -> str | None:
    """Read a bearer token (GitHub PAT or similar) from *path*.

    ``skip_comments``: when True (the default), blank lines and
    ``#``-prefixed lines are ignored and the first remaining non-blank
    line is returned as the token. When False, the whole file is read and
    ``.strip()``-ed as one token, comments and all.

    ``raise_on_empty``: when True, a missing/unreadable/empty (or, with
    ``skip_comments``, comment-only) file raises :class:`TokenError`
    instead of returning ``None``.
    """
    resolved = Path(path)
    try:
        content = resolved.read_text()
    except FileNotFoundError:
        if raise_on_empty:
            raise TokenError(f"PAT file not found: {resolved}") from None
        return None
    except OSError as exc:
        if raise_on_empty:
            raise TokenError(f"Cannot read PAT file {resolved}: {exc}") from exc
        return None

    if skip_comments:
        token = ""
        for line in content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                token = stripped
                break
    else:
        token = content.strip()

    if not token:
        if raise_on_empty:
            raise TokenError(f"PAT file is empty: {resolved}")
        return None
    return token
