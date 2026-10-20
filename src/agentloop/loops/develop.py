"""develop — start coding sessions for ready loop issues.

The dispatch loop owns queue selection and admits ready work to Todo. This loop owns the active
claim and the expensive local development side: moving Todo work to In Progress, creating or
reclaiming the issue worktree, and spawning the configured harness with ``/fix-bug`` or
``/build-feature``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re

from agentloop import journal, proc, spawn, tracker, worktree, workflows
from agentloop.tracker import TrackerError
from agentloop.loops.dispatch import (
    _first_repo_label,
    _repo_root_or_log,
    _type_from_labels,
    branch_name,
    dispatch_prompt,
)
from agentloop.outage import rate_limited_now


@dataclass
class DevelopmentRow:
    id: str
    identifier: str
    scope: str
    repo: str
    type: str
    title: str


@dataclass
class TodoPrRow:
    id: str
    identifier: str
    scope: str


_BLOCKED_VERDICT = re.compile(r"^\s*-\s*Verdict:\s*(?:stale\s*/\s*)?blocked\b", re.IGNORECASE | re.MULTILINE)
_BLOCKED_MARKER = re.compile(r"<sub>agent-loop-blocked:[^<>\s]+</sub>", re.IGNORECASE)


def _is_blocked_verdict(body: object) -> bool:
    text = body if isinstance(body, str) else ""
    return bool(_BLOCKED_VERDICT.search(text) or _BLOCKED_MARKER.search(text))


def _latest_comment_is_recent_blocked(issue: tracker.Issue, cooldown_seconds: int, *, now: datetime | None = None) -> bool:
    """A newer human comment clears a bounded blocked-verdict cooldown."""
    dated = [comment for comment in issue.comments if comment.created_at]
    if not dated:
        return False
    latest = max(dated, key=lambda comment: comment.created_at)
    if not _is_blocked_verdict(latest.body):
        return False
    try:
        created_at = datetime.fromisoformat(latest.created_at.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return False
    current = now or datetime.now(timezone.utc)
    return current - created_at.astimezone(timezone.utc) < timedelta(seconds=cooldown_seconds)


def _has_pr(issue: tracker.Issue) -> bool:
    return bool(issue.linked_pr_urls)


def list_todo_prs(client, cfg) -> list[TodoPrRow]:
    """Repair admitted issues whose PR exists but review handoff did not finish."""
    return [
        TodoPrRow(issue.id, issue.identifier, issue.scope)
        for issue in client.issues_in_states([cfg.state_todo])
        if cfg.wip_label in issue.labels and cfg.blocked_label not in issue.labels
        and _has_pr(issue)
    ]


def advance_todo_prs(client, cfg) -> None:
    """Repair Todo loop-owned issues that already have PRs by moving them to review."""
    if cfg.state_todo == cfg.state_review:
        return

    advanced = 0
    for row in list_todo_prs(client, cfg):
        try:
            client.ensure_scope(row.scope)
        except TrackerError:
            journal.log(cfg, f"{row.identifier}: could not load tracker scope for Todo-PR repair — leaving Todo")
            continue
        journal.log(cfg, f"{row.identifier}: {cfg.state_todo} but already has a GitHub PR -> moving to {cfg.state_review}")
        if not client.move_state(row.id, row.scope, cfg.state_review):
            journal.log(cfg, f"{row.identifier}: move to {cfg.state_review} failed — leaving Todo")
            continue
        client.add_comment(
            row.id,
            f"Advanced by the develop loop: `{row.identifier}` was in **{cfg.state_todo}** but already had a "
            f"linked GitHub PR, so it was moved to **{cfg.state_review}** instead of starting another build session.",
        )
        advanced += 1
    if advanced:
        journal.log(cfg, f"advanced {advanced} Todo PR issue(s) to {cfg.state_review}")


def list_development(client, cfg, *, now: datetime | None = None) -> list[DevelopmentRow]:
    """Loop-owned ready issues that are neither parked nor already represented by a PR."""
    rows = []
    for issue in client.issues_in_states([cfg.state_todo], include_comments=True):
        if cfg.wip_label not in issue.labels:
            continue
        if cfg.blocked_label in issue.labels:
            journal.log(cfg, f"{issue.identifier}: loop-blocked label — skipping develop pickup")
            continue
        if _latest_comment_is_recent_blocked(issue, cfg.blocked_cooldown_seconds, now=now):
            journal.log(cfg, f"{issue.identifier}: recent blocked verdict — skipping develop pickup")
            continue
        if _has_pr(issue):
            continue
        rows.append(DevelopmentRow(issue.id, issue.identifier, issue.scope,
                                   _first_repo_label(issue.labels), _type_from_labels(issue.labels), issue.title))
    return rows


def reconcile_blocked_todos(client, cfg, *, now: datetime | None = None) -> int:
    """Persist the fallback parking label before another development pickup."""
    parked = 0
    for issue in client.issues_in_states([cfg.state_todo], include_comments=True):
        if cfg.wip_label not in issue.labels or cfg.blocked_label in issue.labels:
            continue
        if not _latest_comment_is_recent_blocked(issue, cfg.blocked_cooldown_seconds, now=now):
            continue
        try:
            client.ensure_scope(issue.scope)
        except TrackerError:
            journal.log(cfg, f"{issue.identifier}: could not load scope metadata for loop-blocked fallback")
            continue
        if not client.ensure_required_label(issue.id, issue.scope, cfg.blocked_label):
            journal.log(cfg, f"{issue.identifier}: could not apply loop-blocked fallback label")
            continue
        journal.log(cfg, f"{issue.identifier}: recent blocked verdict -> applied loop-blocked fallback label")
        parked += 1
    return parked


def list_active_development(client, cfg) -> list[DevelopmentRow]:
    """Count active loop-owned implementation work, including PR-backed rows."""
    return [
        DevelopmentRow(issue.id, issue.identifier, issue.scope, _first_repo_label(issue.labels),
                       _type_from_labels(issue.labels), issue.title)
        for issue in client.issues_in_states([cfg.state_active])
        if cfg.wip_label in issue.labels
    ]


def develop_one(client, cfg, row: DevelopmentRow) -> bool:
    """Create the issue worktree and spawn the requested build skill. Returns True when the row
    consumed a develop slot, matching the dispatch/debug loop convention."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if proc.has_session(local_key):
        journal.log(cfg, f"{key}: live develop session already in flight — skipping")
        return True

    repo = cfg.collapse_repo(row.repo)
    if not repo:
        journal.log(cfg, f"{key}: missing repo label — leaving in {cfg.state_todo}")
        return True
    root = _repo_root_or_log(cfg, key, repo)
    if root is None:
        return True
    if not root.is_dir():
        journal.log(cfg, f"{key}: repo '{repo}' path missing ({root}) — skipping")
        return True

    type_ = row.type
    if not type_:
        journal.log(cfg, f"{key}: missing work-type label — leaving in {cfg.state_todo}")
        return True
    task = workflows.implement(type_, key)
    branch = branch_name(cfg, local_key, row.title)
    wt = worktree.worktree_path(cfg, root, local_key)
    journal.log(cfg, f"{key}: developing repo={repo} type={type_} branch={branch}")

    try:
        client.ensure_scope(row.scope)
    except TrackerError:
        journal.log(cfg, f"{key}: could not load tracker scope — leaving in {cfg.state_todo}")
        return True
    if not client.move_state(row.id, row.scope, cfg.state_active):
        journal.log(cfg, f"{key}: move to {cfg.state_active} failed — leaving in {cfg.state_todo}")
        return True

    # Idempotent start: clear LOCAL leftovers for this key first (never the remote), then branch
    # from the configured base. A failed checkout leaves the issue active for a later tick or declutter.
    worktree.cleanup_worktree(cfg, local_key)
    proc.git(cfg, root, "branch", "-D", branch, check=False)
    proc.git(cfg, root, "worktree", "prune")
    remote = cfg.repo_remote(repo)
    base_branch = cfg.repo_base_branch(repo)
    base_ref = f"refs/remotes/{remote}/{base_branch}"
    fetch_refspec = f"+refs/heads/{base_branch}:{base_ref}"
    if not proc.succeeded(proc.git(cfg, root, "fetch", "--quiet", remote, fetch_refspec)):
        journal.log(cfg, f"{key}: fetch {remote}/{base_branch} failed — returning to {cfg.state_todo} for retry")
        if not client.move_state(row.id, row.scope, cfg.state_todo):
            journal.log(cfg, f"{key}: move back to {cfg.state_todo} failed — leaving active for declutter")
        return True
    add = proc.git(cfg, root, "worktree", "add", "-b", branch, str(wt), base_ref)
    if add is not None and add.returncode != 0:
        journal.log(
            cfg, f"{key}: worktree add failed even after cleanup — leaving active for retry/declutter"
        )
        return True

    spawn.tmux_spawn(
        cfg,
        local_key,
        wt,
        dispatch_prompt(
            task,
            key,
            cfg.state_review,
            blocked_label=cfg.blocked_label,
            needs_input_state=cfg.state_needs_input,
            tracker_adapter=client,
            base_ref=f"{remote}/{base_branch}",
        ),
    )
    return True


