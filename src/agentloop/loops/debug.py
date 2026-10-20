"""debug — the loop's PR-review drainer. Owns the In-Review state.

Ports ``debug.sh``: for each In-Review, loop-owned issue with an open PR, read the PR's live
review state and pick one action — fix (spawn the fix skill for unresolved threads newer than
head), approve (green + resolved + approving review -> Approved), wait, or handle merged/closed.
Never merges (deploy owns that). Fix sessions are unbounded apart from the per-ticket live-session
guard, so review drain is not throttled by dispatch capacity.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from agentloop import github, journal, proc, spawn, tracker, worktree, workflows
from agentloop.tracker import TrackerError
from agentloop.outage import rate_limited_now

_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/([0-9]+)")
_GREPTILE_MARKER = "<!-- greptile_comment -->"
_GREPTILE_SCORE = re.compile(r"Confidence Score:\s*([0-9]+)\s*/\s*5", re.IGNORECASE)
_DEFAULT_GREPTILE_MIN_SCORE = 4
# Both bot gates ship as presets but default to OFF: a fresh install requires a human
# approving review, and needs no third-party account. Name the bots to opt in --
# AGENT_LOOP_DEBUG_SCORE_BOT_AUTHORS=greptile-apps and
# AGENT_LOOP_DEBUG_APPROVAL_REACTORS=chatgpt-codex-connector reproduce the old behavior.
_DEFAULT_SCORE_BOT_AUTHORS: frozenset[str] = frozenset()
_DEFAULT_APPROVAL_REACTORS: frozenset[str] = frozenset()
_DEFAULT_APPROVAL_WAIT_MINUTES = 60


@dataclass
class ReviewRow:
    id: str
    identifier: str
    scope: str
    repo: str
    pr_url: str


@dataclass
class DebugConfig:
    skill: str
    require_approval: bool
    ci_fix_enabled: bool = True
    ci_fix_max: int = 2
    greptile_min_score: int = _DEFAULT_GREPTILE_MIN_SCORE
    approval_reactors: frozenset[str] = _DEFAULT_APPROVAL_REACTORS
    score_bot_authors: frozenset[str] = _DEFAULT_SCORE_BOT_AUTHORS
    approval_wait_minutes: int = _DEFAULT_APPROVAL_WAIT_MINUTES

    @classmethod
    def from_env(cls, cfg, environ=None) -> "DebugConfig":
        e = os.environ if environ is None else environ
        return cls(
            skill=e.get("AGENT_LOOP_DEBUG_SKILL", "/triangulate"),
            require_approval=e.get("AGENT_LOOP_DEBUG_REQUIRE_APPROVAL", "1") == "1",
            greptile_min_score=int(
                e.get("AGENT_LOOP_DEBUG_GREPTILE_MIN_SCORE", str(_DEFAULT_GREPTILE_MIN_SCORE))
            ),
            approval_reactors=frozenset(
                reactor
                for raw in e.get(
                    "AGENT_LOOP_DEBUG_APPROVAL_REACTORS",
                    ",".join(_DEFAULT_APPROVAL_REACTORS),
                ).split(",")
                if (reactor := _normalise_reactor(raw))
            ),
            score_bot_authors=frozenset(
                author
                for raw in e.get(
                    "AGENT_LOOP_DEBUG_SCORE_BOT_AUTHORS",
                    ",".join(_DEFAULT_SCORE_BOT_AUTHORS),
                ).split(",")
                if (author := _normalise_reactor(raw))
            ),
            # Self-heal red CI: spawn a fix session for failing checks (not just for review
            # comments), then escalate to Needs Input if it can't be turned green. Kill switch
            # for the rollout — set AGENT_LOOP_DEBUG_CI_FIX=0 to fall back to the old wait-forever
            # behavior. ci_fix_max bounds distinct-commit fix attempts before handing to a human.
            ci_fix_enabled=e.get("AGENT_LOOP_DEBUG_CI_FIX", "1") == "1",
            ci_fix_max=int(e.get("AGENT_LOOP_DEBUG_CI_FIX_MAX", "2")),
            approval_wait_minutes=int(
                e.get("AGENT_LOOP_DEBUG_APPROVAL_WAIT_MINUTES", str(_DEFAULT_APPROVAL_WAIT_MINUTES))
            ),
        )


@dataclass
class Tally:
    approved_n: int = 0
    fixed_n: int = 0
    ci_fixed_n: int = 0
    escalated_n: int = 0


def _jq_gt(a: str | None, b: str | None) -> bool:
    """jq-style ``>``: null sorts below any string (so ``"x" > null`` is true, ``null > "x"`` false).
    Used for the "newest comment strictly after head commit" idempotency check."""
    ra, rb = (0 if a is None else 1), (0 if b is None else 1)
    if ra != rb:
        return ra > rb
    if a is None:  # both null
        return False
    return a > b


def _normalise_reactor(login: str) -> str:
    """Match GitHub App logins whether GitHub includes their ``[bot]`` suffix or not."""
    return login.strip().lower().removesuffix("[bot]").strip()


def _score_from_text(text: object) -> int | None:
    """Return a valid Greptile confidence score from a summary body."""
    if not isinstance(text, str):
        return None
    match = _GREPTILE_SCORE.search(text)
    if match is None:
        return None
    score = int(match.group(1))
    return score if 0 <= score <= 5 else None


def _latest_greptile_comment(pr: dict, authors: frozenset[str]) -> dict | None:
    """Return the newest PR issue comment from an allowlisted score bot, if any."""
    if not authors:
        return None
    comments = (pr.get("comments") or {}).get("nodes") or []
    allowlisted = [
        comment
        for comment in comments
        if isinstance(comment, dict)
        and _normalise_reactor(((comment.get("author") or {}).get("login") or ""))
        in authors
    ]
    return max(allowlisted, key=lambda comment: comment.get("createdAt") or "") if allowlisted else None


def _legacy_greptile_score(pr: dict) -> int | None:
    """Return the body-embedded score used by Greptile before it posted issue comments."""
    body = pr.get("body")
    if not isinstance(body, str) or _GREPTILE_MARKER not in body:
        return None
    return _score_from_text(body.rsplit(_GREPTILE_MARKER, 1)[1])


def _greptile_ok(pr: dict, head: str | None, min_score: int, authors: frozenset[str]) -> bool:
    """Return whether Greptile's current summary reaches the configured threshold.

    Greptile now posts the score as a PR issue comment. Only its newest allowlisted comment
    counts, and it must have been posted after the current head commit. PR-body scores remain a
    legacy fallback for reviews created before that behavior changed.
    """
    if not authors:
        return False
    score = _greptile_score(pr, head, authors)
    return score is not None and score >= min_score


def _greptile_score(pr: dict, head: str | None, authors: frozenset[str]) -> int | None:
    """Return the current valid Greptile score, preferring its newest PR issue comment."""
    if not authors:
        return None
    comment = _latest_greptile_comment(pr, authors)
    if comment is not None:
        if not _jq_gt(comment.get("createdAt"), head):
            return None
        return _score_from_text(comment.get("body"))
    return _legacy_greptile_score(pr)


def _codex_ok(pr: dict, head: str | None, approval_reactors: frozenset[str]) -> bool:
    """Return whether an allowlisted Codex reaction is newer than the head commit."""
    if head is None:
        return False
    reactions = (pr.get("reactions") or {}).get("nodes") or []
    return any(
        reaction.get("content") == "THUMBS_UP"
        and _normalise_reactor(
            ((reaction.get("user") or {}).get("login") or "")
        ) in approval_reactors
        and _jq_gt(reaction.get("createdAt"), head)
        for reaction in reactions
    )


def _approval_gap(pr: dict, dcfg: DebugConfig) -> tuple[str, list[str]] | None:
    """Return the head SHA and missing approval signals for a PR eligible to wait.

    ``decide`` still owns the action classification. This predicate deliberately mirrors its
    green/no-thread/merge-gate preconditions so pending CI and merge conflicts never consume the
    approval-wait budget.
    """
    if not dcfg.require_approval:
        return None
    commit = ((pr.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    head_sha = commit.get("oid") or ""
    head_date = commit.get("committedDate")
    checks = (commit.get("statusCheckRollup") or {}).get("state")
    open_threads = [
        t for t in (pr.get("reviewThreads") or {}).get("nodes", []) if t.get("isResolved") is False
    ]
    if (
        not head_sha
        or checks != "SUCCESS"
        or open_threads
        or pr.get("reviewDecision") == "APPROVED"
        or pr.get("isDraft")
        or pr.get("mergeable") == "CONFLICTING"
        or pr.get("mergeStateStatus") in {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNSTABLE"}
    ):
        return None

    missing: list[str] = []
    if not dcfg.score_bot_authors and not dcfg.approval_reactors:
        # No bot gates configured, so a human review is the only route to Approved.
        # Listing absent Greptile/Codex signals would name tools nobody here set up.
        return head_sha, ["no approving review on the pull request"]

    comment = _latest_greptile_comment(pr, dcfg.score_bot_authors)
    score = _greptile_score(pr, head_date, dcfg.score_bot_authors)
    if comment is not None and not _jq_gt(comment.get("createdAt"), head_date):
        missing.append("the newest allowlisted Greptile comment is not newer than the head commit")
    elif comment is not None and score is None:
        missing.append("the newest allowlisted Greptile comment has no valid confidence score")
    elif comment is None and _legacy_greptile_score(pr) is None:
        missing.append("no allowlisted Greptile comment or valid legacy PR-body score")
    elif score < dcfg.greptile_min_score:
        missing.append(
            f"Greptile confidence score {score}/5 is below the required "
            f"{dcfg.greptile_min_score}/5"
        )

    reactions = (pr.get("reactions") or {}).get("nodes") or []
    thumbs = [r for r in reactions if r.get("content") == "THUMBS_UP"]
    allowed = [
        r for r in thumbs
        if _normalise_reactor(((r.get("user") or {}).get("login") or "")) in dcfg.approval_reactors
    ]
    if not thumbs:
        missing.append("no Codex 👍 reaction")
    elif allowed and not _codex_ok(pr, head_date, dcfg.approval_reactors):
        logins = sorted({((r.get("user") or {}).get("login") or "unknown") for r in allowed})
        missing.append(
            f"Codex 👍 reaction from {', '.join(logins)} is not newer than the head commit"
        )
    elif not allowed:
        logins = sorted({((r.get("user") or {}).get("login") or "unknown") for r in thumbs})
        missing.append(
            "Codex 👍 reaction is only from non-allowlisted login(s): " + ", ".join(logins)
        )

    return (head_sha, missing) if missing else None


def _parse_linear_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def decide(
    pr_json: dict | None,
    require_approval: bool,
    greptile_min_score: int = _DEFAULT_GREPTILE_MIN_SCORE,
    approval_reactors: frozenset[str] = _DEFAULT_APPROVAL_REACTORS,
    score_bot_authors: frozenset[str] = _DEFAULT_SCORE_BOT_AUTHORS,
) -> str:
    """Classify a PR from its gh state JSON into exactly one action (debug.sh:124-141).

    GitHub DateTimes are UTC "Z", so ISO-8601 strings compare lexicographically — no date parsing.
    """
    pr = (((pr_json or {}).get("data") or {}).get("repository") or {}).get("pullRequest")
    if pr is None:
        return "error"
    if pr.get("state") == "MERGED":
        return "merged"
    if pr.get("state") == "CLOSED":
        return "closed"

    commit_nodes = (pr.get("commits") or {}).get("nodes") or []
    commit = (commit_nodes[0].get("commit") if commit_nodes else None) or {}
    head = commit.get("committedDate")
    checks = (commit.get("statusCheckRollup") or {}).get("state")

    open_threads = [
        t for t in (pr.get("reviewThreads") or {}).get("nodes", []) if t.get("isResolved") is False
    ]
    current_open_threads = [t for t in open_threads if t.get("isOutdated") is not True]
    comment_dates = [
        ((t.get("comments") or {}).get("nodes") or [{}])[0].get("createdAt") for t in open_threads
    ]
    non_null = [d for d in comment_dates if d is not None]
    newest = max(non_null) if non_null else None

    if current_open_threads or (open_threads and newest is not None and _jq_gt(newest, head)):
        return "fix"
    # Red CI with no fresher review comment to chase: the old dead zone (parked in "wait"
    # forever). Classify it so debug_one can spawn a fix session and, failing that, escalate.
    # FAILURE/ERROR only — PENDING/EXPECTED/null means CI is still running, so keep waiting.
    if checks in ("FAILURE", "ERROR"):
        return "ci_fix"
    if pr.get("isDraft"):
        return "merge_fix"
    if pr.get("mergeable") == "CONFLICTING":
        return "merge_fix"
    if pr.get("mergeStateStatus") in {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNSTABLE"}:
        return "merge_fix"
    if (
        checks == "SUCCESS"
        and len(open_threads) == 0
        and (
            (not require_approval)
            or pr.get("reviewDecision") == "APPROVED"
            or (
                _greptile_ok(pr, head, greptile_min_score, score_bot_authors)
                and _codex_ok(pr, head, approval_reactors)
            )
        )
    ):
        return "approve"
    return "wait"


def list_in_review(client, cfg) -> list[ReviewRow]:
    """In-review loop issues with a PR and no durable parking label."""
    return [
        ReviewRow(issue.id, issue.identifier, issue.scope,
                  next((name[5:] for name in issue.labels if name.startswith("repo:")), ""),
                  pr_url)
        for issue in client.issues_in_states([cfg.state_review])
        if cfg.wip_label in issue.labels and cfg.blocked_label not in issue.labels
        and (pr_url := next((url for url in issue.linked_pr_urls if _PR_URL.search(url)), ""))
    ]


def spawn_fix(
    client, cfg, dcfg, row: ReviewRow, branch, owner, repo, num,
    reason: str = "unaddressed review comments",
) -> bool:
    """Spawn a headless fix session on the PR's own branch (debug.sh:146-176). Returns True on
    spawn (or dry-run preview), False on any skip. ``reason`` only tunes the log line — the review
    (``fix``) and CI (``ci_fix``) paths share identical checkout + spawn machinery."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if not branch:
        journal.log(cfg, f"{key}: PR #{num} has no head branch — skipping fix")
        return False
    # Route the checkout by the PR's OWN repo (from the URL, `repo`), NOT the ticket's repo: label
    # (debug.sh:146-176). The classifier can mislabel — for example, an E2E ticket can be routed to
    # the wrong checkout while its PR branch exists only in the correct repository. The branch to fix
    # PR is on. Fall back to the label if the PR's repo isn't mapped; never default to the monorepo,
    # since fetching the branch from the wrong checkout always fails.
    repokey = cfg.collapse_repo(repo)
    if repokey not in cfg.repos:
        repokey = cfg.collapse_repo(row.repo)
    root = cfg.repos.get(repokey)
    if not root or not root.is_dir():
        journal.log(
            cfg, f"{key}: PR repo '{repo}' has no local checkout in the loop's repo map — "
            f"can't fetch the branch to fix; leaving in {cfg.state_review}"
        )
        return False

    remote = cfg.repo_remote(repokey)
    wt = worktree.worktree_path(cfg, root, local_key)
    # Idempotent: clear stale loop worktree/branch, then check out the EXISTING PR branch
    # (continuing an open PR, not starting fresh off main).
    worktree.cleanup_worktree(cfg, local_key)
    remote_ref = f"refs/remotes/{remote}/{branch}"
    if not proc.succeeded(proc.git(cfg, root, "fetch", "--quiet", remote, f"+refs/heads/{branch}:{remote_ref}")):
        journal.log(cfg, f"{key}: fetch {remote}/{branch} failed — leaving in {cfg.state_review}")
        return False
    # Free the PR branch if another tree still holds it, then add the loop worktree (debug.sh:164-186).
    # Old skill runs may have left a sibling <repo>-worktrees checkout, or worked the ticket directly
    # in the repo's MAIN checkout. reclaim_branch removes a stale linked worktree but DETACHES a main
    # tree (which git won't `worktree remove`), so the loop self-heals instead of stalling.
    if wt.is_dir():
        # PR heads may have arbitrary names. Reuse a clean existing checkout
        # rather than deleting a branch that lacks the generated execution token.
        registered = any(path.resolve() == wt.resolve() for path, _ in worktree._worktree_branches(root))
        top = proc.capture(["git", "-C", str(wt), "rev-parse", "--show-toplevel"])
        if not registered or top.returncode != 0 or Path(top.stdout.strip()).resolve() != wt.resolve():
            journal.log(cfg, f"{key}: fix path is not a registered worktree — retaining {wt}")
            return False
        status = proc.capture(["git", "-C", str(wt), "status", "--porcelain"])
        if worktree._current_branch(wt) != branch or status.returncode != 0 or status.stdout.strip():
            journal.log(cfg, f"{key}: existing fix worktree has unrelated or unfinished work — retaining {wt}")
            return False
        if not proc.succeeded(proc.git(cfg, wt, "merge", "--ff-only", remote_ref)):
            journal.log(cfg, f"{key}: fix worktree diverged from the PR head — retaining {wt}")
            return False
    else:
        worktree.reclaim_branch(cfg, root, wt, branch)
        if not proc.succeeded(
            proc.git(cfg, root, "worktree", "add", "-B", branch, str(wt), remote_ref)
        ):
            journal.log(cfg, f"{key}: worktree add for {branch} failed — leaving in {cfg.state_review}")
            return False

    prompt = f"{workflows.review(key, remote)}\n\n{client.state_contract()}"
    journal.log(
        cfg, f"{key}: {reason} on PR #{num} -> spawning '{dcfg.skill} {key}' "
        f"(repo={repokey} branch={branch})"
    )
    spawn.tmux_spawn(cfg, local_key, wt, prompt)
    if not cfg.dry_run:
        journal.log(cfg, f"{key}: fix session spawned")
    return True


