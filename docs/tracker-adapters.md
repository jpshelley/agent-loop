# Tracker boundary: first extraction

The orchestrator selects its tracker through `agentloop.tracker.resolve`.
Linear and GitHub Projects are implemented adapters. Select either with
`agent-loop dispatch --tracker linear` or `--tracker github` (or the
`AGENT_LOOP_TRACKER` environment variable). CLI overrides environment, which
overrides the default. Unsupported names fail during configuration before
credential validation or loop execution.

## Contract

`tracker.Issue` and `tracker.Comment` are immutable snapshots. Each issue has a
stable provider ID, a human-facing identifier, an opaque mutation scope, title,
labels, linked PR URLs, timestamped comments, normalized priority, child/dependency
facts, and start timestamp. A Linear scope is a team ID. Core treats it as an
opaque context; it does not construct team cache keys or inspect GraphQL nodes.

The `Tracker` protocol groups these operations:

- `resolve_scope()` discovers the explicitly opted-in queue and reports whether
  it contains any projects.
- `issues_in_states(states, include_comments=False, newest_comments=False)`
  returns normalized facts. Lower `priority_rank` values sort first; Linear's
  priority zero becomes 99. Comments retain server timestamps.
- `ensure_scope`, `move_state`, `ensure_required_label`, `add_label`, `add_comment`,
  and `assign_issue` own provider-specific metadata and mutations.
- CI-repair and approval-wait marker methods preserve the existing durable
  comment formats so in-flight work remains readable.
- `state_contract`, `blocked_contract`, and `classifier_context` supply the
  provider-specific instructions used in headless agent prompts.

Adapters raise `TrackerError` for unreliable reads. `LinearError` subclasses it
for compatibility. Empty collections are successful reads; malformed collections
are errors. In particular, declutter skips orphan cleanup when blocked-work state
cannot be read. Required label creation/application must succeed before admitting
work or treating a blocked handoff as durable.

Loops retain policy: leaf selection, priority ordering, capacity accounting,
blocked cooldowns, PR handoff repair, review decisions, and recovery transitions.
API queries, response decoding, caches, and credential instructions stay in the
adapter. `linear.py` retains its old low-level methods for compatibility, but the
loops no longer call them.

## Verification and current limits

The test suite exercises both real Linear response decoding with fake transport
and a neutral dispatch adapter that has no `query`, `loop_ids`, or label caches.
Existing smoke tests distinguish workflow states explicitly, rather than routing
canned API responses by page size. No live tracker or coding session is required.

This is an extraction, not the full portable release. The following remain:

- The GitHub adapter is a deliberately bounded first slice: it uses one Project
  and a configured repository allowlist. Nested labels read at most 100 values
  per Issue. A blocker or PR-reference collection beyond 100 is rejected before
  admission, rather than being mistaken for complete; a later pagination
  expansion must retain this fail-closed behavior.
- Issue reads traverse all outer pages in batches of 25 to limit per-request
  complexity. Project discovery still reads up to 100 projects; nested reads
  remain bounded at 30 labels, 15 attachments/relations, and 100 comments when
  requested. Complete project/nested pagination is a separate follow-up.
- The bundled develop, review, and deploy workflows are harness-neutral. The
  optional design loop and repository policy remain separate policy hooks.
- Durable ownership, cancellation, full work preservation, and EC2/Bedrock
  validation remain subsequent milestones. Identity checks alone are not a
  security sandbox or a distributed claim mechanism.

## Local execution identities

Human-facing issue identifiers stay in agent prompts and tracker comments.
`tracker.execution_key(cfg, issue)` separately derives the local name used for
session guards, worktree directories, branch tokens, log names, blocked-worktree
protection, and outage recovery. The adapter supplies a stable native issue ID
and opaque scope; generated names hash the provider, scope, and native ID, so
different repositories can both contain issue #7 without sharing a local name.

Generated keys use `al-<provider>-<24 hexadecimal characters>` and are at most 64
ASCII characters. They contain no path separators or tmux target punctuation.
Changing a display identifier does not change the key when its native ID and
scope remain stable. Adapters must supply consistent identity inputs.

Every tracker item receives a generated execution key, including Linear issues.
Independent operators must still select disjoint queues. The design hook retains
its existing `design-<PR number>` session names.

Cleanup rejects unsafe keys, symlink escapes, and worktree branches that do not
match the generated execution token. If removal fails, cleanup retains the
branch. This does not replace the planned durable ownership and unpublished-work
recovery design.

For a PR with an arbitrary existing head branch, debug may reuse an already
registered worktree only after verifying its repository root, matching branch,
and clean working tree. It updates with a fast-forward-only merge; dirty files
or divergent history stop the attempt and remain available for recovery.

## Repository Git targets

String repository entries retain `origin` and `main`. A table entry overrides
the named remote and literal base branch:

```toml
[repos.api]
path = "~/src/api"
remote = "upstream"
base_branch = "release/develop"
```

The remote must already exist in that checkout. Repository aliases inherit the
canonical target. Configuration rejects invalid names, option/refspec syntax,
and unknown table keys. Git fetches update explicit remote-tracking refs, so
narrow local fetchspecs and similarly named local branches cannot select a stale
base. Development starts from that full ref and tells the coding agent which PR
base to use. Review repair uses the selected checkout's remote and the existing
PR branch. Design repository discovery reads the configured remote URL afresh.

## GitHub Issues and Projects

Select the GitHub adapter with `--tracker github` or
`AGENT_LOOP_TRACKER=github`. It requires no Linear credential. Configure one
GitHub Projects v2 board and an explicit repository allowlist:

```sh
export AGENT_LOOP_GITHUB_TOKEN=... # or GH_TOKEN / GITHUB_TOKEN
export AGENT_LOOP_GITHUB_OWNER=travefy
export AGENT_LOOP_GITHUB_PROJECT_NUMBER=7
export AGENT_LOOP_GITHUB_REPOSITORIES=travefy/app,travefy/api
export AGENT_LOOP_GITHUB_STATUS_FIELD=Status # default
```

The selected Project's single-select Status field is the workflow authority.
Its options must include every configured loop state (`Backlog`, `Todo`, `In
Progress`, `In Review`, `Approved`, and `Needs Input` by default). Run
`agent-loop doctor --tracker github` before starting loops; it reads the
Project and validates the status mapping without mutations.

Only non-archived, open Issue items in the configured repositories that carry
the opt-in `loop` label are admitted. Draft items, pull-request items, closed
issues, and other repository items are skipped. The adapter mutates Status with
the selected Project ID, item ID, field ID, and option ID; labels and comments
continue to target the Issue. GitHub issue open/closed state is deliberately
separate from Project workflow state.

The adapter reads all Project-item, field, and decision-bearing comment pages,
rejects malformed or repeated cursors, and preserves the CI-fix and
approval-wait comment markers. It recognizes linked PRs only from GitHub Issue
references, not arbitrary text URLs. GitHub remains a separate forge adapter.
