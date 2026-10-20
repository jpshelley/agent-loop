# Agent Loop open-source plan

Planning date: October 16, 2026. Status: implementation started; see
[implementation progress](implementation-progress.md) for verified work and remaining scope.

This plan is based on local commit `0f99312` on `oss/phase-1-decouple`, including
three decoupling commits beyond the local `main` reference (`02ec6d0`). Those
changes are groundwork, not evidence that an open-source release has shipped.

## Product direction

Make Agent Loop an open-source developer tool that turns explicitly selected
issues into reviewable pull requests, using the developer's repositories,
credentials, coding agent, and workflow. Each person installs and operates their
own instance. A developer can run the same tool on a laptop or a dedicated Linux
host, including EC2, and can share a reusable team configuration with colleagues.

The first audience is a developer outside the original organization, particularly a Travefy
teammate. Success means that person can install it, connect a test repository and
issue queue, preview the proposed work, and get a useful PR without
organization-specific accounts, private skills, directory conventions, or John
being present.

Extract Linear as the first tracker adapter, then implement GitHub Issues and
Projects as the second tracker because that is what Travefy uses. GitHub remains
the first forge adapter independently of which tracker is selected. The Travefy
pilot must use GitHub Issues and Projects without requiring a Linear account.
Defer Jira, GitLab, and a generic plugin marketplace until that path is validated.
Travefy's specific project fields, repository layout, and approved AI providers
remain to be established.

### Personal repository home

The original checkout does not need to be changed to establish the personal
project home. The authenticated GitHub user is `jpshelley`. Use
`jpshelley/agent-loop` as the intended personal home, subject to confirming the
destination name is available.

Establish that home before publishing implementation branches or PRs. Prefer a
personal fork when source visibility and organization policy allow it; otherwise
choose an independent repository with preserved attribution and deliberate history
selection. Moving personal development does not require transferring ownership of
the existing shared repository. Decide repository visibility separately from the
personal-account move; complete the release content/rights review before making
new material public.

Preserve all task work, including the existing decoupling commits and this planning
branch. Verify local branch ancestry and destination contents before changing any
remote. The intended final layout uses `origin` for the personal repository. Add
an upstream remote only when continued source comparison is appropriate. Set
personal branches to push to `origin` and verify the PR base repository explicitly.
Git remotes are shared across linked worktrees, so audit the existing worktrees
before changing their shared remote configuration. Keep the source repository's
branches and visibility intact during this migration.

Acceptance: the personal destination exists with the chosen visibility, the
decoupling/planning work is preserved, push and PR targets resolve to the personal
repository, and the original source remains available for comparison. This plan
records the intended migration. A separate local clone and private personal
repository have now been created; the original checkout's remotes are unchanged.
See the progress record for the current local development location and publication status.

The initial release is a CLI with observable local runs. A shared web service,
multi-tenant accounts, billing, Kubernetes, and a distributed worker fleet are
later product decisions. An EC2 installation initially represents one operator
and one queue partition, just like a laptop installation.

## What exists, and what still needs to change

| Area | Evidence in this checkout | Implication |
| --- | --- | --- |
| Orchestration | Six Python reconciliation loops; existing tests for review decisions, blocked tickets, dry runs, and API boundaries | Preserve the working state machine and failure handling rather than rewrite it |
| Repository configuration | `repos.py`, `examples/repos.toml`; recent commits remove built-in routing and derive a user branch namespace | Build on this work; do not schedule it again as unfinished |
| Coding agents | `harness.py` supports Claude Code, Codex, Cursor, and GitHub Copilot command construction | Useful starting seam, but not yet an agent lifecycle or provider abstraction |
| Workflow behavior | Bundled harness-neutral prompts cover implement, review, deploy, and optional post-merge review operations | Team-specific policy can be added through a future trusted workflow-pack mechanism |
| Organization assumptions | Core workflows, design review, and cleanup operate on configured repositories and canonical Agent Loop worktrees | Add an explicit trusted policy-pack mechanism for team-specific rules |
| Repository assumptions | Develop fetches `origin/main` and starts worktrees there | Support each repository's configured or discovered remote and base branch |
| Credentials | `config.py` requires `LINEAR_API_KEY`; `LINEAR_STATE_CONTRACT` tells agents to use that key directly; GitHub reads use authenticated `gh` | Support explicit identities and keep tracker transitions under the orchestrator's control |
| Scheduling and ownership | CLI interval runners, tmux sessions named by issue key, and Linear labels | tmux is local process deduplication, not a lock across machines; shared queues need an ownership policy |
| Recovery | `outage.py` parses Claude-style usage banners; janitor can delete orphaned worktrees | Normalize failures across agents and preserve unpublished work before cleanup |
| Review gates | Recent commit makes Greptile/Codex approval signals opt-in | Keep human review as the generic default; README defaults need reconciliation |
| API completeness | Linear project discovery uses a fixed first-page query | Audit pagination, request timeouts, and API rate-limit handling before supporting larger workspaces |
| Launcher | `start-loops.sh` uses the operator's configured harness and starts the five core loops | Add validated service profiles for long-running local and EC2 hosts |
| Distribution | Python 3.11+, stdlib-only runtime, Ubuntu CI on Python 3.11/3.12, MIT license, contributor/security/community guidance | Good small foundation; release automation, supported-platform verification, and a deployment image remain |