def debug_one(client, cfg, dcfg, row: ReviewRow, tally: Tally) -> None:
    """Read a PR's review state and take one action (debug.sh:178-234)."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if proc.has_session(local_key):
        journal.log(cfg, f"{key}: live session in flight — skipping this tick")
        return

    m = _PR_URL.search(row.pr_url)
    if not m:
        journal.log(cfg, f"{key}: can't parse PR from '{row.pr_url}' — skipping")
        return
    owner, repo, num = m.group(1), m.group(2), int(m.group(3))

    st = github.pr_review_state(owner, repo, num, dcfg.score_bot_authors)
    if st is None:
        journal.log(cfg, f"{key}: gh query failed or empty for {owner}/{repo}#{num} — skipping")
        return
    decision = decide(
        st,
        dcfg.require_approval,
        dcfg.greptile_min_score,
        dcfg.approval_reactors,
        dcfg.score_bot_authors,
    )

    if decision == "approve":
        _safe_scope(client, cfg, row.scope)
        journal.log(cfg, f"{key}: PR {owner}/{repo}#{num} green + reviewed -> {cfg.state_review} -> {cfg.state_approved}")
        if not client.move_state(row.id, row.scope, cfg.state_approved):
            journal.log(cfg, f"{key}: approve move failed — retry next tick")
            return
        extra = ", plus an approving review" if dcfg.require_approval else ""
        client.add_comment(
            row.id,
            f"✅ Auto-approved by the **debug** loop: PR #{num} has all required checks green and "
            f"no unresolved review threads{extra}. Moved **{cfg.state_review} → {cfg.state_approved}** "
            f"— deploy will ship it (`/ship-it` re-verifies the merge gate before landing).",
        )
        tally.approved_n += 1

    elif decision == "fix":
        branch = (((st.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("headRefName") or ""
        if spawn_fix(client, cfg, dcfg, row, branch, owner, repo, num):
            tally.fixed_n += 1

    elif decision == "merge_fix":
        branch = (((st.get("data") or {}).get("repository") or {}).get("pullRequest") or {}).get("headRefName") or ""
        if spawn_fix(client, cfg, dcfg, row, branch, owner, repo, num, reason="merge gate blocked"):
            tally.fixed_n += 1

    elif decision == "ci_fix":
        if not dcfg.ci_fix_enabled:
            journal.log(cfg, f"{key}: PR #{num} CI failing but ci-fix disabled — leaving in {cfg.state_review}")
            return
        _handle_ci_fix(client, cfg, dcfg, row, st, owner, repo, num, tally)

    elif decision == "merged":
        _safe_scope(client, cfg, row.scope)
        journal.log(cfg, f"{key}: PR #{num} already MERGED but ticket still {cfg.state_review} -> {cfg.state_approved} (deploy/ship-it closes it out to Done)")
        if not client.move_state(row.id, row.scope, cfg.state_approved):
            return
        tally.approved_n += 1

    elif decision == "closed":
        _safe_scope(client, cfg, row.scope)
        journal.log(cfg, f"{key}: PR #{num} CLOSED unmerged but ticket sat in {cfg.state_review} -> {cfg.state_needs_input}")
        if not client.move_state(row.id, row.scope, cfg.state_needs_input):
            return
        client.assign_issue(row.id)
        client.add_comment(
            row.id,
            f"⚠️ The **debug** loop found PR #{num} **closed without merging** while the ticket sat "
            f"in {cfg.state_review}. Moved to **{cfg.state_needs_input}** for a human to decide.",
        )

    elif decision == "wait":
        if _handle_approval_wait(client, cfg, dcfg, row, st, owner, repo, num):
            tally.escalated_n += 1
            return
        journal.log(cfg, f"{key}: PR #{num} not actionable yet (CI running or awaiting review) — leaving in {cfg.state_review}")
    else:
        journal.log(cfg, f"{key}: could not read PR #{num} state (deleted PR / API error) — skipping")


def _handle_approval_wait(client, cfg, dcfg, row: ReviewRow, st, owner, repo, num) -> bool:
    """Start or bound an approval wait, using Linear comments as durable per-head state."""
    pr = (((st.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
    gap = _approval_gap(pr, dcfg)
    if gap is None:
        return False
    head_sha, missing = gap
    started, handoffs = client.approval_wait_markers(row.id)
    started_at = _parse_linear_time(started.get(head_sha))
    if started_at is None:
        client.add_approval_wait_marker(row.id, head_sha, num, missing)
        journal.log(
            cfg,
            f"{row.identifier}: PR #{num} awaits approval signals for {head_sha[:12]} — "
            f"started {dcfg.approval_wait_minutes}-minute timer",
        )
        return False

    elapsed_seconds = (_now() - started_at).total_seconds()
    if elapsed_seconds < dcfg.approval_wait_minutes * 60:
        return False

    if head_sha not in handoffs:
        if not client.add_approval_wait_escalation(
            row.id, head_sha, num, dcfg.approval_wait_minutes, missing,
            cfg.state_review, cfg.state_needs_input,
        ):
            journal.log(cfg, f"{row.identifier}: approval-wait handoff comment failed — retry next tick")
            return False

    _safe_scope(client, cfg, row.scope)
    journal.log(
        cfg,
        f"{row.identifier}: PR #{num} waited {dcfg.approval_wait_minutes} minutes for approval "
        f"signals on {head_sha[:12]} -> {cfg.state_review} -> {cfg.state_needs_input}",
    )
    if not client.move_state(row.id, row.scope, cfg.state_needs_input):
        journal.log(cfg, f"{row.identifier}: approval-wait escalation move failed — retry next tick")
        return False
    client.assign_issue(row.id)
    return True


def _handle_ci_fix(client, cfg, dcfg, row: ReviewRow, st, owner, repo, num, tally: Tally) -> None:
    """Self-heal a red-CI PR — debug's new trigger for the old "failing checks, no fresh review
    comment" dead zone. One fix attempt per failing head commit, tracked by a marker comment on
    the ticket:

      * first time we see a failing head SHA -> spawn the fix skill (it fixes + pushes); a pushed
        fix changes the SHA, so CI re-runs and, if still red, that new commit earns its own attempt;
      * same SHA still red after we tried (agent couldn't green it -> not this PR's break, a flake,
        or beyond auto-fix), or the attempt budget is spent across commits -> move to Needs Input.

    This is the deterministic, loop-owned version of "fix it if we broke it, else hand it to a human."
    """
    key = row.identifier
    pr = (((st.get("data") or {}).get("repository") or {}).get("pullRequest") or {})
    commit = ((pr.get("commits") or {}).get("nodes") or [{}])[0].get("commit") or {}
    head_sha = commit.get("oid") or ""
    branch = pr.get("headRefName") or ""
    if not head_sha:
        journal.log(
            cfg, f"{key}: PR #{num} CI red but head commit SHA unavailable — can't track a CI-fix "
            f"attempt idempotently; leaving in {cfg.state_review}"
        )
        return

    tried = client.ci_fix_markers(row.id)
    short = head_sha[:12]

    # Escalate: either this exact failing commit was already attempted (our fix didn't green it),
    # or we've burned the attempt budget across successive commits.
    if head_sha in tried or len(tried) >= dcfg.ci_fix_max:
        why = (
            f"still red on the same commit {short} after a debug CI-fix attempt"
            if head_sha in tried
            else f"still red after {len(tried)} CI-fix attempt(s)"
        )
        _safe_scope(client, cfg, row.scope)
        journal.log(cfg, f"{key}: PR #{num} {why} -> {cfg.state_review} -> {cfg.state_needs_input}")
        if not client.move_state(row.id, row.scope, cfg.state_needs_input):
            journal.log(cfg, f"{key}: escalation move failed — retry next tick")
            return
        client.assign_issue(row.id)
        client.add_comment(
            row.id,
            f"⚠️ The **debug** loop tried to auto-fix the failing CI on PR #{num} but it's {why}. "
            f"This likely isn't caused by this change (an unrelated or flaky failure), or it needs a "
            f"human. Moved **{cfg.state_review} → {cfg.state_needs_input}** — review it and, if the "
            f"failure is unrelated to this PR, move it to **{cfg.state_approved}** yourself.",
        )
        tally.escalated_n += 1
        return

    # First sighting of this failing commit — try an automated fix.
    if spawn_fix(client, cfg, dcfg, row, branch, owner, repo, num, reason=f"CI checks failing ({short})"):
        # Record the attempt only after the session actually launched, so a failed checkout
        # (fetch/worktree error) retries next tick instead of escalating a commit we never tried.
        client.add_ci_fix_marker(row.id, head_sha, num)
        tally.ci_fixed_n += 1


def _safe_scope(client, cfg, scope) -> None:
    try:
        client.ensure_scope(scope)
    except TrackerError:
        pass


def run_tick(cfg) -> None:
    client = tracker.resolve(cfg)
    dcfg = DebugConfig.from_env(cfg)

    # Outage gate: the fix path spawns Claude sessions (debug.sh:237-243).
    hint = rate_limited_now(cfg)
    if hint is not None:
        journal.log(cfg, f"Claude usage outage — holding this tick, no debug work ({hint})")
        return

    if not github.require_gh(cfg):
        return

    if not client.resolve_scope():
        journal.log(cfg, f"no projects carry the '{cfg.project_label}' label")
        return

    try:
        rows = list_in_review(client, cfg)
    except TrackerError:
        journal.log(cfg, "list_in_review failed — skipping tick")
        return
    if not rows:
        journal.log(cfg, f"no {cfg.state_review} loop-wip PRs this tick")
        return

    inflight = sum(1 for r in rows if proc.has_session(tracker.execution_key(cfg, r)))
    tally = Tally()
    journal.log(cfg, f"in-review PRs={len(rows)} | live sessions={inflight} | debug fixes unbounded")

    for row in rows:
        if not row.id:
            continue
        debug_one(client, cfg, dcfg, row, tally)

    journal.log(
        cfg,
        f"debug tick: approved {tally.approved_n}, fix-spawned {tally.fixed_n}, "
        f"ci-fix-spawned {tally.ci_fixed_n}, escalated {tally.escalated_n}",
    )
