"""Generate safe, editable local configuration for a new Agent Loop operator."""

from __future__ import annotations

import os
import json
import shlex
import tempfile
from pathlib import Path


def parse_repo_spec(value: str) -> tuple[str, Path]:
    """Parse ``label=checkout-path`` without accepting an empty side."""
    label, separator, path = value.partition("=")
    if not separator or not label.strip() or not path.strip():
        raise ValueError("--repo must use LABEL=CHECKOUT_PATH")
    return label.strip(), Path(path.strip()).expanduser()


def render_env(tracker: str, harness: str) -> str:
    lines = [
        "# Agent Loop operator settings. Keep credentials in this file, never in git.",
        f"export AGENT_LOOP_TRACKER={shlex.quote(tracker)}",
        f"export AGENT_LOOP_HARNESS={shlex.quote(harness)}",
        "",
    ]
    if tracker == "github":
        lines.extend(
            [
                "# Set these to the one GitHub Project and repository allowlist this installation may use.",
                "export AGENT_LOOP_GITHUB_TOKEN='replace-with-a-fine-grained-token'",
                "export AGENT_LOOP_GITHUB_OWNER='replace-with-your-org-or-user'",
                "export AGENT_LOOP_GITHUB_PROJECT_NUMBER=1",
                "export AGENT_LOOP_GITHUB_REPOSITORIES='owner/repository'",
                "export AGENT_LOOP_GITHUB_STATUS_FIELD=Status",
            ]
        )
    else:
        lines.extend(
            [
                "# Create a Linear personal API key and set the project label that opts work in.",
                "export LINEAR_API_KEY='replace-with-your-linear-key'",
                "export AGENT_LOOP_PROJECT_LABEL=loop",
            ]
        )
    return "\n".join(lines) + "\n"


def render_repos(repositories: list[tuple[str, Path]]) -> str:
    lines = [
        "# Issue labels use repo:<label> to select one local checkout.",
        "# Change remote/base_branch when a repository does not use origin/main.",
        "",
        "[repos]",
    ]
    for label, path in repositories:
        lines.extend(
            [
                f"{json.dumps(label)} = {{ path = {json.dumps(str(path))}, remote = \"origin\", base_branch = \"main\" }}",
            ]
        )
    lines.extend(
        [
            "",
            "[descriptions]",
            *[f"{json.dumps(label)} = \"Describe this repository for issue routing\"" for label, _ in repositories],
        ]
    )
    return "\n".join(lines) + "\n"


def _write_new(path: Path, content: str, *, replace: bool) -> None:
    if path.exists() and not replace:
        raise FileExistsError(f"{path} already exists; pass --replace to overwrite it")
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(content)
        os.chmod(temp_name, 0o600)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def initialize(
    directory: Path, *, tracker: str, harness: str, repositories: list[tuple[str, Path]], replace: bool
) -> tuple[Path, Path]:
    """Create the local env and repository map, with owner-only permissions."""
    if directory.exists() and not directory.is_dir():
        raise FileExistsError(f"{directory} exists and is not a directory")
    env_path = directory / "env"
    repos_path = directory / "repos.toml"
    # Check both first so a default invocation cannot leave a half-created setup.
    if not replace:
        existing = [str(path) for path in (env_path, repos_path) if path.exists()]
        if existing:
            raise FileExistsError(f"configuration already exists: {', '.join(existing)}; pass --replace to overwrite")
    labels = [label for label, _ in repositories]
    duplicate_labels = sorted({label for label in labels if labels.count(label) > 1})
    if duplicate_labels:
        raise ValueError(f"duplicate repository label(s): {', '.join(duplicate_labels)}")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_new(env_path, render_env(tracker, harness), replace=replace)
    _write_new(repos_path, render_repos(repositories), replace=replace)
    os.chmod(directory, 0o700)
    return env_path, repos_path
