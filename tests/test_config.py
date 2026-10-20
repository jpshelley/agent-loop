"""Config precedence: defaults < env < flags, plus the secret and deploy's flags default."""

from __future__ import annotations

import argparse
import os
import time
from pathlib import Path

import pytest

from datetime import datetime

from agentloop.cli import _build_config, _build_parser, _parse_hhmm, _seconds_until_daily, main
from agentloop.config import Config
from agentloop.loops import deploy


def _args(**kw):
    ns = argparse.Namespace()
    # Every common flag defaults to None (as argparse produces when not passed).
    for name in (
        "user", "branch_prefix", "concurrency", "project_label", "queue_state",
        "todo_state", "active_state", "approved_state", "needs_input_state", "declutter_grace",
        "blocked_label", "blocked_cooldown",
        "tracker", "harness", "harness_flags", "assignee", "workspace_dir", "claude_flags", "log_dir",
        "dry_run", "debug", "interval",
    ):
        setattr(ns, name, None)
    for k, v in kw.items():
        setattr(ns, k, v)
    return ns


def test_defaults():
    cfg = Config.from_env({"HOME": "/home/x"})
    assert cfg.tracker == "linear"
    assert cfg.harness == "claude"
    assert cfg.project_label == "loop"
    assert cfg.state_todo == "Todo"
    assert cfg.state_active == "In Progress"
    assert cfg.blocked_label == "loop-blocked"
    assert cfg.blocked_cooldown_seconds == 3600
    assert cfg.concurrency == 3
    assert cfg.branch_prefix == "loop/"  # no USER in this env -> documented fallback
    assert cfg.claude_flags == "--permission-mode auto"
    assert str(cfg.workspace_dir) == "/home/x/Development"



def test_tracker_env_is_read_before_validation():
    cfg = Config.from_env({"HOME": "/home/x", "AGENT_LOOP_TRACKER": "future-tracker"})
    assert cfg.tracker == "future-tracker"


