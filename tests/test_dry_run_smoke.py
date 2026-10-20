"""Dry-run smoke tests: each loop runs a tick with dry_run=True, a fake LinearClient, and
has_session stubbed. Asserts the exact DRY-RUN spawn preview, that mutating git/tmux went
through the gate (no real subprocess), and the claim/move orderings.
"""

from __future__ import annotations

import types

import pytest

from tests.conftest import FakeClient
from agentloop import tracker


@pytest.fixture(autouse=True)
def _no_live_sessions(monkeypatch):
    # No tmux sessions exist; anything that would shell out to tmux/git is DRY_RUN-gated.
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)
    monkeypatch.setattr("agentloop.loops.design.live_design_sessions", lambda: set())


def _issue(ident, *, labels, extra=None):
    node = {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": ident,
        "team": {"id": "team"},
        "labels": {"nodes": [{"name": n} for n in labels]},
    }
    node.update(extra or {})
    return node


def _inject(monkeypatch, fake):
    monkeypatch.setattr("agentloop.tracker.resolve", lambda cfg: fake)


def test_dispatch_dry_run(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="dispatch")
    ready = _issue(
        "TEAM-1",
        labels=["repo:sample-repo", "Bug"],
        extra={"priority": 1, "children": {"nodes": []}, "inverseRelations": {"nodes": []}},
    )
    fake = FakeClient(
        cfg,
        payloads={
            "states:Todo|In Progress|In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}},        # count_active -> 0 active
            "state:Backlog": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [ready]}}},   # list_ready
        },
    )
    _inject(monkeypatch, fake)

    from agentloop.loops import dispatch

    dispatch.run_tick(cfg)
    err = capsys.readouterr().err

    # Dispatch admits and tags only; develop owns the In Progress claim, git worktree creation, and spawning.
    assert ("move_state", "id-TEAM-1", "team", "Todo") in fake.calls
    assert ("add_label", "id-TEAM-1", "team", "loop-wip") in fake.calls
    assert "DRY-RUN would spawn: tmux new -d -s TEAM-1" not in err
    assert "DRY-RUN would: git" not in err


def test_dispatch_skips_unknown_repo_and_continues_tick(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="dispatch")
    bad = _issue(
        "TEAM-3",
        labels=["repo:not-configured", "Feature"],
        extra={"priority": 1, "children": {"nodes": []}, "inverseRelations": {"nodes": []}},
    )
    good = _issue(
        "TEAM-4",
        labels=["repo:sample-repo", "Feature"],
        extra={"priority": 2, "children": {"nodes": []}, "inverseRelations": {"nodes": []}},
    )
    fake = FakeClient(
        cfg,
        payloads={
            "states:Todo|In Progress|In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}},
            "state:Backlog": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [bad, good]}}},
        },
    )
    _inject(monkeypatch, fake)

    from agentloop.loops import dispatch

    dispatch.run_tick(cfg)
    err = capsys.readouterr().err

    assert "TEAM-3: unknown repository label: 'not-configured'" in err
    assert ("move_state", "id-TEAM-4", "team", "Todo") in fake.calls


