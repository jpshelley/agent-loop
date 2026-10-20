from __future__ import annotations

from pathlib import Path

from agentloop import doctor


class _Cfg:
    harness_bin = "codex"
    repos = {"app": Path("/workspace/app")}

    def repo_remote(self, label):
        assert label == "app"
        return "origin"

    def repo_base_branch(self, label):
        assert label == "app"
        return "main"


def test_local_checks_reports_tools_and_valid_git_target(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    monkeypatch.setattr(doctor, "_run", lambda argv: (True, ""))

    checks = doctor.local_checks(_Cfg())

    assert all(check.ok for check in checks)
    assert {check.name for check in checks} == {
        "tool:git", "tool:tmux", "tool:gh", "tool:codex", "repo:app:base",
    }


def test_local_checks_reports_missing_remote_tracking_branch(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(Path, "is_dir", lambda path: True)

    def run(argv):
        return (not (argv[-1] == "refs/remotes/origin/main"), "reference not found")

    monkeypatch.setattr(doctor, "_run", run)

    checks = doctor.local_checks(_Cfg())

    base = next(check for check in checks if check.name == "repo:app:base")
    assert base.ok is False
    assert "git fetch origin" in base.detail


def test_local_checks_reports_missing_checkout(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(Path, "is_dir", lambda path: False)

    checks = doctor.local_checks(_Cfg())

    assert checks[-1].name == "repo:app"
    assert checks[-1].ok is False


def test_local_checks_reports_git_command_failure_detail(monkeypatch):
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(Path, "is_dir", lambda path: True)
    monkeypatch.setattr(doctor, "_run", lambda argv: (False, "fatal: not a git repository"))

    checks = doctor.local_checks(_Cfg())

    checkout = next(check for check in checks if check.name == "repo:app")
    assert "fatal: not a git repository" in checkout.detail
