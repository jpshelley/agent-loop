"""Tracker boundary contracts, exercised without credentials or live API calls."""

from __future__ import annotations

import pytest

from agentloop import tracker
from agentloop.linear import LinearClient, LinearError


def _node(**overrides):
    node = {
        "id": "linear-id",
        "identifier": "TEAM-7",
        "team": {"id": "team-id"},
        "title": "Keep the adapter boundary portable",
        "priority": 0,
        "labels": {"nodes": [{"name": "repo:sample-repo"}, {"name": "Bug"}]},
        "attachments": {"nodes": [
            {"url": "https://example.com/design"},
            {"url": "https://github.com/example/project/pull/7"},
            {"url": "https://github.com/example/project/issues/8"},
        ]},
        "comments": {"nodes": [{"body": "Ready for review", "createdAt": "2026-10-16T12:00:00Z"}]},
        "children": {"nodes": [{"id": "child-id"}]},
        "inverseRelations": {"nodes": [{
            "type": "blocks",
            "issue": {"state": {"type": "started"}},
            "relatedIssue": {"state": {"type": "completed"}},
        }]},
        "startedAt": "2026-10-15T12:00:00Z",
    }
    node.update(overrides)
    return node


def _client(make_cfg, monkeypatch, nodes):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]
    calls = []

    def query(document, variables=None):
        calls.append((document, variables))
        return {"data": {"issues": {"nodes": nodes, "pageInfo": {"hasNextPage": False, "endCursor": None}}}}

    monkeypatch.setattr(client, "query", query)
    return client, calls


def test_linear_normalizes_issue_facts_at_adapter_boundary(make_cfg, monkeypatch):
    client, _ = _client(make_cfg, monkeypatch, [_node()])

    issue, = client.issues_in_states(["In Progress"], include_comments=True)

    assert isinstance(issue, tracker.Issue)
    assert (issue.id, issue.identifier, issue.scope) == ("linear-id", "TEAM-7", "team-id")
    assert issue.title == "Keep the adapter boundary portable"
    assert issue.labels == ("repo:sample-repo", "Bug")
    assert issue.linked_pr_urls == ("https://github.com/example/project/pull/7",)
    assert issue.comments == (tracker.Comment("Ready for review", "2026-10-16T12:00:00Z"),)
    assert issue.priority_rank == 99
    assert issue.has_children is True
    assert issue.blocked_by_open_issue is True
    assert issue.started_at == "2026-10-15T12:00:00Z"


@pytest.mark.parametrize("state,blocked", [("completed", False), ("canceled", False), ("backlog", True)])
def test_linear_uses_blocker_source_state(make_cfg, monkeypatch, state, blocked):
    relation = {"type": "blocks", "issue": {"state": {"type": state}},
                "relatedIssue": {"state": {"type": "started"}}}
    client, _ = _client(make_cfg, monkeypatch, [_node(inverseRelations={"nodes": [relation]})])

    issue, = client.issues_in_states(["Backlog"])

    assert issue.blocked_by_open_issue is blocked


def test_linear_ignores_nonblocking_relations_and_preserves_priority(make_cfg, monkeypatch):
    client, _ = _client(make_cfg, monkeypatch, [_node(
        priority=2, children={"nodes": []},
        inverseRelations={"nodes": [{"type": "related", "issue": {"state": {"type": "started"}}}]},
    )])

    issue, = client.issues_in_states(["Backlog"])

    assert issue.priority_rank == 2
    assert issue.has_children is False
    assert issue.blocked_by_open_issue is False


@pytest.mark.parametrize("response", [
    {}, {"data": None}, {"data": {}}, {"data": {"issues": None}},
    {"data": {"issues": {}}}, {"data": {"issues": {"nodes": None}}},
    {"data": {"issues": {"nodes": {}}}},
])
def test_linear_rejects_missing_issue_collection(make_cfg, monkeypatch, response):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: response)

    with pytest.raises(tracker.TrackerError):
        client.issues_in_states(["Backlog"])


