"""worktree reclaim + compat-path sweep: the debug loop must self-heal a branch held by a repo's
MAIN working tree (detach, not `worktree remove`), and cleanup/declutter must still reap worktrees
left under the pre-rename `.claude/worktrees/agent-loop` path."""

from __future__ import annotations

from pathlib import Path

from agentloop import worktree


# ── is_main_worktree ────────────────────────────────────────────────────────

def test_is_main_worktree_true_only_for_repo_root(tmp_path):
    root = tmp_path / "sample-e2e"
    root.mkdir()
    assert worktree.is_main_worktree(root, str(root)) is True
    # a linked worktree (under the canonical dir) and a sibling are NOT the main tree
    assert worktree.is_main_worktree(root, str(root / ".worktrees/agent-loop/TEAM-1")) is False
    assert worktree.is_main_worktree(root, str(tmp_path / "sample-e2e-worktrees/TEAM-1")) is False


# ── reclaim_branch: detach a main tree, remove a linked worktree ─────────────

def _record_git(monkeypatch):
    calls = []
    monkeypatch.setattr(worktree.proc, "git", lambda cfg, root, *a, **k: calls.append((str(root), a)) or None)
    return calls


def test_reclaim_detaches_when_branch_held_by_main_tree(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="debug")
    root, wt = Path("/repo"), Path("/repo/.worktrees/agent-loop/TEAM-1")
    monkeypatch.setattr(worktree, "worktree_holding_branch", lambda r, b: "/repo")  # the main tree
    monkeypatch.setattr(worktree, "is_main_worktree", lambda r, held: True)
    calls = _record_git(monkeypatch)

    worktree.reclaim_branch(cfg, root, wt, "john/team-1-x")

    subcmds = [a for _r, a in calls]
    assert ("worktree", "prune") in subcmds
    assert ("checkout", "--detach") in subcmds                       # detached, not removed
    assert all(not (a[0] == "worktree" and a[1] == "remove") for a in subcmds)
    # detach ran against the held main-tree path, not the repo root arg
    assert any(r == "/repo" and a == ("checkout", "--detach") for r, a in calls)


def test_reclaim_removes_when_branch_held_by_linked_worktree(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="debug")
    root, wt = Path("/repo"), Path("/repo/.worktrees/agent-loop/TEAM-1")
    held = "/repo-worktrees/TEAM-1"
    monkeypatch.setattr(worktree, "worktree_holding_branch", lambda r, b: held)
    monkeypatch.setattr(worktree, "is_main_worktree", lambda r, h: False)
    calls = _record_git(monkeypatch)

    worktree.reclaim_branch(cfg, root, wt, "john/team-1-x")

    subcmds = [a for _r, a in calls]
    assert ("worktree", "remove", "--force", held) in subcmds
    assert ("checkout", "--detach") not in subcmds


def test_reclaim_is_noop_when_branch_free_or_at_target(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="debug")
    root, wt = Path("/repo"), Path("/repo/.worktrees/agent-loop/TEAM-1")
    calls = _record_git(monkeypatch)

    monkeypatch.setattr(worktree, "worktree_holding_branch", lambda r, b: None)  # free
    worktree.reclaim_branch(cfg, root, wt, "john/team-1-x")
    monkeypatch.setattr(worktree, "worktree_holding_branch", lambda r, b: str(wt))  # already at target
    worktree.reclaim_branch(cfg, root, wt, "john/team-1-x")

    # only the two prunes ran — nothing evicted in either case
    assert [a for _r, a in calls] == [("worktree", "prune"), ("worktree", "prune")]


# ── cleanup_worktree sweeps the canonical in-repo path ──────────────────────

def test_cleanup_worktree_sweeps_canonical_inrepo_path(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="dispatch")
    root = cfg.repos["sample-e2e"]
    key = "al-linear-111111111111111111111111"
    (root / cfg.worktree_rel / key).mkdir(parents=True)

    monkeypatch.setattr(worktree, "_current_branch", lambda wt: f"john/{key}-x")
    calls = _record_git(monkeypatch)

    worktree.cleanup_worktree(cfg, key)

    removed = [a[3] for _r, a in calls if a[:2] == ("worktree", "remove")]
    assert removed == [str(root / cfg.worktree_rel / key)]