Source areas: [configuration](../src/agentloop/config.py),
[agent commands](../src/agentloop/harness.py),
[spawn](../src/agentloop/spawn.py),
[dispatch contracts](../src/agentloop/loops/dispatch.py),
[develop](../src/agentloop/loops/develop.py),
[worktree management](../src/agentloop/worktree.py),
[review decisions](../src/agentloop/loops/debug.py),
[launcher](../scripts/start-loops.sh), and [packaging](../pyproject.toml).

## Architecture decisions

Separate the concepts that are currently bundled together:

| Concept | Responsibility | First implementations |
| --- | --- | --- |
| Orchestrator | Admission, ownership, retries, review policy, state reconciliation, audit | Existing Python loops, refactored incrementally |
| Tracker adapter | List eligible work, read context, post evidence, map logical states | Linear first; GitHub Issues and Projects next |
| Forge adapter | Repository identity, PR lookup/creation, checks and review evidence, optional merge | GitHub; retain `gh` initially |
| Workflow pack | Task instructions, validation commands, evidence requirements, optional team policy | Bundled generic workflows; optional team overrides |
| Agent adapter | Capabilities, noninteractive invocation, events, cancellation, normalized result | Claude Code, Codex, documented external executable protocol |
| Provider profile | Model endpoint, model ID, credential source, provider limits | Agent-managed login/API access; Claude Code with Bedrock |
| Executor | Start and monitor processes, isolate filesystem/environment, collect output | Local subprocess; optional tmux observation; container execution after local pilot |
| Run store | Durable attempts, ownership metadata, heartbeats, outcomes, artifact locations | SQLite on a local disk, with a schema version |

The orchestrator should ask an agent to implement a task, rather than infer
success from its prose or ask it to manage every service transition. The
orchestrator fetches issue context, creates the worktree, renders the workflow,
starts the run, validates its result against Git/GitHub, and then updates the
selected tracker.

Use a versioned `TaskSpec` containing installation/profile identity, issue ID,
repository identity, base revision, operation, context, policy, and run ID. Pass
large task content through a file or stdin rather than interpolate issue text
into a shell command. Agent output should include a versioned `RunResult` with
outcome, branch, PR URL/head SHA, validation evidence, artifacts, and any blocker.
Adapters also return execution metadata: exit status, timestamps, failure class,
retry hint, usage/cost when available, and whether usage is unknown.

Treat result files and agent statements as untrusted evidence: verify PR
repository, branch, head SHA, and actual existence before changing state. A
successful process exit alone never means an issue is complete. Preserve
reconciliation with external state because writing a result, opening a PR, and
updating a tracker cannot be one transaction.

Start with narrow Python interfaces and explicit built-in registrations. The
external executable adapter provides an escape hatch for another coding agent
without forcing users to fork core. Do not make an API call to an arbitrary
model count as a supported coding agent.

### Bedrock and other model providers

Amazon Bedrock supplies model access; a coding agent supplies file editing,
command execution, iteration, and repository tools. The fastest supported AWS
path is:

```text
Agent Loop -> Claude Code -> Amazon Bedrock
           -> worktree, tests, GitHub PR
```

