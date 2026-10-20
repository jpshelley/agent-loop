# Agent Loop

Agent Loop is a developer-run orchestration loop for coding agents. It reads a
small, explicitly scoped queue from Linear or GitHub Projects, creates an
isolated worktree per issue, and runs your configured local coding harness in a
detached `tmux` session. Each developer owns their own installation, credentials,
working copy, and queue scope.

The project is designed to run on a laptop, a development VM, or a small server.
It shells out to standard local tools instead of depending on a hosted Agent Loop
service. The first supported harnesses are Claude Code, Codex, Cursor, and GitHub
Copilot. See the [open-source plan](docs/open-source-plan.md) for the release
sequence, GitHub-first tracker work, and the later remote-provider path.

## What it does

```text
queue → dispatch → Todo → develop → In Progress → pull request → In Review → debug → Approved → deploy
```

- **dispatch** reads eligible queue entries and admits work up to the configured
  concurrency limit.
- **develop** creates a worktree and starts the chosen coding harness.
- **debug** follows up on CI and review feedback for open pull requests.
- **deploy** runs the configured merge/closeout workflow for approved work.
- **declutter** reclaims abandoned sessions and worktrees.

The default workflow prompts are bundled and portable. A repository can opt into
its own policy or review workflow through its local configuration.

## Install

Agent Loop requires Python 3.11+, `git`, `tmux`, and the GitHub CLI (`gh`). Install
the coding-agent CLI you intend to use separately.

```bash
git clone https://github.com/jpshelley/agent-loop.git
cd agent-loop
python -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/pip install -e '.[dev]'
```

Create editable local configuration with a tracker, harness, and local checkout
map. The command creates `~/.agent-loop` with owner-only permissions and never
accepts or prints credentials:

```bash
agent-loop init --tracker github --harness cursor --repo app="$HOME/Development/app"
```

Set the credential placeholders and GitHub Project values in `~/.agent-loop/env`,
then validate the scope before starting a loop:

```bash
agent-loop doctor
```

`doctor` is read-only. It verifies the required local tools, each configured
checkout, its Git remote and tracked base branch, then validates the selected
tracker scope. Fix every reported failure before starting a live loop.

Run a preview before starting a live session:

```bash
DRY_RUN=1 agent-loop dispatch
DRY_RUN=1 agent-loop develop
```

Start the five core loops with the committed local launcher:

```bash
scripts/start-loops.sh
tmux attach -t develop
```

The launcher uses `~/.agent-loop/env`. It does not choose a coding harness: the
harness comes from your configuration.

## Choose a coding harness

Set the harness in `~/.agent-loop/env`. You may also override the binary and flags
for wrappers, alternate installations, or a remote-agent bridge.

```bash
# Claude Code (default)
export AGENT_LOOP_HARNESS=claude
export AGENT_LOOP_HARNESS_FLAGS='--permission-mode auto'

# Codex
# export AGENT_LOOP_HARNESS=codex
# export AGENT_LOOP_HARNESS_FLAGS='--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check'

# Cursor Agent
# export AGENT_LOOP_HARNESS=cursor
# export AGENT_LOOP_HARNESS_BIN=agent

# GitHub Copilot CLI
# export AGENT_LOOP_HARNESS=copilot
# export AGENT_LOOP_HARNESS_BIN=copilot
```

The harness adapter only controls how Agent Loop starts a command. The tracker,
worktree, and lifecycle behavior are shared across harnesses.

## Configure a tracker

Agent Loop supports Linear and a deliberately scoped GitHub Issues + Projects v2
adapter. Use one tracker at a time per installation.

### GitHub Projects

GitHub is the recommended path for teams that already plan and review work in
GitHub. The token and project scope stay local in `~/.agent-loop/env`:

```bash
export AGENT_LOOP_TRACKER=github
export AGENT_LOOP_GITHUB_TOKEN=github_pat_...
export AGENT_LOOP_GITHUB_OWNER=your-org
export AGENT_LOOP_GITHUB_PROJECT_NUMBER=12
export AGENT_LOOP_GITHUB_REPOSITORIES=your-org/your-repo
export AGENT_LOOP_GITHUB_STATUS_FIELD=Status
```

The selected Project Status field is the workflow source of truth. Only issues
from the explicit repository list are eligible. Validate the configuration before
starting a loop:

```bash
agent-loop doctor --tracker github
```

See [tracker adapters](docs/tracker-adapters.md#github-issues-and-projects) for
permissions, workflow-state mapping, and the queue contract.

### Linear

```bash
export AGENT_LOOP_TRACKER=linear
export LINEAR_API_KEY=...
export AGENT_LOOP_PROJECT_LABEL=loop
```

Linear projects carrying the configured project label opt into the queue.

## Route issues to local checkouts

`agent-loop init --repo label=/path/to/checkout` creates
`~/.agent-loop/repos.toml` to map a `repo:<name>` issue label to a local checkout.
Each entry can select a remote and pull-request base branch.

```toml
[repos.web]
path = "/Users/me/Development/web"
remote = "origin"
base_branch = "main"
description = "Customer-facing web application"

[repos.api]
path = "/Users/me/Development/api"
remote = "origin"
base_branch = "main"
```

Use `agent-loop init --replace` only when you deliberately want to regenerate both
local files. Advanced configurations can copy and adapt
[examples/repos.toml](examples/repos.toml). Agent Loop creates its worktrees below
`.worktrees/agent-loop` in each selected checkout.

## Configuration reference

All settings use `AGENT_LOOP_*` names. Command-line flags override environment
variables, which override defaults. `LINEAR_API_KEY` and GitHub tokens are always
environment-only.

| Setting | Default | Purpose |
| --- | --- | --- |
| `AGENT_LOOP_TRACKER` | `linear` | `linear` or `github` |
| `AGENT_LOOP_CONCURRENCY` | `3` | Maximum admitted/running/review work |
| `AGENT_LOOP_HARNESS` | `claude` | `claude`, `codex`, `cursor`, or `copilot` |
| `AGENT_LOOP_HARNESS_BIN` | harness default | Executable used for the harness |
| `AGENT_LOOP_HARNESS_FLAGS` | harness default | Arguments passed to the harness |
| `AGENT_LOOP_REPOS_FILE` | `~/.agent-loop/repos.toml` | Local repository routing map |
| `AGENT_LOOP_LOG_DIR` | `~/.agent-loop/logs` | Session and audit logs |
| `AGENT_LOOP_BRANCH_PREFIX` | `$USER/` | Namespace for generated branches |
| `AGENT_LOOP_WORKSPACE_DIR` | `~/Development` | Root used when repository paths are relative |

Run `agent-loop dispatch --help` for the complete command flags.

## Naming

Agent Loop uses the `agentloop` Python package, `agent-loop` command,
`AGENT_LOOP_*` configuration prefix, `~/.agent-loop` local directory, and
`agent-loop` durable comment-marker namespace.

## Development

```bash
.venv/bin/python -m pytest
```

The project has no runtime Python dependencies. Its external integrations are the
local command-line tools and the selected tracker APIs.

## Community and security

Agent Loop is available under the [MIT License](LICENSE). Contributions follow
the [contribution guide](CONTRIBUTING.md) and [code of conduct](CODE_OF_CONDUCT.md).
For a suspected vulnerability, follow the private reporting guidance in
[SECURITY.md](SECURITY.md) instead of opening a public issue.
