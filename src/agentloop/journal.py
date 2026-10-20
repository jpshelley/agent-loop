"""journal — logging, the DRY_RUN gate, per-session log paths, and the audit ledger.

Ports ``log()``, ``run()``, ``session_log()`` and ``audit()`` from ``agent-loop-lib.sh``.
Named ``journal`` (not ``logging``) so it doesn't shadow the stdlib module.
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

from agentloop.identity import validate_session_key


def log(cfg, msg: str) -> None:
    """Timestamped, loop-tagged line to stderr (lib:40): ``HH:MM:SS <tag>: <msg>``."""
    print(f"{datetime.now().strftime('%H:%M:%S')} {cfg.log_tag}: {msg}", file=sys.stderr)


def session_log(cfg, key: str) -> Path:
    """Per-session log path ``$LOG_DIR/<tag>/<KEY>-<timestamp>.log`` (lib:47-51).

    One file per spawn (never an append), foldered by the spawning loop. Creates the
    folder and returns the path; callers point ``tmux pipe-pane`` at it.
    """
    validate_session_key(key)
    d = cfg.log_dir / cfg.log_tag
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{key}-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"


def audit(cfg, msg: str) -> None:
    """Append to the loop's daily audit ledger AND echo via :func:`log` (lib:57-63).

    For declutter (which mutates state without spawning a pipe-pane session) this is the
    only durable record of what it touched. Honors DRY_RUN: previews to stderr, writes nothing.
    """
    if cfg.dry_run:
        log(cfg, msg)
        return
    d = cfg.log_dir / cfg.log_tag
    d.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with (d / f"{datetime.now().strftime('%Y%m%d')}.log").open("a") as fh:
        fh.write(f"{stamp} {msg}\n")
    log(cfg, msg)
