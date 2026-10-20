"""Develop pickup protection for blocked work handoffs."""

from __future__ import annotations

from datetime import datetime, timezone

from agentloop.linear import LinearClient
from agentloop.tracker import Comment, Issue
from agentloop.loops import develop
from conftest import FakeClient


class _Client(LinearClient):
    def __init__(self, nodes):
        super().__init__(None)
        self.loop_ids = ["project-1"]
        self.nodes = nodes

    def query(self, query, variables=None):
        return {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self.nodes}}}


def _issue(*, labels=None, comments=None):
    return {
        "id": "issue-1",
        "identifier": "TEAM-1",
        "title": "Blocked work",
        "team": {"id": "team-1"},
        "labels": {"nodes": [{"name": name} for name in (labels or ["loop-wip", "Feature", "repo:agent-loop"])]},
        "comments": {"nodes": comments or []},
        "attachments": {"nodes": []},
    }


def test_list_development_skips_loop_blocked_label(make_cfg):
    rows = develop.list_development(_Client([_issue(labels=["loop-wip", "loop-blocked", "Feature"])]), make_cfg())

    assert rows == []


def test_list_development_skips_recent_blocked_verdict(make_cfg):
    rows = develop.list_development(
        _Client([_issue(comments=[{
            "createdAt": "2026-08-10T18:51:48Z",
            "body": "## Freshness & Dependency Review\n- Verdict: blocked\n",
        }])]),
        make_cfg(),
        now=datetime(2026, 8, 10, 18, 52, tzinfo=timezone.utc),
    )

    # The production incident was a minute-scale requeue loop. Pin the clock so this remains a
    # regression test rather than depending on the current wall clock.
    assert rows == []


def test_marker_only_blocked_comment_releases_no_pickup(make_cfg):
    rows = develop.list_development(
        _Client([_issue(comments=[{
            "createdAt": "2026-08-10T18:51:48Z",
            "body": "Blocked verdict: dependency unavailable\n<sub>agent-loop-blocked:abc123</sub>",
        }])]),
        make_cfg(),
        now=datetime(2026, 8, 10, 18, 52, tzinfo=timezone.utc),
    )

    assert rows == []


def test_newer_comment_releases_blocked_verdict_cooldown():
    node = Issue("issue-1", "TEAM-1", "team-1", comments=(
        Comment("- Verdict: blocked", "2026-08-10T18:51:48Z"),
        Comment("The dependency is now resolved.", "2026-08-10T18:52:48Z"),
    ))

    assert not develop._latest_comment_is_recent_blocked(
        node, 3600, now=datetime(2026, 8, 10, 18, 53, tzinfo=timezone.utc)
    )


def test_reconcile_blocked_todos_applies_durable_fallback_label(make_cfg):
    cfg = make_cfg()
    client = FakeClient(cfg, payloads={"comments(first:100)": {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [_issue(comments=[{
        "createdAt": "2026-08-10T18:51:48Z",
        "body": "## Freshness & Dependency Review\n- Verdict: blocked\n",
    }])]}}}})

    parked = develop.reconcile_blocked_todos(
        client, cfg, now=datetime(2026, 8, 10, 18, 52, tzinfo=timezone.utc)
    )

    assert parked == 1
    assert ("ensure_label", "team-1", "loop-blocked") in client.calls
    assert ("add_label", "issue-1", "team-1", "loop-blocked") in client.calls


def test_todo_pr_repair_skips_blocked_ticket(make_cfg):
    node = _issue(labels=["loop-wip", "loop-blocked"], comments=[])
    node["attachments"] = {"nodes": [{"url": "https://github.com/o/r/pull/1"}]}

    assert develop.list_todo_prs(_Client([node]), make_cfg()) == []