def test_tracker_flag_beats_env(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("AGENT_LOOP_TRACKER", "future-tracker")
    args = _build_parser().parse_args(["dispatch", "--tracker", "linear"])
    assert _build_config("dispatch", args).tracker == "linear"


def test_unsupported_tracker_is_reported_before_missing_credentials():
    env = {"HOME": "/home/x", "AGENT_LOOP_TRACKER": "future-tracker"}
    cfg = Config.from_env(env)
    with pytest.raises(SystemExit, match="unsupported tracker 'future-tracker'"):
        cfg.finalize(env)

def test_codex_harness_defaults():
    cfg = Config.from_env(
        {
            "HOME": "/home/x",
            "AGENT_LOOP_HARNESS": "codex",
        }
    )
    assert cfg.harness == "codex"
    assert cfg.harness_bin == "codex"
    assert cfg.harness_flags == "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"


def test_codex_harness_ignores_legacy_claude_aliases():
    cfg = Config.from_env(
        {
            "HOME": "/home/x",
            "AGENT_LOOP_HARNESS": "codex",
            "CLAUDE_BIN": "claude-stage",
            "AGENT_LOOP_CLAUDE_FLAGS": "--permission-mode auto",
        }
    )
    assert cfg.harness == "codex"
    assert cfg.harness_bin == "codex"
    assert cfg.harness_flags == "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"


def test_env_override():
    cfg = Config.from_env(
        {
            "HOME": "/home/x",
            "AGENT_LOOP_CONCURRENCY": "7",
            "DRY_RUN": "1",
            "AGENT_LOOP_HARNESS": "claude",
        }
    )
    assert cfg.concurrency == 7
    assert cfg.dry_run is True


def test_agent_loop_environment_is_canonical():
    cfg = Config.from_env(
        {
            "HOME": "/home/x",
            "AGENT_LOOP_CONCURRENCY": "7",
            "AGENT_LOOP_TRACKER": "github",
        }
    )
    assert cfg.concurrency == 7
    assert cfg.tracker == "github"


def test_flag_beats_env():
    cfg = Config.from_env({"HOME": "/home/x", "AGENT_LOOP_CONCURRENCY": "7"})
    cfg.apply_flags(_args(concurrency=9))
    assert cfg.concurrency == 9


def test_blocked_handoff_flags_beat_env():
    cfg = Config.from_env(
        {"HOME": "/home/x", "AGENT_LOOP_BLOCKED_LABEL": "parked", "AGENT_LOOP_BLOCKED_COOLDOWN": "120"}
    )
    cfg.apply_flags(_args(blocked_label="blocked", blocked_cooldown=30))

    assert cfg.blocked_label == "blocked"
    assert cfg.blocked_cooldown_seconds == 30


def test_branch_prefix_defaults_to_the_invoking_user():
    # Loop branches land in shared repos, so they carry the operator's name by default
    # rather than a hardcoded one.
    assert Config.from_env({"HOME": "/h", "USER": "alice"}).branch_prefix == "alice/"
    # LOGNAME covers shells that set it instead of USER.
    assert Config.from_env({"HOME": "/h", "LOGNAME": "bob"}).branch_prefix == "bob/"
    # An explicit env var still wins over the derived default.
    assert (
        Config.from_env(
            {"HOME": "/h", "USER": "alice", "AGENT_LOOP_BRANCH_PREFIX": "team/"}
        ).branch_prefix
        == "team/"
    )


def test_user_flag_sets_branch_prefix():
    cfg = Config.from_env({"HOME": "/home/x"})
    cfg.apply_flags(_args(user="alice"))
    assert cfg.branch_prefix == "alice/"
    # A trailing slash is normalized, not doubled.
    cfg.apply_flags(_args(user="bob/"))
    assert cfg.branch_prefix == "bob/"


def test_harness_flag_sets_harness():
    cfg = Config.from_env({"HOME": "/home/x"})
    cfg.apply_flags(_args(harness="codex"))
    assert cfg.harness == "codex"


def test_dry_run_flag_does_not_clobber_env():
    # Env sets DRY_RUN=1; not passing --dry-run must leave it on (store_const default None).
    cfg = Config.from_env({"HOME": "/home/x", "DRY_RUN": "1"})
    cfg.apply_flags(_args(dry_run=None))
    assert cfg.dry_run is True


_REPOS_TOML = """
[repos]
api   = "org/api"
web   = "org/web"
infra = "/absolute/infra"

[aliases]
legacy-api = "api"

[settings]
default_repo = "api"
design_repo  = "web"
infra_repos  = ["infra"]
"""


def _cfg_with_repos(tmp_path, extra_env=None, toml=_REPOS_TOML):
    """Finalize a Config against a written-out repo map.

    These tests pass an explicit environ, so they set the repo-map path themselves
    rather than inheriting the suite-wide autouse fixture.
    """
    repos_file = tmp_path / "repos.toml"
    repos_file.write_text(toml)
    env = {
        "HOME": str(tmp_path),
        "AGENT_LOOP_LOG_DIR": str(tmp_path / "logs"),
        "AGENT_LOOP_REPOS_FILE": str(repos_file),
        **(extra_env or {}),
    }
    return Config.from_env(env).finalize({**env, "LINEAR_API_KEY": "k"})


def test_repo_map_loads_paths_aliases_and_settings(tmp_path):
    cfg = _cfg_with_repos(tmp_path)
    ws = cfg.workspace_dir
    # Relative entries resolve against the workspace dir; absolute ones pass through.
    assert cfg.repo_root("api") == ws / "org" / "api"
    assert cfg.repo_root("web") == ws / "org" / "web"
    assert cfg.repo_root("infra") == Path("/absolute/infra")
    # An alias routes to its target's checkout without being a repo in its own right.
    assert cfg.collapse_repo("legacy-api") == "api"
    assert "legacy-api" not in cfg.repos
    # An unaliased label passes through so the caller's not-found handling still fires.
    assert cfg.collapse_repo("web") == "web"
    assert cfg.default_repo == "api"
    assert cfg.design_repo == "web"
    assert cfg.infra_repos == frozenset({"infra"})


def test_workspace_dir_is_the_base_for_relative_repo_paths(tmp_path):
    cfg = _cfg_with_repos(tmp_path, {"AGENT_LOOP_WORKSPACE_DIR": str(tmp_path / "elsewhere")})
    assert cfg.repo_root("api") == tmp_path / "elsewhere" / "org" / "api"


def test_missing_repo_map_exits_with_a_pointer_to_the_format(tmp_path):
    # No built-in map: refusing to start beats routing work into a guessed repository.
    env = {
        "HOME": str(tmp_path),
        "AGENT_LOOP_LOG_DIR": str(tmp_path / "logs"),
        "AGENT_LOOP_REPOS_FILE": str(tmp_path / "absent.toml"),
    }
    with pytest.raises(SystemExit, match="no repo map at"):
        Config.from_env(env).finalize({**env, "LINEAR_API_KEY": "k"})


def test_unknown_repo_label_raises_with_label_name(tmp_path):
    cfg = _cfg_with_repos(tmp_path)
    with pytest.raises(ValueError, match="unknown-label"):
        cfg.repo_root("unknown-label")


def test_legacy_repo_maps_keep_origin_and_main_defaults(tmp_path):
    cfg = _cfg_with_repos(tmp_path)
    assert cfg.repo_remotes == {"api": "origin", "web": "origin", "infra": "origin"}
    assert cfg.repo_base_branches == {"api": "main", "web": "main", "infra": "main"}
    assert cfg.repo_remote("api") == "origin"
    assert cfg.repo_base_branch("api") == "main"


def test_config_loads_repo_git_targets_and_collapses_aliases(tmp_path):
    cfg = _cfg_with_repos(
        tmp_path,
        toml='[repos.api]\npath = "org/api"\nremote = "upstream"\nbase_branch = "release/next"\n'
        '[aliases]\nlegacy-api = "api"\n',
    )
    assert cfg.repo_remotes == {"api": "upstream"}
    assert cfg.repo_base_branches == {"api": "release/next"}
    assert cfg.repo_remote("legacy-api") == "upstream"
    assert cfg.repo_base_branch("legacy-api") == "release/next"
    copy = cfg.for_loop("develop")
    assert copy.repo_remote("api") == "upstream"
    assert copy.repo_base_branch("api") == "release/next"


@pytest.mark.parametrize("method", ["repo_remote", "repo_base_branch"])
def test_repo_git_target_lookup_requires_a_mapped_label(tmp_path, method):
    cfg = _cfg_with_repos(tmp_path)
    with pytest.raises(ValueError, match="unknown repository label: 'unknown-label'"):
        getattr(cfg, method)("unknown-label")


def test_direct_config_construction_keeps_git_target_defaults():
    cfg = Config(repos={"api": Path("/checkout/api")})
    assert cfg.repo_remote("api") == "origin"
    assert cfg.repo_base_branch("api") == "main"


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    cfg = Config.from_env({"HOME": "/home/x"})
    with pytest.raises(SystemExit):
        cfg.finalize({"HOME": "/home/x"})


def test_github_tracker_needs_no_linear_key_and_uses_explicit_scope(default_repos_file, tmp_path):
    env = {
        "HOME": str(tmp_path),
        "AGENT_LOOP_REPOS_FILE": str(default_repos_file),
        "AGENT_LOOP_TRACKER": "github",
        "AGENT_LOOP_GITHUB_TOKEN": "github-test-token",
        "AGENT_LOOP_GITHUB_OWNER": "travefy",
        "AGENT_LOOP_GITHUB_PROJECT_NUMBER": "7",
        "AGENT_LOOP_GITHUB_REPOSITORIES": "travefy/app,travefy/api",
        "AGENT_LOOP_GITHUB_STATUS_FIELD": "Workflow",
    }

    cfg = Config.from_env(env).finalize(env)

    assert cfg.linear_api_key == ""
    assert cfg.github_token == "github-test-token"
    assert cfg.github_repositories == ("travefy/app", "travefy/api")
    assert cfg.github_status_field == "Workflow"


def test_github_tracker_rejects_missing_explicit_project_scope(default_repos_file, tmp_path):
    env = {
        "HOME": str(tmp_path), "AGENT_LOOP_REPOS_FILE": str(default_repos_file),
        "AGENT_LOOP_TRACKER": "github", "GITHUB_TOKEN": "github-test-token",
    }

    with pytest.raises(SystemExit, match="AGENT_LOOP_GITHUB_OWNER"):
        Config.from_env(env).finalize(env)


def test_doctor_validates_selected_tracker_without_running_a_loop(monkeypatch, capsys):
    cfg = Config(tracker="github")
    calls = []

    class Client:
        def resolve_scope(self):
            calls.append("resolve_scope")
            return True

    monkeypatch.setattr("agentloop.cli._build_config", lambda which, args: cfg)
    monkeypatch.setattr("agentloop.cli.tracker.resolve", lambda selected: Client())
    monkeypatch.setattr("agentloop.cli.doctor.local_checks", lambda selected: [])

    assert main(["doctor", "--tracker", "github"]) == 0
    assert calls == ["resolve_scope"]
    assert "tracker 'github' scope is readable" in capsys.readouterr().out


def test_doctor_does_not_create_the_log_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    repos_file = tmp_path / ".agent-loop" / "repos.toml"
    repos_file.parent.mkdir()
    repos_file.write_text('[repos]\napp = "/tmp/app"\n')
    monkeypatch.setenv("AGENT_LOOP_TRACKER", "github")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_TOKEN", "token")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_OWNER", "owner")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_PROJECT_NUMBER", "1")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_REPOSITORIES", "owner/app")
    monkeypatch.setattr("agentloop.cli.doctor.local_checks", lambda cfg: [])
    monkeypatch.setattr("agentloop.cli.tracker.resolve", lambda cfg: type("C", (), {"resolve_scope": lambda self: True})())

    assert main(["doctor"]) == 0
    assert not (tmp_path / ".agent-loop" / "logs").exists()