def test_develop_dry_run_spawns_claimed_work(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="develop")
    claimed = _issue(
        "TEAM-11",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": []}},
    )
    fake = FakeClient(cfg, payloads={"state:Todo": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [claimed]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import develop

    develop.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-11", "team", "In Progress") in fake.calls
    assert "DRY-RUN would spawn: tmux new -d -s al-linear-" in err
    assert "'/build-feature TEAM-11'" not in err  # prompt is shown double-quoted in the preview
    assert '"Implement TEAM-' in err
    assert "Dispatch handoff contract: once you open a non-draft PR" in err
    assert "Do not inspect CI, run triangulation, address review comments, merge" in err
    assert "The debug loop owns In Review follow-up." in err
    assert "## Deployed Evidence" in err
    assert "Apply/deploy run: <url>" in err
    assert "Observed post-deploy behavior:" in err
    assert "DRY-RUN would: git" in err
    assert "@sample/ui" not in err


def test_develop_advances_todo_prs_to_review(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="develop")
    claimed_with_pr = _issue(
        "TEAM-10",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/example-org/agent-loop/pull/23"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:Todo": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [claimed_with_pr]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import develop

    develop.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-10", "team", "In Review") in fake.calls
    assert ("add_comment", "id-TEAM-10") in fake.calls
    assert "DRY-RUN would spawn: tmux new -d -s TEAM-10" not in err


def test_develop_advances_todo_prs_during_usage_outage(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="develop")
    (cfg.log_dir / "develop").mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / "develop" / "TEAM-99.log").write_text("You've hit your session limit")
    claimed_with_pr = _issue(
        "TEAM-10",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/example-org/agent-loop/pull/23"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:Todo": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [claimed_with_pr]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import develop

    develop.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-10", "team", "In Review") in fake.calls
    assert "Claude usage outage" in err
    assert "DRY-RUN would spawn: tmux new -d -s TEAM-10" not in err


def test_dispatch_advances_queued_prs_to_review(make_cfg):
    cfg = make_cfg(log_tag="dispatch")
    queued_with_pr = _issue(
        "TEAM-10",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/example-org/agent-loop/pull/23"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:Backlog": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [queued_with_pr]}}}})

    from agentloop.loops import dispatch

    dispatch.advance_queued_prs(fake, cfg)

    assert ("move_state", "id-TEAM-10", "team", "In Review") in fake.calls
    assert ("add_comment", "id-TEAM-10") in fake.calls


def test_develop_requeues_todo_when_fetch_fails(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="develop")
    fake = FakeClient(cfg)
    row = _issue("TEAM-10", labels=["repo:sample-repo", "Feature", "loop-wip"])

    from agentloop.loops import develop

    dev_row = develop.DevelopmentRow(
        id=row["id"],
        identifier=row["identifier"],
        scope=row["team"]["id"],
        repo="sample-repo",
        type="feature",
        title=row["title"],
    )

    def _git(cfg, root, *args, **kwargs):
        if args[:3] == ("fetch", "--quiet", "origin"):
            return types.SimpleNamespace(returncode=1)
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("agentloop.worktree.cleanup_worktree", lambda cfg, key: None)
    monkeypatch.setattr("agentloop.proc.git", _git)

    assert develop.develop_one(fake, cfg, dev_row) is True
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-10", "team", "In Progress") in fake.calls
    assert ("move_state", "id-TEAM-10", "team", "Todo") in fake.calls
    assert "fetch origin/main failed" in err
    assert "DRY-RUN would spawn: tmux new -d -s TEAM-10" not in err


def test_develop_skips_unknown_repo_label(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="develop")
    fake = FakeClient(cfg)
    from agentloop.loops import develop

    row = develop.DevelopmentRow(
        id="id-TEAM-13",
        identifier="TEAM-13",
        scope="team",
        repo="not-configured",
        type="feature",
        title="TEAM-13",
    )

    assert develop.develop_one(fake, cfg, row) is True
    err = capsys.readouterr().err

    assert "TEAM-13: unknown repository label: 'not-configured'" in err
    assert ("move_state", "id-TEAM-13", "team", "In Progress") not in fake.calls


