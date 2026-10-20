"""Portable execution identities at process, filesystem and Git boundaries."""

from __future__ import annotations

import os
import shlex
import subprocess

import pytest

from conftest import FakeClient
from agentloop import harness, identity, journal, outage, proc, spawn, tracker, worktree
from agentloop.loops import debug, declutter, deploy, design, develop, dispatch


@pytest.mark.parametrize("janitor", [False, True])
def test_failed_worktree_removal_never_deletes_branch(make_cfg, monkeypatch, janitor):
    cfg = _github_cfg(make_cfg, dry_run=False, log_tag="declutter")
    row = _row()
    key = tracker.execution_key(cfg, row)
    root = cfg.repo_root(row.repo)
    path = root / cfg.worktree_rel / key
    path.mkdir(parents=True)
    os.utime(path, (0, 0))
    calls = []
    monkeypatch.setattr(worktree, "_current_branch", lambda path: f"codex/{key}-work")
    monkeypatch.setattr(worktree, "_worktree_branches", lambda root: [])
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)

    def git(cfg, root, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1 if args[:2] == ("worktree", "remove") else 0)

    monkeypatch.setattr("agentloop.proc.git", git)
    if janitor:
        declutter.gc_orphan_worktrees(cfg)
    else:
        worktree.cleanup_worktree(cfg, key)

    assert any(args[:2] == ("worktree", "remove") for args in calls)
    assert not any(args[:2] == ("branch", "-D") for args in calls)
    assert path.exists()


def _github_cfg(make_cfg, **kwargs):
    cfg = make_cfg(**kwargs)
    # GitHub is an identity namespace here, without resolving an unimplemented adapter.
    cfg.tracker = "github"
    return cfg


def _row(identifier="owner/repository#7", scope="owner/repository", issue_id="native-7"):
    return develop.DevelopmentRow(issue_id, identifier, scope, "sample-e2e", "bug", "Repair failure")


@pytest.mark.parametrize("loop", ["develop", "debug", "deploy"])
def test_issue_identity_remains_in_prompt_while_local_names_are_safe(make_cfg, monkeypatch, loop):
    cfg = _github_cfg(make_cfg, log_tag=loop)
    client = FakeClient(cfg)
    row = _row("owner/repository#7/../../other")
    key = tracker.execution_key(cfg, row)
    guards, spawned, cleaned = [], [], []
    monkeypatch.setattr("agentloop.proc.has_session", lambda value: guards.append(value) or False)
    monkeypatch.setattr("agentloop.worktree.cleanup_worktree", lambda cfg, value: cleaned.append(value))
    monkeypatch.setattr("agentloop.proc.git", lambda *args, **kwargs: None)
    monkeypatch.setattr("agentloop.worktree.reclaim_branch", lambda *args: None)
    monkeypatch.setattr("agentloop.spawn.tmux_spawn", lambda cfg, value, cwd, prompt: spawned.append((value, cwd, prompt)) or True)

    if loop == "develop":
        develop.develop_one(client, cfg, row)
        expected_skill = "/fix-bug"
    elif loop == "debug":
        review = debug.ReviewRow(row.id, row.identifier, row.scope, row.repo, "")
        debug.debug_one(client, cfg, debug.DebugConfig("/triangulate", True), review, debug.Tally())
        debug.spawn_fix(client, cfg, debug.DebugConfig("/triangulate", True), review,
                        "fix/issue-7", "owner", row.repo, 7)
        expected_skill = "/triangulate"
    else:
        approved = deploy.ApprovedRow(row.id, row.identifier, row.scope, row.repo)
        deploy.deploy_one(client, cfg, approved)
        expected_skill = "/ship-it"

    assert guards == [key]
    assert len(spawned) == 1
    actual_key, cwd, prompt = spawned[0]
    assert actual_key == key
    assert row.identifier in prompt
    if loop != "deploy":
        assert cwd == cfg.repo_root(row.repo) / cfg.worktree_rel / key
        assert cleaned == [key]
    else:
        assert cwd == cfg.repo_root(row.repo)


