"""dispatch — one reconcile tick of the Linear-driven queue preparer.

Ports ``dispatch.sh`` queue selection: pick the highest-priority leaf issues from the queue,
triage them to a repo+type, and admit them to the Todo state. The ``develop`` loop owns the
active claim, worktree creation, and detached coding harness session spawning. Concurrency is
bounded by counting loop-wip issues in the todo + active + review states.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass

from agentloop import harness, journal, proc, worktree
from agentloop.config import TYPE_BUG_LABEL, TYPE_FEATURE_LABELS
from agentloop import tracker
from agentloop.tracker import TrackerError
from agentloop.outage import rate_limited_now


@dataclass
class ReadyRow:
    id: str
    identifier: str
    scope: str
    repo: str
    type: str
    title: str


@dataclass
class QueuedPrRow:
    id: str
    identifier: str
    scope: str


_JSON_OBJ = re.compile(r"\{.*\}")


def branch_name(cfg, key: str, title: str) -> str:
    """Use a validated local execution token in the operator branch namespace."""
    return f"{cfg.branch_prefix}{worktree.branch_key(key)}-{slugify(title)}"


def count_active(client, cfg) -> int:
    """Count admitted, unparked loop work without counting human-only issues."""
    return sum(
        1 for issue in client.issues_in_states([cfg.state_todo, cfg.state_active, cfg.state_review])
        if cfg.wip_label in issue.labels and cfg.blocked_label not in issue.labels
    )


def _first_repo_label(label_names: list[str]) -> str:
    for name in label_names:
        if name.startswith("repo:"):
            return name[len("repo:"):]
    return ""


def _type_from_labels(label_names: list[str]) -> str:
    if "Bug" in label_names:
        return "bug"
    if any(n in ("Feature", "Improvement") for n in label_names):
        return "feature"
    return ""


def _is_unblocked(issue: tracker.Issue) -> bool:
    return not issue.blocked_by_open_issue


def list_queued_prs(client, cfg) -> list[QueuedPrRow]:
    """Loop-owned queued issues with an existing PR need review handoff repair."""
    return [
        QueuedPrRow(issue.id, issue.identifier, issue.scope)
        for issue in client.issues_in_states([cfg.state_queue])
        if cfg.wip_label in issue.labels and cfg.blocked_label not in issue.labels
        and issue.linked_pr_urls
    ]


def advance_queued_prs(client, cfg) -> None:
    """Repair queued loop-owned issues that already have PRs by moving them directly to the
    review state, preserving the invariant that queued work has no PR yet."""
    if cfg.state_queue == cfg.state_review:
        return

    advanced = 0
    for row in list_queued_prs(client, cfg):
        try:
            client.ensure_scope(row.scope)
        except TrackerError:
            journal.log(cfg, f"{row.identifier}: could not load tracker scope for queued-PR repair — leaving queued")
            continue
        journal.log(cfg, f"{row.identifier}: {cfg.state_queue} but already has a GitHub PR -> moving to {cfg.state_review}")
        if not client.move_state(row.id, row.scope, cfg.state_review):
            journal.log(cfg, f"{row.identifier}: move to {cfg.state_review} failed — leaving queued")
            continue
        client.add_comment(
            row.id,
            f"Advanced by the dispatcher: `{row.identifier}` was in **{cfg.state_queue}** but already has a linked "
            f"GitHub PR, so it was moved to **{cfg.state_review}** instead of starting another build session.",
        )
        advanced += 1
    if advanced:
        journal.log(cfg, f"advanced {advanced} queued PR issue(s) to {cfg.state_review}")


def list_ready(client, cfg) -> list[ReadyRow]:
    """Select buildable leaves, preserving the tracker's normalized priority order."""
    issues = [
        issue for issue in client.issues_in_states([cfg.state_queue])
        if not issue.has_children and _is_unblocked(issue)
        and cfg.blocked_label not in issue.labels and not issue.linked_pr_urls
    ]
    issues.sort(key=lambda issue: issue.priority_rank)
    return [
        ReadyRow(issue.id, issue.identifier, issue.scope, _first_repo_label(issue.labels),
                 _type_from_labels(issue.labels), issue.title)
        for issue in issues
    ]


def slugify(title: str) -> str:
    """Lowercase, collapse non-alphanumerics to '-', trim, cap at 40 chars (dispatch.sh:121)."""
    s = re.sub(r"[^a-z0-9]+", "-", title.lower())
    s = s.strip("-")
    return s[:40]


