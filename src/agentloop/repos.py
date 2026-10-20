"""Repo routing — maps a Linear ``repo:<label>`` to a local checkout.

The loop drives real git checkouts, opens real pull requests, and (via deploy)
merges them, so this mapping is deployment-specific and must be supplied by the
operator. It deliberately ships **no built-in default**: guessing wrong opens a
plausible-looking pull request in the wrong repository.

Format (``~/.<app>/repos.toml``)::

    [repos]
    api = "~/src/api"           # absolute, ~-relative, or relative to workspace_dir
    web = "~/src/web"

    [repos.tools]                # optional Git settings per checkout
    path = "~/src/tools"
    remote = "upstream"         # defaults to origin
    base_branch = "develop"     # defaults to main

    [aliases]
    legacy-api = "api"          # retired label -> the checkout it merged into

    [descriptions]
    api = "backend services and API"   # feeds the dispatch classifier prompt

    [settings]
    default_repo = "api"        # optional; unset => unclassifiable issues stay queued
    design_repo  = "web"        # optional; unset => the design loop is a no-op
    infra_repos  = ["infra"]    # optional; work here needs deployed evidence before Done
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from agentloop.constants import REPOS_FILENAME


class ReposConfigError(SystemExit):
    """A missing or malformed repo map. Subclasses SystemExit so the CLI exits
    cleanly with the message, matching the LINEAR_API_KEY contract in config."""


_MISSING_HINT = (
    "The loop routes every issue to a local checkout and has no safe default —\n"
    "guessing would open a pull request in the wrong repository.\n\n"
    "Create it with at least one entry:\n\n"
    "    [repos]\n"
    '    api = "~/src/api"\n\n'
    "See examples/repos.toml for the full format."
)


@dataclass(frozen=True)
class RepoMap:
    """Resolved repo routing: labels to checkouts, plus the optional singletons."""

    repos: dict[str, Path] = field(default_factory=dict)
    aliases: dict[str, str] = field(default_factory=dict)
    descriptions: dict[str, str] = field(default_factory=dict)
    default_repo: str | None = None
    design_repo: str | None = None
    infra_repos: frozenset[str] = frozenset()
    remotes: dict[str, str] = field(default_factory=dict)
    base_branches: dict[str, str] = field(default_factory=dict)

    def collapse(self, label: str) -> str:
        """Resolve a retired label to its canonical one. Unknown labels pass through
        unchanged so the caller's own not-found handling still fires."""
        return self.aliases.get(label, label)


def default_path(config_dir: Path) -> Path:
    return config_dir / REPOS_FILENAME


def _expand(raw: str, base: Path) -> Path:
    """Absolute, ``~``-relative, and ``$VAR`` paths pass through; anything else is
    resolved against the workspace dir so short entries stay portable."""
    expanded = Path(os.path.expandvars(raw)).expanduser()
    return expanded if expanded.is_absolute() else base / expanded


def _require_table(data: dict, key: str, path: Path) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise ReposConfigError(f"{path}: [{key}] must be a table, got {type(value).__name__}")
    return value


def _valid_remote(value: object) -> bool:
    """Accept a named remote, never a URL, path, refspec, or Git option."""
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", value) is not None
        and ".." not in value
        and not value.endswith((".", ".lock"))
    )


def _valid_branch(value: object) -> bool:
    """Validate a literal branch name without invoking Git or expanding revisions.

    Follow check-ref-format's branch rules, including component restrictions,
    so a configured branch cannot turn into an option, refspec, or revision range.
    """
    if not isinstance(value, str) or not value or value in {"@", "HEAD"}:
        return False
    if value.startswith(("-", "/")) or value.endswith(("/", ".")):
        return False
    if ".." in value or "@{" in value or "//" in value:
        return False
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 or char in "~^:?*[\\" for char in value):
        return False
    return all(not part.startswith(".") and not part.endswith(".lock") for part in value.split("/"))