Claude Code documents Bedrock support and use of the default AWS credential
chain, including EC2 instance profiles. This makes a small compatibility spike
reasonable before writing a new agent. It does not prove this checkout's
workflow or error handling already works on Bedrock.

The spike should use the existing Claude adapter with
`CLAUDE_CODE_USE_BEDROCK=1`, an explicit AWS region, and a model/inference profile
available in the operator's account. Pin the agent version and tested model
configuration in the compatibility report. Test both a real task and expired
credentials/throttling. Do not reuse the current Codex-specific fleet launcher.

Document a separate provider choice for classification and coding; deterministic
repo labels should avoid inference entirely when possible. Block unsupported
agent/provider combinations during configuration validation. Preserve personal
account usage as an option where the selected agent supports it, while making
company-funded credentials equally usable.

Bedrock support through Claude Code covers the Claude models that agent can use,
not every model in Bedrock. A different Bedrock model or a managed Bedrock agent
would need a compatible coding runtime implementing the same agent contract.
A native runtime built on Bedrock Converse is a later option, justified only if
teams need models the initial agents cannot use. Such a runtime would own the
tool loop, filesystem/shell tools, context management, cancellation, and recovery.

Provider capabilities must be explicit: tool support, structured output, usage
reporting, credential refresh, and retry behavior vary. For example, Claude
Code's Bedrock documentation says its WebSearch tool is unavailable there; generic
workflows must not require that tool.

### Workflows and team configuration

Ship generic workflows for implementing a bug or feature, repairing a PR, and
reporting a blocker. They must include complete instructions and work with no
globally installed slash skills or personal MCP connections. An adapter can
translate a workflow into agent-specific files, but core should not depend on a
slash-command convention.

### Tracker extraction and GitHub adoption

First extract the existing Linear behavior behind a tracker contract without
changing its external behavior. Keep provider-specific queries, state/label IDs,
authentication, and evidence markers inside the adapter. Core operates on logical
states and work-item references rather than provider-specific team IDs or project-key formats.

Then add GitHub Issues and Projects (Projects v2). Keep the GitHub tracker and
forge roles separate even if they share an API client or credential resolver.
Issues carry task descriptions, assignees, labels, comments, and PR links;
the selected project's configured fields supply workflow state. GitHub documents
project item/field access and updates through its GraphQL API. This adapter should
include:

- Explicit organization/user, project, repository, assignee, and opt-in filters;
  paginate all selected items and reject inaccessible or unknown repository routes.
- A mapping from logical loop states to the selected project's Status options,
  validated by `doctor`; keep issue open/closed state distinct from project status.
- Repository-qualified issue identity, such as `owner/repo#123`, plus immutable
  API IDs. The same issue in multiple projects is one task; choose one configured
  project as the authority for its workflow state and deduplicate admission.
- Issue comments for evidence and blockers, verifiable issue-to-PR links, and
  deterministic handling of closed/reopened issues and archived project items.
- Skip project draft items and PR-only items as development input initially;
  buildable work must resolve to a real issue. Preserve the leaf-work rule using
  issue/sub-issue relationships rather than assuming Linear's hierarchy.
- Validate issue and project permissions independently. GitHub-only profiles must
  initialize, preview, run, and reconcile with no `LINEAR_API_KEY` present.
- Inspect existing project automations with the operator during setup and define
  ownership of status changes and issue closure. Do not compete with automation
  that moves a linked issue when a PR opens or merges.

Prove the contract with the Linear adapter and the GitHub adapter using shared
conformance tests. The first GitHub milestone is one scoped issue producing a
non-draft PR and updating the intended project item, with evidence written back
to the issue and no mutations to unrelated projects.

Separate implementation from post-PR review repair as the current loops do.
Make merging an explicit orchestrator policy, disabled by default. Move the
design loop and organization-specific UI/infra rules into optional workflow hooks
or a team pack. Generic hooks may request deployment evidence when configured; a merged
code PR must not be represented as a completed production deployment.

