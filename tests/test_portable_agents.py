from agentloop import harness


def test_cursor_and_copilot_profiles_use_noninteractive_argv():
    cursor = harness.HarnessAdapter("cursor", harness.default_bin("cursor"), harness.default_flags("cursor"))
    copilot = harness.HarnessAdapter("copilot", harness.default_bin("copilot"), harness.default_flags("copilot"))

    assert cursor.build_capture_argv("implement") == ["agent", "-p", "--force", "--output-format", "text", "implement"]
    assert copilot.build_capture_argv("implement") == ["copilot", "-p", "--no-ask-user", "--output-format", "text", "implement"]
