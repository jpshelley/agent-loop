"""Shared test fixtures: a Config factory and a fake LinearClient.

Conventions mirror the team's external Python e2e suite: pytest, ``fixtures/`` JSON, and
parametrized cases.
"""

from __future__ import annotations

import pytest

from agentloop.config import Config
from agentloop.linear import LinearClient


#: Repo map the suite runs against. Paths are relative, so they resolve under the
#: fixture's tmp workspace dir. Label names still mirror the original deployment;
#: they are renamed with the rest of the name sweep.
FIXTURE_REPOS_TOML = """
[repos]
sample          = "example-org/sample-repo"
sample-infra   = "example-org/sample-repo-infra2"
sample-repo = "example-org/sample-repo"
sample-e2e      = "example-org/sample-e2e"
sample-skills   = "example-org/sample-repo-skills"
plugins  = "example-org/plugins"
agents       = "example-org/agents"
hosting           = "example-org/hosting"
agent-loop      = "agent-loop"

[aliases]
sample-engine = "sample-repo"
sample-cli    = "sample-repo"

[descriptions]
sample          = "web/admin client UI"
sample-infra   = "infra, Terraform, AWS, DNS, deploy lanes"
sample-repo = "backend services, API, agent runtime/engine, desktop, CLI"
sample-e2e      = "end-to-end test suite"
sample-skills   = "skills-only platform fork"
plugins  = "agent plugins"
agents       = "custom client agents"
hosting           = "agent hosting platform"
agent-loop      = "this loop's own orchestration code"

[settings]
default_repo = "sample-repo"
design_repo  = "sample"
infra_repos  = ["sample-infra"]
"""


@pytest.fixture(autouse=True)
def default_repos_file(tmp_path_factory, monkeypatch):
    """Point every test at a valid repo map.

    The loop refuses to start without one — there is no built-in map — so tests that
    build a Config from the ambient environment need this. Tests passing an explicit
    ``environ`` dict set the key themselves.
    """
    path = tmp_path_factory.mktemp("repomap") / "repos.toml"
    path.write_text(FIXTURE_REPOS_TOML)
    monkeypatch.setenv("AGENT_LOOP_REPOS_FILE", str(path))
    return path


@pytest.fixture
def make_cfg(tmp_path):
    """Return a factory that builds a finalized Config with a tmp workspace + log dir and a
    fake API key. Repo checkout dirs are created so ``root.is_dir()`` passes."""

    def _make(*, dry_run=True, **overrides):
        workspace = tmp_path / "workspace"
        logdir = tmp_path / "logs"
        repos_file = tmp_path / "repos.toml"
        if not repos_file.exists():
            repos_file.write_text(overrides.pop("repos_toml", FIXTURE_REPOS_TOML))
        else:
            overrides.pop("repos_toml", None)
        environ = {
            "HOME": str(tmp_path),
            "AGENT_LOOP_WORKSPACE_DIR": str(workspace),
            "AGENT_LOOP_LOG_DIR": str(logdir),
            "AGENT_LOOP_REPOS_FILE": str(repos_file),
            "LINEAR_API_KEY": "test-key",
        }
        for k, v in overrides.pop("env", {}).items():
            environ[k] = v
        cfg = Config.from_env(environ).for_loop(overrides.pop("log_tag", "dispatch"))
        cfg.dry_run = dry_run
        for k, v in overrides.items():
            setattr(cfg, k, v)
        cfg.finalize(environ)
        # Create the standard repo checkouts so repo_root().is_dir() is True.
        for path in set(cfg.repos.values()):
            path.mkdir(parents=True, exist_ok=True)
        return cfg

    return _make


class FakeClient(LinearClient):
    """A stand-in for LinearClient that returns canned query payloads and records mutations."""

    def __init__(self, cfg, payloads=None, ci_fix_markers=None, approval_wait_markers=None,
                 approval_wait_escalations=None):
        super().__init__(cfg)
        self.loop_ids = ["proj-1"]
        self.payloads = payloads or {}
        self.calls: list[tuple] = []  # ordered record of mutating operations
        self.state_id: dict[str, str] = {}
        self.label_id: dict[str, str] = {}
        self.comment_bodies: dict[str, list[str]] = {}
        # SHAs the loop has already recorded a CI-fix attempt for; add_ci_fix_marker appends here.
        self._ci_fix_markers: list[str] = list(ci_fix_markers or [])
        self._approval_wait_markers: dict[str, str] = dict(approval_wait_markers or {})
        self._approval_wait_escalations: set[str] = set(approval_wait_escalations or [])

    # Route issue reads by requested states, or other queries by a distinctive substring.
    def query(self, query: str, variables=None):
        states = (variables or {}).get("s", [])
        for key, payload in self.payloads.items():
            if key.startswith("state:"):
                if states == [key.removeprefix("state:")]:
                    return payload
            elif key.startswith("states:"):
                if states == key.removeprefix("states:").split("|"):
                    return payload
            elif key in query:
                return payload
        return {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}}}

    def resolve_scope(self):
        self.calls.append(("resolve_scope",))
        return bool(self.loop_ids)

    def ensure_scope(self, scope):
        self.ensure_team_meta(scope)

    def ensure_required_label(self, issue_id, scope, label):
        self.ensure_label(scope, label)
        return self.add_label(issue_id, scope, label)

    def ensure_team_meta(self, team):
        self.calls.append(("ensure_team_meta", team))

    def ensure_label(self, team, label):
        self.calls.append(("ensure_label", team, label))

    def add_label(self, issue_id, team, label):
        self.calls.append(("add_label", issue_id, team, label))
        return True

    def move_state(self, issue_id, team, state):
        self.calls.append(("move_state", issue_id, team, state))
        return True

    def add_comment(self, issue_id, body):
        self.calls.append(("add_comment", issue_id))
        self.comment_bodies.setdefault(issue_id, []).append(body)
        return True

    def ci_fix_markers(self, issue_id):
        return list(self._ci_fix_markers)

    def add_ci_fix_marker(self, issue_id, sha, pr_num):
        self.calls.append(("add_ci_fix_marker", issue_id, sha))
        self._ci_fix_markers.append(sha)
        return True

    def approval_wait_markers(self, issue_id):
        return dict(self._approval_wait_markers), set(self._approval_wait_escalations)

    def add_approval_wait_marker(self, issue_id, sha, pr_num, missing):
        self.calls.append(("add_approval_wait_marker", issue_id, sha))
        self._approval_wait_markers.setdefault(sha, "2026-08-08T00:00:00Z")
        return True

    def add_approval_wait_escalation(self, issue_id, sha, pr_num, minutes, missing, review, needs_input):
        self.calls.append(("add_approval_wait_escalation", issue_id, sha))
        self._approval_wait_escalations.add(sha)
        return True

    def assign_issue(self, issue_id):
        self.calls.append(("assign_issue", issue_id))
        return True


@pytest.fixture
def fake_client_cls():
    return FakeClient
