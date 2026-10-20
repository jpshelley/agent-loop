"""list_ready() selection logic: umbrellas dropped, blocked issues dropped, priority ordering."""

from __future__ import annotations

from agentloop.linear import LinearClient
from agentloop.loops import dispatch


class _Client(LinearClient):
    def __init__(self, nodes):
        super().__init__(None)
        self.loop_ids = ["p"]
        self._nodes = nodes

    def query(self, query, variables=None):
        return {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self._nodes}}}


def _issue(ident, *, priority=1, children=0, blocks=None, labels=None):
    return {
        "id": f"id-{ident}",
        "identifier": ident,
        "title": ident,
        "priority": priority,
        "team": {"id": "team"},
        "labels": {"nodes": [{"name": n} for n in (labels or [])]},
        "attachments": {"nodes": []},
        "children": {"nodes": [{"id": f"c{i}"} for i in range(children)]},
        # Mirror Linear's real inverseRelations shape: the BLOCKER is `.issue` (the relation
        # source); `.relatedIssue` points back at THIS issue (always "backlog" here). The filter
        # must read `.issue.state.type` -- pinning relatedIssue=self guards against regressing to it.
        "inverseRelations": {
            "nodes": [
                {
                    "type": "blocks",
                    "issue": {"state": {"type": s}},
                    "relatedIssue": {"state": {"type": "backlog"}},
                }
                for s in (blocks or [])
            ]
        },
    }


def test_umbrella_with_children_dropped(make_cfg):
    rows = dispatch.list_ready(_Client([_issue("TEAM-1", children=2), _issue("TEAM-2")]), make_cfg())
    assert [r.identifier for r in rows] == ["TEAM-2"]


def test_blocked_by_open_blocker_dropped(make_cfg):
    nodes = [
        _issue("TEAM-1", blocks=["started"]),        # blocked by a non-terminal issue -> dropped
        _issue("TEAM-2", blocks=["completed"]),      # blocker done -> kept
        _issue("TEAM-3", blocks=["completed", "canceled"]),  # all terminal -> kept
        _issue("TEAM-4"),                            # no relations -> kept
    ]
    rows = dispatch.list_ready(_Client(nodes), make_cfg())
    assert {r.identifier for r in rows} == {"TEAM-2", "TEAM-3", "TEAM-4"}


def test_priority_zero_sorts_last(make_cfg):
    nodes = [_issue("TEAM-A", priority=0), _issue("TEAM-B", priority=2), _issue("TEAM-C", priority=1)]
    rows = dispatch.list_ready(_Client(nodes), make_cfg())
    assert [r.identifier for r in rows] == ["TEAM-C", "TEAM-B", "TEAM-A"]


def test_repo_and_type_derived_from_labels(make_cfg):
    nodes = [_issue("TEAM-1", labels=["repo:sample", "Bug"]), _issue("TEAM-2", labels=["Improvement"])]
    rows = dispatch.list_ready(_Client(nodes), make_cfg())
    by_id = {r.identifier: r for r in rows}
    assert (by_id["TEAM-1"].repo, by_id["TEAM-1"].type) == ("sample", "bug")
    assert (by_id["TEAM-2"].repo, by_id["TEAM-2"].type) == ("", "feature")


def test_pr_backed_issue_dropped_from_ready_queue(make_cfg):
    pr_backed = _issue("TEAM-1", labels=["repo:sample-repo", "Feature"])
    pr_backed["attachments"] = {"nodes": [{"url": "https://github.com/o/r/pull/1"}]}
    fresh = _issue("TEAM-2", labels=["repo:sample-repo", "Feature"])
    rows = dispatch.list_ready(_Client([pr_backed, fresh]), make_cfg())
    assert [r.identifier for r in rows] == ["TEAM-2"]


def test_loop_blocked_issue_dropped_from_ready_queue(make_cfg):
    blocked = _issue("TEAM-1", labels=["repo:sample-repo", "Feature", "loop-blocked"])
    ready = _issue("TEAM-2", labels=["repo:sample-repo", "Feature"])

    rows = dispatch.list_ready(_Client([blocked, ready]), make_cfg())

    assert [row.identifier for row in rows] == ["TEAM-2"]


def test_loop_blocked_queued_pr_is_not_repaired(make_cfg):
    blocked = _issue("TEAM-1", labels=["repo:sample-repo", "Feature", "loop-wip", "loop-blocked"])
    blocked["attachments"] = {"nodes": [{"url": "https://github.com/o/r/pull/1"}]}

    assert dispatch.list_queued_prs(_Client([blocked]), make_cfg()) == []
