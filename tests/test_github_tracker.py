"""GitHub Projects tracker contract, exercised with canned GraphQL responses."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentloop.github_tracker import GitHubClient, GitHubError, _PROJECT_ITEMS_QUERY


def test_project_items_query_has_balanced_graphql_delimiters():
    assert _PROJECT_ITEMS_QUERY.count("{") == _PROJECT_ITEMS_QUERY.count("}")
    assert _PROJECT_ITEMS_QUERY.count("(") == _PROJECT_ITEMS_QUERY.count(")")


def _cfg(**overrides):
    values = dict(
        github_token="test-token", github_owner="travefy", github_project_number=7,
        github_repositories=("travefy/app",), github_repository="travefy/app",
        github_status_field="Status", project_label="loop", state_queue="Backlog",
        state_todo="Todo", state_active="In Progress", state_review="In Review",
        state_approved="Approved", state_needs_input="Needs Input", assignee_id="",
        dry_run=False, log_tag="test",
    )
    values.update(overrides)
    return SimpleNamespace(**values)


def _page(nodes, *, has_next=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


def _options():
    return [{"id": f"option-{index}", "name": name} for index, name in enumerate(
        ("Backlog", "Todo", "In Progress", "In Review", "Approved", "Needs Input"), 1
    )]


def _item(**overrides):
    item = {
        "id": "item-1", "type": "ISSUE", "isArchived": False,
        "fieldValueByName": {"name": "Backlog", "optionId": "option-1"},
        "content": {
            "id": "issue-7", "number": 7, "title": "Portable queue", "url": "https://github.com/travefy/app/issues/7",
            "body": "", "state": "OPEN", "repository": {"id": "repo-1", "nameWithOwner": "travefy/app"},
            "labels": {"nodes": [{"name": "loop"}, {"name": "repo:app"}]},
            "subIssues": {"totalCount": 0},
            "blockedBy": {"nodes": [{"state": "CLOSED"}], "pageInfo": {"hasNextPage": False, "endCursor": None}},
            "closedByPullRequestsReferences": {
                "nodes": [{"url": "https://github.com/travefy/app/pull/8"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
            "timelineItems": {
                "nodes": [{"source": {"url": "https://github.com/travefy/app/pull/9"}}],
                "pageInfo": {"hasNextPage": False, "endCursor": None},
            },
        },
    }
    item.update(overrides)
    return item


def _client(monkeypatch, *, items=None):
    client = GitHubClient(_cfg())
    calls = []

    def query(document, variables=None):
        calls.append((document, variables))
        if "repositoryOwner" in document:
            return {"data": {"repositoryOwner": {"projectV2": {"id": "project-1", "title": "Loop"}}}}
        if "fields(first:100" in document:
            return {"data": {"node": {"fields": _page([{"id": "status-1", "name": "Status", "options": _options()}])}}}
        if "items(" in document:
            return {"data": {"node": {"items": _page(items if items is not None else [_item()])}}}
        if "updateProjectV2ItemFieldValue" in document:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "item-1"}}}}
        raise AssertionError(document)

    monkeypatch.setattr(client, "query", query)
    return client, calls


def test_github_project_normalizes_allowed_open_issue(monkeypatch):
    client, _ = _client(monkeypatch)

    assert client.resolve_scope() is True
    issue, = client.issues_in_states(["Backlog"])

    assert (issue.id, issue.identifier, issue.scope) == ("issue-7", "travefy/app#7", "project-1")
    assert issue.labels == ("loop", "repo:app")
    assert issue.linked_pr_urls == ("https://github.com/travefy/app/pull/8", "https://github.com/travefy/app/pull/9")
    assert issue.has_children is False
    assert issue.blocked_by_open_issue is False


def test_github_project_filters_archived_drafts_closed_wrong_repo_and_opt_out(monkeypatch):
    wrong_repo = _item(content={**_item()["content"], "repository": {"id": "repo-2", "nameWithOwner": "travefy/other"}})
    closed = _item(id="item-2", content={**_item()["content"], "id": "issue-closed", "state": "CLOSED"})
    opt_out = _item(id="item-3", content={**_item()["content"], "id": "issue-no-label", "labels": {"nodes": []}})
    client, _ = _client(monkeypatch, items=[_item(isArchived=True), _item(type="PULL_REQUEST"), wrong_repo, closed, opt_out, _item()])

    client.resolve_scope()
    issues = client.issues_in_states(["Backlog"])

    assert [issue.id for issue in issues] == ["issue-7"]


def test_github_project_rejects_missing_status_option(monkeypatch):
    client, _ = _client(monkeypatch)
    original = client.query

    def query(document, variables=None):
        result = original(document, variables)
        if "fields(first:100" in document:
            result["data"]["node"]["fields"]["nodes"][0]["options"] = _options()[:-1]
        return result

    monkeypatch.setattr(client, "query", query)
    with pytest.raises(GitHubError, match="Needs Input"):
        client.resolve_scope()


def test_github_project_rejects_repeated_item_cursor(monkeypatch):
    client, _ = _client(monkeypatch)
    original = client.query

    def query(document, variables=None):
        if "items(" in document:
            item = _item() if variables["after"] is None else _item(
                id="item-2", content={**_item()["content"], "id": "issue-8", "number": 8}
            )
            return {"data": {"node": {"items": _page([item], has_next=True, cursor="again")}}}
        return original(document, variables)

    monkeypatch.setattr(client, "query", query)
    client.resolve_scope()
    with pytest.raises(GitHubError, match="repeated"):
        client.issues_in_states(["Backlog"])


def test_github_status_mutation_is_pinned_to_selected_project_item(monkeypatch):
    client, calls = _client(monkeypatch)
    client.resolve_scope()
    issue, = client.issues_in_states(["Backlog"])

    assert client.move_state(issue.id, issue.scope, "Todo") is True

    _, variables = calls[-1]
    assert variables == {"p": "project-1", "i": "item-1", "f": "status-1", "o": "option-2"}


def test_github_status_move_rejects_wrong_project_scope(monkeypatch):
    client, _ = _client(monkeypatch)
    client.resolve_scope()
    issue, = client.issues_in_states(["Backlog"])

    with pytest.raises(GitHubError, match="configured GitHub Project"):
        client.move_state(issue.id, "other-project", "Todo")


def test_github_project_rejects_duplicate_issue_item(monkeypatch):
    duplicate = _item(id="item-2")
    client, _ = _client(monkeypatch, items=[_item(), duplicate])
    client.resolve_scope()

    with pytest.raises(GitHubError, match="more than once"):
        client.issues_in_states(["Backlog"])


def test_github_status_move_requires_the_selected_item_in_response(monkeypatch):
    client, _ = _client(monkeypatch)
    original = client.query

    def query(document, variables=None):
        if "updateProjectV2ItemFieldValue" in document:
            return {"data": {"updateProjectV2ItemFieldValue": {"projectV2Item": {"id": "other-item"}}}}
        return original(document, variables)

    monkeypatch.setattr(client, "query", query)
    client.resolve_scope()
    issue, = client.issues_in_states(["Backlog"])

    assert client.move_state(issue.id, issue.scope, "Todo") is False


@pytest.mark.parametrize("connection", ["blockedBy", "closedByPullRequestsReferences", "timelineItems"])
def test_github_project_rejects_truncated_decision_connections(monkeypatch, connection):
    content = {**_item()["content"]}
    content[connection] = {**content[connection], "pageInfo": {"hasNextPage": True, "endCursor": "more"}}
    client, _ = _client(monkeypatch, items=[_item(content=content)])
    client.resolve_scope()

    with pytest.raises(GitHubError, match="pagination bound"):
        client.issues_in_states(["Backlog"])