def test_develop_codex_dry_run_uses_build_skill(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(
        log_tag="develop",
        harness="codex",
        harness_bin="codex",
        harness_flags="--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check",
    )
    claimed = _issue(
        "TEAM-12",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": []}},
    )
    fake = FakeClient(cfg, payloads={"state:Todo": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [claimed]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import develop

    develop.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-12", "team", "In Progress") in fake.calls
    assert "DRY-RUN would spawn: tmux new -d -s al-linear-" in err
    assert 'codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check' in err
    assert '"Implement TEAM-' in err
    assert "Dispatch handoff contract: once you open a non-draft PR" in err


def test_dispatch_repairs_queued_prs_before_slots(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="dispatch")
    queued_pr = _issue(
        "TEAM-9",
        labels=["repo:sample-repo", "Feature", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/o/r/pull/9"}]}},
    )
    fake = FakeClient(
        cfg,
        payloads={
            "state:Backlog": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [queued_pr]}}},
            "states:Todo|In Progress|In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}},
        },
    )
    _inject(monkeypatch, fake)

    from agentloop.loops import dispatch

    dispatch.run_tick(cfg)

    assert ("move_state", "id-TEAM-9", "team", "In Review") in fake.calls
    assert ("move_state", "id-TEAM-9", "team", "In Progress") not in fake.calls
    ops = [c[0] for c in fake.calls]
    assert ops.index("ensure_team_meta") < ops.index("move_state")


def test_deploy_dry_run(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue("TEAM-2", labels=["repo:sample-repo", "loop-wip"])
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err
    assert 'DRY-RUN would spawn: tmux new -d -s al-linear-' in err
    assert '"Ship the approved pull request for TEAM-' in err
    assert "Do NOT use the Linear MCP connector" in err  # Linear state contract on the ship-it prompt


def test_deploy_skips_unknown_repo_label(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    fake = FakeClient(cfg)
    from agentloop.loops import deploy

    row = deploy.ApprovedRow("id-TEAM-14", "TEAM-14", "team", "not-configured")
    assert deploy.deploy_one(fake, cfg, row) is False
    err = capsys.readouterr().err

    assert "TEAM-14: unknown repository label: 'not-configured'" in err


def test_deploy_moves_blocked_approved_pr_back_to_review(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue(
        "TEAM-1306",
        labels=["repo:agent-loop", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/example-org/agent-loop/pull/25"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr(
        "agentloop.github.pr_deploy_gate_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": None,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewThreads": {"nodes": [
                    {"isResolved": False, "isOutdated": False, "comments": {"nodes": [{"createdAt": "2026-07-10T20:42:32Z"}]}}
                ]},
            }}}
        },
    )

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-1306", "team", "In Review") in fake.calls
    assert ("add_comment", "id-TEAM-1306") in fake.calls
    assert '"Ship the approved pull request for TEAM-' not in err
    assert "unresolved current review thread" in err


def test_deploy_infra_coupled_ticket_requires_deployed_evidence(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue(
        "TEAM-1329",
        labels=["repo:sample-infra", "loop-wip"],
        extra={
            "title": "Rotate task definition secret",
            "comments": {"nodes": []},
            "attachments": {"nodes": [{"url": "https://github.com/example-org/sample-repo-infra2/pull/31"}]},
        },
    )
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr(
        "agentloop.github.pr_deploy_gate_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": None,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewThreads": {"nodes": []},
            }}}
        },
    )

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-1329", "team", "Needs Input") in fake.calls
    assert ("add_comment", "id-TEAM-1329") in fake.calls
    body = "\n".join(fake.comment_bodies["id-TEAM-1329"])
    assert "Evidence owed before this ticket can move to Done" in body
    assert "Apply/deploy run link" in body
    assert "Observed post-deploy behavior" in body
    assert '"Ship the approved pull request for TEAM-' not in err
    assert "lacks deployed evidence" in err


def test_deploy_infra_coupled_ticket_runs_preflight_before_evidence_hold(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue(
        "TEAM-1331",
        labels=["repo:sample-infra", "loop-wip"],
        extra={
            "title": "Rotate task definition secret",
            "comments": {"nodes": []},
            "attachments": {"nodes": [{"url": "https://github.com/example-org/sample-repo-infra2/pull/33"}]},
        },
    )
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr(
        "agentloop.github.pr_deploy_gate_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": "CHANGES_REQUESTED",
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewThreads": {"nodes": []},
            }}}
        },
    )

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-1331", "team", "In Review") in fake.calls
    assert ("move_state", "id-TEAM-1331", "team", "Needs Input") not in fake.calls
    assert '"Ship the approved pull request for TEAM-' not in err
    assert "CHANGES_REQUESTED" in err


