"""triage() — repo/type label resolution + the cheap classifier fallback."""

from __future__ import annotations

import types

from agentloop import proc
from agentloop.linear import LinearClient
from agentloop.loops import dispatch


class RecordingClient(LinearClient):
    def __init__(self):
        super().__init__(None)
        self.labels = []
        self.calls = []
        self.unavailable_labels = set()

    def ensure_scope(self, scope):
        self.ensure_team_meta(scope)

    def ensure_required_label(self, issue_id, scope, label):
        self.ensure_label(scope, label)
        if label in self.unavailable_labels:
            return False
        return self.add_label(issue_id, scope, label)

    def ensure_team_meta(self, team):
        self.calls.append(("ensure_team_meta", team))

    def ensure_label(self, team, label):
        self.calls.append(("ensure_label", team, label))

    def add_label(self, issue_id, team, label):
        self.calls.append(("add_label", issue_id, team, label))
        self.labels.append(label)
        return True

    def move_state(self, issue_id, team, state):
        self.calls.append(("move_state", issue_id, team, state))
        return True


def _capture_returning(stdout):
    def _cap(argv, **kw):
        return types.SimpleNamespace(stdout=stdout, returncode=0)

    return _cap


def _capture_boom(argv, **kw):
    raise AssertionError("classifier should not be called when labels are present")


def test_existing_labels_short_circuit(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_boom)
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "sample", "bug", "t")
    assert (repo, type_) == ("sample", "bug")
    assert client.labels == []


def test_engine_collapses_to_monorepo(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_boom)
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "sample-engine", "feature", "t")
    assert (repo, type_) == ("sample-repo", "feature")


def test_classifier_fills_and_labels(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_returning('{"repo":"sample","type":"bug"}'))
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "", "", "t")
    assert (repo, type_) == ("sample", "bug")
    assert "repo:sample" in client.labels
    assert "Bug" in client.labels


def test_classifier_uses_harness_capture_argv(make_cfg, monkeypatch):
    seen = {}

    def _capture(argv, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(stdout='{"repo":"sample","type":"bug"}', returncode=0)

    monkeypatch.setattr(proc, "capture", _capture)
    cfg = make_cfg()
    cfg.harness_bin = "claude-stage"
    client = RecordingClient()
    dispatch.triage(client, cfg, "id1", "TEAM-1", "team", "", "", "t")
    assert seen["argv"] == [
        "claude-stage",
        "-p",
        "--output-format",
        "text",
        dispatch._classify_prompt(cfg, "TEAM-1", "t"),
    ]


def test_classifier_uses_codex_exec_capture_argv(make_cfg, monkeypatch):
    seen = {}

    def _capture(argv, **kw):
        seen["argv"] = argv
        return types.SimpleNamespace(stdout='{"repo":"sample","type":"bug"}', returncode=0)

    monkeypatch.setattr(proc, "capture", _capture)
    cfg = make_cfg()
    cfg.harness = "codex"
    cfg.harness_bin = "codex"
    cfg.harness_flags = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"
    client = RecordingClient()
    dispatch.triage(client, cfg, "id1", "TEAM-1", "team", "", "", "t")
    assert seen["argv"] == [
        "codex",
        "--ask-for-approval",
        "never",
        "--sandbox",
        "danger-full-access",
        "exec",
        "--skip-git-repo-check",
        dispatch._classify_prompt(cfg, "TEAM-1", "t"),
    ]


def test_classifier_json_extraction_tolerates_junk_and_collapses(make_cfg, monkeypatch):
    monkeypatch.setattr(
        proc, "capture", _capture_returning('chatter {"repo":"sample-cli","type":"feature"} tail')
    )
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "", "", "t")
    assert (repo, type_) == ("sample-repo", "feature")
    assert "repo:sample-repo" in client.labels
    assert "Feature" in client.labels


def test_classifier_prompt_lists_every_configured_repo(make_cfg):
    # The catalog is generated from the operator's repo map, so every label must be both
    # described and selectable. Assert presence, not prose — descriptions are editorial.
    cfg = make_cfg()
    prompt = dispatch._classify_prompt(cfg, "TEAM-1", "t")
    for label in cfg.repos:
        assert f"{label} = " in prompt
        assert label in prompt.rsplit("repo:", 1)[-1]


def test_classifier_prompt_omits_fallback_when_no_default_repo(make_cfg):
    # With no configured default, the prompt must not invent one — an unclassifiable
    # issue is meant to stay queued rather than land in an arbitrary checkout.
    cfg = make_cfg()
    cfg.default_repo = None
    assert "if unsure" not in dispatch._classify_prompt(cfg, "TEAM-1", "t")


