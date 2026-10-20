"""Provider-neutral issue data and the tracker boundary used by the loops.

Tracker adapters own API payloads, credential requirements, and instructions for
headless agents. Loops own selection and workflow policy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Sequence

from agentloop import identity


class TrackerError(Exception):
    """A tracker read or operation could not be completed reliably."""


@dataclass(frozen=True)
class Comment:
    body: str
    created_at: str = ""


@dataclass(frozen=True)
class Issue:
    id: str
    identifier: str
    scope: str
    title: str = ""
    labels: tuple[str, ...] = ()
    linked_pr_urls: tuple[str, ...] = ()
    comments: tuple[Comment, ...] = ()
    priority_rank: int = 99
    has_children: bool = False
    blocked_by_open_issue: bool = False
    started_at: str = ""


class Tracker(Protocol):
    def resolve_scope(self) -> bool: ...

    def issues_in_states(
        self, states: Sequence[str], *, include_comments: bool = False,
        newest_comments: bool = False,
    ) -> list[Issue]: ...

    def ensure_scope(self, scope: str) -> None: ...
    def ensure_required_label(self, issue_id: str, scope: str, label: str) -> bool: ...
    def move_state(self, issue_id: str, scope: str, state_name: str) -> bool: ...
    def add_label(self, issue_id: str, scope: str, label_name: str) -> bool: ...
    def add_comment(self, issue_id: str, body: str) -> bool: ...
    def assign_issue(self, issue_id: str) -> bool: ...
    def ci_fix_markers(self, issue_id: str) -> list[str]: ...
    def add_ci_fix_marker(self, issue_id: str, sha: str, pr_num: int) -> bool: ...
    def approval_wait_markers(self, issue_id: str) -> tuple[dict[str, str], set[str]]: ...
    def add_approval_wait_marker(
        self, issue_id: str, sha: str, pr_num: int, missing: list[str],
    ) -> bool: ...
    def add_approval_wait_escalation(
        self, issue_id: str, sha: str, pr_num: int, minutes: int,
        missing: list[str], review_state: str, needs_input_state: str,
    ) -> bool: ...
    def state_contract(self) -> str: ...
    def blocked_contract(self, blocked_label: str, needs_input_state: str) -> str: ...
    def classifier_context(self) -> str: ...


def validate_tracker(name: str) -> None:
    if name not in {"github", "linear"}:
        raise ValueError(f"unsupported tracker '{name}'; supported trackers: github, linear")


def resolve(cfg) -> Tracker:
    name = getattr(cfg, "tracker", "linear")
    validate_tracker(name)
    # Import at resolution time so adapters remain independent of this boundary,
    # and existing integrations that replace LinearClient continue to work.
    if name == "linear":
        from agentloop.linear import LinearClient

        return LinearClient(cfg)
    from agentloop.github_tracker import GitHubClient

    return GitHubClient(cfg)


def execution_key(cfg, issue) -> str:
    """Local execution identity; never use it as a tracker lookup identifier."""
    return identity.execution_key(cfg.tracker, issue.identifier, issue.scope, issue.id)