def test_dispatch_session_guard_is_repository_qualified(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg)
    client = FakeClient(cfg)
    rows = [dispatch.ReadyRow("native-7", "#7", scope, "sample-e2e", "bug", "Repair")
            for scope in ("owner/one", "owner/two")]
    first_key = tracker.execution_key(cfg, rows[0])
    guards = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda value: guards.append(value) or value == first_key)
    for row in rows:
        dispatch.dispatch_one(client, cfg, row)
    assert guards == [first_key, tracker.execution_key(cfg, rows[1])]
    assert guards[0] != guards[1]
    assert [(call[0], call[1]) for call in client.calls if call[0] == "move_state"] == [("move_state", "native-7")]


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_prompt_shell_roundtrip_preserves_arbitrary_issue_identity(agent):
    prompt = "/fix-bug owner's/repo#7\n\nKeep literal $(touch /tmp/unwanted); `printf x` and 'quotes'."
    adapter = harness.HarnessAdapter(agent, f"/opt/agent tools/{agent}", "--flag 'two words'")
    argv = shlex.split(adapter.build_headless_cmd(prompt))
    assert argv[0] == f"/opt/agent tools/{agent}"
    assert argv[-1] == prompt
    assert argv[argv.index("--flag") + 1] == "two words"


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True).stdout.strip()


def test_develop_fetches_real_custom_remote_and_nested_base_branch(make_cfg, monkeypatch, tmp_path):
    """No origin/main exists: a complete local checkout must use the configured target."""
    cfg = _github_cfg(make_cfg, dry_run=False, log_tag="develop")
    bare, source = tmp_path / "remote.git", tmp_path / "source"
    bare.mkdir()
    source.mkdir()
    _git(bare, "init", "--bare")
    _git(source, "init", "-b", "release/develop")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    (source / "base.txt").write_text("custom base\n")
    _git(source, "add", "base.txt")
    _git(source, "commit", "-m", "Custom base")
    initial = _git(source, "rev-parse", "HEAD")
    _git(source, "remote", "add", "publish", str(bare))
    _git(source, "push", "publish", "release/develop")
    (source / "remote-marker.txt").write_text("new remote base\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "New base after local shadow")
    _git(source, "push", "publish", "release/develop")
    root = cfg.repo_root("sample-e2e")
    _git(root, "init")
    _git(root, "remote", "add", "personal", str(bare))
    _git(root, "config", "remote.personal.fetch", "+refs/heads/main:refs/remotes/personal/main")
    _git(root, "fetch", "personal", "+refs/heads/release/develop:refs/remotes/personal/release/develop")
    _git(root, "branch", "personal/release/develop", initial)
    cfg.repo_remotes["sample-e2e"] = "personal"
    cfg.repo_base_branches["sample-e2e"] = "release/develop"
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)
    spawned = []
    monkeypatch.setattr("agentloop.spawn.tmux_spawn", lambda cfg, key, cwd, prompt: spawned.append((key, cwd, prompt)) or True)

    row = _row()
    assert develop.develop_one(FakeClient(cfg), cfg, row)

    assert len(spawned) == 1
    key, cwd, prompt = spawned[0]
    assert key == tracker.execution_key(cfg, row)
    assert (cwd / "base.txt").read_text() == "custom base\n"
    assert (cwd / "remote-marker.txt").read_text() == "new remote base\n"
    assert _git(root, "rev-parse", "refs/heads/personal/release/develop") == initial
    assert _git(cwd, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")
    assert _git(cwd, "branch", "--show-current").startswith(cfg.branch_prefix + key + "-")
    assert row.identifier in prompt


@pytest.mark.parametrize("pr_repo,selected", [("sample-e2e", "sample-e2e"), ("unmapped-repository", "sample")])
def test_debug_uses_remote_for_actual_checkout_including_fallback(make_cfg, monkeypatch, pr_repo, selected):
    cfg = _github_cfg(make_cfg, log_tag="debug")
    cfg.repo_remotes.update({"sample": "application", "sample-e2e": "testing"})
    row = debug.ReviewRow("id", "owner/app#7", "owner/app", "sample", "")
    calls, spawned = [], []
    monkeypatch.setattr("agentloop.worktree.cleanup_worktree", lambda *args: None)
    monkeypatch.setattr("agentloop.worktree.reclaim_branch", lambda *args: None)
    monkeypatch.setattr("agentloop.proc.git", lambda cfg, root, *args, **kwargs: calls.append((root, args)))
    monkeypatch.setattr("agentloop.spawn.tmux_spawn", lambda cfg, key, cwd, prompt: spawned.append(cwd) or True)

    assert debug.spawn_fix(FakeClient(cfg), cfg, debug.DebugConfig("/triangulate", True),
                           row, "fix/issue-7", "owner", pr_repo, 7)

    remote = cfg.repo_remote(selected)
    assert calls[0] == (cfg.repo_root(selected), ("fetch", "--quiet", remote,
                                                f"+refs/heads/fix/issue-7:refs/remotes/{remote}/fix/issue-7"))
    assert calls[1][1][-1] == f"refs/remotes/{remote}/fix/issue-7"
    assert spawned == [cfg.repo_root(selected) / cfg.worktree_rel / tracker.execution_key(cfg, row)]


def _old_directory(path):
    path.mkdir(parents=True)
    os.utime(path, (1, 1))
    return path


def test_gc_retains_blocked_generated_key(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg, dry_run=False, log_tag="declutter")
    root = cfg.repo_root("sample-e2e")
    key = tracker.execution_key(cfg, _row())
    blocked = _old_directory(root / cfg.worktree_rel / key)
    calls = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)
    monkeypatch.setattr("agentloop.worktree._current_branch", lambda wt: f"codex/{key}-repair")
    monkeypatch.setattr("agentloop.proc.git", lambda *args, **kwargs: calls.append(args))

    declutter.gc_orphan_worktrees(cfg, protected_keys={key})

    assert blocked.exists()
    assert calls == []