def test_doctor_failure_does_not_create_the_log_directory(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    repos_file = tmp_path / ".agent-loop" / "repos.toml"
    repos_file.parent.mkdir()
    repos_file.write_text('[repos]\napp = "/tmp/app"\n')
    monkeypatch.setenv("AGENT_LOOP_TRACKER", "github")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_TOKEN", "token")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_OWNER", "owner")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_PROJECT_NUMBER", "1")
    monkeypatch.setenv("AGENT_LOOP_GITHUB_REPOSITORIES", "owner/app")
    monkeypatch.setattr("agentloop.cli.doctor.local_checks", lambda cfg: [])
    monkeypatch.setattr(
        "agentloop.cli.tracker.resolve",
        lambda cfg: type("C", (), {"resolve_scope": lambda self: (_ for _ in ()).throw(RuntimeError("denied"))})(),
    )

    assert main(["doctor"]) == 1
    assert not (tmp_path / ".agent-loop" / "logs").exists()


def test_generic_harness_flags_env_beats_legacy_env():
    cfg = Config.from_env(
        {
            "HOME": "/home/x",
            "AGENT_LOOP_HARNESS_FLAGS": "--new flags",
            "AGENT_LOOP_CLAUDE_FLAGS": "--old flags",
        }
    )
    assert cfg.harness_flags == "--new flags"
    assert cfg.claude_flags == "--new flags"