def test_linear_accepts_empty_issue_collection(make_cfg, monkeypatch):
    client, _ = _client(make_cfg, monkeypatch, [])

    assert client.issues_in_states(["Backlog"]) == []


@pytest.mark.parametrize("node", [None, {}, {"id": "id"}, {"identifier": "TEAM-7"}])
def test_linear_rejects_issue_without_stable_identity(make_cfg, monkeypatch, node):
    client, _ = _client(make_cfg, monkeypatch, [node])

    with pytest.raises(tracker.TrackerError):
        client.issues_in_states(["Backlog"])


@pytest.mark.parametrize("labels", [
    None, [], {}, {"nodes": None}, {"nodes": {}}, {"nodes": [None]},
    {"nodes": [{}]}, {"nodes": [{"name": ""}]}, {"nodes": [{"name": 7}]},
])
def test_linear_rejects_unreliable_issue_ownership_labels(make_cfg, monkeypatch, labels):
    client, _ = _client(make_cfg, monkeypatch, [_node(labels=labels)])

    with pytest.raises(tracker.TrackerError):
        client.issues_in_states(["In Progress"])


def test_linear_accepts_issue_with_no_labels(make_cfg, monkeypatch):
    client, _ = _client(make_cfg, monkeypatch, [_node(labels={"nodes": []})])

    issue, = client.issues_in_states(["In Progress"])

    assert issue.labels == ()


@pytest.mark.parametrize("newest,direction", [(False, "first"), (True, "last")])
def test_linear_requests_comment_order_needed_by_consumer(make_cfg, monkeypatch, newest, direction):
    client, calls = _client(make_cfg, monkeypatch, [])

    client.issues_in_states(["In Progress"], include_comments=True, newest_comments=newest)

    document, variables = calls[0]
    assert f"comments({direction}:100)" in document
    assert "body createdAt" in document
    assert variables == {"ids": ["project-id"], "s": ["In Progress"], "after": None}


def test_linear_reads_every_issue_page_before_returning(make_cfg, monkeypatch):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]
    calls = []

    def query(document, variables=None):
        calls.append((document, variables))
        if variables["after"] is None:
            return {"data": {"issues": {
                "nodes": [_node(id="first-id", identifier="TEAM-1")],
                "pageInfo": {"hasNextPage": True, "endCursor": "page-one"},
            }}}
        assert variables["after"] == "page-one"
        return {"data": {"issues": {
            "nodes": [_node(id="second-id", identifier="TEAM-2")],
            "pageInfo": {"hasNextPage": False, "endCursor": "page-two"},
        }}}

    monkeypatch.setattr(client, "query", query)

    issues = client.issues_in_states(["Backlog"])

    assert [issue.identifier for issue in issues] == ["TEAM-1", "TEAM-2"]
    assert len(calls) == 2
    assert all("issues(first:25" in document for document, _ in calls)
    assert all("pageInfo{hasNextPage endCursor}" in document for document, _ in calls)


@pytest.mark.parametrize("page_info", [
    None, {}, {"hasNextPage": "false"}, {"hasNextPage": 0},
    {"hasNextPage": True}, {"hasNextPage": True, "endCursor": None},
    {"hasNextPage": True, "endCursor": ""}, {"hasNextPage": True, "endCursor": 7},
])
def test_linear_rejects_invalid_pagination_metadata(make_cfg, monkeypatch, page_info):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: {
        "data": {"issues": {"nodes": [_node()], "pageInfo": page_info}},
    })

    with pytest.raises(tracker.TrackerError):
        client.issues_in_states(["Backlog"])


def test_linear_rejects_repeated_cursor_without_returning_partial_results(make_cfg, monkeypatch):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]
    calls = []

    def query(document, variables=None):
        calls.append(variables["after"])
        assert len(calls) <= 2, "repeated cursor must not loop forever"
        return {"data": {"issues": {
            "nodes": [_node()], "pageInfo": {"hasNextPage": True, "endCursor": "same-page"},
        }}}

    monkeypatch.setattr(client, "query", query)

    with pytest.raises(tracker.TrackerError):
        client.issues_in_states(["Backlog"])
    assert calls == [None, "same-page"]


