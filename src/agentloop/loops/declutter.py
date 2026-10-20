"""declutter — the loop's janitor: GC orphaned worktrees + recover zombies.

Ports ``declutter.sh``. Two jobs each tick: (1) remove every orphaned loop worktree whose tmux
session is gone; (2) reconcile issues stuck in the active state with no live session — one that
already produced a PR is a stranded hand-off, advanced to In Review for the debug loop; otherwise
it is a genuine zombie: a Claude usage-limit exit is re-queued to the backlog (dispatch resumes it
once usage returns), any other death goes to Needs Input for a human. Never outage-gated (no Claude).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import datetime

from agentloop import identity, journal, proc, tracker, worktree
from agentloop.journal import audit
from agentloop.tracker import TrackerError
from agentloop.outage import key_outage_log



@dataclass
class StuckRow:
    id: str
    identifier: str
    scope: str
    has_pr: bool = False  # an active-state issue that already produced a GitHub PR


def _started_epoch(started_at: str) -> float | None:
    """Parse a Linear timestamp (may carry a 'Z' and milliseconds). 3.11 fromisoformat handles
    both. Returns epoch seconds, or None if unparseable."""
    try:
        return datetime.fromisoformat(started_at).timestamp()
    except (ValueError, TypeError):
        return None


def list_stuck(client, cfg) -> list[StuckRow]:
    """Past-grace active work; linked PRs are recovered instead of discarded."""
    now = time.time()
    rows = []
    for issue in client.issues_in_states([cfg.state_active]):
        if cfg.wip_label not in issue.labels or cfg.blocked_label in issue.labels:
            continue
        epoch = _started_epoch(issue.started_at) if issue.started_at else None
        if epoch is None or now - epoch <= cfg.grace_seconds:
            continue
        rows.append(StuckRow(issue.id, issue.identifier, issue.scope, bool(issue.linked_pr_urls)))
    return rows


def gc_orphan_worktrees(cfg, *, protected_keys: set[str] | None = None) -> None:
    """Remove every orphaned worktree across repos — any whose tmux session is gone and which is
    past the grace window (declutter.sh:80-103). The branch is deleted too (its PR, if any, is
    already pushed)."""
    mins = max(1, cfg.grace_seconds // 60)
    now = time.time()
    n = 0
    for root in worktree.unique_repo_roots(cfg):
        canonical_rows = []
        d = root / cfg.worktree_rel
        if not d.is_symlink() and d.resolve().is_relative_to(root.resolve()) and d.is_dir():
            for wt in sorted(d.iterdir()):
                if not wt.is_symlink() and wt.is_dir():
                    canonical_rows.append((wt, wt.name, worktree._current_branch(wt)))

        for wt, key, br in canonical_rows:
            if wt.is_symlink() or not wt.is_dir():
                continue
            # Require both a recognized local key and a matching branch token.
            if not identity.is_execution_key(key) or not worktree.branch_matches_key(br, key):
                continue
            if protected_keys is not None and key in protected_keys:
                journal.log(cfg, f"{key}: loop-blocked worktree — retaining orphan until explicitly requeued")
                continue
            if proc.has_session(key):  # live -> keep
                continue
            if (now - wt.stat().st_mtime) <= mins * 60:  # too fresh -> keep
                continue
            audit(cfg, f"{key}: orphaned worktree (no live session) -> removing worktree" + (f" + branch {br}" if br else ""))
            removed = proc.git(cfg, root, "worktree", "remove", "--force", str(wt))
            if not proc.succeeded(removed):
                journal.log(cfg, f"{key}: worktree removal failed — retaining its branch")
                continue
            if br and br != "HEAD":
                proc.git(cfg, root, "branch", "-D", br, check=False)
            n += 1
    journal.log(cfg, f"gc: removed {n} orphaned worktree(s)")


def list_blocked_worktree_keys(client, cfg) -> set[str] | None:
    """Fail closed on unreadable tracker state before any orphan filesystem cleanup."""
    try:
        issues = client.issues_in_states([cfg.state_active])
    except TrackerError:
        journal.log(cfg, "could not read blocked active tickets — skipping orphan worktree GC")
        return None
    return {tracker.execution_key(cfg, issue) for issue in issues if cfg.blocked_label in issue.labels}


def declutter_one(client, cfg, row: StuckRow) -> bool:
    """Recover one zombie (declutter.sh:105-135). Returns True if actioned."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if proc.has_session(local_key):  # alive -> not a zombie
        return False
    try:
        client.ensure_scope(row.scope)
    except TrackerError:
        return False

    # Stranded hand-off, not a zombie: the session opened a PR but exited without moving
    # In Progress -> In Review (crash after PR create, or the skill's move step failed). The PR is
    # the real checkpoint, so hand it to the debug loop by advancing to In Review — rather than
    # reaping it (it's not an abandoned claim) or leaving it stuck forever (debug only watches In
    # Review). The worktree is left for gc_orphan_worktrees. No GitHub call: the debug loop
    # classifies the PR (open/merged/closed) from In Review.
    if row.has_pr:
        journal.log(cfg, f"{key}: {cfg.state_active} with a PR but no live session -> advancing to {cfg.state_review} (debug loop drains it)")
        if not client.move_state(row.id, row.scope, cfg.state_review):
            journal.log(cfg, f"{key}: move to {cfg.state_review} failed — will retry next tick")
            return False
        client.add_comment(
            row.id,
            f"🔁 Advanced by the loop janitor: `{key}` had a linked PR but sat in **{cfg.state_active}** "
            f"with no live session (the build session opened the PR, then exited before moving it). "
            f"Moved **{cfg.state_active} → {cfg.state_review}** so the debug loop drains review activity.",
        )
        audit(cfg, f"{key}: advanced {cfg.state_active} -> {cfg.state_review} (linked PR, no live session)")
        return True

    # Outage victim vs. genuine zombie. A usage-limit exit is re-queued to the backlog so
    # dispatch resumes it automatically once usage returns (the retry branches fresh off main,
    # so clearing this claim's worktree loses nothing).
    if key_outage_log(cfg, local_key):
        journal.log(cfg, f"{key}: {cfg.state_active} but session hit the Claude usage cap -> re-queuing to {cfg.state_queue}")
        if not client.move_state(row.id, row.scope, cfg.state_queue):
            journal.log(cfg, f"{key}: re-queue to {cfg.state_queue} failed — will retry next tick")
            return False
        client.add_comment(
            row.id,
            f"⏳ Auto-requeued by the loop: the `{key}` session exited on a Claude **usage limit** "
            f"(capacity, not a failure). Moved **{cfg.state_active} → {cfg.state_queue}** so dispatch "
            f"restarts it automatically once usage resets; the orphaned worktree/branch were cleared. "
            f"Session logs: ~/.agent-loop/logs/develop/{local_key}-*.log (newest = last run).",
        )
        worktree.cleanup_worktree(cfg, local_key)
        audit(cfg, f"{key}: re-queued {cfg.state_active} -> {cfg.state_queue} (Claude usage-limit exit); worktree/branch cleared")
        return True

    journal.log(cfg, f"{key}: {cfg.state_active} but no live session -> decluttering to {cfg.state_needs_input}")
    # Move state FIRST. If it fails, bail WITHOUT commenting so the next tick retries cleanly
    # instead of stacking duplicate "decluttered" comments.
    if not client.move_state(row.id, row.scope, cfg.state_needs_input):
        journal.log(cfg, f"{key}: move to {cfg.state_needs_input} failed — will retry next tick")
        return False
    client.assign_issue(row.id)
    client.add_comment(
        row.id,
        f"🧹 Decluttered by the loop: marked **{cfg.state_active}** but no live `{key}` tmux session "
        f"(the session died or never spawned). Moved to **{cfg.state_needs_input}**; the orphaned "
        f"worktree/branch were cleared. Re-queue it (move back to the queue state) to retry, or check "
        f"the session logs at ~/.agent-loop/logs/develop/{local_key}-*.log (newest = last run).",
    )
    worktree.cleanup_worktree(cfg, local_key)
    audit(cfg, f"{key}: decluttered {cfg.state_active} -> {cfg.state_needs_input} (no live session); worktree/branch cleared")
    return True


def run_tick(cfg) -> None:
    client = tracker.resolve(cfg)

    if not client.resolve_scope():
        gc_orphan_worktrees(cfg)  # no loop project means no blocked ticket can be protected
        journal.log(cfg, f"no projects carry the '{cfg.project_label}' label")
        return

    protected_keys = list_blocked_worktree_keys(client, cfg)
    if protected_keys is None:
        journal.log(cfg, "skipping orphan worktree GC because blocked-ticket state is unavailable")
    else:
        gc_orphan_worktrees(cfg, protected_keys=protected_keys)

    advanced = 0
    decluttered = 0
    for row in list_stuck(client, cfg):
        if not row.id:
            continue
        if not declutter_one(client, cfg, row):
            continue
        if row.has_pr:
            advanced += 1
        else:
            decluttered += 1

    journal.log(cfg, f"advanced {advanced} to {cfg.state_review}, decluttered {decluttered} zombie(s) this tick")
