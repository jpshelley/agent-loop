"""Safe local execution names, independent of tracker display identifiers."""

from __future__ import annotations

import hashlib
import json
import re

_PROVIDER_MAX_LENGTH = 36
_GENERATED_KEY = re.compile(r"al-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{24}", re.ASCII)
_GENERATED_BRANCH = re.compile(
    r"(?:^|/)(al-[a-z0-9]+(?:-[a-z0-9]+)*-[0-9a-f]{24})(?=-|$)", re.ASCII
)


def execution_key(provider: str, identifier: str, scope: str = "", issue_id: str = "") -> str:
    """Return a stable name for local sessions, worktrees, logs and branches.

    ``scope`` distinguishes namespaces (for example ``owner/repository``), while
    ``issue_id`` should be the immutable tracker ID when available. Generated
    names using a native ID survive changes to an item's display identifier.
    Provider spelling and scope are identity inputs and must be supplied consistently.
    """
    if not isinstance(provider, str) or not provider.strip():
        raise ValueError("provider must be a nonempty string")
    if not isinstance(identifier, str) or not identifier.strip():
        raise ValueError("identifier must be a nonempty string")
    if not isinstance(scope, str) or not isinstance(issue_id, str):
        raise ValueError("scope and issue_id must be strings")
    name = re.sub(r"[^a-z0-9]+", "-", provider.lower(), flags=re.ASCII).strip("-")
    name = name[:_PROVIDER_MAX_LENGTH].rstrip("-") or "provider"
    identity = json.dumps([provider, scope, issue_id or identifier], ensure_ascii=True, separators=(",", ":"))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    return f"al-{name}-{digest}"


def is_execution_key(value: str) -> bool:
    """Recognize the exact generated safe-name format."""
    if not isinstance(value, str):
        return False
    return len(value) <= 64 and bool(_GENERATED_KEY.fullmatch(value))


def validate_execution_key(value: str) -> str:
    """Reject unsupported local names before filesystem or process operations."""
    if not is_execution_key(value):
        raise ValueError(f"unsupported execution key: {value!r}")
    return value


def validate_session_key(value: str) -> str:
    """Issue execution keys plus the optional design loop's existing session names."""
    if isinstance(value, str) and len(value) <= 64 and re.fullmatch(r"design-[0-9]+", value, re.ASCII):
        return value
    return validate_execution_key(value)


def branch_execution_key(branch: str) -> str:
    """Find a known execution token at the start of a branch path segment.

    A token must end the branch or be followed by a hyphen and a human suffix.
    Only generated tokens are recognized so cleanup never adopts an unrelated
    branch based on a human-facing issue identifier.
    """
    if not isinstance(branch, str):
        return ""
    match = _GENERATED_BRANCH.search(branch)
    if match and is_execution_key(match.group(1)):
        return match.group(1)
    return ""
