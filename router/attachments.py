"""Slack file-attachment ingestion for named agents (#328).

Downloads files attached to Slack messages (via ``url_private``), validates
them against the mimetype allowlist and size caps, writes them to the
per-thread scratch directory, and builds the ``[ATTACHMENTS]`` prompt block.

Feature flag: ``ATTACHMENTS_ENABLED`` env var — default off.
Workers-bot: this module is intentionally *not* called for workers-bot
(workers stay post-only; ``files:read`` scope is NOT added to workers-bot).
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Sizing caps ───────────────────────────────────────────────────────────────

MAX_FILE_BYTES: int = 25 * 1024 * 1024  # 25 MB per file
MAX_FILES_PER_MSG: int = 5  # files per Slack message
MAX_THREAD_BYTES: int = 100 * 1024 * 1024  # 100 MB cumulative per thread

# ── Filename sanitisation ─────────────────────────────────────────────────────

MAX_FILENAME_LEN: int = 80
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_PATH_SEPARATORS: frozenset[str] = frozenset("/\\")

# ── Mimetype allowlist ────────────────────────────────────────────────────────

_ALLOWED_PREFIXES: tuple[str, ...] = ("image/", "text/")

# Exact mimetypes beyond the prefix-matched set.
_ALLOWED_EXACT: frozenset[str] = frozenset(
    {
        "application/pdf",
        "application/json",
        # Common code / data types served as application/*
        "application/javascript",
        "application/x-javascript",
        "application/typescript",
        "application/x-sh",
        "application/x-shellscript",
        "application/x-yaml",
        "application/toml",
        "application/xml",
        # Office formats (.docx, .xlsx, .pptx)
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/msword",
    }
)

_ATTACHMENTS_ROOT: str = "/var/lib/attachments"


# ── Feature flag ──────────────────────────────────────────────────────────────


def attachments_enabled() -> bool:
    """Return True when ``ATTACHMENTS_ENABLED`` env var is set to a truthy value."""
    return os.environ.get("ATTACHMENTS_ENABLED", "").lower() in ("1", "true", "yes")


# ── Mimetype validation ───────────────────────────────────────────────────────


def is_allowed_mimetype(mimetype: str) -> bool:
    """Return True iff *mimetype* passes the ingest allowlist.

    Strips charset and parameter suffixes (e.g. ``text/plain; charset=utf-8``
    → ``text/plain``) before comparing.
    """
    if not mimetype:
        return False
    mt = mimetype.lower().split(";")[0].strip()
    for prefix in _ALLOWED_PREFIXES:
        if mt.startswith(prefix):
            return True
    return mt in _ALLOWED_EXACT


# ── Filename sanitisation ─────────────────────────────────────────────────────


def sanitise_filename(
    name: str,
    *,
    file_id: str = "",
    existing: set[str] | None = None,
) -> str:
    """Return a filesystem-safe filename derived from *name*.

    Processing steps:
    1. Strip control characters (U+0000–U+001F, U+007F).
    2. NFKC-normalise to prevent unicode look-alike attacks.
    3. Replace path separators (``/`` and ``\\``) with underscores.
    4. Strip leading/trailing dots and whitespace.
    5. Truncate to ``MAX_FILENAME_LEN`` characters.
    6. If the result collides with an entry in *existing*, prepend
       ``<file_id>_`` (or ``dup_`` when *file_id* is empty) and re-truncate.
    """
    if not name:
        name = "unnamed"

    sanitised = _CONTROL_CHAR_RE.sub("", name)
    sanitised = unicodedata.normalize("NFKC", sanitised)
    sanitised = "".join("_" if c in _PATH_SEPARATORS else c for c in sanitised)
    sanitised = sanitised.strip(". ")

    if not sanitised:
        sanitised = "unnamed"

    sanitised = sanitised[:MAX_FILENAME_LEN]
    if not sanitised:
        sanitised = "unnamed"

    if existing is not None and sanitised in existing:
        prefix = f"{file_id}_" if file_id else "dup_"
        sanitised = (prefix + sanitised)[:MAX_FILENAME_LEN]

    return sanitised


# ── Cap enforcement ───────────────────────────────────────────────────────────


def _thread_used_bytes(thread_dir: Path) -> int:
    """Return the total bytes of all regular files under *thread_dir*."""
    if not thread_dir.is_dir():
        return 0
    total = 0
    try:
        for entry in thread_dir.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return total


def validate_files(
    files: list[dict],
    thread_ts: str,
    *,
    attachments_root: str = _ATTACHMENTS_ROOT,
) -> tuple[list[dict], str | None]:
    """Validate *files* against the allowlist and size caps.

    Returns ``(valid_files, rejection_reason)``.

    On the *first* violation, returns ``([], reason)`` — the whole message is
    rejected, per the spec. External-mode files (no ``url_private``) are
    excluded from cap accounting and mimetype checks; they are silently skipped
    during ingest.

    When all checks pass, returns ``(files, None)``.
    """
    if not files:
        return [], None

    # Per-message count cap
    if len(files) > MAX_FILES_PER_MSG:
        return [], (
            f"Too many files: {len(files)} attached but the limit is "
            f"{MAX_FILES_PER_MSG} per message. Please re-upload with fewer attachments."
        )

    thread_dir = Path(attachments_root) / thread_ts
    existing_bytes = _thread_used_bytes(thread_dir)
    total_new_bytes = 0

    for f in files:
        # External-mode (no url_private): skip caps/mimetype checks entirely.
        if not f.get("url_private"):
            continue

        name = f.get("name") or f.get("title") or f.get("id") or "unknown"
        mimetype = f.get("mimetype", "")
        size = int(f.get("size") or 0)

        # Mimetype check
        if not is_allowed_mimetype(mimetype):
            return [], (
                f"File `{name}` has unsupported type `{mimetype or 'unknown'}`. "
                "Allowed: images, PDF, plain text, code files, and Office documents."
            )

        # Per-file size cap
        if size > MAX_FILE_BYTES:
            mb = size / (1024 * 1024)
            cap_mb = MAX_FILE_BYTES / (1024 * 1024)
            return [], (
                f"File `{name}` is {mb:.1f} MB, which exceeds the "
                f"{cap_mb:.0f} MB per-file limit. Please compress or split the file."
            )

        total_new_bytes += size

    # Per-thread cumulative cap
    if existing_bytes + total_new_bytes > MAX_THREAD_BYTES:
        total_mb = (existing_bytes + total_new_bytes) / (1024 * 1024)
        cap_mb = MAX_THREAD_BYTES / (1024 * 1024)
        return [], (
            f"Adding these files would bring this thread's total to {total_mb:.0f} MB, "
            f"exceeding the {cap_mb:.0f} MB per-thread limit. "
            "Please start a fresh thread for new attachments."
        )

    return files, None


# ── File download ─────────────────────────────────────────────────────────────


async def _download_url(url: str, token: str, dest: Path) -> None:
    """Download *url* authenticated with *token* and write to *dest*.

    Raises ``RuntimeError`` on HTTP errors, re-raises network exceptions.
    """
    import aiohttp  # local import; aiohttp is in the router's requirements

    headers = {"Authorization": f"Bearer {token}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            if resp.status != 200:
                raise RuntimeError(f"HTTP {resp.status} downloading {url}")
            content = await resp.read()

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(content)


# ── Main ingest entry point ───────────────────────────────────────────────────


async def ingest_files(
    files: list[dict],
    thread_ts: str,
    bot_token: str,
    *,
    attachments_root: str = _ATTACHMENTS_ROOT,
) -> list[str]:
    """Download and store all ingestable files. Return list of absolute paths.

    ``files`` should have already passed :func:`validate_files`. External-mode
    entries (no ``url_private``) are silently skipped here; download errors are
    logged but do not abort the rest of the batch.

    Bumps the thread directory mtime on completion so the GC TTL resets.
    """
    thread_dir = Path(attachments_root) / thread_ts
    thread_dir.mkdir(parents=True, exist_ok=True)

    existing_names: set[str] = {p.name for p in thread_dir.iterdir() if p.is_file()}
    paths: list[str] = []

    for f in files:
        url = f.get("url_private")
        if not url:
            logger.info(
                "attachments: external-mode file skipped file_id=%s name=%s",
                f.get("id", ""),
                f.get("name", ""),
            )
            continue

        file_id = f.get("id", "")
        raw_name = f.get("name") or f.get("title") or file_id or "unnamed"
        dest_name = sanitise_filename(raw_name, file_id=file_id, existing=existing_names)
        existing_names.add(dest_name)

        dest_path = thread_dir / dest_name
        try:
            await _download_url(url, bot_token, dest_path)
            paths.append(str(dest_path.resolve()))
            logger.info(
                "attachments: ingested file_id=%s dest=%s",
                file_id,
                dest_path,
            )
        except Exception:
            logger.exception(
                "attachments: download failed file_id=%s url=%s dest=%s",
                file_id,
                url,
                dest_path,
            )

    # Bump mtime so the GC TTL resets for this thread.
    try:
        os.utime(thread_dir, None)
    except OSError:
        logger.debug("attachments: failed to bump mtime for %s", thread_dir)

    return paths


# ── Prompt injection ──────────────────────────────────────────────────────────


def build_attachments_block(paths: list[str]) -> str:
    """Return the ``[ATTACHMENTS]`` prompt block listing *paths* (one per line).

    Returns an empty string when *paths* is empty.
    """
    if not paths:
        return ""
    lines = "\n".join(paths)
    return f"[ATTACHMENTS]\n{lines}\n\n"


# ── Startup channel-membership helpers ────────────────────────────────────────


async def log_channel_membership_warnings(client, agent_name: str) -> None:
    """Log a WARNING for each channel where the bot lacks membership.

    Calls ``users.conversations`` to enumerate channels the bot has joined.
    Channels where the bot is absent cannot serve file downloads — this warning
    surfaces that gap at startup so the operator knows before a user reports it.

    Safe-degrades: any Slack API error is logged at DEBUG and does not prevent
    startup.
    """
    try:
        joined: set[str] = set()
        cursor = None
        while True:
            kwargs: dict = {"types": "public_channel,private_channel,mpim", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = await client.users_conversations(**kwargs)
            for ch in resp.get("channels") or []:
                ch_id = ch.get("id")
                if ch_id:
                    joined.add(ch_id)

            next_cursor = (resp.get("response_metadata") or {}).get("next_cursor")
            if not next_cursor:
                break
            cursor = next_cursor

        logger.info(
            "attachments: agent=%s is a member of %d channel(s); file downloads are limited to these channels",
            agent_name,
            len(joined),
        )
        if not joined:
            logger.warning(
                "attachments: agent=%s is not a member of any channels — "
                "file downloads via url_private will fail until the bot is invited",
                agent_name,
            )
    except Exception:
        logger.debug(
            "attachments: could not check channel membership for agent=%s",
            agent_name,
            exc_info=True,
        )
