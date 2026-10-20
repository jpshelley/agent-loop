"""deploy — the loop's deployer: merge + close out human-approved PRs.

Ports ``deploy.sh``: for each loop-owned issue in the Approved state, spawn a ``/ship-it <KEY>``
session (in the repo root, not a worktree) that re-verifies the merge gate, merges, and moves the
issue to Done. Infra-coupled tickets first need recorded deployed evidence, because merge + CI is
not enough proof that AWS/Terraform/task-definition changes converged. Unbounded — approved PRs
should land promptly. ``/ship-it`` owns merge safety.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from agentloop import github, journal, proc, spawn, tracker, workflows
from agentloop.harness import CLAUDE_DEPLOY_FLAGS_DEFAULT as _CLAUDE_DEPLOY_FLAGS_DEFAULT
from agentloop.tracker import TrackerError
from agentloop.loops.dispatch import _repo_root_or_log
from agentloop.outage import rate_limited_now

_PR_URL = re.compile(r"github\.com/([^/]+)/([^/]+)/pull/([0-9]+)")

# Deploy runs ONLY /ship-it (which re-verifies its own gate). The loop's human-approval gate is
# the Linear "Approved" state, not a GitHub review, so auto-mode's "Merge Without Review" guard
# would block every loop-shipped merge as a false positive. Default deploy to dontAsk paired with
# an explicit --allowedTools allowlist of exactly what ship-it uses. See deploy.sh:51-74 for the
# full rationale. ORDERING MATTERS: --allowedTools is variadic, so --permission-mode must follow
# it (else it swallows the trailing '/ship-it <KEY>' positional). The tool list is one
# comma-separated token so the shell/tmux re-exec passes it through as a single value.
DEPLOY_CLAUDE_FLAGS_DEFAULT = (
    _CLAUDE_DEPLOY_FLAGS_DEFAULT
)


@dataclass
class ApprovedRow:
    id: str
    identifier: str
    scope: str
    repo: str
    title: str = ""
    labels: tuple[str, ...] = ()
    comments: tuple[str, ...] = ()
    pr_url: str = ""


def list_approved(client, cfg) -> list[ApprovedRow]:
    """Approved loop work with normalized evidence for the merge preflight."""
    return [
        ApprovedRow(
            id=issue.id, identifier=issue.identifier, scope=issue.scope,
            repo=next((name[5:] for name in issue.labels if name.startswith("repo:")), ""),
            title=issue.title, labels=issue.labels,
            comments=tuple(comment.body for comment in issue.comments),
            pr_url=next((url for url in issue.linked_pr_urls if _PR_URL.search(url)), ""),
        )
        for issue in client.issues_in_states([cfg.state_approved], include_comments=True, newest_comments=True)
        if cfg.wip_label in issue.labels
    ]


def _pr(st: dict | None) -> dict | None:
    return (((st or {}).get("data") or {}).get("repository") or {}).get("pullRequest")


def deploy_blockers(pr_json: dict | None) -> list[str]:
    """Return reasons an Approved PR should go back to In Review instead of spawning ship-it."""
    pr = _pr(pr_json)
    if pr is None:
        return ["GitHub did not return PR state"]
    if pr.get("state") == "MERGED":
        return []
    if pr.get("state") != "OPEN":
        return [f"PR state is {pr.get('state') or 'unknown'}"]

    blockers: list[str] = []
    if pr.get("isDraft"):
        blockers.append("PR is still a draft")
    mergeable = pr.get("mergeable")
    if mergeable == "CONFLICTING":
        blockers.append("GitHub reports merge conflicts")
    merge_state = pr.get("mergeStateStatus")
    if merge_state in {"BEHIND", "BLOCKED", "DIRTY", "DRAFT", "UNSTABLE"}:
        blockers.append(f"GitHub merge state is {merge_state}")

    commit_nodes = (pr.get("commits") or {}).get("nodes") or []
    commit = (commit_nodes[0].get("commit") if commit_nodes else None) or {}
    checks = (commit.get("statusCheckRollup") or {}).get("state")
    if checks and checks != "SUCCESS":
        blockers.append(f"check rollup is {checks}")
    if pr.get("reviewDecision") == "CHANGES_REQUESTED":
        blockers.append("GitHub review decision is CHANGES_REQUESTED")

    current_open_threads = [
        t for t in (pr.get("reviewThreads") or {}).get("nodes", [])
        if t.get("isResolved") is False and t.get("isOutdated") is not True
    ]
    if current_open_threads:
        blockers.append(f"{len(current_open_threads)} unresolved current review thread(s)")
    return blockers


def _safe_scope(client, cfg, scope) -> None:
    try:
        client.ensure_scope(scope)
    except TrackerError:
        pass


_INFRA_COUPLED_LABELS = {
    "infra",
    "infrastructure",
    "terraform",
    "deployment",
    "secret",
    "secrets",
    "iam",
    "task definition",
    "task-definition",
    "ecs",
    "aws",
    "dns",
    "rds",
}

_INFRA_COUPLED_TITLE_PATTERNS = (
    re.compile(r"\binfra(?:structure)?\b"),
    re.compile(r"\bterraform\b"),
    re.compile(r"\bsecret(?:s)?\b"),
    re.compile(r"\biam\b"),
    re.compile(r"\btask[- ]definition\b"),
    re.compile(r"\becs\b"),
    re.compile(r"\baws\b"),
    re.compile(r"\bdns\b"),
    re.compile(r"\brds\b"),
    re.compile(r"\bdeploy(?:ment)?\s+(?:lane|machinery|pipeline|infra|terraform|task)\b"),
    re.compile(r"\b(?:stage|staging)\s+(?:deploy|deployment|lane|environment|env|terraform|task|ecs|rds|secret)\b"),
)


def is_infra_coupled(cfg, row: ApprovedRow) -> bool:
    """Best-effort metadata gate for tickets whose Done proof depends on deployed systems."""
    if cfg.collapse_repo(row.repo) in cfg.infra_repos:
        return True
    labels = {label.lower().removeprefix("type:") for label in row.labels}
    if labels & _INFRA_COUPLED_LABELS:
        return True
    title = row.title.lower()
    return any(pattern.search(title) for pattern in _INFRA_COUPLED_TITLE_PATTERNS)


def has_deployed_evidence(row: ApprovedRow) -> bool:
    """Recognize the explicit evidence block humans/ship-it should record before Done."""
    for body in row.comments:
        text = body.lower()
        if (
            "## deployed evidence" in text
            and "apply/deploy run:" in text
            and "observed post-deploy behavior:" in text
            and "http" in text
        ):
            return True
    return False


def hold_for_deployed_evidence(client, cfg, row: ApprovedRow, pr_num: int | None) -> bool:
    key = row.identifier
    _safe_scope(client, cfg, row.scope)
    pr_ref = f"PR #{pr_num}" if pr_num is not None else "the linked PR"
    journal.log(
        cfg,
        f"{key}: infra-coupled {pr_ref} lacks deployed evidence -> {cfg.state_needs_input}",
    )
    body = (
        f"Holding this ticket before spawning `/ship-it`: {pr_ref} is infra-coupled, so "
        "merge + green CI is not enough to close Done.\n\n"
        "Evidence owed before this ticket can move to Done:\n"
        "- Apply/deploy run link for the relevant lane/environment.\n"
        "- One observed post-deploy behavior, such as task exit 0, a healthy endpoint, or a rendered page.\n\n"
        "When that is available, record it exactly as:\n\n"
        "## Deployed Evidence\n"
        "- Apply/deploy run: <url>\n"
        "- Observed post-deploy behavior: <task exit 0 | health endpoint | rendered page>"
    )
    if not client.add_comment(row.id, body):
        journal.log(
            cfg,
            f"{key}: could not record deployed-evidence instructions — leaving {cfg.state_approved}",
        )
        return False
    if not client.move_state(row.id, row.scope, cfg.state_needs_input):
        journal.log(cfg, f"{key}: move to {cfg.state_needs_input} failed — retry next tick")
        return False
    client.add_comment(
        row.id,
        f"Moved **{cfg.state_approved} → {cfg.state_needs_input}** instead of spawning `/ship-it`: "
        f"deployed evidence is required for {pr_ref}.",
    )
    return True


def move_back_to_review(client, cfg, row: ApprovedRow, pr_num: int, blockers: list[str]) -> bool:
    key = row.identifier
    _safe_scope(client, cfg, row.scope)
    reason_text = "; ".join(blockers)
    journal.log(
        cfg,
        f"{key}: Approved PR #{pr_num} is not deployable ({reason_text}) -> {cfg.state_review}",
    )
    if not client.move_state(row.id, row.scope, cfg.state_review):
        journal.log(cfg, f"{key}: move back to {cfg.state_review} failed — retry next tick")
        return False
    client.add_comment(
        row.id,
        f"Moved back to **{cfg.state_review}** from **{cfg.state_approved}**: PR #{pr_num} "
        f"is not currently deployable.\n\nBlocking gate(s): {reason_text}.\n\n"
        "The debug loop owns In Review follow-up and will retry or escalate from there.",
    )
    return True


def deploy_one(client, cfg, row: ApprovedRow) -> bool:
    """Spawn a detached ship-it session for one approved issue (deploy.sh:101-126). cwd = repo
    root (the merge is a remote op). Returns True if a session was spawned, False if skipped."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if proc.has_session(local_key):
        journal.log(cfg, f"{key} already has a live session — skipping (still building or already deploying)")
        return False
    repo = cfg.collapse_repo(row.repo)
    root = _repo_root_or_log(cfg, key, repo)
    if root is None:
        return False
    if not root.is_dir():
        journal.log(cfg, f"{key}: repo '{repo}' path missing ({root}) — skipping")
        return False

    if row.pr_url:
        m = _PR_URL.search(row.pr_url)
        if m:
            owner, pr_repo, num = m.group(1), m.group(2), int(m.group(3))
            st = github.pr_deploy_gate_state(owner, pr_repo, num)
            if st is None:
                journal.log(
                    cfg,
                    f"{key}: deploy preflight unavailable for {owner}/{pr_repo}#{num} — "
                    "falling back to evidence gate/ship-it",
                )
            else:
                blockers = deploy_blockers(st)
                if blockers:
                    move_back_to_review(client, cfg, row, num, blockers)
                    return False
            if is_infra_coupled(cfg, row) and not has_deployed_evidence(row):
                hold_for_deployed_evidence(client, cfg, row, num)
                return False
        else:
            journal.log(cfg, f"{key}: can't parse PR from '{row.pr_url}' — falling back to ship-it")
    elif is_infra_coupled(cfg, row) and not has_deployed_evidence(row):
        hold_for_deployed_evidence(client, cfg, row, None)
        return False

    journal.log(cfg, f"{key}: approved -> deploying (repo={repo})")
    spawn.tmux_spawn(cfg, local_key, root, f"{workflows.ship(key)}\n\n{client.state_contract()}")
    if not cfg.dry_run:
        journal.log(cfg, f"{key}: ship-it session spawned")
    return True


def run_tick(cfg) -> None:
    client = tracker.resolve(cfg)

    # Outage gate: a spawned ship-it would exit instantly on the cap and stay Approved — the
    # doomed every-tick retry (deploy.sh:129-135).
    hint = rate_limited_now(cfg)
    if hint is not None:
        journal.log(cfg, f"Claude usage outage — holding this tick, no ship-it spawns ({hint})")
        return

    if not client.resolve_scope():
        journal.log(cfg, f"no projects carry the '{cfg.project_label}' label")
        return

    deployed = 0
    for row in list_approved(client, cfg):
        if not row.id:
            continue
        if deploy_one(client, cfg, row):
            deployed += 1

    journal.log(cfg, f"deployed {deployed} approved PR(s) this tick")
