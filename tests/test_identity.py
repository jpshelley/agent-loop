"""Tracker-independent local names and conservative cleanup recognition."""

import hashlib
import json
import re

import pytest

from agentloop.identity import (
    branch_execution_key,
    execution_key,
    is_execution_key,
    validate_execution_key,
)


@pytest.mark.parametrize("identifier", ["TEAM-123", "TEAM2-0", "X-1234567890"])
def test_linear_identifiers_get_generated_local_names(identifier):
    key = execution_key("linear", identifier, issue_id="native-id")
    assert key.startswith("al-linear-")
    assert key != identifier
    assert validate_execution_key(key) == key


def test_human_facing_identifiers_are_not_execution_keys():
    assert not is_execution_key("TEAM-123")
    assert not is_execution_key("team-123")
    assert not branch_execution_key("codex/team-123-description")


def test_generated_name_uses_documented_identity_tuple():
    identity = ["github", "owner/repo", "I_kwDOimmutable"]
    digest = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()[:24]
    assert execution_key("github", "owner/repo#12", "owner/repo", "I_kwDOimmutable") == f"al-github-{digest}"


def test_native_identity_survives_display_name_changes():
    first = execution_key("github", "old/name#12", "repo-node-id", "issue-node-id")
    assert execution_key("github", "new/name#34", "repo-node-id", "issue-node-id") == first


def test_generated_names_distinguish_trackers_scopes_and_items():
    names = {
        execution_key("github", "#12", "one/repo"),
        execution_key("github", "#12", "two/repo"),
        execution_key("linear", "#12", "one/repo"),
        execution_key("github", "#13", "one/repo"),
        execution_key("github", "#12", "one/repo", "native-one"),
        execution_key("github", "#12", "one/repo", "native-two"),
    }
    assert len(names) == 6


@pytest.mark.parametrize("provider", ["GitHub Projects", "../../bad:provider#\n", "🚀", "A" * 1000, "a" * 35 + "--b"])
def test_generated_names_are_bounded_safe_ascii_basenames(provider):
    key = execution_key(provider, "owner/repo#2:\n🚀", "../../scope")
    assert key.isascii()
    assert len(key) <= 64
    assert re.fullmatch(r"al-[a-z0-9-]+-[0-9a-f]{24}", key)
    assert is_execution_key(key)
    assert validate_execution_key(key) == key


@pytest.mark.parametrize("provider,identifier", [("", "x"), (" \n", "x"), (None, "x"), ("linear", ""), ("github", "\t "), ("github", None)])
def test_empty_required_identity_inputs_fail_clearly(provider, identifier):
    with pytest.raises(ValueError, match="nonempty"):
        execution_key(provider, identifier)


@pytest.mark.parametrize("name", [
    "", "TEAM-1/..", "../TEAM-1", "team-1", "TEAM-1\n", "TEAM-1:2", "-TEAM-1", "TEAM-1.lock", "TEAM-1#2",
    "TEAM-١", "TËAM-1", "al-github-" + "a" * 23, "al-github-" + "a" * 25,
    "al-github-" + "A" * 24, "al--github-" + "a" * 24,
    "al-" + "a" * 37 + "-" + "a" * 24, "A" * 63 + "-1", None,
])
def test_unknown_cleanup_names_are_rejected(name):
    assert not is_execution_key(name)
    with pytest.raises(ValueError, match="unsupported execution key"):
        validate_execution_key(name)


def test_generated_branch_tokens_keep_their_identity():
    key = execution_key("github-projects", "owner/repo#12")
    for branch in [key, f"codex/{key}", f"codex/{key}-implement"]:
        assert branch_execution_key(branch) == key


@pytest.mark.parametrize("branch", [
    "main", "feature/fix-auth", "codex/some-team-123-fix", "team-123abc", "codex/team-123/other",
    "codex/al-github-deadbeef", "codex/al-github-" + "a" * 25,
    "codex/al-github-" + "A" * 24, "codex/al-github-" + "a" * 24 + "extra",
    "codex/team-١-fix", None,
])
def test_arbitrary_branches_are_not_treated_as_execution_keys(branch):
    assert branch_execution_key(branch) == ""
