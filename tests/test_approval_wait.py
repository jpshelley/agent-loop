"""debug approval-wait path: bound missing approval signals per PR head commit."""

from __future__ import annotations

from datetime import datetime, timezone

from agentloop.loops import debug
from agentloop.loops.debug import DebugConfig, ReviewRow, Tally

SHA = "abc123def4567890"
NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _dcfg(cfg, **over):
    d = DebugConfig.from_env(cfg, environ={})
    # This file exercises the bot approval path, which ships off by default — opt in so the
    # scenarios under test (a score plus a fresh reaction) can reach "approve" at all.
    d.score_bot_authors = frozenset({"greptile-apps"})
    d.approval_reactors = frozenset({"chatgpt-codex-connector"})
    for key, value in over.items():
        setattr(d, key, value)
    return d


def _row():
    return ReviewRow("iss-1", "TEAM-9", "team-1", "agent-loop", "https://github.com/example-org/agent-loop/pull/56")


def _reaction(login="chatgpt-codex-connector[bot]", created="2026-08-08T11:01:00Z"):
    return {"content": "THUMBS_UP", "user": {"login": login}, "createdAt": created}


def _state(*, body=None, reactions=None, decision="REVIEW_REQUIRED", sha=SHA):
    return {"data": {"repository": {"pullRequest": {
        "state": "OPEN", "mergeable": "MERGEABLE", "mergeStateStatus": "CLEAN",
        "reviewDecision": decision, "body": body, "reactions": {"nodes": reactions or []},
        "commits": {"nodes": [{"commit": {"oid": sha, "committedDate": "2026-08-08T11:00:00Z",
                                              "statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviewThreads": {"nodes": []},
    }}}}


def _run(monkeypatch, cfg, client, dcfg, state):
    monkeypatch.setattr(debug.proc, "has_session", lambda key: False)
    monkeypatch.setattr(debug.github, "pr_review_state", lambda owner, repo, num, *_: state)
    monkeypatch.setattr(debug, "_now", lambda: NOW)
    tally = Tally()
    debug.debug_one(client, cfg, dcfg, _row(), tally)
    return tally


def test_within_approval_window_waits(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T11:01:00Z"})
    tally = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))

    assert not any(call[0] == "move_state" for call in client.calls)
    assert tally.escalated_n == 0


def test_missing_score_escalates_after_window(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T10:59:00Z"})
    tally = _run(
        monkeypatch, cfg, client, _dcfg(cfg),
        _state(body="<!-- greptile_comment --><h3>Confidence Score: 3/5</h3>", reactions=[_reaction()]),
    )

    operations = [call[0] for call in client.calls]
    assert operations.index("add_approval_wait_escalation") < operations.index("move_state")
    assert ("move_state", "iss-1", "team-1", cfg.state_needs_input) in client.calls
    assert ("add_approval_wait_escalation", "iss-1", SHA) in client.calls
    assert tally.escalated_n == 1


def test_stale_codex_reaction_escalates_after_window(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T10:59:00Z"})
    _run(
        monkeypatch, cfg, client, _dcfg(cfg),
        _state(
            body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>",
            reactions=[_reaction(created="2026-08-08T10:59:59Z")],
        ),
    )

    assert ("add_approval_wait_escalation", "iss-1", SHA) in client.calls


def test_approval_arriving_before_window_closes_approves(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T11:01:00Z"})
    tally = _run(
        monkeypatch, cfg, client, _dcfg(cfg),
        _state(
            body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>", reactions=[_reaction()],
        ),
    )

    assert ("move_state", "iss-1", "team-1", cfg.state_approved) in client.calls
    assert not any(call[0] == "add_approval_wait_escalation" for call in client.calls)
    assert tally.escalated_n == 0


def test_existing_handoff_does_not_duplicate_comment(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(
        cfg,
        approval_wait_markers={SHA: "2026-08-08T10:59:00Z"},
        approval_wait_escalations={SHA},
    )
    tally = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))

    assert ("move_state", "iss-1", "team-1", cfg.state_needs_input) in client.calls
    assert not any(call[0] == "add_approval_wait_escalation" for call in client.calls)
    assert tally.escalated_n == 1


def test_failed_handoff_comment_does_not_move_state(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T10:59:00Z"})
    client.add_approval_wait_escalation = lambda *args: False

    tally = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))

    assert not any(call[0] == "move_state" for call in client.calls)
    assert tally.escalated_n == 0


def test_failed_state_move_retries_without_duplicate_handoff(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={SHA: "2026-08-08T10:59:00Z"})
    attempts = iter([False, True])
    client.move_state = lambda *args: client.calls.append(("move_state", *args)) or next(attempts)

    first = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))
    second = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))

    assert first.escalated_n == 0
    assert second.escalated_n == 1
    assert sum(call[0] == "add_approval_wait_escalation" for call in client.calls) == 1


def test_new_head_starts_a_fresh_approval_window(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg, approval_wait_markers={"oldabc123": "2026-08-08T10:00:00Z"})
    tally = _run(monkeypatch, cfg, client, _dcfg(cfg), _state(body=None))

    assert ("add_approval_wait_marker", "iss-1", SHA) in client.calls
    assert not any(call[0] == "move_state" for call in client.calls)
    assert tally.escalated_n == 0


def test_approval_requirement_disabled_is_unchanged(make_cfg, fake_client_cls, monkeypatch):
    cfg = make_cfg()
    client = fake_client_cls(cfg)
    tally = _run(monkeypatch, cfg, client, _dcfg(cfg, require_approval=False), _state(body=None))

    assert ("move_state", "iss-1", "team-1", cfg.state_approved) in client.calls
    assert not any(call[0] == "add_approval_wait_marker" for call in client.calls)
    assert tally.escalated_n == 0


def test_escalation_reason_names_the_review_not_unconfigured_bots(make_cfg, fake_client_cls, monkeypatch):
    # With no bot gates configured — the default — the handoff must say a review is missing
    # rather than citing Greptile or Codex signals the operator never opted into.
    cfg = make_cfg()
    dcfg = _dcfg(cfg, score_bot_authors=frozenset(), approval_reactors=frozenset())
    pr = _state()["data"]["repository"]["pullRequest"]

    gap = debug._approval_gap(pr, dcfg)

    assert gap is not None
    _, missing = gap
    assert missing == ["no approving review on the pull request"]
    assert not any("greptile" in reason.lower() or "codex" in reason.lower() for reason in missing)
