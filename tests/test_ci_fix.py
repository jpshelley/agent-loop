"""debug ci_fix path: one auto-fix attempt per failing head commit, then escalate to Needs Input.

Covers _handle_ci_fix's attempt-vs-escalate decision (unit) and debug_one's routing of the
`ci_fix` classification, including the AGENT_LOOP_DEBUG_CI_FIX kill switch.
"""

from __future__ import annotations

import pytest

from agentloop.loops import debug
from agentloop.loops.debug import DebugConfig, ReviewRow, Tally

SHA = "abc123def4567890"


def _dcfg(cfg, **over):
    d = DebugConfig.from_env(cfg, environ={})  # defaults: ci_fix on, max 2, approval on
    for k, v in over.items():
        setattr(d, k, v)
    return d


def _row():
    return ReviewRow(id="iss-1", identifier="TEAM-9", scope="team-1", repo="sample-e2e",
                     pr_url="https://github.com/example-org/sample-e2e/pull/56")


def _state(
    sha=SHA,
    checks="FAILURE",
    branch="john/TEAM-9-x",
    state="OPEN",
    threads=None,
    decision=None,
    mergeable=None,
    merge_state=None,
):
    return {"data": {"repository": {"pullRequest": {
        "state": state,
        "mergeable": mergeable,
        "mergeStateStatus": merge_state,
        "reviewDecision": decision,
        "headRefName": branch,
        "commits": {"nodes": [{"commit": {"oid": sha, "committedDate": "2026-07-09T00:00:00Z",
                                          "statusCheckRollup": {"state": checks}}}]},
        "reviewThreads": {"nodes": threads or []},
    }}}}


@pytest.fixture(autouse=True)
def _stub_spawn(monkeypatch):
    """Replace the real checkout+tmux spawn with a recorder so these tests stay pure."""
    spawned = []

    def fake_spawn(client, cfg, dcfg, row, branch, owner, repo, num, reason="review comments"):
        spawned.append({"key": row.identifier, "branch": branch, "num": num, "reason": reason})
        return True

    monkeypatch.setattr(debug, "spawn_fix", fake_spawn)
    return spawned


# ── _handle_ci_fix: attempt vs escalate ─────────────────────────────────────

def test_first_sighting_spawns_fix_and_records_marker(make_cfg, fake_client_cls, _stub_spawn):
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg), _row(), _state(), "example-org", "sample-e2e", 56, tally)

    assert len(_stub_spawn) == 1 and _stub_spawn[0]["num"] == 56
    assert ("add_ci_fix_marker", "iss-1", SHA) in client.calls  # attempt recorded on the ticket
    assert tally.ci_fixed_n == 1 and tally.escalated_n == 0
    assert not any(c[0] == "move_state" for c in client.calls)  # ticket stays In Review


def test_same_failing_sha_escalates_to_needs_input(make_cfg, fake_client_cls, _stub_spawn):
    cfg = make_cfg()
    client = fake_client_cls(cfg, ci_fix_markers=[SHA])  # already tried THIS commit
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg), _row(), _state(), "example-org", "sample-e2e", 56, tally)

    assert _stub_spawn == []  # no second attempt on the same red commit
    assert ("move_state", "iss-1", "team-1", cfg.state_needs_input) in client.calls
    assert ("assign_issue", "iss-1") in client.calls
    assert any(c[0] == "add_comment" for c in client.calls)
    assert tally.escalated_n == 1 and tally.ci_fixed_n == 0


def test_attempt_budget_exhausted_across_commits_escalates(make_cfg, fake_client_cls, _stub_spawn):
    cfg = make_cfg()
    client = fake_client_cls(cfg, ci_fix_markers=["oldsha1", "oldsha2"])  # 2 prior attempts, max=2
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg, ci_fix_max=2), _row(), _state(), "o", "r", 56, tally)

    assert _stub_spawn == []
    assert ("move_state", "iss-1", "team-1", cfg.state_needs_input) in client.calls
    assert tally.escalated_n == 1


def test_second_distinct_commit_still_gets_an_attempt_under_budget(make_cfg, fake_client_cls, _stub_spawn):
    cfg = make_cfg()
    client = fake_client_cls(cfg, ci_fix_markers=["oldsha1"])  # 1 prior attempt, max=2, new SHA
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg, ci_fix_max=2), _row(), _state(), "o", "r", 56, tally)

    assert len(_stub_spawn) == 1  # a pushed fix (new SHA) earns a fresh attempt
    assert tally.ci_fixed_n == 1 and tally.escalated_n == 0


def test_ci_fix_is_not_throttled_by_debug_capacity(make_cfg, fake_client_cls, _stub_spawn):
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg), _row(), _state(), "o", "r", 56, tally)

    assert len(_stub_spawn) == 1 and tally.ci_fixed_n == 1 and tally.escalated_n == 0
    assert ("add_ci_fix_marker", "iss-1", SHA) in client.calls


def test_missing_head_sha_is_a_safe_noop(make_cfg, fake_client_cls, _stub_spawn):
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug._handle_ci_fix(client, cfg, _dcfg(cfg), _row(), _state(sha=None), "o", "r", 56, tally)

    assert _stub_spawn == [] and not client.calls  # can't track idempotently -> do nothing


# ── debug_one routing + kill switch ─────────────────────────────────────────

def test_debug_one_routes_red_ci_to_ci_fix(make_cfg, fake_client_cls, _stub_spawn, monkeypatch):
    monkeypatch.setattr(debug.proc, "has_session", lambda key: False)
    monkeypatch.setattr(debug.github, "pr_review_state", lambda o, r, n, *_: _state())
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug.debug_one(client, cfg, _dcfg(cfg), _row(), tally)
    assert len(_stub_spawn) == 1 and tally.ci_fixed_n == 1


def test_debug_one_routes_merge_gate_blocker_to_fix(make_cfg, fake_client_cls, _stub_spawn, monkeypatch):
    monkeypatch.setattr(debug.proc, "has_session", lambda key: False)
    monkeypatch.setattr(
        debug.github,
        "pr_review_state",
        lambda o, r, n, *_: _state(
            checks="SUCCESS",
            decision="APPROVED",
            mergeable="MERGEABLE",
            merge_state="BEHIND",
        ),
    )
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug.debug_one(client, cfg, _dcfg(cfg), _row(), tally)

    assert len(_stub_spawn) == 1
    assert _stub_spawn[0]["reason"] == "merge gate blocked"
    assert tally.fixed_n == 1 and tally.ci_fixed_n == 0
    assert not any(c[0] == "move_state" for c in client.calls)


def test_debug_one_ci_fix_kill_switch_leaves_in_review(make_cfg, fake_client_cls, _stub_spawn, monkeypatch):
    monkeypatch.setattr(debug.proc, "has_session", lambda key: False)
    monkeypatch.setattr(debug.github, "pr_review_state", lambda o, r, n, *_: _state())
    cfg, client = make_cfg(), fake_client_cls(make_cfg())
    tally = Tally()
    debug.debug_one(client, cfg, _dcfg(cfg, ci_fix_enabled=False), _row(), tally)
    assert _stub_spawn == [] and tally.ci_fixed_n == 0 and not client.calls
