"""Local setup scaffolding stays inspectable, safe, and credential-free."""

from __future__ import annotations

import stat

import pytest

from agentloop.cli import main
from agentloop.onboarding import parse_repo_spec


def test_init_creates_a_github_configuration(tmp_path, capsys):
    directory = tmp_path / "agent-loop"

    assert main([
        "init", "--tracker", "github", "--harness", "cursor",
        "--repo", "web=/work/web", "--repo", "api=/work/api",
        "--config-dir", str(directory),
    ]) == 0

    env = (directory / "env").read_text()
    repos = (directory / "repos.toml").read_text()
    assert "AGENT_LOOP_TRACKER=github" in env
    assert "AGENT_LOOP_HARNESS=cursor" in env
    assert "replace-with-a-fine-grained-token" in env
    assert '"web" = { path = "/work/web"' in repos
    assert '"api" = { path = "/work/api"' in repos
    assert stat.S_IMODE((directory / "env").stat().st_mode) == 0o600
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert "agent-loop doctor" in capsys.readouterr().out


def test_init_refuses_to_overwrite_existing_configuration(tmp_path):
    directory = tmp_path / "agent-loop"
    args = ["init", "--repo", "web=/work/web", "--config-dir", str(directory)]
    assert main(args) == 0
    original = (directory / "env").read_text()

    with pytest.raises(SystemExit, match="pass --replace"):
        main(args)

    assert (directory / "env").read_text() == original


def test_refused_init_does_not_change_an_existing_directory_mode(tmp_path):
    directory = tmp_path / "already-here"
    directory.mkdir(mode=0o755)
    (directory / "env").write_text("existing")
    before = stat.S_IMODE(directory.stat().st_mode)

    with pytest.raises(SystemExit, match="pass --replace"):
        main(["init", "--repo", "web=/work/web", "--config-dir", str(directory)])

    assert stat.S_IMODE(directory.stat().st_mode) == before


def test_init_can_replace_both_generated_files(tmp_path):
    directory = tmp_path / "agent-loop"
    assert main(["init", "--repo", "web=/work/web", "--config-dir", str(directory)]) == 0

    assert main([
        "init", "--tracker", "linear", "--harness", "copilot", "--repo", "api=/work/api",
        "--config-dir", str(directory), "--replace",
    ]) == 0

    assert "LINEAR_API_KEY" in (directory / "env").read_text()
    assert "AGENT_LOOP_HARNESS=copilot" in (directory / "env").read_text()
    assert '"api" = { path = "/work/api"' in (directory / "repos.toml").read_text()


@pytest.mark.parametrize("value", ["web", "=~/work/web", "web="])
def test_repo_spec_requires_a_label_and_path(value):
    with pytest.raises(ValueError, match="LABEL=CHECKOUT_PATH"):
        parse_repo_spec(value)


def test_init_rejects_duplicate_repository_labels_before_writing(tmp_path):
    directory = tmp_path / "agent-loop"

    with pytest.raises(SystemExit, match="duplicate repository label"):
        main([
            "init", "--repo", "web=/work/one", "--repo", "web=/work/two",
            "--config-dir", str(directory),
        ])

    assert not directory.exists()