def test_deploy_evidence_hold_requires_comment_before_move(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue(
        "TEAM-1332",
        labels=["repo:sample-infra", "loop-wip"],
        extra={
            "title": "Rotate task definition secret",
            "comments": {"nodes": []},
            "attachments": {"nodes": [{"url": "https://github.com/example-org/sample-repo-infra2/pull/34"}]},
        },
    )
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    fake.add_comment = lambda issue_id, body: fake.calls.append(("add_comment", issue_id)) or False
    _inject(monkeypatch, fake)
    monkeypatch.setattr(
        "agentloop.github.pr_deploy_gate_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": None,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewThreads": {"nodes": []},
            }}}
        },
    )

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("add_comment", "id-TEAM-1332") in fake.calls
    assert ("move_state", "id-TEAM-1332", "team", "Needs Input") not in fake.calls
    assert "could not record deployed-evidence instructions" in err


def test_deploy_infra_classifier_avoids_stage_title_false_positive(make_cfg):
    from agentloop.loops import deploy

    row = deploy.ApprovedRow(
        id="id-TEAM-1333",
        identifier="TEAM-1333",
        scope="team",
        repo="agent-loop",
        title="Stage 2 of the parser rollout",
        labels=("loop-wip", "repo:agent-loop"),
    )

    assert deploy.is_infra_coupled(make_cfg(), row) is False


def test_deploy_infra_coupled_ticket_with_deployed_evidence_can_ship(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="deploy")
    approved = _issue(
        "TEAM-1330",
        labels=["repo:sample-infra", "loop-wip"],
        extra={
            "title": "Rotate task definition secret",
            "comments": {
                "nodes": [{
                    "body": "## Deployed Evidence\n"
                    "- Apply/deploy run: https://github.com/example-org/sample-repo-infra2/actions/runs/1\n"
                    "- Observed post-deploy behavior: stage task exited 0"
                }]
            },
            "attachments": {"nodes": [{"url": "https://github.com/example-org/sample-repo-infra2/pull/32"}]},
        },
    )
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr(
        "agentloop.github.pr_deploy_gate_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN",
                "isDraft": False,
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "reviewDecision": None,
                "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
                "reviewThreads": {"nodes": []},
            }}}
        },
    )

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err

    assert ("move_state", "id-TEAM-1330", "team", "Needs Input") not in fake.calls
    assert '"Ship the approved pull request for TEAM-' in err


def test_deploy_codex_dry_run_uses_ship_skill(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(
        log_tag="deploy",
        harness="codex",
        harness_bin="codex",
        harness_flags="--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check",
    )
    approved = _issue("TEAM-12", labels=["repo:sample-repo", "loop-wip"])
    fake = FakeClient(cfg, payloads={"state:Approved": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [approved]}}}})
    _inject(monkeypatch, fake)

    from agentloop.loops import deploy

    deploy.run_tick(cfg)
    err = capsys.readouterr().err
    assert 'DRY-RUN would spawn: tmux new -d -s al-linear-' in err
    assert '"Ship the approved pull request for TEAM-' in err


def test_debug_dry_run_fix_path(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="debug")
    review = _issue(
        "TEAM-3",
        labels=["repo:sample-repo", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/o/r/pull/7"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [review]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr("agentloop.github.require_gh", lambda cfg: True)
    # A PR with an unresolved thread newer than head -> decide() returns "fix".
    monkeypatch.setattr(
        "agentloop.github.pr_review_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "headRefName": "feature/x",
                "commits": {"nodes": [{"commit": {"committedDate": "2020-01-01T00:00:00Z", "statusCheckRollup": {"state": "PENDING"}}}]},
                "reviewThreads": {"nodes": [{"isResolved": False, "comments": {"nodes": [{"createdAt": "2020-02-02T00:00:00Z"}]}}]},
            }}}
        },
    )

    from agentloop.loops import debug

    debug.run_tick(cfg)
    err = capsys.readouterr().err
    assert "DRY-RUN would spawn: tmux new -d -s al-linear-" in err
    assert '"Review and repair the existing pull request for TEAM-' in err
    assert "Do NOT use the Linear MCP connector" in err  # Linear state contract on the fix prompt