Introduce a versioned, non-executable `agent-loop.toml`. A project/team file holds
shareable repository and workflow policy; a user profile holds operator identity,
local paths, provider choices, and secret references. Credentials stay outside
Git. Precedence should be CLI > environment > user profile > project config >
defaults, with an inspect command showing effective values and their origin,
redacted. Organization policy should constrain which overrides are permitted.
Loading project instructions and executable hooks requires an explicitly trusted
repository; issue text must not silently change credentials or execution policy.

Make `agentloop`, `agent-loop`, `AGENT_LOOP_*`, `~/.agent-loop`, and
`agent-loop` durable markers the only supported namespace. Search code, tests,
scripts, package metadata, and documentation for remaining old names before
releasing; name constants alone do not cover all hardcoding.

### Multiple developers and ownership

Every installation needs a stable ID and every run needs its own ID. Scope run
keys by profile, repository, issue, and operation; issue numbers alone can collide
across tracker workspaces and GitHub repositories. Stamp loop ownership explicitly and count concurrency
only within the selected ownership scope.

For v0.1, require disjoint queue partitions: explicitly assigned operator plus
team/project/repository allowlists, or an equivalent dedicated opt-in queue.
One installation is active per partition. Existing human assignments must be
respected; an instance must never drain every `loop` issue visible to its token.
The current `assignee_id` setting is a starting point to audit, not proof this
is enforced across every loop.

Use transactional local claims and a supervisor lock to prevent duplicate runs
within an installation. This does not coordinate two laptops. Document the
one-writer constraint and stop the laptop instance before activating the same
partition on EC2. Arbitrary overlapping consumers and active-active hosts are
unsupported initially. Before enabling those, implement an actual shared atomic
claim/lease with expiry, renewal, and fencing; Linear status changes and tmux
checks are not a compare-and-swap lease.

### Runtime, identity, and recovery

Target macOS and Linux first; offer Windows through WSL2 until native support is
tested. "Any machine" means any supported host with the agent, Git, required
repository toolchains, network access, and credentials. Python installation alone
cannot supply every project's build dependencies. A remote EC2 host is a normal
installation reached over SSH initially, not a remote-execution control plane.

Replace mandatory tmux lifecycle tracking with a supervised subprocess executor.
Persist exit status and logs from process start, enforce wall-clock limits,
terminate child process groups on cancellation, and recover after a supervisor
restart. Keep tmux as an optional observation backend while migrating. Use SQLite
for local execution facts; keep the selected tracker as the user-facing work queue and GitHub
as the authority for PR/check/review state. Do not put SQLite on a shared network
filesystem to simulate a distributed coordinator.

Normalize `completed`, `blocked`, `rate_limited`, `auth_required`, `transient_error`,
`failed`, `timed_out`, and `cancelled`. Backoff should be scoped to provider/account
and, where relevant, model/region, so one exhausted subscription does not pause
every agent. Apply concurrency and retry caps to review repair and merge work as
well as development. Enforce elapsed-time and attempt limits; record cost when
available and report unknown values honestly. A hard dollar limit requires an
enforceable metering/gateway path; do not promise it from delayed usage estimates.

Preserve unpublished commits and dirty worktrees before cleanup, timeout, or host
replacement. Record recovery instructions and retention policy; do not infer that
a missing process means its files can be deleted. Cleanup must use durable
ownership records, protect the primary checkout, and avoid deleting another
developer's branches merely because an issue key matches.

Default to human approval and no automatic merge or infrastructure deployment.
Evaluate required checks, unresolved review threads, branch protection, approved
reviewers, and current PR head in deterministic code. Revalidate at action time;
delegate final merge to GitHub's protected merge/queue mechanism where possible.
Bot approval signals remain explicit presets rather than a generic trust rule.

Personal operation may use the developer's existing `gh` credentials and, when
the Linear tracker is selected, Linear credentials.
That mode inherits their permissions; prompt instructions cannot turn a broad
GitHub token into an enforced prohibition on merge. Document the boundary. For
hosted automation, use a separate identity, repository-scoped GitHub App tokens,
and a credential boundary that withholds merge/admin credentials from coding
workers. Tracker writes should belong to the controller; worker tooling should
receive only the credentials it actually needs. Worktrees provide workspace
separation, not a security sandbox. Container execution and narrowly scoped
credentials are needed when stronger isolation is required; do not mount an
unrestricted Docker socket as an alleged sandbox.

