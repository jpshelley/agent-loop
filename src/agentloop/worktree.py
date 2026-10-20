"""worktree — remove the loop's local worktrees + branches.

Ports ``cleanup_worktree`` (lib:305-320) and the repository duplicate-path dedup that both it and
declutter's orphan GC rely on. It touches only Agent Loop's configured worktree directory — never
the remote (a pushed branch/PR is the durable checkpoint).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from agentloop import identity, journal, proc

def worktree_path(cfg, root: Path, key: str) -> Path:
    """Resolve an execution directory without allowing traversal or symlink escapes."""
    identity.validate_execution_key(key)
    path = root / cfg.worktree_rel / key
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError(f"worktree path escapes the repository: {path}")
    return path


def branch_key(key: str) -> str:
    """Git branch token derived only from a validated local execution key."""
    return identity.validate_execution_key(key).lower()


def unique_repo_roots(cfg) -> list[Path]:
    """The repo map can point several labels at one checkout (aliases). Return each
    distinct checkout root once, preserving order — mirrors the bash ``seen`` associative array."""
    seen: set[Path] = set()
    roots: list[Path] = []
    for root in cfg.repos.values():
        if root in seen:
            continue
        seen.add(root)
        roots.append(root)
    return roots


def _current_branch(root: Path) -> str:
    r = proc.capture(["git", "-C", str(root), "rev-parse", "--abbrev-ref", "HEAD"])
    return r.stdout.strip() if r.returncode == 0 else ""


def branch_matches_key(branch: str, key: str) -> bool:
    """Whether a branch belongs to this loop issue under old or new branch conventions."""
    if not identity.is_execution_key(key):
        return False
    lower = branch_key(key)
    upper = key.upper()
    return (
        branch == lower
        or branch.startswith(f"{lower}-")
        or branch.endswith(f"/{lower}")
        or f"/{lower}-" in branch
        or branch == upper
        or branch.startswith(f"{upper}-")
        or branch.endswith(f"/{upper}")
        or f"/{upper}-" in branch
    )


def _worktree_branches(root: Path) -> list[tuple[Path, str]]:
    r = proc.capture(
        ["git", "-C", str(root), "worktree", "list", "--porcelain"], stderr=subprocess.DEVNULL
    )
    if r.returncode != 0:
        return []
    rows: list[tuple[Path, str]] = []
    wt: Path | None = None
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            wt = Path(line[len("worktree "):])
        elif line.startswith("branch refs/heads/") and wt is not None:
            rows.append((wt, line[len("branch refs/heads/"):]))
    return rows


def worktree_holding_branch(root: Path, branch: str) -> str | None:
    """Path of the worktree in ``root`` that currently has ``branch`` checked out, or None. Lets
    the debug fix path reclaim a PR branch the build skill left checked out in a worktree OUTSIDE
    the loop's WORKTREE_REL (e.g. ``<repo>-worktrees/``), which cleanup_worktree can't see. Mirrors
    debug.sh's ``git worktree list --porcelain`` + awk. Note: ``git worktree list`` includes the
    repo's MAIN working tree as its first entry, so a branch checked out there is reported too."""
    for wt, br in _worktree_branches(root):
        if br == branch:
            return str(wt)
    return None


def is_main_worktree(root: Path, path: str) -> bool:
    """True if ``path`` is ``root``'s MAIN working tree (the repo root itself), not a linked
    worktree. Git refuses ``worktree remove`` on a main tree, so the reclaim path must detach it
    instead. cfg.repos values are always the primary checkout, so a path-equality check suffices."""
    try:
        return Path(path).resolve() == Path(root).resolve()
    except OSError:
        return False


def reclaim_branch(cfg, root: Path, target_wt: Path, branch: str) -> None:
    """Free ``branch`` so it can be checked out at ``target_wt``, then leave the add to the caller.

    Prune dead worktrees, then if some OTHER tree still holds ``branch``, evict it: a linked
    worktree is ``worktree remove``d; the repo's MAIN working tree can't be (git refuses), so it is
    detached at HEAD instead — non-destructive (no file changes, any uncommitted work is kept), it
    just frees the branch name. No-op if the branch is free or already at ``target_wt``.

    The main-tree case happens when a skill session worked the ticket directly in the product
    repo's primary checkout instead of a loop worktree; without this the loop would stall every
    tick (``fatal: '<path>' is a main working tree``).
    """
    proc.git(cfg, root, "worktree", "prune")
    held = worktree_holding_branch(root, branch)
    if not held or held == str(target_wt):
        return
    if is_main_worktree(root, held):
        journal.log(
            cfg,
            f"branch {branch} is checked out in the MAIN working tree at {held} — loop work landed "
            f"on the main checkout, not a worktree; detaching HEAD there to free the branch",
        )
        proc.git(cfg, Path(held), "checkout", "--detach")
    else:
        journal.log(cfg, f"branch {branch} already checked out at {held} (stale skill worktree) — reclaiming it")
        proc.git(cfg, root, "worktree", "remove", "--force", held)


def cleanup_worktree(cfg, key: str) -> None:
    """Remove the loop's LOCAL worktree + its branch for a key, across repos (lib:309-320).

    Used by declutter to GC a recovered zombie, and by dispatch to make a re-claim idempotent
    (clear a stale leftover before branching fresh).
    """
    identity.validate_execution_key(key)
    for root in unique_repo_roots(cfg):
        wt = root / cfg.worktree_rel / key
        if wt.is_symlink() or not wt.resolve().is_relative_to(root.resolve()) or not wt.is_dir():
            continue
        br = _current_branch(wt)
        if not branch_matches_key(br, key):
            journal.log(cfg, f"{key}: worktree branch does not match execution identity — retaining {wt}")
            continue
        removed = proc.git(cfg, root, "worktree", "remove", "--force", str(wt))
        if not proc.succeeded(removed):
            journal.log(cfg, f"{key}: could not remove worktree — retaining its branch")
            continue
        if br and br != "HEAD":
            # `|| true` in bash — a failed branch delete is non-fatal.
            proc.git(cfg, root, "branch", "-D", br, check=False)
