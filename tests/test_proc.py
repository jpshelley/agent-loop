"""proc.capture — must accept a stderr override without the capture_output ValueError.

Regression for the triage path, which calls capture(..., stderr=DEVNULL). The unit tests for
triage monkeypatch capture, so this exercises the real implementation directly.
"""

from __future__ import annotations

import subprocess

from agentloop import proc


def test_capture_default_captures_stdout():
    r = proc.capture(["printf", "hi"])
    assert r.stdout == "hi"
    assert r.returncode == 0


def test_capture_allows_stderr_override():
    # The exact combination triage uses: capture stdout, silence stderr. Must not raise.
    r = proc.capture(["sh", "-c", "printf out; printf err 1>&2"], stderr=subprocess.DEVNULL)
    assert r.stdout == "out"
    assert r.returncode == 0