## Developer experience to build

These commands illustrate the target experience. `init` now writes an editable,
credential-free local scaffold; the remaining commands are planned:

```bash
agent-loop init --repo app=~/src/app # write a local profile and repository map
agent-loop doctor                   # validate tools, identity, states, repos, capabilities
agent-loop plan --issue TEAM-123     # inspect routing, workflow, permissions, proposed actions
agent-loop run --issue TEAM-123      # one bounded development attempt
agent-loop status                   # show attempts, PRs, blockers, usage availability
agent-loop logs <run-id>
agent-loop stop <run-id>             # cancel and preserve recoverable work
agent-loop serve                    # continuously reconcile this operator's queue
```

`doctor` now catches missing local tools, repository checkouts, configured remotes,
and remote-tracking base branches before validating tracker scope. Extend it to
catch missing workflow state, expired login, unavailable model, and unsafe queue
scope before work starts. Optional network/inference probes must be
identified explicitly. `plan` should be read-only and require no coding-agent
execution; explain when live reads are required and redact secrets. Define a
strict dry-run contract, including whether local diagnostic files are permitted,
and test it rather than inherit the current broad README claim unexamined.

Shareable examples should use neutral repository names and fake issue data.
Provide separate local personal-account, local AWS-profile, and EC2 role-based
guides. Team packs can contain Travefy conventions once learned, without making
them new global defaults.

## Delivery sequence

Work packages are sized relatively, not calendar commitments. Assign effort and
owners after the first compatibility spike and Travefy workflow check.

| Phase | Concrete deliverables | Exit criteria | Size / dependency |
| --- | --- | --- | --- |
| Repository setup | Establish `jpshelley/agent-loop` as the personal home; preserve local work; configure personal `origin` and source `upstream`; verify branch/PR destinations | Personal development targets the user's repository and preserves the existing source and task branches | Small; before publishing implementation work |
| 0. Establish the external path | Verify existing decoupling commits; inventory organization-specific workflow dependencies; confirm Travefy's GitHub project fields/automations, OS, repo tooling and AI access; run a bounded Claude Code/Bedrock spike in a test repo; document results | A tested host/agent/provider combination and one real external workflow are chosen; unknowns have owners | Small; first |
| 1. Extract Linear and make a generic one-shot run work | Tracker contract with Linear as first adapter, versioned config, `init`/`doctor`, configurable remote/base and issue keys, generic workflow pack, explicit operator scope, and optional policy hooks | Linear behavior is preserved behind the contract; a clean installation opens a non-draft test PR without organization-specific skills/accounts; bad routing and credentials fail before work starts | Medium; phase 0 |
| 1b. Add GitHub Issues and Projects | Second tracker adapter, project field mapping and permissions checks, qualified issue identity, issue evidence/PR links, pagination, automation reconciliation, shared adapter conformance tests | A GitHub-only installation takes one scoped issue through PR handoff and project status update without Linear credentials or unrelated project changes | Medium; phase 1; next tracker and required for Travefy |
| 2. Make continuous personal operation dependable | `TaskSpec`/`RunResult`, agent lifecycle adapters, subprocess executor, local run store and claims, controller-owned tracker writes, status/logs/stop, bounded retries and preservation, deterministic review policy | Restart and failure exercises preserve work, avoid duplicate local runs, and never mark unverified success; all loops respect ownership with either tracker | Large; phase 1; verify both adapters after 1b |
| 3. Validate portability and AWS hosting | Bedrock provider profile and diagnostics, external-agent protocol conformance fixture, Linux systemd example, container recipe, persistent volumes, scoped identities, EC2 runbook and backup/restore | Same tool completes the test workflow on a clean Linux/EC2 host with role credentials and no copied personal AI login; recovery and renewal are exercised | Medium; phases 1–2; spike starts in 0 |
| 4. Prepare the public beta | License/provenance review, LICENSE and contributor/security docs, clean README/quickstart, architecture docs, package/release CI, secret/history scan, neutral fixtures | An outside developer completes the quickstart from a release artifact; rights and any history cleanup are resolved before publication | Medium; phases 1, 1b, 2–3; documentation work can start earlier |
| 5. Pilot and release | Travefy volunteer pilot using GitHub Issues and Projects on approved test work, macOS/Linux matrix, bounded observation window, usability fixes, versioned beta release and changelog | At least two independent operators complete the flow in disjoint scopes; GitHub tracker, local, and EC2/Bedrock evidence exists; no unexplained duplicate work or silent state failures | Medium; phase 4 |