def test_gc_removes_generated_orphan_but_not_symlink_target(make_cfg, monkeypatch, tmp_path):
    cfg = _github_cfg(make_cfg, dry_run=False, log_tag="declutter")
    root = cfg.repo_root("sample-e2e")
    key = tracker.execution_key(cfg, _row())
    orphan = _old_directory(root / cfg.worktree_rel / key)
    outside = _old_directory(tmp_path / "human-work")
    linked_key = identity.execution_key("github", "other/repo#7", "other/repo")
    (root / cfg.worktree_rel / linked_key).symlink_to(outside, target_is_directory=True)
    calls = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)
    monkeypatch.setattr("agentloop.worktree._current_branch", lambda wt: f"codex/{wt.name}-repair")
    monkeypatch.setattr("agentloop.worktree._worktree_branches", lambda root: [])
    monkeypatch.setattr("agentloop.proc.git", lambda cfg, root, *args, **kwargs: calls.append(args))

    declutter.gc_orphan_worktrees(cfg)

    assert calls == [("worktree", "remove", "--force", str(orphan)),
                     ("branch", "-D", f"codex/{key}-repair")]
    assert outside.exists()


def test_cleanup_rejects_traversal_and_preserves_unrelated_branch(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg, dry_run=False)
    root = cfg.repo_root("sample-e2e")
    key = tracker.execution_key(cfg, _row())
    _old_directory(root / cfg.worktree_rel / key)
    calls = []
    monkeypatch.setattr("agentloop.worktree._current_branch", lambda wt: "feature/human-work")
    monkeypatch.setattr("agentloop.worktree._worktree_branches", lambda root: [])
    monkeypatch.setattr("agentloop.proc.git", lambda cfg, root, *args, **kwargs: calls.append(args))

    with pytest.raises(ValueError):
        worktree.cleanup_worktree(cfg, "../../human-work")
    worktree.cleanup_worktree(cfg, key)

    assert calls == []


def test_declutter_and_outage_lookup_share_generated_session_key(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg, log_tag="declutter")
    row = declutter.StuckRow("native-7", "owner/repository#7", "owner/repository")
    key = tracker.execution_key(cfg, row)
    log = cfg.log_dir / "develop" / f"{key}-123.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text("You've hit your session limit\n")
    assert outage.newest_key_log(cfg, key) == log
    guarded, cleaned = [], []
    monkeypatch.setattr("agentloop.proc.has_session", lambda value: guarded.append(value) or False)
    monkeypatch.setattr("agentloop.worktree.cleanup_worktree", lambda cfg, value: cleaned.append(value))
    client = FakeClient(cfg)

    assert declutter.declutter_one(client, cfg, row)

    assert guarded == [key] and cleaned == [key]
    assert ("move_state", row.id, row.scope, cfg.state_queue) in client.calls


def test_linear_execution_names_are_generated(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="develop")
    row = _row("ENG-24", "linear-team", "uuid")
    guards = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: guards.append(key) or True)
    assert develop.develop_one(FakeClient(cfg), cfg, row)
    key = tracker.execution_key(cfg, row)
    assert guards == [key]
    assert dispatch.branch_name(cfg, key, "Repair") == cfg.branch_prefix + key + "-repair"