def test_debug_fix_drain_ignores_removed_concurrency_env(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="debug", env={"AGENT_LOOP_DEBUG_CONCURRENCY": "0"})
    reviews = [
        _issue(
            "TEAM-31",
            labels=["repo:sample-repo", "loop-wip"],
            extra={"attachments": {"nodes": [{"url": "https://github.com/o/r/pull/31"}]}},
        ),
        _issue(
            "TEAM-32",
            labels=["repo:sample-repo", "loop-wip"],
            extra={"attachments": {"nodes": [{"url": "https://github.com/o/r/pull/32"}]}},
        ),
    ]
    fake = FakeClient(cfg, payloads={"state:In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": reviews}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr("agentloop.github.require_gh", lambda cfg: True)
    monkeypatch.setattr(
        "agentloop.github.pr_review_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "headRefName": f"feature/{n}",
                "commits": {"nodes": [{"commit": {"committedDate": "2020-01-01T00:00:00Z", "statusCheckRollup": {"state": "PENDING"}}}]},
                "reviewThreads": {"nodes": [{"isResolved": False, "comments": {"nodes": [{"createdAt": "2020-02-02T00:00:00Z"}]}}]},
            }}}
        },
    )

    from agentloop.loops import debug

    debug.run_tick(cfg)
    err = capsys.readouterr().err
    assert "in-review PRs=2 | live sessions=0 | debug fixes unbounded" in err
    assert err.count("DRY-RUN would spawn: tmux new -d -s al-linear-") == 2


def test_debug_codex_dry_run_uses_triangulate_skill(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(
        log_tag="debug",
        harness="codex",
        harness_bin="codex",
        harness_flags="--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check",
    )
    review = _issue(
        "TEAM-13",
        labels=["repo:sample-repo", "loop-wip"],
        extra={"attachments": {"nodes": [{"url": "https://github.com/o/r/pull/7"}]}},
    )
    fake = FakeClient(cfg, payloads={"state:In Review": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [review]}}}})
    _inject(monkeypatch, fake)
    monkeypatch.setattr("agentloop.github.require_gh", lambda cfg: True)
    monkeypatch.setattr(
        "agentloop.github.pr_review_state",
        lambda o, r, n, *_: {
            "data": {"repository": {"pullRequest": {
                "state": "OPEN", "reviewDecision": "REVIEW_REQUIRED", "headRefName": "feature/x",
                "commits": {"nodes": [{"commit": {"committedDate": "2020-01-01T00:00:00Z", "statusCheckRollup": {"state": "PENDING"}}}]},
                "reviewThreads": {"nodes": [{"isResolved": False, "comments": {"nodes": [{"createdAt": "2020-02-02T00:00:00Z"}]}}]},
            }}}
        },
    )

    from agentloop.loops import debug

    debug.run_tick(cfg)
    err = capsys.readouterr().err
    assert "DRY-RUN would spawn: tmux new -d -s al-linear-" in err
    assert '"Review and repair the existing pull request for TEAM-' in err