def _classify_prompt(cfg, ident: str, title: str, *, tracker_adapter=None) -> str:
    """Build the repo/type classifier prompt from the operator's own repo map.

    The catalog is generated, not hardcoded: the loop has no idea what anyone's repos
    are for, so [descriptions] in repos.toml is what makes the guess any good. Without
    them the classifier still runs, but only sees bare labels.
    """
    labels = sorted(cfg.repos)
    catalog = "; ".join(
        f"{label} = {cfg.repo_descriptions[label]}" if label in cfg.repo_descriptions else label
        for label in labels
    )
    fallback = f"Use {cfg.default_repo} if unsure. " if cfg.default_repo else ""
    issue_tracker = tracker_adapter if tracker_adapter is not None else tracker.resolve(cfg)
    return (
        f'Classify {issue_tracker.classifier_context()} {ident} ("{title}"). '
        "Read it using the tracker state contract below if needed. "
        f"Route by repo: {catalog}. {fallback}"
        "Reply with ONE line of JSON only: "
        f'{{"repo":"{"|".join(labels)}","type":"bug|feature"}}.'
        f"\n\n{issue_tracker.state_contract()}"
    )


def dispatch_prompt(
    skill: str,
    key: str,
    review_state: str = "In Review",
    *,
    blocked_label: str = "loop-blocked",
    needs_input_state: str = "Needs Input",
    tracker_adapter=None,
    base_ref: str = "origin/main",
) -> str:
    """Prompt contract for build sessions spawned for loop-claimed work.

    The slash skill still owns the coding workflow, but dispatch owns only the work until a PR
    exists. Post-PR CI and review follow-up belongs to the debug loop.
    """
    if tracker_adapter is None:
        # Preserve direct callers while runtime paths pass the selected adapter.
        from agentloop.config import Config

        tracker_adapter = tracker.resolve(Config())
    return (
        f"{skill}\n\n"
        f"{tracker_adapter.state_contract()}\n\n"
        f"{tracker_adapter.blocked_contract(blocked_label, needs_input_state).replace('origin/main', base_ref)}\n\n"
        f"Repository base: {base_ref}. Open the pull request against {base_ref.partition('/')[2]}.\n\n"
        "Dispatch handoff contract: once you open a non-draft PR, do only bounded closeout — add "
        f"concise evidence and move {key} to {review_state} (via the tracker state contract above), "
        "then exit. Do not inspect CI, run triangulation, "
        "address review comments, merge, or continue post-PR follow-up. The debug loop owns "
        f"{review_state} follow-up.\n\n"
        "Infra-coupled work contract: if this touches infra, secrets, IAM, task definitions, "
        "or deploy machinery, Done requires deployed evidence on the issue: the relevant "
        "apply/deploy run link plus one observed post-deploy behavior. Record it exactly as:\n\n"
        "## Deployed Evidence\n"
        "- Apply/deploy run: <url>\n"
        "- Observed post-deploy behavior: <task exit 0 | health endpoint | rendered page>\n\n"
        "If the work spans repos (for example app contract plus infra binding), verify both halves "
        "exist on their configured base branches before building either half."
    )


def _repo_root_or_log(cfg, identifier: str, repo: str):
    """Resolve one issue's repo without allowing a bad label to abort the tick."""
    try:
        return cfg.repo_root(repo)
    except ValueError as error:
        journal.log(cfg, f"{identifier}: {error} — leaving queued/skipping")
        return None


def _apply_required_label(client, cfg, issue_id: str, scope: str, label_name: str, identifier: str) -> bool:
    """Required-label persistence belongs to the tracker, including creation failures."""
    if not client.ensure_required_label(issue_id, scope, label_name):
        journal.log(cfg, f"{identifier}: required label '{label_name}' unavailable — leaving queued")
        return False
    return True


