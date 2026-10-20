"""github — read PR review state via the ``gh`` CLI (debug loop only).

Ports ``require_gh`` and ``pr_review_state`` from ``debug.sh``. The only loop that reads GitHub
review state directly. Returns parsed JSON (or None on error); the pure classification of that
JSON into an action lives in :func:`agentloop.loops.debug.decide`.
"""

from __future__ import annotations

import json
import shutil

from agentloop import journal
from agentloop.proc import capture

_PR_STATE_QUERY = """query($owner:String!,$repo:String!,$num:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      state
      isDraft
      mergeable
      mergeStateStatus
      reviewDecision
      headRefName
      body
      comments(last:100){nodes{body createdAt author{login}} pageInfo{hasPreviousPage startCursor}}
      reactions(first:20){nodes{content user{login} createdAt}}
      commits(last:1){nodes{commit{oid committedDate statusCheckRollup{state}}}}
      reviewThreads(first:100){nodes{isResolved isOutdated comments(last:1){nodes{createdAt}}}}
    }
  }
}"""

_PR_DEPLOY_GATE_QUERY = """query($owner:String!,$repo:String!,$num:Int!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      state
      isDraft
      mergeable
      mergeStateStatus
      reviewDecision
      body
      comments(last:100){nodes{body createdAt author{login}} pageInfo{hasPreviousPage startCursor}}
      reactions(first:20){nodes{content user{login} createdAt}}
      commits(last:1){nodes{commit{oid statusCheckRollup{state}}}}
      reviewThreads(first:100){nodes{isResolved isOutdated comments(last:1){nodes{createdAt}}}}
    }
  }
}"""

_PR_COMMENTS_PAGE_QUERY = """query($owner:String!,$repo:String!,$num:Int!,$before:String!){
  repository(owner:$owner,name:$repo){
    pullRequest(number:$num){
      comments(last:100,before:$before){nodes{body createdAt author{login}} pageInfo{hasPreviousPage startCursor}}
    }
  }
}"""


def _has_bot_comment(pr: dict, authors: frozenset[str]) -> bool:
    comments = (pr.get("comments") or {}).get("nodes") or []
    return any(
        isinstance(comment, dict)
        and ((comment.get("author") or {}).get("login") or "").strip().lower().removesuffix("[bot]").strip()
        in authors
        for comment in comments
    )


def _run_pr_query(query: str, owner: str, repo: str, number: int) -> dict | None:
    r = capture(
        [
            "gh", "api", "graphql",
            "-f", f"query={query}",
            "-f", f"owner={owner}",
            "-f", f"repo={repo}",
            "-F", f"num={number}",
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def _paginate_bot_comments(
    pr_json: dict, owner: str, repo: str, number: int, authors: frozenset[str]
) -> dict:
    """Fetch older comment pages only when the newest page has no score-bot signal.

    With no bots configured there is nothing to page back for, so the extra round trips
    are skipped entirely.
    """
    if not authors:
        return pr_json
    pr = (((pr_json.get("data") or {}).get("repository") or {}).get("pullRequest"))
    if not isinstance(pr, dict):
        return pr_json
    comments = pr.get("comments") or {}
    nodes = comments.get("nodes") or []
    while not _has_bot_comment(pr, authors):
        if not comments.get("pageInfo", {}).get("hasPreviousPage"):
            break
        before = comments.get("pageInfo", {}).get("startCursor")
        if not before:
            break
        page = _run_pr_query_with_before(owner, repo, number, before)
        page_pr = (((page or {}).get("data") or {}).get("repository") or {}).get("pullRequest")
        if not isinstance(page_pr, dict):
            break
        page_comments = page_pr.get("comments") or {}
        page_nodes = page_comments.get("nodes") or []
        if not page_nodes and page_comments.get("pageInfo", {}).get("startCursor") == before:
            break
        nodes = page_nodes + nodes
        comments = page_comments
        pr["comments"] = {"nodes": nodes, "pageInfo": comments.get("pageInfo") or {}}
    return pr_json


def _run_pr_query_with_before(owner: str, repo: str, number: int, before: str) -> dict | None:
    r = capture(
        [
            "gh", "api", "graphql",
            "-f", f"query={_PR_COMMENTS_PAGE_QUERY}",
            "-f", f"owner={owner}",
            "-f", f"repo={repo}",
            "-F", f"num={number}",
            "-f", f"before={before}",
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        return None
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return None


def require_gh(cfg) -> bool:
    """True if ``gh`` is installed AND authenticated (debug.sh:74-77). Fails the tick cleanly
    (not the whole loop) when missing, so a transient auth blip just skips a tick."""
    if shutil.which("gh") is None:
        journal.log(
            cfg,
            "gh CLI not found — the debug loop needs it to read PR review state; skipping tick",
        )
        return False
    if capture(["gh", "auth", "status"]).returncode != 0:
        journal.log(cfg, "gh not authenticated (run 'gh auth login') — skipping tick")
        return False
    return True


def pr_review_state(
    owner: str, repo: str, number: int, score_bot_authors: frozenset[str] = frozenset()
) -> dict | None:
    """Fetch review state and enough comment pages to find a Greptile signal.

    The common case is one GraphQL round-trip; older comment pages are fetched only when the
    newest page has no Greptile-authored comment. Returns parsed JSON, or None on any error/empty
    output (debug.sh:107-120).
    """
    state = _run_pr_query(_PR_STATE_QUERY, owner, repo, number)
    return (
        _paginate_bot_comments(state, owner, repo, number, score_bot_authors)
        if state is not None
        else None
    )


def pr_deploy_gate_state(
    owner: str, repo: str, number: int, score_bot_authors: frozenset[str] = frozenset()
) -> dict | None:
    """Fetch the PR fields deploy needs before spawning ``/ship-it``.

    This is intentionally a read-only preflight. ``/ship-it`` still owns the final merge gate; the
    deploy loop only uses this to avoid repeatedly launching ship-it for an Approved issue whose PR
    has already drifted back into review/debug work.
    """
    try:
        state = _run_pr_query(_PR_DEPLOY_GATE_QUERY, owner, repo, number)
    except OSError:
        return None
    return (
        _paginate_bot_comments(state, owner, repo, number, score_bot_authors)
        if state is not None
        else None
    )
