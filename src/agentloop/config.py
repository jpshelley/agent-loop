"""Config — the loop's configuration contract.

Ports the top of ``agent-loop-lib.sh`` (defaults + env vars), ``parse_flags``
(flag > env > default precedence) and ``finalize`` (require the API key, make the
log dir, build the repo map). The env var names, defaults, and flag names are kept
identical to the bash so the Python port is a drop-in: the same ``~/.agent-loop/env``
and the same tmux launch commands keep working.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

from agentloop import repos as repos_config
from agentloop.tracker import validate_tracker
from agentloop.constants import REPOS_FILENAME, config_dir, env_var
from agentloop.harness import default_bin, default_flags, validate_harness

# Constants that were plain assignments in the lib (not env-configurable).
TYPE_BUG_LABEL = "Bug"  # -> /fix-bug
# Any of these types -> /build-feature. First entry ("Feature") is the one we apply.
TYPE_FEATURE_LABELS = ["Feature", "Improvement"]


def _default_branch_prefix(environ: dict[str, str]) -> str:
    """Namespace loop branches under the operator's own username.

    Branches are pushed to shared repositories, so they need an owner in the name.
    USER/LOGNAME are absent in some containers; ``loop/`` keeps the branch valid
    rather than producing a bare ``/issue-key`` ref.
    """
    user = environ.get("USER") or environ.get("LOGNAME") or "loop"
    return f"{user.strip().rstrip('/')}/"


def _env_flag(environ: dict[str, str], name: str, default: str = "0") -> bool:
    """Match bash's ``[[ "$X" == 1 ]]`` truthiness: only the literal string "1" is true."""
    return environ.get(name, default) == "1"