Phase 1 should produce these reviewable changes in order: finish generic naming
and configuration; remove provider-specific identifiers and `origin/main` assumptions; extract organization policy;
bundle generic workflow instructions; add one-shot onboarding and routing checks.
Extract the Linear adapter before adding the GitHub Issues and Projects adapter;
use the second implementation to expose any Linear assumptions left in core.
Keep each change small and covered by the existing boundary tests where relevant.
Do not mix cosmetic package moves with run-store or state-machine changes.

Use a clean Agent Loop installation for the pilot. Canary one selected issue
before switching on continuous execution. Back up configuration and state before
schema migration. Rollback must restore the saved database or fail clearly on an
unsupported schema. Never discard in-flight runs during an upgrade.

### EC2 reference installation

Use one Linux instance for one operator/partition, a dedicated OS user, systemd
restart handling, and persistent encrypted storage for checkout caches, worktrees,
run metadata, and logs. Provide an optional Docker image for reproducible runtime
dependencies; repository-specific build tooling remains an explicit image/setup
choice. No public inbound application endpoint is required for polling operation.

Use the EC2 role for Bedrock access and narrowly scoped access to any configured
secret store. Keep model/inference profile and region explicit. Fetch GitHub and
selected-tracker credentials through the configured credential resolver; refresh short-lived
tokens without restarting the fleet. Model role access must not accidentally
grant workers infrastructure administration privileges. Containers on EC2 require
a tested credential delivery and metadata-access policy, not an assumption that
instance credentials are isolated from arbitrary child code.

Document outbound connectivity, disk/log limits, backups, restore onto a replacement
host, graceful shutdown, and how to suspend admission while active runs finish.
Application backup and host replacement must include unpublished work; GitHub
alone is not a complete backup. Start with one tested instance architecture and
size it from measured runs rather than promise a universal EC2 instance size.

GitHub Apps offer installation-scoped access tokens that expire after one hour;
the credential resolver must renew them. Linear supports personal keys, OAuth
user/app identities, and a client-credentials flow for automation. Keep personal
keys as the simplest initial local path and choose an app identity for persistent
automation where appropriate. App authorization/header handling differs from the
current raw-key client and needs dedicated tests. OAuth onboarding should not
become a prerequisite for a developer trying the CLI.

### Public release preparation

The repository now carries the MIT license text and public contribution,
security, and community guidance. Confirm publication rights for pre-open-source
code, prompts, and any workflow content being redistributed; do not copy private
skills into the public package by default. If ownership or policy is unresolved,
track that specific release blocker while implementation continues.

Inspect tracked content and Git history for credentials, customer/internal data,
and private references. Rotate any exposed secret; removing a file is insufficient.
Decide whether to publish the existing history after cleanup or a new public
repository with a clean history and proper attribution. Repository visibility and
history rewriting are explicit publication decisions, not changes this plan makes.

Add CONTRIBUTING, SECURITY, CODE_OF_CONDUCT, issue/PR templates, a maintainer and
release policy, supported-platform/provider matrix, and a quickstart verified from
an installed wheel in a clean environment. Check availability of the intended
package/CLI name before renaming distribution metadata. Add package build/install
checks, Python/macOS/Linux coverage, dependency/security checks, and a tagged
release process. The project is open source even when optional coding agents or
model endpoints require commercial accounts; state those dependencies clearly.

## Validation and pilot evidence

Retain existing decision and boundary tests. Add tests at the new seams, focused
on behavior that could lose work, act in the wrong repository, or bypass policy:

- Non-`HAR` identifiers, non-`main` branches, multiple workspace identities,
  paths containing spaces, and prompts containing quotes or shell metacharacters.
- Neutral fixture issue -> isolated worktree -> verified PR -> review handoff;
  invalid or forged result files cannot advance state.
- Two local supervisors racing for a run; two independent users with disjoint
  queues; no cross-owner cleanup or capacity accounting.
