"""Read-only local preflight checks for a configured Agent Loop installation."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def _run(argv: list[str]) -> tuple[bool, str]:
    """Run a read-only command and retain a concise failure explanation."""
    try:
        result = subprocess.run(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except OSError as error:
        return False, str(error)
    if result.returncode == 0:
        return True, ""
    detail = (result.stderr or "").strip().replace("\n", " ")
    return False, detail[:240] or f"command exited {result.returncode}"


def local_checks(cfg) -> list[Check]:
    """Check executable and Git prerequisites without mutating local or remote state."""
    checks = [
        Check(f"tool:{tool}", bool(shutil.which(tool)), "found" if shutil.which(tool) else "not on PATH")
        for tool in ("git", "tmux", "gh", cfg.harness_bin)
    ]
    for label, root in cfg.repos.items():
        root = Path(root)
        if not root.is_dir():
            checks.append(Check(f"repo:{label}", False, f"missing directory: {root}"))
            continue
        is_worktree, detail = _run(["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"])
        if not is_worktree:
            checks.append(Check(f"repo:{label}", False, f"not a Git work tree: {root} ({detail})"))
            continue
        remote = cfg.repo_remote(label)
        base = cfg.repo_base_branch(label)
        has_remote, detail = _run(["git", "-C", str(root), "remote", "get-url", remote])
        if not has_remote:
            checks.append(Check(f"repo:{label}:remote", False, f"could not read remote {remote!r}: {detail}"))
            continue
        ref = f"refs/remotes/{remote}/{base}"
        has_base, detail = _run(["git", "-C", str(root), "show-ref", "--verify", "--quiet", ref])
        checks.append(
            Check(
                f"repo:{label}:base",
                has_base,
                f"found {remote}/{base}"
                if has_base
                else f"could not verify {remote}/{base}: {detail}; run git fetch {remote}",
            )
        )
    return checks
