"""Name-derived constants — the one place this project's own name is written down.

Renaming the project is a change to this file plus a package directory move: every
path the tool reads or writes is derived from these values. Nothing else in the
codebase should hardcode the project name.
"""

from __future__ import annotations

from pathlib import Path

#: User-facing command name.
APP_NAME = "agent-loop"

#: Environment variable namespace: ``<ENV_PREFIX>_CONCURRENCY``, and so on.
ENV_PREFIX = "AGENT_LOOP"

#: Per-user config directory under ``$HOME``, holding ``env``, ``repos.toml``, ``logs/``.
CONFIG_DIR_NAME = ".agent-loop"

#: Filename of the repo routing map inside the config directory.
REPOS_FILENAME = "repos.toml"


def env_var(suffix: str) -> str:
    """Full environment variable name for a config knob."""
    return f"{ENV_PREFIX}_{suffix}"


def config_dir(home: Path) -> Path:
    """The per-user config directory."""
    return home / CONFIG_DIR_NAME
