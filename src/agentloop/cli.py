"""cli — argparse subcommands and the interval runner (replaces ``loop.sh``).

    agent-loop dispatch [flags]      # one tick (like ./dispatch.sh)
    agent-loop develop --interval 120 [flags]  # loop forever (like loop.sh develop)

Flags override env vars, which override built-in defaults. ``LINEAR_API_KEY`` stays in the
environment (a secret, not a flag). Loads ``~/.agent-loop/env`` before the first tick so the
loop has the key regardless of tmux's env-inheritance quirk.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from agentloop import doctor, journal, tracker
from agentloop.config import Config
from agentloop.constants import APP_NAME, config_dir
from agentloop.harness import deploy_default_flags
from agentloop.loops import declutter, deploy, design, develop, dispatch, debug
from agentloop import onboarding

_LOOPS = {
    "dispatch": dispatch.run_tick,
    "develop": develop.run_tick,
    "debug": debug.run_tick,
    "deploy": deploy.run_tick,
    "declutter": declutter.run_tick,
    "design": design.run_tick,
}


def _load_env_file(path: Path, environ: dict) -> None:
    """Source a bash-style ``env`` file and merge the result into ``environ``.

    Reproduces ``loop.sh:30`` (``[ -r file ] && source file``) faithfully by letting bash evaluate
    the file, so ``export FOO=$(...)`` etc. still work. No-op if the file isn't readable or bash
    isn't available.
    """
    if not os.access(path, os.R_OK):
        return
    try:
        out = subprocess.run(
            ["bash", "-c", f'set -a; . "{path}"; env -0'],
            capture_output=True,
            check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return
    for entry in out.split(b"\x00"):
        if not entry:
            continue
        k, _, v = entry.partition(b"=")
        environ[k.decode()] = v.decode()


def _add_common_flags(sp: argparse.ArgumentParser) -> None:
    # All flags default to None so only values the user passes override the env layer
    # (flag > env > default). --dry-run/--debug use store_const so they never clobber env.
    sp.add_argument("--user")
    sp.add_argument("--branch-prefix")
    sp.add_argument("-c", "--concurrency", type=int)
    sp.add_argument("--project-label")
    sp.add_argument("--queue-state")
    sp.add_argument("--todo-state")
    sp.add_argument("--active-state")
    sp.add_argument("--approved-state")
    sp.add_argument("--needs-input-state")
    sp.add_argument("--blocked-label")
    sp.add_argument("--blocked-cooldown", type=int)
    sp.add_argument("--declutter-grace", type=int)
    sp.add_argument("--assignee")
    sp.add_argument("--workspace-dir")
    sp.add_argument("--harness")
    sp.add_argument("--tracker", help="Issue tracker adapter (github or linear).")
    sp.add_argument("--harness-flags")
    sp.add_argument("--claude-flags")
    sp.add_argument("--log-dir")
    sp.add_argument("--dry-run", action="store_const", const=True, default=None)
    sp.add_argument("--debug", action="store_const", const=True, default=None)
    sp.add_argument(
        "--interval",
        type=int,
        help="Run this tick every N seconds, forever (replaces loop.sh). Omit for a single tick.",
    )
    sp.add_argument(
        "--daily-at",
        metavar="HH:MM",
        help="Run one tick per day at this local clock time (e.g. 00:00), forever. Sleeps until the "
        "next occurrence — one wake per day, exact regardless of when it was launched — so there is "
        "no polling. Mutually exclusive with --interval. Meant for the design loop's nightly batch.",
    )


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="Tracker-driven tmux orchestration loop (dispatch/develop/debug/deploy/declutter/design).",
    )
    sub = p.add_subparsers(dest="which", required=True)
    for name in _LOOPS:
        _add_common_flags(sub.add_parser(name, help=f"the {name} loop"))
    _add_common_flags(sub.add_parser("doctor", help="validate the selected tracker scope without mutations"))
    init = sub.add_parser("init", help="create editable local configuration without credentials")
    init.add_argument("--tracker", choices=("github", "linear"), default="github")
    init.add_argument("--harness", choices=("claude", "codex", "cursor", "copilot"), default="claude")
    init.add_argument(
        "--repo", action="append", required=True, metavar="LABEL=CHECKOUT_PATH",
        help="local checkout to map from a repo:LABEL issue label; repeat for each repository",
    )
    init.add_argument("--config-dir", type=Path, help="directory to create (defaults to ~/.agent-loop)")
    init.add_argument("--replace", action="store_true", help="overwrite existing env and repos.toml")
    return p


def _parse_hhmm(value: str) -> tuple[int, int]:
    """Parse a ``HH:MM`` 24-hour local clock time, raising ValueError on anything malformed."""
    parts = value.split(":")
    if len(parts) != 2:
        raise ValueError(f"expected HH:MM, got {value!r}")
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError(f"{value!r} is not a valid 24-hour time")
    return hour, minute


def _seconds_until_daily(value: str, now: datetime) -> float:
    """Real seconds from ``now`` (naive local) until the next local occurrence of ``HH:MM``.

    Resolves both wall-clock times to their own local UTC offset before differencing, so the wait
    honors a 23-/25-hour DST day instead of assuming every day is 24h. Without this, a process
    sleeping toward the next local midnight wakes an hour early/late on the two transition days.
    """
    hour, minute = _parse_hhmm(value)
    base = now.replace(tzinfo=None)  # treat the input as a naive local wall-clock time
    target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= base:
        target += timedelta(days=1)
    # astimezone() on a naive datetime attaches the local offset in effect on THAT date, so a
    # transition falling between now and target is counted as the real elapsed time.
    return (target.astimezone() - base.astimezone()).total_seconds()


def _tick_once(which: str, run_tick, cfg) -> None:
    """Run one tick, logging (not raising) any failure so the daemon loops keep going."""
    try:
        run_tick(cfg)
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001 — mirror bash's "log and continue" on any failure
        journal.log(cfg, f"{which} tick exited non-zero — continuing ({type(e).__name__}: {e})")


def _single_tick(which: str, run_tick, cfg) -> int:
    """Run exactly one tick and return the process exit code (1 on failure). Mirrors ./dispatch.sh
    (set -e): the specific cause is already logged, so avoid a noisy traceback."""
    try:
        run_tick(cfg)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        journal.log(cfg, f"{which} tick failed: {type(e).__name__}: {e}")
        return 1
    return 0


def _build_config(which: str, args) -> Config:
    environ = dict(os.environ)
    home = Path.home()
    _load_env_file(config_dir(home) / "env", environ)
    # Mirror the loaded env-file secrets back so finalize() (LINEAR_API_KEY) and spawned children
    # (claude/gh/git) see them, regardless of tmux's env-inheritance quirk. Do this BEFORE the
    # deploy synthetic default so that default never leaks into the process environment.
    os.environ.update(environ)
    # Deploy defaults the current harness flags in the ENV layer (before from_env) only when
    # neither generic nor Claude-specific flag env var is set. An explicit CLI flag still
    # overrides this later in apply_flags().
    harness_name = getattr(args, "harness", None) or environ.get("AGENT_LOOP_HARNESS", "claude")
    if getattr(args, "harness", None) is not None:
        environ["AGENT_LOOP_HARNESS"] = args.harness
    if (
        which == "deploy"
        and "AGENT_LOOP_HARNESS_FLAGS" not in environ
        and "AGENT_LOOP_CLAUDE_FLAGS" not in environ
    ):
        default_flags = deploy_default_flags(harness_name)
        if default_flags is not None:
            environ["AGENT_LOOP_HARNESS_FLAGS"] = default_flags

    cfg = Config.from_env(environ).for_loop(which)
    cfg.apply_flags(args)
    cfg.finalize(environ, create_log_dir=which != "doctor")
    return cfg


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    which = args.which
    if which == "init":
        try:
            repositories = [onboarding.parse_repo_spec(value) for value in args.repo]
            directory = args.config_dir or config_dir(Path.home())
            env_path, repos_path = onboarding.initialize(
                directory, tracker=args.tracker, harness=args.harness,
                repositories=repositories, replace=args.replace,
            )
        except (ValueError, FileExistsError) as error:
            raise SystemExit(f"init failed: {error}") from error
        print(f"created {env_path} and {repos_path}")
        print("Set the credential placeholders, then run: agent-loop doctor")
        return 0
    daily_at = getattr(args, "daily_at", None)
    if daily_at is not None:
        if args.interval is not None:
            raise SystemExit("pass either --interval or --daily-at, not both")
        try:
            _parse_hhmm(daily_at)  # validate before building config / entering the loop
        except ValueError as e:
            raise SystemExit(f"--daily-at {e}")
    cfg = _build_config(which, args)
    if which == "doctor":
        checks = doctor.local_checks(cfg)
        for check in checks:
            state = "ok" if check.ok else "failed"
            print(f"{state}: {check.name}: {check.detail}")
        try:
            tracker.resolve(cfg).resolve_scope()
        except Exception as error:  # validation must return a nonzero status for automation
            print(f"doctor failed: {type(error).__name__}: {error}", file=sys.stderr)
            return 1
        if not all(check.ok for check in checks):
            return 1
        print(f"tracker '{cfg.tracker}' scope is readable and its configured workflow is valid")
        return 0
    run_tick = _LOOPS[which]

    if daily_at is not None:
        if cfg.dry_run:
            # A DRY_RUN=1 preview must run immediately, never sleep to the target — otherwise the
            # documented "preview runs immediately" would hang until midnight. Same as any other
            # single-tick preview.
            return _single_tick(which, run_tick, cfg)
        # Daily scheduler: sleep to the next local HH:MM, run one tick, repeat. One wake per day,
        # so there is no busy poll. A failed tick is logged and the loop continues.
        print(
            f"loop: {which} daily at {daily_at} local (started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
            file=sys.stderr,
        )
        while True:
            time.sleep(_seconds_until_daily(daily_at, datetime.now()))
            _tick_once(which, run_tick, cfg)

    if args.interval is None:
        return _single_tick(which, run_tick, cfg)

    # Interval runner (replaces loop.sh): a failed tick is logged and the loop continues — a
    # transient Linear/network hiccup won't kill it (loop.sh:39-42).
    interval = args.interval
    print(
        f"loop: {which} every {interval}s (started {datetime.now().strftime('%Y-%m-%d %H:%M:%S')})",
        file=sys.stderr,
    )
    while True:
        _tick_once(which, run_tick, cfg)
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