def test_develop_capacity_counts_namespaced_live_sessions(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg, concurrency=1, log_tag="develop")
    client = FakeClient(cfg)
    row = _row()
    guarded, developed = [], []
    monkeypatch.setattr(tracker, "resolve", lambda cfg: client)
    monkeypatch.setattr(develop, "advance_todo_prs", lambda *args: None)
    monkeypatch.setattr(develop, "reconcile_blocked_todos", lambda *args: 0)
    monkeypatch.setattr(develop, "rate_limited_now", lambda cfg: None)
    monkeypatch.setattr(develop, "list_development", lambda *args: [row])
    monkeypatch.setattr(develop, "list_active_development", lambda *args: [row])
    monkeypatch.setattr(develop, "develop_one", lambda *args: developed.append(args))
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: guarded.append(key) or True)

    develop.run_tick(cfg)

    assert guarded == [tracker.execution_key(cfg, row)]
    assert developed == []


def test_blocked_protection_uses_same_key_as_runtime(make_cfg, monkeypatch):
    cfg = _github_cfg(make_cfg, log_tag="declutter")
    client = FakeClient(cfg)
    issue = tracker.Issue("native-7", "owner/repository#7", "owner/repository", labels=(cfg.blocked_label,))
    monkeypatch.setattr(client, "issues_in_states", lambda *args, **kwargs: [issue])
    assert declutter.list_blocked_worktree_keys(client, cfg) == {tracker.execution_key(cfg, issue)}


def test_design_lookup_distinguishes_remotes_on_same_checkout(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="design")
    reads = []

    def capture(argv):
        reads.append(argv[-1])
        url = "git@github.com:personal/app.git" if argv[-1] == "remote.personal.url" else "https://github.com/team/app.git"
        return subprocess.CompletedProcess(argv, 0, url + "\n")

    monkeypatch.setattr("agentloop.proc.capture", capture)
    design._slug_from_remote_url.cache_clear()
    try:
        cfg.repo_remotes[cfg.design_repo] = "personal"
        assert design.design_slug(cfg) == "personal/app"
        cfg.repo_remotes[cfg.design_repo] = "team"
        assert design.design_slug(cfg) == "team/app"
        assert reads == ["remote.personal.url", "remote.team.url"]
    finally:
        design._slug_from_remote_url.cache_clear()


def _debug_git_context(make_cfg, monkeypatch, tmp_path):
    cfg = _github_cfg(make_cfg, dry_run=False, log_tag="debug")
    bare, source = tmp_path / "remote.git", tmp_path / "source"
    bare.mkdir()
    source.mkdir()
    _git(bare, "init", "--bare")
    _git(source, "init", "-b", "manual/fix-seven")
    _git(source, "config", "user.name", "Test")
    _git(source, "config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("initial\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "Initial PR head")
    _git(source, "remote", "add", "personal", str(bare))
    _git(source, "push", "personal", "manual/fix-seven")
    root = cfg.repo_root("sample-e2e")
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "remote", "add", "personal", str(bare))
    cfg.repo_remotes["sample-e2e"] = "personal"
    row = debug.ReviewRow("native-7", "owner/repository#7", "owner/repository", "sample-e2e", "")
    calls = []
    monkeypatch.setattr("agentloop.spawn.tmux_spawn", lambda cfg, key, cwd, prompt: calls.append((key, cwd, prompt)) or True)

    def fix():
        return debug.spawn_fix(FakeClient(cfg), cfg, debug.DebugConfig("/triangulate", True), row,
                               "manual/fix-seven", "owner", "sample-e2e", 7)

    wt = root / cfg.worktree_rel / tracker.execution_key(cfg, row)
    return fix, wt, source, calls