def load(path: Path, base: Path) -> RepoMap:
    """Read and validate the repo map. ``base`` is the workspace dir that relative
    checkout paths resolve against."""
    if not path.is_file():
        raise ReposConfigError(f"no repo map at {path}\n\n{_MISSING_HINT}")
    try:
        data = tomllib.loads(path.read_text())
    except OSError as error:
        raise ReposConfigError(f"could not read repo map {path}: {error}") from error
    except tomllib.TOMLDecodeError as error:
        raise ReposConfigError(f"{path} is not valid TOML: {error}") from error

    raw_repos = _require_table(data, "repos", path)
    if not raw_repos:
        raise ReposConfigError(f"{path}: [repos] is empty\n\n{_MISSING_HINT}")

    repos: dict[str, Path] = {}
    remotes: dict[str, str] = {}
    base_branches: dict[str, str] = {}
    for label, value in raw_repos.items():
        entry = value if isinstance(value, dict) else {"path": value}
        unknown = entry.keys() - {"path", "remote", "base_branch"}
        if unknown:
            raise ReposConfigError(
                f"{path}: [repos] {label!r} has unknown keys: {', '.join(sorted(unknown))}"
            )
        checkout = entry.get("path")
        if not isinstance(checkout, str) or not checkout.strip():
            raise ReposConfigError(
                f"{path}: [repos] {label!r} must be a non-empty path string, got {checkout!r}"
            )
        remote = entry.get("remote", "origin")
        branch = entry.get("base_branch", "main")
        if not _valid_remote(remote):
            raise ReposConfigError(
                f"{path}: [repos] {label!r} remote must be a named Git remote, got {remote!r}"
            )
        if not _valid_branch(branch):
            raise ReposConfigError(
                f"{path}: [repos] {label!r} base_branch must be a valid Git branch name, got {branch!r}"
            )
        repos[label] = _expand(checkout.strip(), base)
        remotes[label] = remote
        base_branches[label] = branch

    aliases: dict[str, str] = {}
    for label, target in _require_table(data, "aliases", path).items():
        if not isinstance(target, str):
            raise ReposConfigError(f"{path}: [aliases] {label!r} must be a string, got {target!r}")
        if target not in repos:
            raise ReposConfigError(
                f"{path}: [aliases] {label!r} points at {target!r}, which is not in [repos]"
            )
        if label in repos:
            raise ReposConfigError(
                f"{path}: {label!r} is both a [repos] entry and an [aliases] key — pick one"
            )
        aliases[label] = target

    descriptions: dict[str, str] = {}
    for label, text in _require_table(data, "descriptions", path).items():
        resolved = aliases.get(label, label)
        if resolved not in repos:
            raise ReposConfigError(
                f"{path}: [descriptions] {label!r} is not a known repo label"
            )
        if not isinstance(text, str) or not text.strip():
            raise ReposConfigError(
                f"{path}: [descriptions] {label!r} must be a non-empty string, got {text!r}"
            )
        descriptions[resolved] = text.strip()

    settings = _require_table(data, "settings", path)
    singletons: dict[str, str | None] = {}
    for key in ("default_repo", "design_repo"):
        value = settings.get(key)
        if value is None:
            singletons[key] = None
            continue
        if not isinstance(value, str):
            raise ReposConfigError(f"{path}: [settings] {key} must be a string, got {value!r}")
        resolved = aliases.get(value, value)
        if resolved not in repos:
            raise ReposConfigError(
                f"{path}: [settings] {key} = {value!r} is not a known repo label"
            )
        singletons[key] = resolved

    raw_infra = settings.get("infra_repos", [])
    if not isinstance(raw_infra, list):
        raise ReposConfigError(
            f"{path}: [settings] infra_repos must be a list of repo labels, got {raw_infra!r}"
        )
    infra_repos = set()
    for value in raw_infra:
        if not isinstance(value, str):
            raise ReposConfigError(
                f"{path}: [settings] infra_repos entries must be strings, got {value!r}"
            )
        resolved = aliases.get(value, value)
        if resolved not in repos:
            raise ReposConfigError(
                f"{path}: [settings] infra_repos names {value!r}, which is not a known repo label"
            )
        infra_repos.add(resolved)

    return RepoMap(
        repos=repos,
        aliases=aliases,
        descriptions=descriptions,
        default_repo=singletons["default_repo"],
        design_repo=singletons["design_repo"],
        infra_repos=frozenset(infra_repos),
        remotes=remotes,
        base_branches=base_branches,
    )
