"""Cross-harness contract tests for operator-supported harness switching.

The loop owns the Linear state machine and Example Organization slash-skill prompts.
Harness adapters may change the executable and unattended flags, but they must
not translate `/build-feature`, `/fix-bug`, `/triangulate`, or `/ship-it` into a
harness-specific prompt vocabulary.
"""

from __future__ import annotations

import pytest

from agentloop import harness, spawn


@pytest.mark.parametrize(
    ("name", "expected_bin", "expected_flags"),
    [
        ("claude", "claude", "--permission-mode auto"),
        ("codex", "codex", "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"),
    ],
)
def test_supported_harness_defaults(name, expected_bin, expected_flags):
    assert name in harness.supported_harnesses()
    assert harness.default_bin(name) == expected_bin
    assert harness.default_flags(name) == expected_flags


@pytest.mark.parametrize("prompt", ["/build-feature TEAM-1", "/fix-bug TEAM-2", "/triangulate TEAM-3", "/ship-it TEAM-4"])
@pytest.mark.parametrize("name", ["claude", "codex"])
def test_headless_spawn_preserves_sample_works_slash_prompt(make_cfg, name, prompt):
    cfg = make_cfg(harness=name, harness_bin=harness.default_bin(name), harness_flags=harness.default_flags(name))

    cmd = spawn.build_spawn_cmd(cfg, prompt)

    assert prompt in cmd
    assert cmd.endswith(f"'{prompt}'")
    if name == "claude":
        assert cmd.startswith("claude -p --permission-mode auto ")
    else:
        assert cmd.startswith(
            "codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check "
        )


@pytest.mark.parametrize("prompt", ["/build-feature TEAM-1", "/fix-bug TEAM-2", "/triangulate TEAM-3", "/ship-it TEAM-4"])
@pytest.mark.parametrize(
    ("name", "expected_prefix"),
    [
        ("claude", ["claude", "-p", "--output-format", "text"]),
        (
            "codex",
            [
                "codex",
                "--ask-for-approval",
                "never",
                "--sandbox",
                "danger-full-access",
                "exec",
                "--skip-git-repo-check",
            ],
        ),
    ],
)
def test_capture_argv_preserves_slash_prompt(name, expected_prefix, prompt):
    adapter = harness.HarnessAdapter(
        name=name,
        bin=name,
        flags=harness.default_flags(name),
    )

    argv = adapter.build_capture_argv(prompt)

    assert argv == [*expected_prefix, prompt]
