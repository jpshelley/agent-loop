# Architecture overview

Agent Loop is a local, developer-operated command-line tool. It does not run a
hosted control plane or store user credentials remotely.

```text
selected tracker -> loop policy -> local Git worktree -> coding harness -> GitHub pull request
                         |                |                  |
                     tmux session      local logs        configured credentials
```

## Current components

- **CLI and configuration** load local `~/.agent-loop/env` and `repos.toml`.
  Environment variables and flags select the tracker, harness, repository route,
  and local execution options. Credentials remain outside repository files.
- **Tracker adapters** normalize queue reads and workflow-state transitions.
  Linear and GitHub Issues + Projects v2 are supported independently; one is
  selected per installation.
- **Loops** reconcile explicit queue states for dispatch, development, review
  repair, deployment handoff, and cleanup. They enforce the configured
  concurrency and operate only on routed repositories.
- **Worktree management** creates isolated Git worktrees below the selected
  checkout and protects unpushed or dirty work from unsafe cleanup.
- **Harness adapters** construct commands for Claude Code, Codex, Cursor, or
  GitHub Copilot. The harness owns its own login and model-provider integration.
- **GitHub CLI and Git** provide repository, pull-request, and local checkout
  operations. `tmux` provides detached local process observation.

`agent-loop doctor` is a read-only preflight: it verifies local tools, routed
checkouts, configured Git remotes/base refs, and the selected tracker scope.

## Operational boundary

An installation is one operator and one explicitly scoped queue partition. It is
safe to run on a laptop, development VM, or small server where the configured
tools, checkouts, and credentials are present. Multiple installations must use
disjoint queue scopes; the current local process tracking is not a distributed
lease.

Human review and manual merge are the default policy. A coding harness can edit
the selected worktree and invoke tools available to its local account, so
worktrees are isolation for source changes rather than a security sandbox.

## Planned boundaries

The open-source plan proposes a durable local run store, a supervised subprocess
executor, and a versioned external-agent protocol. Those are not part of the
current runtime contract. See the [open-source plan](open-source-plan.md) for
the staged delivery and EC2/Bedrock path.
