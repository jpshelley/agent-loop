"""Bundled, harness-neutral instructions for the core loop operations."""

from __future__ import annotations


def implement(kind: str, identifier: str) -> str:
    work = "fix the reported defect" if kind == "bug" else "implement the requested change"
    return (
        f"Implement {identifier}: {work}. Work only in the current worktree. Inspect the issue and "
        "existing code, make the smallest complete change, run relevant tests, commit and push it, "
        "then open a non-draft pull request against the configured base."
    )


def review(identifier: str, remote: str) -> str:
    return (
        f"Review and repair the existing pull request for {identifier}. Work only on its current branch from {remote}. "
        "Read live CI and review feedback, make only necessary fixes, test, commit, and push. Do not merge the PR "
        "or change tracker workflow state."
    )


def ship(identifier: str) -> str:
    return (
        f"Ship the approved pull request for {identifier}. Recheck mergeability, required checks, and unresolved "
        "review feedback immediately before merging. Merge only if all gates remain satisfied, then record concise "
        "tracker evidence and perform the configured terminal transition."
    )


def design_review(pr_number: int, url: str) -> str:
    """Return the optional post-merge review prompt without organization policy."""
    return (
        f"Review the merged pull request #{pr_number} ({url}) for user-facing design and maintainability concerns. "
        "Inspect the diff and surrounding implementation. Record concise, actionable findings in the repository "
        "normal review channel when needed. Do not modify code, merge another pull request, or use proprietary skills."
    )
