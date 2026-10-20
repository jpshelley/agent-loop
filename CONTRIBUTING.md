# Contributing to Agent Loop

Thanks for helping improve Agent Loop. The project is a local orchestration tool,
so changes must keep credentials local, preserve work in progress, and avoid
acting outside a configured repository and tracker scope.

## Before opening a pull request

1. Discuss substantial changes in an issue first so the proposed behavior and
   compatibility impact are clear.
2. Create a focused branch from the current `main` branch.
3. Add or update tests for behavior that can start work, modify tracker state,
   create a pull request, or clean up a worktree.
4. Run the full suite:

   ```bash
   python -m pytest -q
   ```

5. Update the README or documentation when installation, configuration, or
   operator-visible behavior changes.

## Pull-request expectations

Describe the trigger, resulting behavior, and validation performed. Do not
include credentials, repository-specific customer data, or private agent prompts
in code, test fixtures, logs, or screenshots.

Keep source and workflow changes neutral: a fresh installation must not require
organization-specific accounts, skills, directories, or infrastructure. New tracker,
forge, harness, or provider support needs clear boundaries and failure behavior.

Human review and manual merges are the default product policy. A change that
widens automation, changes cleanup behavior, or permits tracker state transitions
needs explicit tests and maintainer review.

## Development setup

Use Python 3.11 or newer. The runtime has no Python dependencies; install the
development extras in a virtual environment:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

See the [README](README.md) for local configuration and the
[architecture overview](docs/architecture.md) for the current component model.
