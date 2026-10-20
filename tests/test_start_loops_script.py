from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "start-loops.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(0o755)


def _run_launcher(
    tmp_path: Path, *, fail_session: str | None = None, user: str | None = "john"
) -> tuple[subprocess.CompletedProcess[str], str, Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    loop_bin = tmp_path / "venv=with-equals" / "bin" / "agent-loop"
    loop_bin.parent.mkdir(parents=True)
    log_path = tmp_path / "tmux.log"
    env_file = tmp_path / "env"
    env_file.write_text("export LINEAR_API_KEY=test-key\nexport AGENT_LOOP_HARNESS=cursor\n")

    _write_executable(loop_bin, "#!/usr/bin/env bash\nexit 0\n")
    _write_executable(
        bin_dir / "tmux",
        """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$TMUX_LOG"
if [[ "$1" == "kill-session" ]]; then exit 0; fi
if [[ "$1" == "new-session" ]]; then
  name=""
  for ((i = 1; i <= $#; i++)); do
    if [[ "${!i}" == "-s" ]]; then next=$((i + 1)); name="${!next}"; fi
  done
  [[ "${TMUX_FAIL_SESSION:-}" == "$name" ]] && exit 42
  bash -c "${@: -1}"
  exit $?
fi
""",
    )

    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "AGENT_LOOP_ENV_FILE": str(env_file),
        "AGENT_LOOP_BIN": str(loop_bin),
        "TMUX_LOG": str(log_path),
    }
    env.pop("AGENT_LOOP_USER", None)
    if user is not None:
        env["AGENT_LOOP_USER"] = user
    if fail_session is not None:
        env["TMUX_FAIL_SESSION"] = fail_session

    result = subprocess.run(
        ["bash", str(SCRIPT)], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    return result, log_path.read_text() if log_path.exists() else "", loop_bin


def _new_session_line(log: str, session: str) -> str:
    return next(line for line in log.splitlines() if line.startswith(f"new-session -d -s {session} "))


def test_start_loops_uses_configured_agent_and_starts_core_loops(tmp_path: Path) -> None:
    result, log, loop_bin = _run_launcher(tmp_path)

    assert result.returncode == 0, result.stderr
    for session, interval in {
        "dispatch": "600", "develop": "120", "debug": "300", "deploy": "120", "declutter": "1800",
    }.items():
        line = _new_session_line(log, session)
        assert str(loop_bin) in line
        assert f"--interval {interval} --user john" in line.replace("\\ ", " ")
    assert "new-session -d -s design" not in log
    assert "--harness" not in log


def test_start_loops_continues_after_one_tmux_start_failure(tmp_path: Path) -> None:
    result, log, _loop_bin = _run_launcher(tmp_path, fail_session="develop")

    assert result.returncode == 1
    assert "failed to start develop" in result.stderr
    assert "failed to start 1 loop session(s)" in result.stderr
    assert "started dispatch" in result.stdout
    assert "started debug" in result.stdout
    assert "started deploy" in result.stdout
    assert "started declutter" in result.stdout
    assert _new_session_line(log, "declutter")


def test_start_loops_defaults_the_branch_namespace_to_the_invoking_user(tmp_path: Path) -> None:
    result, log, _ = _run_launcher(tmp_path, user=None)

    assert result.returncode == 0, result.stderr
    expected = os.environ.get("USER") or "loop"
    assert f"--user {expected}" in _new_session_line(log, "dispatch").replace("\\ ", " ")
