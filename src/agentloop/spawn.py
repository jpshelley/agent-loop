"""spawn — build the harness command string and launch it in a detached tmux session.

The command reaches ``tmux new-session`` as one string and tmux runs it through a
shell. Harness adapters quote the executable and prompt safely. Configured flags
remain a trusted operator-supplied shell fragment for backward compatibility.
"""

from __future__ import annotations

from agentloop import harness
from agentloop import journal
from agentloop.journal import session_log
from agentloop.identity import validate_session_key
import subprocess


def build_spawn_cmd(cfg, prompt: str) -> str:
    """Delegate shell construction to the selected harness adapter."""
    return harness.resolve(cfg).build_headless_cmd(prompt)


def _dry_run_preview(cfg, key: str, cwd, prompt: str) -> str:
    """The human-readable DRY-RUN line. Matches the bash format character-for-character (the
    prompt shown in double quotes inside the single-quoted command) so the parity harness can
    diff it (dispatch:186, debug:168, deploy:118)."""
    return harness.resolve(cfg).dry_run_preview(cfg, key, cwd, prompt)


def _session_exists(key: str) -> bool:
    return (
        subprocess.run(
            ["tmux", "has-session", "-t", key],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode
        == 0
    )


def tmux_spawn(cfg, key: str, cwd, prompt: str) -> bool:
    """Spawn a detached tmux session running ``<harness> -p`` and tee its output to a per-session
    log via ``pipe-pane``. Honors DRY_RUN (previews, spawns nothing). Returns True (spawned or
    previewed)."""
    validate_session_key(key)
    if cfg.dry_run:
        journal.log(cfg, _dry_run_preview(cfg, key, cwd, prompt))
        return True
    logf = session_log(cfg, key)
    cmd = build_spawn_cmd(cfg, prompt)
    # List form: `cmd` reaches tmux as ONE argv element, identical to the bash quoted string.
    try:
        created = subprocess.run(
            ["tmux", "new-session", "-d", "-s", key, "-c", str(cwd), cmd],
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        journal.log(cfg, f"{key}: tmux new-session failed ({exc})")
        return False
    if created.returncode != 0:
        journal.log(cfg, f"{key}: tmux new-session failed with exit {created.returncode}")
        return False
    # pipe-pane taps the pane's output to the log without redirecting stdout (so `tmux attach`
    # still shows it live). `-p` exits when the task is done -> the session self-closes.
    try:
        piped = subprocess.run(
            ["tmux", "pipe-pane", "-o", "-t", key, f"cat >> '{logf}'"],
            stdin=subprocess.DEVNULL,
        )
    except OSError as exc:
        if not _session_exists(key):
            journal.log(cfg, f"{key}: tmux pipe-pane failed and session is not live ({exc})")
            return False
        journal.log(cfg, f"{key}: tmux pipe-pane failed; session is live but output is not logged ({exc})")
        return True
    if piped.returncode != 0:
        if not _session_exists(key):
            journal.log(cfg, f"{key}: tmux pipe-pane failed with exit {piped.returncode}; session is not live")
            return False
        journal.log(cfg, f"{key}: tmux pipe-pane failed with exit {piped.returncode}; session is live")
        return True
    journal.log(cfg, f"{key}: spawned (log: {logf})")
    return True
