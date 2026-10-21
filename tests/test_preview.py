"""Personal ownership and hierarchy boundaries for the query-only selector."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from agentloop.cli import main
from agentloop.github_tracker import GitHubError
from agentloop.preview import Candidate, PreviewClient, PreviewProfile, evaluate, load_profile


def profile(**overrides):
    values = dict(
        owner="team", number=44, repositories=("team/planning",), assignee="alice",
        repository_routes={"team/planning": "team/app"}, inherit_parent=True,
    )
    values.update(overrides)
    return PreviewProfile(**values)


def candidate(**overrides):
    values = dict(
        id="leaf", identifier="team/planning#2", title="An issue",
        url="https://github.com/team/planning/issues/2", repository="team/planning",
        state="OPEN", status="Ready", labels=("loop",), assignees=("alice",),
        milestone=None, parent=None, has_children=False, blocked=False, archived=False,
    )
    values.update(overrides)
    return Candidate(**values)


def parent(**overrides):
    return candidate(**{
        "id": "root", "identifier": "team/planning#1", "has_children": True,
        **overrides,
    })


def test_direct_opt_in_and_explicit_route():
    decision, = evaluate(profile(), [candidate()])
    assert decision.eligible
    assert decision.code_repository == "team/app"
    assert decision.work_root is None


def test_ready_opted_in_parent_authorizes_unassigned_unlabeled_child():
    child, root = evaluate(profile(), [candidate(parent="root", labels=(), assignees=()), parent()])
    assert child.eligible
    assert child.work_root == "team/planning#1"
    assert not root.eligible
    assert "has_sub_issues" in root.reasons


@pytest.mark.parametrize(("changes", "reason"), [
    ({"archived": True}, "archived"),
    ({"state": "CLOSED"}, "closed"),
    ({"status": "Triage"}, "not_ready"),
    ({"status": "In Progress"}, "not_ready"),
    ({"status": "In Review"}, "not_ready"),
    ({"status": "In QA"}, "not_ready"),
    ({"status": "Done"}, "not_ready"),
    ({"status": ""}, "not_ready"),
    ({"has_children": True}, "has_sub_issues"),
    ({"blocked": True}, "open_blocker"),
    ({"labels": ()}, "missing_loop_label"),
    ({"assignees": ()}, "not_assigned_to_operator"),
    ({"assignees": ("bob",)}, "assigned_to_another_user"),
    ({"assignees": ("alice", "bob")}, "assigned_to_another_user"),
    ({"repository": "team/other"}, "repository_not_allowed"),
    ({"parent": "unseen"}, "parent_outside_project"),
])
def test_leaf_exclusions(changes, reason):
    decision, = evaluate(profile(), [candidate(**changes)])
    assert not decision.eligible
    assert reason in decision.reasons


@pytest.mark.parametrize("changes", [
    {"status": "Triage"}, {"status": "In Progress"}, {"labels": ()}, {"assignees": ()},
    {"assignees": ("bob",)}, {"assignees": ("alice", "bob")}, {"blocked": True},
    {"state": "CLOSED"}, {"archived": True}, {"repository": "team/other"},
])
def test_parent_inheritance_requires_one_authorizing_root(changes):
    decisions = evaluate(profile(), [candidate(parent="root", labels=(), assignees=()), parent(**changes)])
    assert not decisions[0].eligible


@pytest.mark.parametrize(("changes", "reason"), [
    ({"assignees": ("bob",)}, "parent_assigned_to_another_user"),
    ({"assignees": ("alice", "bob")}, "parent_assigned_to_another_user"),
    ({"blocked": True}, "parent_open_blocker"),
    ({"state": "CLOSED"}, "parent_inactive"),
    ({"archived": True}, "parent_inactive"),
])
def test_direct_opt_in_cannot_override_parent_safety(changes, reason):
    child, _ = evaluate(profile(), [candidate(parent="root"), parent(**changes)])
    assert not child.eligible
    assert reason in child.reasons


def test_inheritance_can_be_disabled_and_is_case_insensitive_for_usernames():
    leaf = candidate(parent="root", labels=(), assignees=())
    root = parent(assignees=("ALICE",))
    assert evaluate(profile(), [leaf, root])[0].eligible
    assert not evaluate(profile(inherit_parent=False), [leaf, root])[0].eligible


def test_leaf_milestone_precedence_and_qualified_parent_inheritance():
    cfg = profile(milestones=("team/planning#12",))
    root = parent(milestone="team/planning#12")
    leaf = candidate(parent="root", labels=(), assignees=())
    assert evaluate(cfg, [leaf, root])[0].eligible
    assert evaluate(cfg, [leaf, root])[0].milestone == "team/planning#12"
    for milestone in ("team/planning#13", "team/other#12"):
        decision = evaluate(cfg, [replace(leaf, milestone=milestone), root])[0]
        assert "milestone_not_selected" in decision.reasons
    assert not evaluate(cfg, [candidate()])[0].eligible
    assert evaluate(cfg, [candidate(milestone="team/planning#12")])[0].eligible


def test_does_not_inherit_an_unrelated_grandparents_opt_in():
    middle = parent(labels=(), assignees=(), parent="grandparent")
    grandparent = parent(id="grandparent", identifier="team/planning#3")
    decisions = evaluate(profile(), [candidate(parent="root", labels=(), assignees=()), middle, grandparent])
    assert not decisions[0].eligible


def test_duplicate_issues_stop_selection():
    with pytest.raises(GitHubError, match="duplicate"):
        evaluate(profile(), [candidate(), candidate()])


PROFILE = '''
[project]
owner = "team"
number = 44
repositories = ["Team/Planning"]
[selection]
assignee = "alice"
inherit_parent = true
milestones = ["TEAM/PLANNING#012"]
[repository_routes]
"team/planning" = "team/app"
'''


def test_load_profile_normalizes_scope_and_preserves_safe_defaults(tmp_path):
    path = tmp_path / "profile.toml"
    path.write_text(PROFILE)
    cfg = load_profile(path)
    assert cfg.repositories == ("team/planning",)
    assert cfg.milestones == ("team/planning#12",)
    assert cfg.label == "loop" and cfg.ready_status == "Ready"


@pytest.mark.parametrize("source", [
    PROFILE.replace('assignee = "alice"', 'assignee = ""'),
    PROFILE.replace('number = 44', 'number = true'),
    PROFILE.replace('inherit_parent = true', 'inherit_parent = "true"'),
    PROFILE.replace('inherit_parent = true', 'inherit_parents = true'),
    PROFILE.replace('"team/planning" = "team/app"', '"team/other" = "team/app"'),
    PROFILE.replace('"team/app"', '"not-a-repository"'),
    PROFILE.replace('"TEAM/PLANNING#012"', '"team/other#12"'),
    PROFILE.replace('"TEAM/PLANNING#012"', '"team/planning#0"'),
    PROFILE + '\n[workflow]\napproved = "In QA"\n',
])
def test_profile_rejects_ambiguous_and_unknown_settings(tmp_path, source):
    path = tmp_path / "profile.toml"
    path.write_text(source)
    with pytest.raises(ValueError):
        load_profile(path)


def connection(nodes, has_next=False, cursor=None):
    return {"nodes": nodes, "pageInfo": {"hasNextPage": has_next, "endCursor": cursor}}


def item():
    return {
        "id": "project-item", "type": "ISSUE", "isArchived": False,
        "fieldValueByName": {"name": "Ready"},
        "content": {
            "id": "leaf", "number": 2, "title": "An issue",
            "url": "https://github.com/team/planning/issues/2", "state": "OPEN",
            "repository": {"nameWithOwner": "team/planning"},
            "labels": connection([{"name": "loop"}]),
            "assignees": connection([{"login": "alice"}]),
            "milestone": None, "parent": None,
            "subIssues": {"totalCount": 0}, "blockedBy": connection([]),
        },
    }


def client(monkeypatch, items=None):
    reader = PreviewClient("test-token")
    calls = []

    def query(document, variables=None):
        assert document.lstrip().startswith("query")
        assert "mutation" not in document
        calls.append((document, variables))
        if "viewer" in document:
            return {"data": {"viewer": {"login": "alice"}}}
        if "repositoryOwner" in document:
            return {"data": {"repositoryOwner": {"projectV2": {"id": "project"}}}}
        if "fields(" in document:
            return {"data": {"node": {"fields": connection([
                {"name": "Status", "options": [{"name": name} for name in (
                    "Triage", "Ready", "In Progress", "In Review", "In QA", "Done"
                )]},
            ])}}}
        if "items(" in document:
            return {"data": {"node": {"items": connection(items if items is not None else [item()])}}}
        raise AssertionError(document)

    monkeypatch.setattr(reader._transport, "query", query)
    return reader, calls


def test_reader_accepts_team_statuses_without_legacy_execution_statuses(monkeypatch):
    reader, calls = client(monkeypatch)
    decision, = reader.run(profile())
    assert decision.eligible
    assert len(calls) == 4


def test_reader_paginates_fields_and_items_before_returning_decisions(monkeypatch):
    reader, _ = client(monkeypatch)
    original = reader._transport.query

    def query(document, variables=None):
        if "fields(" in document and variables["after"] is None:
            return {"data": {"node": {"fields": connection([], True, "fields-next")}}}
        if "items(" in document and variables["after"] is None:
            return {"data": {"node": {"items": connection([], True, "items-next")}}}
        return original(document, variables)

    monkeypatch.setattr(reader._transport, "query", query)
    assert reader.run(profile())[0].eligible


@pytest.mark.parametrize("document", [
    "mutation { something }", "query { viewer { login } } mutation { something }", "subscription { something }",
])
def test_reader_rejects_a_mutation_before_network_access(monkeypatch, document):
    reader, calls = client(monkeypatch)
    with pytest.raises(GitHubError, match="queries only"):
        reader._read(document)
    assert calls == []


def test_viewer_must_match_before_reading_any_project(monkeypatch):
    reader, calls = client(monkeypatch)
    with pytest.raises(GitHubError, match="authenticated GitHub user"):
        reader.run(profile(assignee="bob"))
    assert len(calls) == 1


@pytest.mark.parametrize("options", [[{"name": "Triage"}], [None]])
def test_reader_rejects_absent_ready_or_malformed_status_options(monkeypatch, options):
    reader, _ = client(monkeypatch)
    original = reader._transport.query

    def query(document, variables=None):
        result = original(document, variables)
        if "fields(" in document:
            result["data"]["node"]["fields"]["nodes"][0]["options"] = options
        return result

    monkeypatch.setattr(reader._transport, "query", query)
    with pytest.raises(GitHubError):
        reader.run(profile())


@pytest.mark.parametrize("name", ["labels", "assignees", "blockedBy"])
def test_truncated_decision_metadata_fails_closed(monkeypatch, name):
    data = item()
    data["content"][name]["pageInfo"]["hasNextPage"] = True
    reader, _ = client(monkeypatch, [item(), data])
    with pytest.raises(GitHubError, match="pagination bound"):
        reader.run(profile())


def test_reader_rejects_missing_parent_and_inaccessible_items(monkeypatch):
    data = item()
    del data["content"]["parent"]
    reader, _ = client(monkeypatch, [data])
    with pytest.raises(GitHubError, match="missing preview parent"):
        reader.run(profile())
    reader, _ = client(monkeypatch, [None])
    with pytest.raises(GitHubError, match="inaccessible"):
        reader.run(profile())


def test_reader_rejects_repeated_pagination_cursor(monkeypatch):
    reader, _ = client(monkeypatch)
    original = reader._transport.query

    def query(document, variables=None):
        if "items(" in document:
            return {"data": {"node": {"items": connection([], True, "repeat")}}}
        return original(document, variables)

    monkeypatch.setattr(reader._transport, "query", query)
    with pytest.raises(GitHubError, match="repeated"):
        reader.run(profile())


def test_reader_ignores_prs_and_drafts(monkeypatch):
    reader, _ = client(monkeypatch, [{"type": "DRAFT_ISSUE"}, {"type": "PULL_REQUEST"}])
    assert reader.run(profile()) == []


def test_cli_is_independent_of_loop_config_and_has_no_local_mutations(monkeypatch, tmp_path, capsys):
    path = tmp_path / "profile.toml"
    path.write_text(PROFILE)
    monkeypatch.setenv("GH_TOKEN", "test-token")
    monkeypatch.setenv("AGENT_LOOP_TRACKER", "linear")
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    def forbidden(*args, **kwargs):
        raise AssertionError("preview must not build runtime config or execute loops")

    monkeypatch.setattr("agentloop.cli._build_config", forbidden)
    monkeypatch.setattr("agentloop.cli._load_env_file", forbidden)
    monkeypatch.setattr("agentloop.cli._LOOPS", {"dispatch": forbidden})
    monkeypatch.setattr(PreviewClient, "run", lambda self, cfg: evaluate(cfg, [candidate()]))
    before = sorted(tmp_path.rglob("*"))
    assert main(["preview", "--profile", str(path), "--json"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["read_only"] is True
    assert output["decisions"][0]["eligible"] is False  # selected milestone is missing
    assert sorted(tmp_path.rglob("*")) == before


def test_cli_failure_has_no_partial_json_output(monkeypatch, tmp_path, capsys):
    path = tmp_path / "profile.toml"
    path.write_text(PROFILE)
    monkeypatch.setenv("GH_TOKEN", "test-token")

    def failed(self, cfg):
        raise GitHubError("incomplete data")

    monkeypatch.setattr(PreviewClient, "run", failed)
    assert main(["preview", "--profile", str(path), "--json"]) == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "incomplete data" in output.err