def test_design_dry_run_filters_markers_and_backend_prs(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")
    cfg.concurrency = 3

    from agentloop.loops import design

    prs = [
        design.MergedPR(
            number=10,
            title="UI",
            url="https://github.com/example-org/sample-repo/pull/10",
            merge_sha="abc",
            merged_at="2026-07-10T00:00:00Z",
            files=["apps/web/app/page.tsx"],
            comments=[],
        ),
        design.MergedPR(
            number=11,
            title="Backend",
            url="https://github.com/example-org/sample-repo/pull/11",
            merge_sha="def",
            merged_at="2026-07-10T00:00:00Z",
            files=["apps/api/jobs/sync.py"],
            comments=[],
        ),
        design.MergedPR(
            number=12,
            title="Already reviewed",
            url="https://github.com/example-org/sample-repo/pull/12",
            merge_sha="fed",
            merged_at="2026-07-10T00:00:00Z",
            files=["packages/ui/tokens.ts"],
            comments=["agent-loop-design-reviewed:fed"],
        ),
    ]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "design scan: merged=3 candidates=1 skipped=2 cap=3 live=0 slots=3" in err
    assert "design-10: candidate https://github.com/example-org/sample-repo/pull/10 -> post-merge review" in err
    assert "DRY-RUN would spawn: tmux new -d -s design-10" in err
    assert '"Review the merged pull request #10' in err
    assert "Do NOT use the Linear MCP connector" in err  # Linear state contract remains on the prompt
    assert "design-11" not in err
    assert "design-12" not in err


def test_design_dry_run_filters_spawn_time_claim_marker(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")

    from agentloop.loops import design

    prs = [
        design.MergedPR(
            number=13,
            title="Claimed but not completed",
            url="https://github.com/example-org/sample-repo/pull/13",
            merge_sha="claimed-sha",
            merged_at="2026-07-10T00:00:00Z",
            files=["apps/web/app/page.tsx"],
            comments=["agent-loop-design-claimed:claimed-sha"],
        )
    ]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "design scan: merged=1 candidates=0 skipped=1 cap=3 live=0 slots=3" in err
    assert "spawned 0 post-merge review session(s) this tick" in err
    assert "DRY-RUN would spawn" not in err


def test_design_dry_run_holds_during_usage_outage(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")
    (cfg.log_dir / "design").mkdir(parents=True, exist_ok=True)
    (cfg.log_dir / "design" / "design-88.log").write_text("You've hit your session limit")

    from agentloop.loops import design

    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr(
        "agentloop.loops.design.list_recent_merged_prs",
        lambda cfg: pytest.fail("design loop should not scan PRs during an outage"),
    )

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "Claude usage outage" in err
    assert "no post-merge review spawns" in err
    assert "DRY-RUN would spawn" not in err


def test_design_dry_run_respects_concurrency_cap(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")
    cfg.concurrency = 1

    from agentloop.loops import design

    prs = [
        design.MergedPR(n, "", f"https://github.com/example-org/sample-repo/pull/{n}", str(n), "", ["app/page.tsx"], [])
        for n in (21, 22)
    ]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "DRY-RUN would spawn: tmux new -d -s design-21" in err
    assert "DRY-RUN would spawn: tmux new -d -s design-22" not in err
    assert "post-merge review cap held 1 candidate(s) over the -c=1 safety cap for the next run" in err


def test_design_reviews_oldest_merge_first_when_capped(make_cfg, monkeypatch, capsys):
    # When the nightly cap holds overflow, the OLDEST merge is reviewed and the newest is deferred,
    # so the deferred PR has the most runway before it ages out of the lookback window.
    cfg = make_cfg(log_tag="design")
    cfg.concurrency = 1

    from agentloop.loops import design

    # Input order is newest-first; the loop must reorder to review the older #40 first.
    prs = [
        design.MergedPR(50, "", "https://github.com/example-org/sample-repo/pull/50", "50", "2026-07-10T00:00:00Z", ["app/page.tsx"], []),
        design.MergedPR(40, "", "https://github.com/example-org/sample-repo/pull/40", "40", "2026-07-01T00:00:00Z", ["app/page.tsx"], []),
    ]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "DRY-RUN would spawn: tmux new -d -s design-40" in err  # oldest reviewed
    assert "DRY-RUN would spawn: tmux new -d -s design-50" not in err  # newest deferred
    assert "post-merge review cap held 1 candidate(s) over the -c=1 safety cap for the next run" in err


def test_design_dry_run_counts_live_sessions_against_cap(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")
    cfg.concurrency = 1

    from agentloop.loops import design

    prs = [design.MergedPR(31, "", "https://github.com/example-org/sample-repo/pull/31", "31", "", ["app/page.tsx"], [])]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)
    monkeypatch.setattr("agentloop.loops.design.live_design_sessions", lambda: {"design-31"})

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "design scan: merged=1 candidates=1 skipped=0 cap=1 live=1 slots=0" in err
    assert "no free post-merge review slots" in err
    assert "DRY-RUN would spawn" not in err


def test_design_dry_run_counts_out_of_window_live_sessions_against_cap(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="design")
    cfg.concurrency = 1

    from agentloop.loops import design

    prs = [design.MergedPR(32, "", "https://github.com/example-org/sample-repo/pull/32", "32", "", ["app/page.tsx"], [])]
    monkeypatch.setattr("agentloop.loops.design.require_gh", lambda cfg: True)
    monkeypatch.setattr("agentloop.loops.design.list_recent_merged_prs", lambda cfg: prs)
    monkeypatch.setattr("agentloop.loops.design.live_design_sessions", lambda: {"design-99"})

    design.run_tick(cfg)
    err = capsys.readouterr().err

    assert "design scan: merged=1 candidates=1 skipped=0 cap=1 live=1 slots=0" in err
    assert "no free post-merge review slots" in err
    assert "DRY-RUN would spawn" not in err


def test_declutter_dry_run_move_before_comment(make_cfg, monkeypatch, capsys):
    cfg = make_cfg(log_tag="declutter")
    # A loop-wip In-Progress issue with no PR and an old startedAt -> a true zombie.
    zombie = _issue(
        "TEAM-4",
        labels=["loop-wip"],
        extra={"startedAt": "2000-01-01T00:00:00Z", "attachments": {"nodes": []}},
    )
    fake = FakeClient(cfg, payloads={"state:In Progress": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [zombie]}}}})
    _inject(monkeypatch, fake)
    # Not an outage victim -> declutter to Needs Input.
    monkeypatch.setattr("agentloop.loops.declutter.key_outage_log", lambda cfg, key: False)

    from agentloop.loops import declutter

    declutter.run_tick(cfg)

    ops = [c[0] for c in fake.calls]
    assert "move_state" in ops and "add_comment" in ops
    # Move state BEFORE commenting (so a transient move failure doesn't stack duplicate comments).
    assert ops.index("move_state") < ops.index("add_comment")
    assert ("move_state", "id-TEAM-4", "team", "Needs Input") in fake.calls


def test_declutter_usage_outage_requeues_to_backlog(make_cfg, monkeypatch):
    cfg = make_cfg(log_tag="declutter")
    from agentloop.loops import declutter

    fake = FakeClient(cfg)
    row = declutter.StuckRow(id="id-TEAM-4", identifier="TEAM-4", scope="team", has_pr=False)
    cleaned = []
    monkeypatch.setattr("agentloop.loops.declutter.key_outage_log", lambda cfg, key: True)
    monkeypatch.setattr("agentloop.worktree.cleanup_worktree", lambda cfg, key: cleaned.append(key))

    assert declutter.declutter_one(fake, cfg, row) is True
    assert ("move_state", "id-TEAM-4", "team", "Backlog") in fake.calls
    assert ("move_state", "id-TEAM-4", "team", "Needs Input") not in fake.calls
    assert cleaned == [tracker.execution_key(cfg, row)]


def test_declutter_gc_sweeps_only_canonical_inrepo_paths(make_cfg, monkeypatch):
    import os

    cfg = make_cfg(log_tag="declutter", dry_run=False)
    cfg.grace_seconds = 60
    root = cfg.repos["agent-loop"]
    canonical_key = "al-linear-111111111111111111111111"
    live_key = "al-linear-333333333333333333333333"
    canonical = root / cfg.worktree_rel / canonical_key
    live = root / cfg.worktree_rel / live_key
    unrelated = root / cfg.worktree_rel / "notes"
    for path in (canonical, live, unrelated):
        path.mkdir(parents=True)
        os.utime(path, (0, 0))

    from agentloop.loops import declutter

    branches = {
        canonical: f"john/{canonical_key}-declutter",
        live: f"john/{live_key}-declutter",
        unrelated: "notes",
    }
    calls = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: key == live_key)
    monkeypatch.setattr("agentloop.worktree._current_branch", lambda wt: branches[wt])
    monkeypatch.setattr("agentloop.proc.git", lambda cfg, root, *args, **kwargs: calls.append((root, args)))

    declutter.gc_orphan_worktrees(cfg)

    removed = [args[3] for _root, args in calls if args[:2] == ("worktree", "remove")]
    assert str(canonical) in removed
    assert str(live) not in removed
    assert str(unrelated) not in removed
    deleted_branches = [args[2] for _root, args in calls if args[:2] == ("branch", "-D")]
    assert deleted_branches == [f"john/{canonical_key}-declutter"]