- Crash before/after PR creation and before/after tracker mutation; reconciliation
  finds durable external effects without blindly duplicating them.
- Expired/revoked credentials, rate limits, network timeouts, failed CI, missing
  states, exhausted repair attempts, host restart, and full disk during logging.
- Multi-page tracker/forge responses do not silently omit eligible issues, review
  evidence, or repositories; incomplete evidence cannot satisfy a policy gate.
- Both tracker adapters pass the same logical lifecycle tests. GitHub tests cover
  duplicate issue numbers across repositories, an issue in multiple projects,
  project status versus issue closure, draft/PR items, missing project permissions,
  and external automation changing fields. GitHub-only runs require no Linear key.
- Dirty worktree recovery and cleanup ownership checks; timeout/cancel preserves
  unpublished work and stops descendants.
- Approval for an older head cannot authorize a newer head; no automatic merge
  in the default profile; optional merge honors current repository policy.
- Dry-run actions cause no external mutations, no agent launches, and no unintended
  repository writes. Compatibility profiles preserve old configuration deliberately.

Use fake tracker/forge/agent fixtures for deterministic CI. Run live credentials
and provider probes only in explicit integration jobs with scoped test resources.
Capture the supported versions and results; the executable-protocol fixture tests
the extension contract, while a real alternate agent is required before claiming
that agent is supported.

For the Travefy pilot, use GitHub Issues and Projects with no Linear dependency.
Start with one developer and one low-risk repository, admission
concurrency one, non-draft PRs, and manual merging. Expand to a second developer
with a disjoint queue, then move one operator's instance to EC2. Record onboarding
time, successful verified PR handoffs, manual interventions and reasons, retry
counts, usage/cost availability, and recovery results. Suggested beta usability
target: first preview in 15 minutes and first small PR in one working session,
excluding account provisioning and repository-specific setup. These are targets
to measure, not current capabilities.

## Decisions and remaining questions

Decided for this plan: developer-first installation; optional EC2 hosting; Python
core retained; personal repository home under `jpshelley`; Linear extracted as the first tracker, GitHub Issues and Projects
next and required for Travefy; GitHub as the initial forge; bundled generic workflows; agent/provider
separation; Claude Code on Bedrock as the first AWS path; human review and manual
merge by default; explicit scope; no active-active hosts in v0.1.

Before committing the implementation backlog, establish:

1. Which GitHub organization/project, Status options, opt-in rules, existing
   automations, operating systems, build tools, and approved model providers should
   the first Travefy profile use? The tracker choice is GitHub Issues and Projects.
2. Is removing Claude Code as a dependency itself a requirement, or is company
   AWS billing/identity sufficient? The former changes the Bedrock work materially.
3. Confirm publication rights, visibility/history strategy for the personal
   repository, package namespace, and ongoing maintenance. The intended personal
   owner is `jpshelley`. Is MIT the intended license given the existing metadata?
4. Which team rules belong in shareable policy, and what is the exact criterion
   for an issue being ready for an unattended attempt?

## External references

Provider and authentication details were checked against official documentation
on October 16, 2026. Revalidate versions and account availability during the spike.

- [Claude Code on Amazon Bedrock](https://code.claude.com/docs/en/amazon-bedrock):
  AWS credential chain, EC2 profile support, provider setup, model pins, and tool differences.
- [IAM roles for EC2](https://docs.aws.amazon.com/AWSEC2/latest/UserGuide/iam-roles-for-amazon-ec2.html):
  role-based credentials for applications running on an instance.
- [Amazon Bedrock Converse](https://docs.aws.amazon.com/bedrock/latest/userguide/conversation-inference.html):
  model inference interface; a possible building block for a later coding runtime.
- [GitHub App installation tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app):
  repository/permission scoping and token expiry.
- [Linear OAuth authentication](https://linear.app/developers/oauth-2-0-authentication):
  user/app authorization, refresh, and client-credentials automation flow.
- [Using the API to manage GitHub Projects](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-api-to-manage-projects):
  project items, fields, GraphQL updates, and authentication.
- [GitHub Projects built-in automations](https://docs.github.com/en/issues/planning-and-tracking-with-projects/automating-your-project/using-the-built-in-automations):
  existing workflow behavior to account for during setup.