def test_linear_later_page_failure_does_not_look_like_complete_inventory(make_cfg, monkeypatch):
    client = LinearClient(make_cfg())
    client.loop_ids = ["project-id"]

    def query(document, variables=None):
        if variables["after"] is None:
            return {"data": {"issues": {
                "nodes": [_node()], "pageInfo": {"hasNextPage": True, "endCursor": "page-one"},
            }}}
        raise LinearError("second page unavailable")

    monkeypatch.setattr(client, "query", query)

    with pytest.raises(tracker.TrackerError, match="second page unavailable"):
        client.issues_in_states(["In Progress"])


@pytest.mark.parametrize("response", [
    {}, {"data": None}, {"data": {}}, {"data": {"projects": None}},
    {"data": {"projects": {}}}, {"data": {"projects": {"nodes": None}}},
    {"data": {"projects": {"nodes": {}}}},
])
def test_linear_rejects_malformed_project_discovery(make_cfg, monkeypatch, response):
    client = LinearClient(make_cfg())
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: response)

    with pytest.raises(tracker.TrackerError):
        client.resolve_scope()


@pytest.mark.parametrize("project", [
    None, {}, {"id": "project-id"},
    {"id": "project-id", "name": "Project", "labels": {"nodes": None}},
    {"id": "project-id", "name": "Project", "labels": {"nodes": [{}]}},
])
def test_linear_rejects_malformed_project_instead_of_reporting_no_scope(make_cfg, monkeypatch, project):
    client = LinearClient(make_cfg())
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: {
        "data": {"projects": {"nodes": [project]}},
    })

    with pytest.raises(tracker.TrackerError):
        client.resolve_scope()


def test_linear_valid_empty_project_collection_has_no_scope(make_cfg, monkeypatch):
    client = LinearClient(make_cfg())
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: {"data": {"projects": {"nodes": []}}})

    assert client.resolve_scope() is False


@pytest.mark.parametrize("loop_name,reader", [("debug", "list_in_review"), ("deploy", "list_approved")])
def test_review_and_deploy_select_valid_pr_after_malformed_link(make_cfg, loop_name, reader):
    from agentloop.loops import debug, deploy

    cfg = make_cfg()
    valid = "https://github.com/example/project/pull/7"

    class NeutralTracker:
        def issues_in_states(self, states, **kwargs):
            return [tracker.Issue(
                id="issue-id", identifier="TEAM-7", scope="scope", labels=(cfg.wip_label,),
                linked_pr_urls=("https://github.com/example/project/pull/not-a-number", valid),
            )]

    module = {"debug": debug, "deploy": deploy}[loop_name]
    row, = getattr(module, reader)(NeutralTracker(), cfg)

    assert row.pr_url == valid


def test_required_label_fails_closed_when_creation_does_not_provide_id(make_cfg, monkeypatch):
    client = LinearClient(make_cfg(dry_run=False))
    monkeypatch.setattr(client, "ensure_label", lambda *args: None)
    applied = []
    monkeypatch.setattr(client, "add_label", lambda *args: applied.append(args) or True)

    assert client.ensure_required_label("issue-id", "team-id", "loop-wip") is False
    assert applied == []


def test_required_label_checks_creation_success_before_applying(make_cfg, monkeypatch):
    client = LinearClient(make_cfg(dry_run=False))
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: {
        "data": {"issueLabelCreate": {"success": False, "issueLabel": {"id": "rejected-id"}}},
    })
    applied = []
    monkeypatch.setattr(client, "add_label", lambda *args: applied.append(args) or True)

    assert client.ensure_required_label("issue-id", "team-id", "loop-wip") is False
    assert "team-id|loop-wip" not in client.label_id
    assert applied == []


