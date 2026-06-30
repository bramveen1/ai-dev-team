"""CLI entry point for the path_to_hired pack.

The agent invokes this script from Bash:

    python /config/packs/path_to_hired/handler.py <verb> [--<arg> <value> ...] [--approved]

Read-only verbs execute immediately.  Single-target write verbs also execute
immediately (low blast-radius).  Approval-gated verbs return an
``approval_required`` payload — the agent turns that into a ``draft-approval``
block for a human to review.  Once approved, the agent re-invokes the handler
with ``--approved`` and the same arguments.

All output is JSON on stdout; errors are structured (never raw tracebacks).

Verbs
-----
Read-only:
  list_users, list_pending_users, get_user, list_blog_posts,
  get_blog_post, preview_blog_post

Single-target writes (ungated):
  approve_user, unlock_user, create_blog_post_draft,
  update_blog_post_draft

Approval-gated (need --approved to execute):
  publish_blog_post, unpublish_blog_post, delete_blog_post,
  update_user_role, reject_user, disable_user, reset_user_password,
  delete_user, create_user
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import uuid
from pathlib import Path
from typing import Any

# Add the pack directory to sys.path so ``helpers`` resolves when the script
# is invoked directly (e.g. ``python /config/packs/path_to_hired/handler.py``).
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from helpers.auth import AuthError, RateLimitError  # noqa: E402
from helpers.client import PathToHiredClient  # noqa: E402
from helpers.secrets import CredentialError, load_credentials  # noqa: E402

logger = logging.getLogger(__name__)

APPROVAL_GATED: frozenset[str] = frozenset(
    {
        "publish_blog_post",
        "unpublish_blog_post",
        "delete_blog_post",
        "update_user_role",
        "reject_user",
        "disable_user",
        "reset_user_password",
        "delete_user",
        "create_user",
    }
)

ALL_VERBS: frozenset[str] = APPROVAL_GATED | frozenset(
    {
        "list_users",
        "list_pending_users",
        "get_user",
        "list_blog_posts",
        "get_blog_post",
        "preview_blog_post",
        "approve_user",
        "unlock_user",
        "create_blog_post_draft",
        "update_blog_post_draft",
    }
)


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Path to Hired pack handler")
    p.add_argument("verb", choices=sorted(ALL_VERBS), help="Action to perform")
    p.add_argument("--approved", action="store_true", default=False, help="Confirm execution of a gated action")
    p.add_argument("--user_id", "--user-id", dest="user_id", help="User ID")
    p.add_argument("--post_id", "--post-id", dest="post_id", help="Blog post ID")
    p.add_argument("--email", help="User email")
    p.add_argument("--username", help="Username")
    p.add_argument("--role", help="Role name")
    p.add_argument("--title", help="Blog post title")
    p.add_argument("--content", help="Blog post body (markdown)")
    p.add_argument("--excerpt", help="Blog post excerpt")
    p.add_argument("--tags", help="Comma-separated blog post tags")
    p.add_argument("--reason", help="Reason (for reject/disable actions)")
    p.add_argument("--name", help="Display name for new user")
    return p


# ---------------------------------------------------------------------------
# Approval helpers
# ---------------------------------------------------------------------------


def _approval_required(verb: str, payload: dict[str, Any]) -> dict[str, Any]:
    draft_id = f"{verb}-{uuid.uuid4().hex[:8]}"
    return {
        "status": "approval_required",
        "draft_id": draft_id,
        "pack": "path_to_hired",
        "action_verb": verb,
        "payload": payload,
    }


def _build_gated_payload(verb: str, args: argparse.Namespace) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    if args.user_id:
        payload["user_id"] = args.user_id
    if args.post_id:
        payload["post_id"] = args.post_id
    if args.email:
        payload["email"] = args.email
    if args.username:
        payload["username"] = args.username
    if args.role:
        payload["role"] = args.role
    if args.reason:
        payload["reason"] = args.reason
    if args.name:
        payload["name"] = args.name
    if args.title:
        payload["title"] = args.title
    if args.content:
        payload["content"] = args.content
    return payload


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


def _ok(data: Any) -> dict[str, Any]:
    return {"status": "ok", "data": data}


def _error(message: str, http_status: int | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "error", "message": message}
    if http_status is not None:
        result["http_status"] = http_status
    return result


def _check_response(resp: Any, context: str) -> dict[str, Any] | None:
    """Return an error dict if resp is not 2xx, else None."""
    if resp.status_code < 200 or resp.status_code >= 300:
        try:
            body = resp.json()
        except Exception:
            body = resp.text
        return _error(f"{context} failed: {body}", http_status=resp.status_code)
    return None


# ---------------------------------------------------------------------------
# Action implementations
# ---------------------------------------------------------------------------


async def _list_users(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    resp = await client.get("/api/admin/users")
    if err := _check_response(resp, "list_users"):
        return err
    return _ok(resp.json())


async def _list_pending_users(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    resp = await client.get("/api/admin/users", params={"status": "pending"})
    if err := _check_response(resp, "list_pending_users"):
        return err
    return _ok(resp.json())


async def _get_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id and not args.email:
        return _error("get_user requires --user_id or --email")
    resp = await client.get("/api/admin/users")
    if err := _check_response(resp, "get_user"):
        return err
    data = resp.json()
    users = data if isinstance(data, list) else data.get("users", data.get("data", []))
    for user in users:
        if args.user_id and str(user.get("id", "")) == str(args.user_id):
            return _ok(user)
        if args.email and (user.get("email") or "").lower() == args.email.lower():
            return _ok(user)
    return _error(f"user not found (user_id={args.user_id!r}, email={args.email!r})")


async def _list_blog_posts(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    resp = await client.get("/api/admin/blog/posts")
    if err := _check_response(resp, "list_blog_posts"):
        return err
    return _ok(resp.json())


async def _get_blog_post(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("get_blog_post requires --post_id")
    resp = await client.get(f"/api/admin/blog/posts/{args.post_id}")
    if err := _check_response(resp, "get_blog_post"):
        return err
    return _ok(resp.json())


async def _preview_blog_post(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("preview_blog_post requires --post_id")
    resp = await client.get(f"/api/admin/blog/posts/{args.post_id}/preview")
    if err := _check_response(resp, "preview_blog_post"):
        return err
    return _ok(resp.json())


async def _approve_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("approve_user requires --user_id")
    resp = await client.post(f"/api/admin/users/{args.user_id}/approve")
    if err := _check_response(resp, "approve_user"):
        return err
    return _ok(resp.json())


async def _unlock_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("unlock_user requires --user_id")
    resp = await client.post(f"/api/admin/users/{args.user_id}/unlock")
    if err := _check_response(resp, "unlock_user"):
        return err
    return _ok(resp.json())


async def _create_blog_post_draft(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.title:
        return _error("create_blog_post_draft requires --title")
    body: dict[str, Any] = {"title": args.title, "published": False}
    if args.content:
        body["content"] = args.content
    if args.excerpt:
        body["excerpt"] = args.excerpt
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    resp = await client.post("/api/admin/blog/posts", json=body)
    if err := _check_response(resp, "create_blog_post_draft"):
        return err
    return _ok(resp.json())


async def _update_blog_post_draft(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("update_blog_post_draft requires --post_id")
    check = await client.get(f"/api/admin/blog/posts/{args.post_id}")
    if err := _check_response(check, "update_blog_post_draft (fetch)"):
        return err
    post = check.json()
    post_data = post if isinstance(post, dict) else post.get("post", post.get("data", {}))
    if post_data.get("published"):
        return _error("Cannot update a published post; unpublish it first.")
    body: dict[str, Any] = {}
    if args.title:
        body["title"] = args.title
    if args.content:
        body["content"] = args.content
    if args.excerpt:
        body["excerpt"] = args.excerpt
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if not body:
        return _error("update_blog_post_draft: no fields to update")
    resp = await client.put(f"/api/admin/blog/posts/{args.post_id}", json=body)
    if err := _check_response(resp, "update_blog_post_draft"):
        return err
    return _ok(resp.json())


async def _publish_blog_post(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("publish_blog_post requires --post_id")
    resp = await client.post(f"/api/admin/blog/posts/{args.post_id}/publish")
    if err := _check_response(resp, "publish_blog_post"):
        return err
    return _ok(resp.json())


async def _unpublish_blog_post(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("unpublish_blog_post requires --post_id")
    resp = await client.post(f"/api/admin/blog/posts/{args.post_id}/unpublish")
    if err := _check_response(resp, "unpublish_blog_post"):
        return err
    return _ok(resp.json())


async def _delete_blog_post(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.post_id:
        return _error("delete_blog_post requires --post_id")
    resp = await client.delete(f"/api/admin/blog/posts/{args.post_id}")
    if err := _check_response(resp, "delete_blog_post"):
        return err
    return _ok({"deleted": True, "post_id": args.post_id})


async def _update_user_role(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("update_user_role requires --user_id")
    if not args.role:
        return _error("update_user_role requires --role")
    resp = await client.put(f"/api/admin/users/{args.user_id}/role", json={"role": args.role})
    if err := _check_response(resp, "update_user_role"):
        return err
    return _ok(resp.json())


async def _reject_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("reject_user requires --user_id")
    body: dict[str, Any] = {}
    if args.reason:
        body["reason"] = args.reason
    resp = await client.post(f"/api/admin/users/{args.user_id}/reject", json=body)
    if err := _check_response(resp, "reject_user"):
        return err
    return _ok(resp.json())


async def _disable_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("disable_user requires --user_id")
    body: dict[str, Any] = {}
    if args.reason:
        body["reason"] = args.reason
    resp = await client.post(f"/api/admin/users/{args.user_id}/disable", json=body)
    if err := _check_response(resp, "disable_user"):
        return err
    return _ok(resp.json())


async def _reset_user_password(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("reset_user_password requires --user_id")
    resp = await client.post(f"/api/admin/users/{args.user_id}/reset-password")
    if err := _check_response(resp, "reset_user_password"):
        return err
    return _ok(resp.json())


async def _delete_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.user_id:
        return _error("delete_user requires --user_id")
    resp = await client.delete(f"/api/admin/users/{args.user_id}")
    if err := _check_response(resp, "delete_user"):
        return err
    return _ok({"deleted": True, "user_id": args.user_id})


async def _create_user(args: argparse.Namespace, client: PathToHiredClient) -> dict[str, Any]:
    if not args.email:
        return _error("create_user requires --email")
    body: dict[str, Any] = {"email": args.email}
    if args.username:
        body["username"] = args.username
    if args.role:
        body["role"] = args.role
    if args.name:
        body["name"] = args.name
    resp = await client.post("/api/admin/users", json=body)
    if err := _check_response(resp, "create_user"):
        return err
    return _ok(resp.json())


_VERB_HANDLERS = {
    "list_users": _list_users,
    "list_pending_users": _list_pending_users,
    "get_user": _get_user,
    "list_blog_posts": _list_blog_posts,
    "get_blog_post": _get_blog_post,
    "preview_blog_post": _preview_blog_post,
    "approve_user": _approve_user,
    "unlock_user": _unlock_user,
    "create_blog_post_draft": _create_blog_post_draft,
    "update_blog_post_draft": _update_blog_post_draft,
    "publish_blog_post": _publish_blog_post,
    "unpublish_blog_post": _unpublish_blog_post,
    "delete_blog_post": _delete_blog_post,
    "update_user_role": _update_user_role,
    "reject_user": _reject_user,
    "disable_user": _disable_user,
    "reset_user_password": _reset_user_password,
    "delete_user": _delete_user,
    "create_user": _create_user,
}


# ---------------------------------------------------------------------------
# Public dispatch entry point (used directly by tests)
# ---------------------------------------------------------------------------


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    """Dispatch a parsed verb to its handler.

    For approval-gated verbs without ``--approved``, returns an
    ``approval_required`` payload without touching the API.  Once the agent
    receives approval and re-invokes with ``--approved``, the action runs.
    """
    verb = args.verb

    if verb in APPROVAL_GATED and not args.approved:
        return _approval_required(verb, _build_gated_payload(verb, args))

    try:
        creds = load_credentials()
    except CredentialError as exc:
        return _error(str(exc))

    try:
        async with PathToHiredClient(creds) as client:
            return await _VERB_HANDLERS[verb](args, client)
    except (AuthError, RateLimitError) as exc:
        return _error(str(exc))
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error in %s", verb)
        return _error(f"unexpected error: {exc}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    result = asyncio.run(dispatch(args))
    print(json.dumps(result))


if __name__ == "__main__":
    main()