def test_declutter_gc_preserves_blocked_worktree(make_cfg, monkeypatch):
    import os
    from agentloop.loops import declutter

    cfg = make_cfg(log_tag="declutter", dry_run=False)
    cfg.grace_seconds = 60
    root = cfg.repos["agent-loop"]
    blocked = root / cfg.worktree_rel / "TEAM-1"
    blocked.mkdir(parents=True)
    os.utime(blocked, (0, 0))

    calls = []
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)
    monkeypatch.setattr("agentloop.worktree._current_branch", lambda wt: "john/team-1-blocked")
    monkeypatch.setattr("agentloop.worktree._worktree_branches", lambda root: [])
    monkeypatch.setattr("agentloop.proc.git", lambda cfg, root, *args, **kwargs: calls.append(args))

    declutter.gc_orphan_worktrees(cfg, protected_keys={"TEAM-1"})

    assert calls == []


def test_spawn_fix_routes_by_pr_repo_not_label(make_cfg, capsys):
    """spawn_fix checks out the repo the PR actually lives in (sample-e2e), even when the ticket's
    repo: label says otherwise (sample). Guards the sample-e2e routing fix — the old code routed by
    the repo: label and fetched the branch from the wrong checkout."""
    cfg = make_cfg(log_tag="debug")
    from agentloop.loops import debug

    fake = FakeClient(cfg)
    dcfg = debug.DebugConfig(skill="/triangulate", require_approval=True)
    # Ticket labelled repo:sample, but its PR is in the sample-e2e repo.
    row = debug.ReviewRow(
        id="id-TEAM-9", identifier="TEAM-9", scope="team", repo="sample",
        pr_url="https://github.com/example-org/sample-e2e/pull/9",
    )
    ok = debug.spawn_fix(fake, cfg, dcfg, row, "john/team-9-x", "example-org", "sample-e2e", 9)
    assert ok is True
    err = capsys.readouterr().err
    assert "repo=sample-e2e" in err  # routed to the PR's repo, NOT the repo:sample label
    # and the DRY-RUN spawn happens in the sample-e2e worktree, not sample/
    assert "sample-e2e/.worktrees/agent-loop" in err


def test_declutter_advances_pr_backed_to_review(make_cfg):
    """A stranded In-Progress issue WITH a PR is advanced to In Review (handed to the debug loop),
    never reaped to Needs Input. declutter_one is called directly so the assertion doesn't depend
    on gc/worktree filesystem state (the autouse fixture already stubs has_session -> False)."""
    cfg = make_cfg(log_tag="declutter")
    from agentloop.loops import declutter

    fake = FakeClient(cfg)
    row = declutter.StuckRow(id="id-TEAM-9", identifier="TEAM-9", scope="team", has_pr=True)

    assert declutter.declutter_one(fake, cfg, row) is True
    assert ("move_state", "id-TEAM-9", "team", "In Review") in fake.calls
    assert ("move_state", "id-TEAM-9", "team", "Needs Input") not in fake.calls
    ops = [c[0] for c in fake.calls]
    assert ops.index("move_state") < ops.index("add_comment")  # move before comment
