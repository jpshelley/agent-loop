# Implementation progress

Updated October 20, 2026.

## Personal project setup

- Personal GitHub destination: `https://github.com/jpshelley/agent-loop`.
- Independent local clone: `/Users/jshelley/Development/personal/agent-loop`.
- Dedicated implementation worktree:
  `/Users/jshelley/Development/personal/agent-loop/.worktrees/execution-identity`.
- Current feature branch: `codex/execution-identity`, based on
  `codex/extract-linear-tracker`.
- `origin` points to the personal repository. Personal pushes default to `origin`.
- The original checkout and its remote configuration remain unchanged.
- Git history through `0f99312`, including all three existing decoupling commits,
  is preserved. The earlier planning document is included in this worktree.

The personal repository is private. Public release review remains outstanding.

## First implementation milestone

Implemented the tracker boundary with Linear as the first adapter:

- Immutable normalized issue/comment data and a typed tracker protocol/factory.
- All tracker-consuming loops use adapter reads and opaque mutation scopes;
  GraphQL response parsing and Linear cache access are removed from loops.
- Linear-specific agent state/blocked/classification instructions come from the
  selected adapter, including review, deploy, and design session prompts.
- Required-label creation/application fails closed, including a rejected create
  mutation that happens to return an ID.
- Explicit tracker selection through environment and CLI; unsupported providers
  are rejected before requesting credentials.

## Validation

- Original baseline: 270 tests passed.
- Final extracted implementation: 338 tests passed.
- Existing regression checks cover dispatch selection, blocked-ticket parking,
  PR handoff repair, approval waits, CI-fix idempotency, and dry-run behavior.
- New tests cover normalized adapter data, malformed reads, provider selection,
  label persistence failures, complete issue pagination, numeric PR link selection,
  and dispatch using a neutral tracker.
- Final review corrected an oversized query by using 25-issue pages with cursor
  validation; malformed project/ownership data fails closed before cleanup.
- A distributable wheel builds successfully; installation/CLI checks use an
  isolated environment.

Tests use fake service responses. No live Linear issues, production queues,
coding sessions, merges, or deployments were exercised.

## Second implementation milestone

- Separated tracker display identifiers from safe local execution keys across
  dispatch, develop, debug, deploy, blocked-worktree protection, and recovery logs.
- Preserved existing canonical Linear keys while adding bounded keys derived from
  provider, scope, and native issue ID for other identities.
- Added per-repository named remotes and base branches, preserving string-entry
  defaults and alias behavior. Fetches and checkout use full remote-tracking refs.
- Kept original identifiers in agent prompts and quoted Claude prompts safely.
- Hardened cleanup against unsafe keys, symlink escapes, unrelated branches, and
  failed worktree removals.
- Added safe repeated debug worktree reuse for arbitrary existing PR branches,
  preserving unpushed commits and refusing dirty/divergent or unregistered paths.
- Added real local Git integration checks for custom/nested bases, narrow
  fetchspecs, repeated debug attempts, and recovery protections. No external
  tracker or coding-agent sessions are needed for these tests.

Validation: **498 tests passed**, including local branch/ref collisions,
unregistered nested paths, dirty/divergent worktree retention, symlink escapes,
and branch retention on failed cleanup. Independent source review found no
remaining introduced regressions. The wheel builds; isolated installation checks
verify the new identity module and repository target parsing.

## Third implementation milestone

- Added the GitHub Issues + Projects v2 tracker behind the existing tracker
  protocol, with a GitHub-only configuration profile and no Linear dependency.
- Scoped each run to one configured Project, single-select workflow field, and
  explicit repository allowlist. Open issue state remains separate from Project
  workflow state.
- Added `agent-loop doctor --tracker github` to validate the Project, status
  mapping, and readable scope without writing to GitHub.
- Project fields, items, and marker comments paginate with strict cursor checks;
  archived, draft, pull-request-only, closed, unlisted, and opt-out items cannot
  enter the queue. Status writes confirm the selected Project item.
- Reused stable provider/scoped execution keys and existing loop policy; labels,
  comments, assignments, CI-fix markers, and approval-wait markers now map to
  GitHub Issue operations.

Validation: **512 tests passed** with fake GraphQL responses. Tests cover
GitHub-only configuration, scope/status validation, qualified issue identity,
repository and opt-in filtering, full item pagination, duplicate items, cursor
failures, truncated blocker/PR-reference rejection, and status mutations pinned to the selected Project/item. No live
GitHub Project, Travefy Issue, coding session, or provider credential was used.

## Fourth implementation milestone

- Added a local, read-only `doctor` preflight for required tools, configured
  repository checkouts, Git worktree validity, named remotes, and remote-tracking
  base branches before tracker validation.
- `doctor` does not create logs, including when tracker validation fails, and
  returns bounded Git diagnostics so an operator can fix local setup errors.

Validation: **526 tests passed** locally. GitHub CI passed on Python 3.11 and
3.12 before [PR #7](https://github.com/jpshelley/agent-loop/pull/7) merged as
`1442ef6`.

## Next work

1. Demonstrate one non-draft PR plus issue evidence and the intended project
   status update, then validate continuous recovery and EC2/Bedrock.

Before a live Travefy pilot, choose an authorized test repository and Project,
confirm its Status options and automations, and select an approved coding agent.

## Fifth implementation milestone

- Removed the remaining migration behavior for pre-rename execution keys and
  branches. Every tracker item now receives a generated, tracker-scoped local
  execution key; cleanup only recognizes those generated keys.
- Removed the tracked internal work record and remaining organization-specific
  references from the public contributor and planning material.

Validation: **520 tests passed** locally. This change intentionally does not
adopt or clean up existing pre-rename sessions, worktrees, branches, or logs.

## Sixth implementation milestone

- Replaced organization-specific repository names, issue identifiers, and pull
  request URLs in the test suite with neutral `example-org` fixture data.
- Removed historical source-repository identifiers from the shareable planning
  and progress material.

Validation: **520 tests passed** locally.

## Review evidence for the second milestone

[PR #1](https://github.com/jpshelley/agent-loop/pull/1) merged into personal
`main` as `3d6518c` after its Python 3.11 and 3.12 checks completed. This branch
normally merges that reviewed tracker baseline before PR #2 is retargeted, so the
stack has no rewritten history. Independent Codex review of the execution identity
and Git-target source at `83ae50a` found no blocking introduced defects, with all
498 tests passing. GitHub CI and the final review on the integrated branch are
verified before the second merge.