def test_required_label_creates_before_applying(make_cfg, monkeypatch):
    client = LinearClient(make_cfg(dry_run=False))
    operations = []

    def query(document, variables=None):
        operations.append((document, variables))
        if "issueLabelCreate" in document:
            return {"data": {"issueLabelCreate": {"success": True, "issueLabel": {"id": "label-id"}}}}
        return {"data": {"issueAddLabel": {"success": True}}}

    monkeypatch.setattr(client, "query", query)

    assert client.ensure_required_label("issue-id", "team-id", "loop-wip") is True
    assert len(operations) == 2
    assert "issueLabelCreate" in operations[0][0]
    assert "issueAddLabel" in operations[1][0]
    assert operations[1][1] == {"id": "issue-id", "l": "label-id"}


def test_required_label_propagates_application_failure(make_cfg, monkeypatch):
    client = LinearClient(make_cfg(dry_run=False))
    client.label_id["team-id|loop-wip"] = "label-id"
    monkeypatch.setattr(client, "add_label", lambda *args: False)

    assert client.ensure_required_label("issue-id", "team-id", "loop-wip") is False


def test_required_label_dry_run_never_calls_api(make_cfg, monkeypatch):
    client = LinearClient(make_cfg(dry_run=True))
    monkeypatch.setattr(client, "query", lambda *args, **kwargs: pytest.fail("dry-run must not create or apply labels"))

    assert client.ensure_required_label("issue-id", "team-id", "loop-wip") is True


def test_linear_errors_are_tracker_errors():
    assert issubclass(LinearError, tracker.TrackerError)


def test_resolver_selects_linear_adapter(make_cfg, monkeypatch):
    cfg = make_cfg()
    sentinel = object()
    seen = []
    monkeypatch.setattr("agentloop.linear.LinearClient", lambda selected: seen.append(selected) or sentinel)

    assert tracker.resolve(cfg) is sentinel
    assert seen == [cfg]


def test_resolver_selects_github_adapter(make_cfg, monkeypatch):
    cfg = make_cfg()
    cfg.tracker = "github"
    sentinel = object()
    seen = []
    monkeypatch.setattr("agentloop.github_tracker.GitHubClient", lambda selected: seen.append(selected) or sentinel)

    assert tracker.resolve(cfg) is sentinel
    assert seen == [cfg]


@pytest.mark.parametrize("label_available", [True, False])
def test_dispatch_accepts_neutral_tracker_without_linear_internals(make_cfg, monkeypatch, label_available):
    from agentloop.loops import dispatch

    cfg = make_cfg()

    class NeutralTracker:
        __slots__ = ("calls",)

        def __init__(self):
            self.calls = []

        def resolve_scope(self):
            self.calls.append(("resolve_scope",))
            return True

        def issues_in_states(self, states, **kwargs):
            if list(states) != [cfg.state_queue]:
                return []
            return [tracker.Issue(
                id="neutral-id", identifier="example/repo#7", scope="example/repo",
                title="A tracker independent issue", labels=("repo:sample-repo", "Bug"),
            )]

        def ensure_scope(self, scope):
            self.calls.append(("ensure_scope", scope))

        def ensure_required_label(self, issue_id, scope, label):
            self.calls.append(("ensure_required_label", issue_id, scope, label))
            return label_available

        def move_state(self, issue_id, scope, state):
            self.calls.append(("move_state", issue_id, scope, state))
            return True

    client = NeutralTracker()
    monkeypatch.setattr(tracker, "resolve", lambda selected: client)
    monkeypatch.setattr("agentloop.proc.has_session", lambda key: False)

    dispatch.run_tick(cfg)

    assert ("resolve_scope",) in client.calls
    assert ("ensure_scope", "example/repo") in client.calls
    assert ("ensure_required_label", "neutral-id", "example/repo", cfg.wip_label) in client.calls
    moved = ("move_state", "neutral-id", "example/repo", cfg.state_todo) in client.calls
    assert moved is label_available