def test_repeated_debug_arbitrary_branch_reuses_checkout_and_fast_forwards(make_cfg, monkeypatch, tmp_path):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    assert fix()
    (source / "tracked.txt").write_text("updated remotely\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "New PR head")
    _git(source, "push", "personal", "manual/fix-seven")

    assert fix()

    assert len(spawned) == 2
    assert _git(wt, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")
    assert (wt / "tracked.txt").read_text() == "updated remotely\n"


def test_repeated_debug_preserves_clean_unpushed_commits(make_cfg, monkeypatch, tmp_path):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    assert fix()
    (wt / "local.txt").write_text("unpublished work\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "Local progress")
    before = _git(wt, "rev-parse", "HEAD")
    assert before != _git(source, "rev-parse", "HEAD")

    assert fix()

    assert len(spawned) == 2
    assert _git(wt, "rev-parse", "HEAD") == before
    assert (wt / "local.txt").read_text() == "unpublished work\n"


@pytest.mark.parametrize("filename", ["tracked.txt", "untracked.txt"])
def test_repeated_debug_retains_dirty_arbitrary_branch_without_spawning(make_cfg, monkeypatch, tmp_path, filename):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    assert fix()
    before = _git(wt, "rev-parse", "HEAD")
    (wt / filename).write_text("unfinished work\n")

    assert not fix()

    assert len(spawned) == 1
    assert _git(wt, "rev-parse", "HEAD") == before
    assert (wt / filename).read_text() == "unfinished work\n"
    assert _git(wt, "status", "--porcelain")


def test_repeated_debug_retains_divergent_history_without_spawning(make_cfg, monkeypatch, tmp_path):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    assert fix()
    (wt / "local.txt").write_text("local commit\n")
    _git(wt, "add", ".")
    _git(wt, "commit", "-m", "Local progress")
    before = _git(wt, "rev-parse", "HEAD")
    (source / "remote.txt").write_text("remote commit\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "Remote progress")
    _git(source, "push", "personal", "manual/fix-seven")

    assert not fix()

    assert len(spawned) == 1
    assert _git(wt, "rev-parse", "HEAD") == before
    assert (wt / "local.txt").read_text() == "local commit\n"
    assert not (wt / "remote.txt").exists()
    assert _git(wt, "status", "--porcelain") == ""


def test_debug_remote_ref_is_not_shadowed_by_local_branch(make_cfg, monkeypatch, tmp_path):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    root = wt.parents[2]
    initial = _git(source, "rev-parse", "HEAD")
    (source / "tracked.txt").write_text("new remote head\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "New remote head")
    _git(source, "push", "personal", "manual/fix-seven")
    _git(root, "fetch", "personal", "+refs/heads/manual/fix-seven:refs/remotes/personal/manual/fix-seven")
    _git(root, "branch", "personal/manual/fix-seven", initial)

    assert fix()

    assert len(spawned) == 1
    assert _git(wt, "rev-parse", "HEAD") == _git(source, "rev-parse", "HEAD")
    assert (wt / "tracked.txt").read_text() == "new remote head\n"
    assert _git(root, "rev-parse", "refs/heads/personal/manual/fix-seven") == initial


def test_debug_rejects_ordinary_nested_directory_without_merging_primary(make_cfg, monkeypatch, tmp_path):
    fix, wt, source, spawned = _debug_git_context(make_cfg, monkeypatch, tmp_path)
    root = wt.parents[2]
    _git(root, "fetch", "personal", "+refs/heads/manual/fix-seven:refs/remotes/personal/manual/fix-seven")
    _git(root, "checkout", "-b", "manual/fix-seven", "refs/remotes/personal/manual/fix-seven")
    (root / ".git/info/exclude").write_text(".worktrees/\n")
    wt.mkdir(parents=True)
    (wt / "notes.txt").write_text("leftover human notes\n")
    before = _git(root, "rev-parse", "HEAD")
    assert _git(root, "status", "--porcelain") == ""
    (source / "tracked.txt").write_text("new remote head\n")
    _git(source, "add", ".")
    _git(source, "commit", "-m", "New remote head")
    _git(source, "push", "personal", "manual/fix-seven")

    assert not fix()

    assert spawned == []
    assert _git(root, "rev-parse", "HEAD") == before
    assert (root / "tracked.txt").read_text() == "initial\n"
    assert (wt / "notes.txt").read_text() == "leftover human notes\n"
    assert _git(root, "status", "--porcelain") == ""


def test_worktree_path_rejects_symlinked_parent(make_cfg, tmp_path):
    cfg = make_cfg()
    root = cfg.repo_root("sample-e2e")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / ".worktrees").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError):
        worktree.worktree_path(cfg, root, "ENG-7")
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize("sink", ["guard", "spawn", "log"])
def test_unsafe_session_names_fail_before_subprocess_or_log_creation(make_cfg, monkeypatch, sink):
    cfg = make_cfg(dry_run=False)
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError):
        if sink == "guard":
            proc.has_session("../unsafe")
        elif sink == "spawn":
            spawn.tmux_spawn(cfg, "../unsafe", cfg.workspace_dir, "prompt")
        else:
            journal.session_log(cfg, "../unsafe")
    assert calls == []
    assert not (cfg.log_dir / cfg.log_tag).exists()