def run_tick(cfg) -> None:
    client = tracker.resolve(cfg)

    if not client.resolve_scope():
        journal.log(cfg, f"no projects carry the '{cfg.project_label}' label")
        return

    advance_todo_prs(client, cfg)

    # Development is the build-class loop, so it owns the usage-outage gate that dispatch used to
    # carry when claiming and spawning were coupled. Non-spawning repairs above can still run.
    hint = rate_limited_now(cfg)
    if hint is not None:
        journal.log(cfg, f"Claude usage outage — holding this tick, no develop spawns ({hint})")
        return

    reconcile_blocked_todos(client, cfg)

    rows = list_development(client, cfg)
    if not rows:
        journal.log(cfg, f"no {cfg.state_todo} loop-wip issues ready for develop")
        return

    active_rows = list_active_development(client, cfg)
    inflight = sum(1 for r in active_rows if proc.has_session(tracker.execution_key(cfg, r)))
    slots = cfg.concurrency - inflight
    journal.log(
        cfg,
        f"develop-ready={len(rows)} cap={cfg.concurrency} inflight={inflight} slots={slots}",
    )
    if slots <= 0:
        journal.log(cfg, "no free develop slots")
        return

    started = 0
    for row in rows:
        if started >= slots:
            break
        if not row.id:
            continue
        develop_one(client, cfg, row)
        started += 1

    journal.log(cfg, f"develop-spawned {started} this tick")