def test_classifier_unknown_type_defaults_to_feature(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_returning('{"repo":"sample","type":"improvement"}'))
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "", "", "t")
    assert (repo, type_) == ("sample", "feature")
    assert "repo:sample" in client.labels
    assert "Feature" in client.labels
    assert "Bug" not in client.labels


def test_empty_classifier_falls_back_to_defaults(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_returning("no json here"))
    client = RecordingClient()
    repo, type_ = dispatch.triage(client, make_cfg(), "id1", "TEAM-1", "team", "", "", "t")
    assert (repo, type_) == ("sample-repo", "feature")
    assert "repo:sample-repo" in client.labels
    assert "Feature" in client.labels


def test_dispatch_persists_fallback_labels_before_todo_handoff(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_returning("no json here"))
    monkeypatch.setattr(proc, "has_session", lambda key: False)
    monkeypatch.setattr(dispatch, "rate_limited_now", lambda cfg: None)
    client = RecordingClient()
    row = dispatch.ReadyRow(
        id="id1",
        identifier="TEAM-1",
        scope="team",
        repo="",
        type="",
        title="Unlabelled work",
    )

    assert dispatch.dispatch_one(client, make_cfg(), row) is True

    assert ("add_label", "id1", "team", "repo:sample-repo") in client.calls
    assert ("add_label", "id1", "team", "Feature") in client.calls
    assert ("add_label", "id1", "team", "loop-wip") in client.calls
    assert ("move_state", "id1", "team", "Todo") in client.calls
    assert client.calls.index(("add_label", "id1", "team", "Feature")) < client.calls.index(
        ("move_state", "id1", "team", "Todo")
    )
    assert client.calls.index(("add_label", "id1", "team", "loop-wip")) < client.calls.index(
        ("move_state", "id1", "team", "Todo")
    )


def test_dispatch_leaves_queued_when_wip_label_unavailable(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_boom)
    monkeypatch.setattr(proc, "has_session", lambda key: False)
    monkeypatch.setattr(dispatch, "rate_limited_now", lambda cfg: None)
    client = RecordingClient()
    client.unavailable_labels = {"loop-wip"}
    row = dispatch.ReadyRow(
        id="id1",
        identifier="TEAM-1",
        scope="team",
        repo="sample-repo",
        type="feature",
        title="Label failure",
    )

    assert dispatch.dispatch_one(client, make_cfg(dry_run=False), row) is True

    assert ("ensure_label", "team", "loop-wip") in client.calls
    assert ("add_label", "id1", "team", "loop-wip") not in client.calls
    assert ("move_state", "id1", "team", "Todo") not in client.calls


def test_dispatch_leaves_queued_when_inferred_route_label_unavailable(make_cfg, monkeypatch):
    monkeypatch.setattr(proc, "capture", _capture_returning("no json here"))
    monkeypatch.setattr(proc, "has_session", lambda key: False)
    monkeypatch.setattr(dispatch, "rate_limited_now", lambda cfg: None)
    client = RecordingClient()
    client.unavailable_labels = {"repo:sample-repo"}
    row = dispatch.ReadyRow(
        id="id1",
        identifier="TEAM-1",
        scope="team",
        repo="",
        type="",
        title="Unlabelled work",
    )

    assert dispatch.dispatch_one(client, make_cfg(dry_run=False), row) is True

    assert ("ensure_label", "team", "repo:sample-repo") in client.calls
    assert ("add_label", "id1", "team", "repo:sample-repo") not in client.calls
    assert ("add_label", "id1", "team", "loop-wip") not in client.calls
    assert ("move_state", "id1", "team", "Todo") not in client.calls


def test_dispatch_holds_unlabelled_work_during_classifier_outage(make_cfg, monkeypatch):
    monkeypatch.setattr(dispatch, "rate_limited_now", lambda cfg: "for up to ~5m (backoff)")
    monkeypatch.setattr(proc, "capture", _capture_boom)
    monkeypatch.setattr(proc, "has_session", lambda key: False)
    client = RecordingClient()
    row = dispatch.ReadyRow(
        id="id1",
        identifier="TEAM-1",
        scope="team",
        repo="",
        type="",
        title="Unlabelled work",
    )

    assert dispatch.dispatch_one(client, make_cfg(), row) is True

    assert ("move_state", "id1", "team", "Todo") not in client.calls
    assert client.labels == []