@dataclass
class Config:
    """Every ``AGENT_LOOP_*`` knob, plus derived state.

    Built in three steps that mirror the bash lifecycle:
      1. :meth:`from_env`  — defaults overridden by env vars (the env layer).
      2. :meth:`apply_flags` — argparse values (all default ``None``) override env (the flag layer).
      3. :meth:`finalize`  — require ``LINEAR_API_KEY``, make the log dir, build ``repos``.
    """

    tracker: str = "linear"
    project_label: str = "loop"
    state_queue: str = "Backlog"
    state_todo: str = "Todo"
    state_active: str = "In Progress"
    state_review: str = "In Review"
    state_approved: str = "Approved"
    state_needs_input: str = "Needs Input"
    wip_label: str = "loop-wip"
    blocked_label: str = "loop-blocked"
    blocked_cooldown_seconds: int = 3600
    concurrency: int = 3
    grace_seconds: int = 900
    outage_backoff: int = 900
    branch_prefix: str = field(
        default_factory=lambda: _default_branch_prefix(os.environ)
    )
    workspace_dir: Path = field(default_factory=lambda: Path.home() / "Development")
    workspace_dir_is_checkout_root: bool = False
    worktree_rel: str = ".worktrees/agent-loop"
    repos_file: Path = field(
        default_factory=lambda: config_dir(Path.home()) / REPOS_FILENAME
    )
    log_dir: Path = field(default_factory=lambda: config_dir(Path.home()) / "logs")
    harness: str = "claude"
    harness_bin: str = "claude"
    harness_flags: str = "--permission-mode auto"
    assignee_id: str = ""
    dry_run: bool = False
    debug: bool = False

    # Secret — env-only, never a flag. Populated in finalize().
    linear_api_key: str = ""
    # GitHub tracker credentials and immutable queue scope. These stay env-only so
    # a process list never reveals the token. ``github_repository`` is the one
    # repository whose Issues may enter the selected Project queue.
    github_token: str = ""
    github_owner: str = ""
    github_project_number: int = 0
    github_repository: str = ""
    github_repositories: tuple[str, ...] = ()
    github_status_field: str = "Status"
    # repo:<x> label -> local checkout path. Loaded in finalize() from repos_file.
    repos: dict[str, Path] = field(default_factory=dict)
    # Canonical repo labels -> named remote and pull-request base branch.
    repo_remotes: dict[str, str] = field(default_factory=dict)
    repo_base_branches: dict[str, str] = field(default_factory=dict)
    # Retired repo label -> canonical label, for repos that merged into another checkout.
    aliases: dict[str, str] = field(default_factory=dict)
    # Optional one-line blurb per repo, used to build the dispatch classifier prompt.
    repo_descriptions: dict[str, str] = field(default_factory=dict)
    # Fallback repo when the classifier can't name one. None => leave the issue queued.
    default_repo: str | None = None
    # Repo whose merged PRs the design loop reviews. None => the design loop is a no-op.
    design_repo: str | None = None
    # Repos whose work needs deployed evidence on the issue before deploy will ship it.
    infra_repos: frozenset[str] = frozenset()
    # Per-loop tag (dispatch|debug|deploy|declutter): selects the log subfolder + audit ledger.
    log_tag: str = "agent-loop"

    # ── Step 1: defaults <- env vars ────────────────────────────────────────
    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "Config":
        e = os.environ if environ is None else environ

        def _int(name: str, default: int) -> int:
            raw = e.get(name)
            return int(raw) if raw not in (None, "") else default

        home = Path(e.get("HOME", str(Path.home())))
        default_workspace = home / "Development"
        default_config_dir = config_dir(home)
        default_logdir = default_config_dir / "logs"
        harness = e.get("AGENT_LOOP_HARNESS", "claude")
        flags = e.get("AGENT_LOOP_HARNESS_FLAGS")
        if flags is None:
            if harness == "claude":
                flags = e.get("AGENT_LOOP_CLAUDE_FLAGS", default_flags(harness))
            else:
                flags = default_flags(harness)
        legacy_bin = e.get("CLAUDE_BIN") if harness == "claude" else None
        workspace_override = e.get("AGENT_LOOP_WORKSPACE_DIR")
        configured_repositories = tuple(
            value.strip() for value in e.get("AGENT_LOOP_GITHUB_REPOSITORIES", "").split(",") if value.strip()
        )
        legacy_repository = e.get("AGENT_LOOP_GITHUB_REPOSITORY", "")
        if not configured_repositories and legacy_repository:
            configured_repositories = (legacy_repository,)
        return cls(
            tracker=e.get("AGENT_LOOP_TRACKER", "linear"),
            project_label=e.get("AGENT_LOOP_PROJECT_LABEL", "loop"),
            state_queue=e.get("AGENT_LOOP_STATE_QUEUE", "Backlog"),
            state_todo=e.get("AGENT_LOOP_STATE_TODO", "Todo"),
            state_active=e.get("AGENT_LOOP_STATE_ACTIVE", "In Progress"),
            state_review=e.get("AGENT_LOOP_STATE_REVIEW", "In Review"),
            state_approved=e.get("AGENT_LOOP_STATE_APPROVED", "Approved"),
            state_needs_input=e.get("AGENT_LOOP_STATE_NEEDS_INPUT", "Needs Input"),
            wip_label=e.get("AGENT_LOOP_WIP_LABEL", "loop-wip"),
            blocked_label=e.get("AGENT_LOOP_BLOCKED_LABEL", "loop-blocked"),
            blocked_cooldown_seconds=_int("AGENT_LOOP_BLOCKED_COOLDOWN", 3600),
            concurrency=_int("AGENT_LOOP_CONCURRENCY", 3),
            grace_seconds=_int("AGENT_LOOP_DECLUTTER_GRACE", 900),
            outage_backoff=_int("AGENT_LOOP_OUTAGE_BACKOFF", 900),
            branch_prefix=e.get("AGENT_LOOP_BRANCH_PREFIX", _default_branch_prefix(e)),
            workspace_dir=Path(workspace_override if workspace_override is not None else default_workspace),
            workspace_dir_is_checkout_root=workspace_override is not None,
            worktree_rel=e.get("AGENT_LOOP_WORKTREE_REL", ".worktrees/agent-loop"),
            repos_file=Path(
                e.get(env_var("REPOS_FILE"), str(default_config_dir / REPOS_FILENAME))
            ),
            log_dir=Path(e.get("AGENT_LOOP_LOG_DIR", str(default_logdir))),
            harness=harness,
            harness_bin=e.get("AGENT_LOOP_HARNESS_BIN", legacy_bin or default_bin(harness)),
            harness_flags=flags,
            assignee_id=e.get("AGENT_LOOP_ASSIGNEE_ID", ""),
            github_owner=e.get("AGENT_LOOP_GITHUB_OWNER", ""),
            github_project_number=_int("AGENT_LOOP_GITHUB_PROJECT_NUMBER", 0),
            github_repository=legacy_repository,
            github_repositories=configured_repositories,
            github_status_field=e.get("AGENT_LOOP_GITHUB_STATUS_FIELD", "Status"),
            dry_run=_env_flag(e, "DRY_RUN"),
            debug=_env_flag(e, "DEBUG"),
        )

    # ── Step 2: flags override env (only non-None values) ───────────────────
    def apply_flags(self, args) -> None:
        """Apply argparse results. Every flag defaults to ``None`` in the parser, so only
        values the user actually passed override the env layer — giving flag > env > default."""
        if getattr(args, "tracker", None) is not None:
            self.tracker = args.tracker
        if getattr(args, "branch_prefix", None) is not None:
            self.branch_prefix = args.branch_prefix
        if getattr(args, "user", None) is not None:
            # --user foo -> branch namespace "foo/" (lib:177, strip a trailing slash first).
            self.branch_prefix = args.user.rstrip("/") + "/"
        if getattr(args, "concurrency", None) is not None:
            self.concurrency = args.concurrency
        if getattr(args, "project_label", None) is not None:
            self.project_label = args.project_label
        if getattr(args, "queue_state", None) is not None:
            self.state_queue = args.queue_state
        if getattr(args, "todo_state", None) is not None:
            self.state_todo = args.todo_state
        if getattr(args, "active_state", None) is not None:
            self.state_active = args.active_state
        if getattr(args, "approved_state", None) is not None:
            self.state_approved = args.approved_state
        if getattr(args, "needs_input_state", None) is not None:
            self.state_needs_input = args.needs_input_state
        if getattr(args, "blocked_label", None) is not None:
            self.blocked_label = args.blocked_label
        if getattr(args, "blocked_cooldown", None) is not None:
            self.blocked_cooldown_seconds = args.blocked_cooldown
        if getattr(args, "declutter_grace", None) is not None:
            self.grace_seconds = args.declutter_grace
        if getattr(args, "assignee", None) is not None:
            self.assignee_id = args.assignee
        if getattr(args, "workspace_dir", None) is not None:
            self.workspace_dir = Path(args.workspace_dir)
            self.workspace_dir_is_checkout_root = True
        if getattr(args, "harness", None) is not None:
            self.harness = args.harness
        if getattr(args, "harness_flags", None) is not None:
            self.harness_flags = args.harness_flags
        if getattr(args, "claude_flags", None) is not None:
            self.harness_flags = args.claude_flags
        if getattr(args, "log_dir", None) is not None:
            self.log_dir = Path(args.log_dir)
        # store_const flags: only flip on when passed (so they never clobber env DRY_RUN=1/DEBUG=1).
        if getattr(args, "dry_run", None):
            self.dry_run = True
        if getattr(args, "debug", None):
            self.debug = True

    # ── Step 3: enforce the secret, make dirs, build the repo map ───────────
    def finalize(self, environ: dict[str, str] | None = None, *, create_log_dir: bool = True) -> "Config":
        e = os.environ if environ is None else environ
        try:
            validate_tracker(self.tracker)
        except ValueError as error:
            raise SystemExit(str(error)) from error
        if self.tracker == "linear":
            key = e.get("LINEAR_API_KEY", "")
            if not key:
                # Same contract as lib:202 — it's a secret, not a flag.
                raise SystemExit(
                    "LINEAR_API_KEY must be exported as an env var; it is a secret, not a flag"
                )
            self.linear_api_key = key
        else:
            token = e.get("AGENT_LOOP_GITHUB_TOKEN") or e.get("GH_TOKEN") or e.get("GITHUB_TOKEN", "")
            if not token:
                raise SystemExit(
                    "AGENT_LOOP_GITHUB_TOKEN or GITHUB_TOKEN must be exported for the github tracker"
                )
            if not self.github_owner or self.github_project_number <= 0 or not self.github_repositories:
                raise SystemExit(
                    "github tracker requires AGENT_LOOP_GITHUB_OWNER, "
                    "AGENT_LOOP_GITHUB_PROJECT_NUMBER, and AGENT_LOOP_GITHUB_REPOSITORIES"
                )
            for configured_repository in self.github_repositories:
                owner, sep, repository = configured_repository.partition("/")
                if not sep or not owner or not repository or "/" in repository:
                    raise SystemExit(
                        "AGENT_LOOP_GITHUB_REPOSITORIES must contain explicit owner/repository values"
                    )
            self.github_repository = self.github_repositories[0]
            if not self.github_status_field:
                raise SystemExit("AGENT_LOOP_GITHUB_STATUS_FIELD must not be empty")
            self.github_token = token
        validate_harness(self.harness)
        if create_log_dir:
            self.log_dir.mkdir(parents=True, exist_ok=True)
        # Repo routing is operator-supplied (see repos.py). There is deliberately no
        # built-in map: a wrong default opens a plausible-looking pull request in the
        # wrong repository. Relative paths resolve against workspace_dir.
        repo_map = repos_config.load(self.repos_file, self.workspace_dir)
        self.repos = repo_map.repos
        self.repo_remotes = repo_map.remotes
        self.repo_base_branches = repo_map.base_branches
        self.aliases = repo_map.aliases
        self.repo_descriptions = repo_map.descriptions
        self.default_repo = repo_map.default_repo
        self.design_repo = repo_map.design_repo
        self.infra_repos = repo_map.infra_repos
        return self

    def repo_root(self, repo_label: str) -> Path:
        """Resolve a configured ``repo:<label>`` suffix to its checkout path.

        An unmapped label must be explicit: falling back to the monorepo can open a
        plausible-looking pull request in the wrong repository.
        """
        try:
            return self.repos[repo_label]
        except KeyError as error:
            raise ValueError(f"unknown repository label: {repo_label!r}") from error

    def collapse_repo(self, repo_label: str) -> str:
        """Resolve a retired repo label to its canonical one. Unknown labels pass
        through unchanged so the caller's own not-found handling still fires."""
        return self.aliases.get(repo_label, repo_label)

    def repo_remote(self, repo_label: str) -> str:
        """Return the configured named remote, accepting aliases and rejecting unknown labels."""
        canonical = self.collapse_repo(repo_label)
        self.repo_root(canonical)
        return self.repo_remotes.get(canonical, "origin")

    def repo_base_branch(self, repo_label: str) -> str:
        """Return the configured PR base branch, accepting aliases and rejecting unknown labels."""
        canonical = self.collapse_repo(repo_label)
        self.repo_root(canonical)
        return self.repo_base_branches.get(canonical, "main")

    def for_loop(self, tag: str) -> "Config":
        """Return a copy tagged for a specific loop (selects log subfolder + audit ledger)."""
        return replace(self, log_tag=tag)

    @property
    def claude_bin(self) -> str:
        """Backward-compatible alias while Claude is the only concrete harness."""
        return self.harness_bin

    @claude_bin.setter
    def claude_bin(self, value: str) -> None:
        self.harness_bin = value

    @property
    def claude_flags(self) -> str:
        """Backward-compatible alias while Claude is the only concrete harness."""
        return self.harness_flags

    @claude_flags.setter
    def claude_flags(self, value: str) -> None:
        self.harness_flags = value
