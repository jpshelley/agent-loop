# Read-only GitHub Project selection

`agent-loop preview` explains which work a personal loop could pick up. It uses a
separate TOML profile and GraphQL queries only. It does not run dispatch/develop,
start an agent, claim work, create local directories, or change GitHub resources.
It does not read or source `~/.agent-loop/env`; credentials come from the process
environment. Existing Linear and GitHub loop configurations remain unchanged.

Copy [the example](../examples/github-preview.toml) to a local profile and set the
Project owner/number, allowed issue repositories, your GitHub username, and code
repository routes. With an exported `AGENT_LOOP_GITHUB_TOKEN`, `GH_TOKEN`, or
`GITHUB_TOKEN`:

```bash
agent-loop preview --profile /path/to/github-preview.toml
agent-loop preview --profile /path/to/github-preview.toml --json
```

The token needs read access to the selected Project and its private issue
repositories. The authenticated GitHub username must match `selection.assignee`.
No coding harness, tmux, local checkout map, or Linear key is needed.

## Selection rules

Every candidate must be an open, unarchived leaf Issue in the selected Project,
from an allowed repository, whose configured status is `Ready`. Parent issues
are work roots; they are never execution candidates. Pull requests and draft
Project items are ignored. Issues outside the Project are not expanded or added.

A leaf is opted in by its own `loop` label and assignment to the operator. With
`inherit_parent = true`, its **immediate parent** may instead authorize it when
that parent is also in this Project, open, unarchived, Ready, from an allowed
repository, labeled `loop`, and assigned only to the operator. Label and
ownership inheritance come from that same eligible parent. A leaf's own Ready
status is always required. Grandparent inheritance is not part of this slice.

Any assignee other than the operator on a leaf or its immediate parent prevents
selection, including mixed assignments that also include the operator. Open
blockers on either prevent selection. Closed or archived parents also prevent
selection. A parent outside the Project prevents selection because its readiness,
ownership, and blockers cannot be verified in this snapshot.

`selection.milestones` optionally restricts the queue to qualified
`owner/repository#milestone-number` values. A leaf's own milestone takes
precedence; a leaf without one inherits its authorizing parent's milestone. A
milestone itself has no issue label or Ready status, so selecting it does not
authorize every issue inside it.

## Repository routing

GitHub's built-in Repository field is the issue repository, not necessarily the
repository containing its implementation. `repository_routes` maps each allowed
issue repository explicitly to a code repository; preview never guesses from an
issue title or existing labels. The initial profile supports one code repository
per issue repository. For planning repos spanning multiple codebases, run a
profile restricted to a known milestone/work scope and confirm the route before
adding execution; finer routing remains future work.

## Output and safety boundary

Text output lists each Issue as eligible or skipped with reason codes. JSON also
includes the proposed code repository, inherited work root, and effective
milestone. A valid empty selection exits successfully. Invalid profiles, user
mismatches, inaccessible Projects, absent Ready options, malformed responses, and
incomplete labels/assignees/blocker collections fail with a nonzero exit and no
partial selection output. Project items and fields are fully paginated.

This is a snapshot, not a lock. A future claim step must reread eligibility,
prevent competing claims, and verify its writes before starting an agent.

## Team workflow profile roadmap

This preview supports a Project workflow of
`Triage → Ready → In Progress → In Review → In QA → Done` without requiring
legacy `Todo`, `Approved`, or `Needs Input` options. Only the Ready option is
validated here. It does **not** reconfigure the live loop to use this lifecycle.

The next slice is an ownership-aware claim that assigns the chosen leaf before
moving it to In Progress. After that, add review/merge handoff to In QA with
deployment disabled, then validate a deliberately opted-in pilot hierarchy.
Human QA completion and parent status rollups are separate future steps.
