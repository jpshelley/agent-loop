"""Repo map loading: path resolution, alias/label validation, and slug derivation.

The map is the one piece of config that decides which repository the loop pushes to,
so malformed input must fail loudly at startup rather than route work somewhere wrong.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentloop import repos
from agentloop.loops import design

BASE = Path("/workspace")


def _load(tmp_path, body: str):
    f = tmp_path / "repos.toml"
    f.write_text(body)
    return repos.load(f, BASE)


def test_missing_file_names_the_path_and_the_format(tmp_path):
    with pytest.raises(SystemExit) as excinfo:
        repos.load(tmp_path / "absent.toml", BASE)
    message = str(excinfo.value)
    assert "absent.toml" in message
    assert "[repos]" in message


def test_relative_absolute_and_home_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    monkeypatch.setenv("SRC", "/from/env")
    m = _load(
        tmp_path,
        '[repos]\n'
        'rel = "org/api"\n'
        'abs = "/srv/web"\n'
        'home = "~/checkouts/infra"\n'
        'fromenv = "$SRC/tools"\n',
    )
    assert m.repos["rel"] == BASE / "org" / "api"
    assert m.repos["abs"] == Path("/srv/web")
    assert m.repos["home"] == Path("/home/tester/checkouts/infra")
    assert m.repos["fromenv"] == Path("/from/env/tools")


def test_aliases_resolve_and_do_not_become_repos(tmp_path):
    m = _load(tmp_path, '[repos]\napi = "org/api"\n\n[aliases]\nold-api = "api"\n')
    assert m.collapse("old-api") == "api"
    assert m.collapse("api") == "api"
    assert "old-api" not in m.repos
    # An unknown label passes through untouched so repo_root() still raises on it.
    assert m.collapse("nope") == "nope"


@pytest.mark.parametrize(
    "body, expected",
    [
        ("", "[repos] is empty"),
        ('[repos]\napi = 7\n', "must be a non-empty path string"),
        ('[repos]\napi = "org/api"\n[aliases]\nold = "ghost"\n', "not in [repos]"),
        ('[repos]\napi = "org/api"\n[aliases]\napi = "api"\n', "pick one"),
        ('[repos]\napi = "org/api"\n[descriptions]\nghost = "x"\n', "not a known repo label"),
        ('[repos]\napi = "org/api"\n[settings]\ndefault_repo = "ghost"\n', "not a known repo label"),
        ('[repos]\napi = "org/api"\n[settings]\ndesign_repo = "ghost"\n', "not a known repo label"),
        ('[repos]\napi = "org/api"\n[settings]\ninfra_repos = "api"\n', "must be a list"),
        ('[repos]\napi = "org/api"\n[settings]\ninfra_repos = ["ghost"]\n', "not a known repo label"),
        ("[repos\n", "not valid TOML"),
    ],
)
def test_malformed_maps_are_rejected(tmp_path, body, expected):
    with pytest.raises(SystemExit, match=expected.replace("[", r"\[")):
        _load(tmp_path, body)


def test_settings_accept_an_alias_and_store_the_canonical_label(tmp_path):
    m = _load(
        tmp_path,
        '[repos]\napi = "org/api"\n\n[aliases]\nold-api = "api"\n\n'
        '[settings]\ndefault_repo = "old-api"\ninfra_repos = ["old-api"]\n',
    )
    assert m.default_repo == "api"
    assert m.infra_repos == frozenset({"api"})


def test_optional_settings_default_to_unset(tmp_path):
    m = _load(tmp_path, '[repos]\napi = "org/api"\n')
    assert m.default_repo is None
    assert m.design_repo is None
    assert m.infra_repos == frozenset()
    assert m.descriptions == {}


def test_mixed_string_and_table_repositories_store_canonical_git_targets(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/home/tester")
    m = _load(
        tmp_path,
        '[repos]\nweb = "org/web"\n'
        '[repos.api]\npath = "~/api"\nremote = "upstream"\nbase_branch = "release/next"\n'
        '[repos.tools]\npath = "org/tools"\n'
        '[aliases]\nbackend = "api"\n',
    )
    assert m.repos == {
        "web": BASE / "org/web",
        "api": Path("/home/tester/api"),
        "tools": BASE / "org/tools",
    }
    assert m.remotes == {"web": "origin", "api": "upstream", "tools": "origin"}
    assert m.base_branches == {"web": "main", "api": "release/next", "tools": "main"}
    assert m.collapse("backend") == "api"


@pytest.mark.parametrize("body", [
    '[repos.api]\nremote = "origin"\n',
    '[repos.api]\npath = " "\n',
    '[repos.api]\npath = 1\n',
])
def test_table_repositories_require_a_nonempty_path(tmp_path, body):
    with pytest.raises(repos.ReposConfigError, match="non-empty path string"):
        _load(tmp_path, body)


def test_repository_table_rejects_typo_in_git_target(tmp_path):
    with pytest.raises(repos.ReposConfigError, match="unknown keys: base_brnach"):
        _load(tmp_path, '[repos.api]\npath = "api"\nbase_brnach = "develop"\n')


@pytest.mark.parametrize("value", [
    "", " ", "origin remote", "origin\n", "--upload-pack=evil", "-origin",
    ".", "..", ".origin", "origin..backup", "origin.", "origin.lock",
    "git@github.com:org/repo", "https://github.com/org/repo", "../repo",
    "team/origin", "origin:other", "origin\x00", 1, True, [], {},
])
def test_repository_remote_rejects_non_names(tmp_path, value):
    with pytest.raises(repos.ReposConfigError, match="named Git remote"):
        _load(tmp_path, '[repos.api]\npath = "api"\nremote = ' + json.dumps(value) + '\n')


@pytest.mark.parametrize("value", ["origin", "upstream", "team-backup", "fork_2", "my.remote"])
def test_repository_remote_accepts_named_remotes(tmp_path, value):
    m = _load(tmp_path, '[repos.api]\npath = "api"\nremote = ' + json.dumps(value) + '\n')
    assert m.remotes["api"] == value


@pytest.mark.parametrize("value", [
    "", " ", "--all", "-main", "HEAD", "@", "@{-1}", "main@{yesterday}",
    "main:other", "main..other", "main...other", "main ", "main\n", "main\x00",
    "main\x7f", "feature//next", "/main", "main/", ".main", "main.",
    "feature/.hidden", "main.lock", "main.lock/next", "main~1", "main^", "main?",
    "main*", "main[0]", "feature\\next", 1, True, [], {},
])
def test_repository_base_branch_rejects_invalid_refs(tmp_path, value):
    with pytest.raises(repos.ReposConfigError, match="valid Git branch name"):
        _load(tmp_path, '[repos.api]\npath = "api"\nbase_branch = ' + json.dumps(value) + '\n')


@pytest.mark.parametrize("value", ["main", "develop", "release/next", "feature/new-api", "v1.2", "release/2026.10"])
def test_repository_base_branch_accepts_literal_branch_names(tmp_path, value):
    m = _load(tmp_path, '[repos.api]\npath = "api"\nbase_branch = ' + json.dumps(value) + '\n')
    assert m.base_branches["api"] == value


@pytest.mark.parametrize(
    "url, expected",
    [
        ("git@github.com:acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets.git", "acme/widgets"),
        ("https://github.com/acme/widgets", "acme/widgets"),
        ("ssh://git@github.com/acme/widgets.git", "acme/widgets"),
        ("not-a-remote", None),
    ],
)
def test_design_slug_parses_remote_forms(monkeypatch, url, expected):
    import types

    design._slug_from_remote_url.cache_clear()
    monkeypatch.setattr(
        design.proc, "capture", lambda argv, **kw: types.SimpleNamespace(stdout=url, returncode=0)
    )
    assert design._slug_for_checkout(f"/checkout/{expected}") == expected


def test_design_slug_is_none_without_a_configured_repo(make_cfg):
    cfg = make_cfg()
    cfg.design_repo = None
    assert design.design_slug(cfg) is None
