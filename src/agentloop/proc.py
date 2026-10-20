"""proc — subprocess wrappers and the tmux live-session lock.

Centralizes the "never let a spawned child inherit our stdin" rule (the bash FD-3 dance)
by defaulting every child to ``stdin=DEVNULL``, and ports the ``run()`` DRY_RUN gate for
mutating commands (git/tmux).
"""

from __future__ import annotations

import subprocess

from agentloop import journal
from agentloop.identity import validate_session_key


def run_gated(cfg, argv, *, check: bool = False, **kwargs) -> subprocess.CompletedProcess | None:
    """DRY_RUN gate for a mutating command (ports ``run()`` at lib:41).

    When ``cfg.dry_run`` is set, log ``DRY-RUN would: <argv>`` and execute nothing
    (returns ``None``). Otherwise run it. Children never inherit our stdin.
    """
    if cfg.dry_run:
        journal.log(cfg, "DRY-RUN would: " + " ".join(str(a) for a in argv))
        return None
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(argv, check=check, **kwargs)


def capture(argv, **kwargs) -> subprocess.CompletedProcess:
    """Run a read-only command, capturing stdout as text. Never runs through the DRY_RUN
    gate (reads are always safe) and never inherits our stdin.

    Uses explicit ``stdout``/``stderr`` rather than ``capture_output`` so callers can override
    either (e.g. keep stderr silenced) — ``capture_output=True`` combined with an explicit
    ``stderr`` raises ValueError. stderr defaults to DEVNULL, matching the bash call sites' ``2>/dev/null``.
    """
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    kwargs.setdefault("stdout", subprocess.PIPE)
    kwargs.setdefault("stderr", subprocess.DEVNULL)
    kwargs.setdefault("text", True)
    return subprocess.run(argv, **kwargs)


def succeeded(result: subprocess.CompletedProcess | None) -> bool:
    """Interpret a gated command result: a dry-run (``None``) counts as success — matching bash,
    where ``run`` in DRY_RUN mode runs ``log`` (exit 0) instead of the real command."""
    return result is None or result.returncode == 0


def has_session(key: str) -> bool:
    """``tmux has-session -t <key>`` -> True if a live session exists.

    This is local process deduplication; it does not coordinate multiple machines.
    """
    validate_session_key(key)
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", key],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def git(cfg, root, *args, gated: bool = True, **kwargs) -> subprocess.CompletedProcess | None:
    """Convenience for ``git -C <root> <args...>``. Gated by DRY_RUN when it mutates."""
    argv = ["git", "-C", str(root), *[str(a) for a in args]]
    if gated:
        return run_gated(cfg, argv, **kwargs)
    kwargs.setdefault("stdin", subprocess.DEVNULL)
    return subprocess.run(argv, **kwargs)