def _triage(client, cfg, issue_id, identifier, scope, repo, type_, title) -> tuple[str, str, bool]:
    """Classify repo/type and persist any missing handoff labels."""
    labels_ok = True
    repo = cfg.collapse_repo(repo)
    if not repo or not type_:
        guess = {}
        try:
            adapter = harness.resolve(cfg)
            r = proc.capture(
                adapter.build_capture_argv(_classify_prompt(cfg, identifier, title, tracker_adapter=client)),
                stderr=subprocess.DEVNULL,
            )
            m = _JSON_OBJ.search(r.stdout or "")
            if m:
                guess = json.loads(m.group(0))
        except (OSError, json.JSONDecodeError):
            guess = {}
        if not repo:
            # No configured fallback => leave repo empty; repo_root() then refuses the
            # issue and it stays queued, rather than landing in an arbitrary checkout.
            repo = guess.get("repo") or cfg.default_repo or ""
        if not type_:
            type_ = guess.get("type") or "feature"
        if type_ not in ("bug", "feature"):
            type_ = "feature"
        repo = cfg.collapse_repo(repo)
        if repo:
            labels_ok = (
                _apply_required_label(client, cfg, issue_id, scope, f"repo:{repo}", identifier)
                and labels_ok
            )
        if type_ == "bug":
            labels_ok = (
                _apply_required_label(client, cfg, issue_id, scope, TYPE_BUG_LABEL, identifier)
                and labels_ok
            )
        elif type_ == "feature":
            labels_ok = (
                _apply_required_label(client, cfg, issue_id, scope, TYPE_FEATURE_LABELS[0], identifier)
                and labels_ok
            )  # first -> "Feature"
    return (repo, type_, labels_ok)


def triage(client, cfg, issue_id, identifier, scope, repo, type_, title) -> tuple[str, str]:
    """Ensure repo/type labels exist; classify with a cheap headless harness call if missing,
    persisting the guess as the workspace's labels (dispatch.sh:95-119). Returns ``(repo, type)``."""
    repo, type_, _ = _triage(client, cfg, issue_id, identifier, scope, repo, type_, title)
    return (repo, type_)


def dispatch_one(client, cfg, row: ReadyRow) -> bool:
    """Admit one issue for later development. Always returns True (each processed issue consumes
    a slot, matching the bash ``&& taken=$((taken+1))`` semantics where every early return is
    ``return 0``)."""
    key = row.identifier
    local_key = tracker.execution_key(cfg, row)
    if proc.has_session(local_key):
        journal.log(cfg, f"{key} already has a live session — skipping")
        return True
    try:
        client.ensure_scope(row.scope)
    except TrackerError:
        journal.log(cfg, f"{key}: could not load tracker scope — skipping")
        return True

    if (not row.repo or not row.type) and (hint := rate_limited_now(cfg)) is not None:
        journal.log(
            cfg,
            f"{key}: missing repo/type labels and classifier may be rate-limited — leaving queued ({hint})",
        )
        return True

    repo, type_, labels_ok = _triage(client, cfg, row.id, key, row.scope, row.repo, row.type, row.title)
    if not labels_ok:
        return True
    root = _repo_root_or_log(cfg, key, repo)
    if root is None:
        return True
    if not root.is_dir():
        journal.log(cfg, f"{key}: repo '{repo}' path missing ({root}) — skipping")
        return True

    journal.log(cfg, f"{key} -> repo={repo} type={type_} (ready for develop)")

    # Tag loop-wip before leaving the queue so develop can pick up the Todo ticket and
    # count_active counts THIS work but not humans' work. Auto-create once.
    if not _apply_required_label(client, cfg, row.id, row.scope, cfg.wip_label, key):
        return True

    # Dispatch admits ready work; develop performs the active claim when it starts a session.
    if not client.move_state(row.id, row.scope, cfg.state_todo):
        journal.log(cfg, f"{key}: move to {cfg.state_todo} failed — leaving queued")
        return True

    return True


def run_tick(cfg) -> None:
    client = tracker.resolve(cfg)

    if not client.resolve_scope():
        journal.log(cfg, f"no projects carry the '{cfg.project_label}' label")
        return

    advance_queued_prs(client, cfg)

    active = count_active(client, cfg)
    slots = cfg.concurrency - active
    journal.log(cfg, f"active={active} cap={cfg.concurrency} slots={slots}")
    if slots <= 0:
        journal.log(cfg, "no free slots")
        return

    rows = list_ready(client, cfg)
    if cfg.debug:
        journal.log(cfg, "── raw list_ready ──")
        for r in rows:
            journal.log(cfg, repr(r))
        journal.log(cfg, "── end raw ──")

    claimed = 0
    for row in rows:
        if claimed >= slots:
            break
        if not row.id:
            continue
        dispatch_one(client, cfg, row)
        claimed += 1

    journal.log(cfg, f"claimed {claimed} this tick")
