# Security policy

## Supported versions

Security fixes are made on the latest `main` branch. There is not yet a supported
release branch.

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability or exposed
credential. Use GitHub's private vulnerability-reporting form for this repository
when it is available. If private reporting is unavailable, contact
[@jpshelley](https://github.com/jpshelley) privately and include only enough
information to establish a secure reporting channel.

Reports are most useful with a clear reproduction, affected version or commit,
impact, and any mitigation already identified. The maintainer will acknowledge
the report, assess it, and coordinate a fix before public disclosure when
possible.

## Scope

Agent Loop runs local coding-agent commands and uses operator-provided tracker
and GitHub credentials. Report vulnerabilities that could expose secrets, escape
the configured repository or worktree boundary, modify tracker or GitHub state
without the operator's intended scope, or delete unpublished work.
