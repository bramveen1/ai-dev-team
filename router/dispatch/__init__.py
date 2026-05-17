"""Router-side support for the dispatch pack.

Two concerns live here:

* :mod:`router.dispatch.state` — read/write helpers for the sidecar state
  files under ``/var/lib/dispatch/<dispatch_id>/`` that the dispatch
  handler, babysit subprocess, and supervisor coordinate through.
* :mod:`router.dispatch.supervision` — the supervision callable that the
  scheduled-tasks loop invokes every ~120s to read those files, post
  deltas/terminals to Slack, and deregister itself on terminal state
  (#163). Replaces the old in-turn waiting model where the dispatching
  agent's own session blocked on the subprocess.
"""