def test_deploy_claude_flags_default(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.delenv("AGENT_LOOP_HARNESS_FLAGS", raising=False)
    monkeypatch.delenv("AGENT_LOOP_CLAUDE_FLAGS", raising=False)
    parser = _build_parser()
    # deploy without --claude-flags gets the dontAsk allowlist default.
    cfg = _build_config("deploy", parser.parse_args(["deploy"]))
    assert cfg.claude_flags == deploy.DEPLOY_CLAUDE_FLAGS_DEFAULT
    # dispatch does NOT get it.
    cfg2 = _build_config("dispatch", parser.parse_args(["dispatch"]))
    assert cfg2.claude_flags == "--permission-mode auto"


def test_deploy_codex_flags_default(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.delenv("AGENT_LOOP_HARNESS_FLAGS", raising=False)
    monkeypatch.delenv("AGENT_LOOP_CLAUDE_FLAGS", raising=False)
    parser = _build_parser()
    cfg = _build_config("deploy", parser.parse_args(["deploy", "--harness", "codex"]))
    assert cfg.harness == "codex"
    assert cfg.harness_bin == "codex"
    assert cfg.harness_flags == "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"


def test_deploy_uses_agent_loop_harness_profile(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.setenv("AGENT_LOOP_HARNESS", "cursor")
    monkeypatch.delenv("AGENT_LOOP_HARNESS_FLAGS", raising=False)
    monkeypatch.delenv("AGENT_LOOP_HARNESS_FLAGS", raising=False)
    monkeypatch.delenv("AGENT_LOOP_CLAUDE_FLAGS", raising=False)

    cfg = _build_config("deploy", _build_parser().parse_args(["deploy"]))

    assert cfg.harness == "cursor"
    assert cfg.harness_flags == "--force --output-format text"


def test_deploy_claude_flags_flag_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.delenv("AGENT_LOOP_CLAUDE_FLAGS", raising=False)
    parser = _build_parser()
    cfg = _build_config("deploy", parser.parse_args(["deploy", "--claude-flags", "--custom x"]))
    assert cfg.claude_flags == "--custom x"


def test_generic_harness_flags_flag_overrides_default(monkeypatch, tmp_path):
    monkeypatch.setattr("agentloop.cli.Path.home", lambda: tmp_path)
    monkeypatch.setenv("LINEAR_API_KEY", "k")
    monkeypatch.delenv("AGENT_LOOP_HARNESS_FLAGS", raising=False)
    monkeypatch.delenv("AGENT_LOOP_CLAUDE_FLAGS", raising=False)
    parser = _build_parser()
    cfg = _build_config("deploy", parser.parse_args(["deploy", "--harness-flags", "--custom y"]))
    assert cfg.harness_flags == "--custom y"


def test_unsupported_harness_raises(monkeypatch):
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="unsupported harness"):
        Config.from_env({"HOME": "/home/x", "AGENT_LOOP_HARNESS": "nope"})


# ── --daily-at scheduler (the design loop's once-a-day trigger) ──────────────


def test_parser_accepts_daily_at():
    args = _build_parser().parse_args(["design", "--daily-at", "00:00", "--user", "john"])
    assert args.daily_at == "00:00"
    assert args.interval is None


@pytest.mark.parametrize("value", ["00:00", "23:59", "06:30", "9:05"])
def test_parse_hhmm_accepts_valid_times(value):
    hour, minute = _parse_hhmm(value)
    assert 0 <= hour < 24 and 0 <= minute < 60


@pytest.mark.parametrize("value", ["24:00", "12:60", "-1:00", "9", "9:5:5", "aa:bb", ""])
def test_parse_hhmm_rejects_bad_times(value):
    with pytest.raises(ValueError):
        _parse_hhmm(value)


def test_seconds_until_daily_before_target_same_day():
    # 23:00 -> next 00:00 is one hour away.
    assert _seconds_until_daily("00:00", datetime(2026, 7, 14, 23, 0, 0)) == 3600


def test_seconds_until_daily_after_target_rolls_to_next_day():
    # 06:30 already passed today -> next 06:00 is 23.5h out.
    assert _seconds_until_daily("06:00", datetime(2026, 7, 14, 6, 30, 0)) == 23.5 * 3600


def test_seconds_until_daily_exactly_at_target_waits_a_full_day():
    # Exactly on the target counts as passed, so it schedules tomorrow — never a same-instant re-fire.
    assert _seconds_until_daily("00:00", datetime(2026, 7, 14, 0, 0, 0)) == 86400


def test_seconds_until_daily_is_dst_correct():
    # US spring-forward: DST begins 2026-03-08 at 02:00 (CST -06:00 -> CDT -05:00). From 23:00 the
    # evening before to 06:00 the next morning crosses the lost hour, so the real wait is 6 hours,
    # not the naive 7 — proving the scheduler honors a 23-hour day.
    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "America/Chicago"
    time.tzset()
    try:
        secs = _seconds_until_daily("06:00", datetime(2026, 3, 7, 23, 0, 0))
    finally:
        if old_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
    assert secs == 6 * 3600


def test_daily_at_dry_run_runs_one_tick_immediately(monkeypatch, tmp_path):
    # DRY_RUN=1 + --daily-at must preview immediately (one tick, exit), never sleep to the target.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("LINEAR_API_KEY", "test-key")
    monkeypatch.setenv("DRY_RUN", "1")
    ticks: list = []
    monkeypatch.setattr("agentloop.cli._LOOPS", {"design": lambda cfg: ticks.append(cfg)})

    def _no_sleep(_seconds):
        raise AssertionError("a --daily-at dry-run preview must not sleep to the target")

    monkeypatch.setattr("agentloop.cli.time.sleep", _no_sleep)

    rc = main(["design", "--daily-at", "00:00"])
    assert rc == 0
    assert len(ticks) == 1
    assert ticks[0].dry_run is True


def test_main_rejects_interval_and_daily_at_together():
    with pytest.raises(SystemExit, match="either --interval or --daily-at"):
        main(["design", "--daily-at", "00:00", "--interval", "3600"])


def test_main_rejects_malformed_daily_at(monkeypatch):
    # Validation happens before config is built, so no API key is needed to reach it.
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)
    with pytest.raises(SystemExit, match="--daily-at"):
        main(["design", "--daily-at", "25:00"])
