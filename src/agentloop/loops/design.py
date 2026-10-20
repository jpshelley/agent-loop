"""design — optional post-merge design-review producer loop.

Scans a bounded recent window of merged PRs in the configured design repo, cheaply filters to UI-touching
changes, skips PRs already carrying a design-review dedup marker, claims each PR before spawning,
and starts a bundled, harness-neutral review prompt. This producer owns the spawn-time claim so
failed reviews do not respawn without bound.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import os
import shutil
import subprocess

from functools import lru_cache
import re

from agentloop import journal, proc, spawn, tracker, workflows
from agentloop.outage import rate_limited_now

MARKER_PREFIX = "agent-loop-design-reviewed:"
CLAIM_PREFIX = "agent-loop-design-claimed:"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_SCAN_LIMIT = 50

_UI_EXTENSIONS = (".tsx", ".jsx", ".css", ".scss", ".sass", ".less")
_UI_PATH_PARTS = ("styled", "styles", "theme", "themes", "tokens")
_UI_PATH_PREFIXES = ("packages/ui/", "apps/")


@dataclass
class MergedPR:
    number: int
    title: str
    url: str
    merge_sha: str
    merged_at: str
    files: list[str]
    comments: list[str]


@dataclass
class DesignClaim:
    marker: str
    comment_id: str | None


_REMOTE_SLUG = re.compile(r"[:/](?P<owner>[^/:]+)/(?P<repo>[^/]+?)(?:\.git)?/?$")


def _slug_for_checkout(root: str, remote: str = "origin") -> str | None:
    """Read the selected remote each time, so changing its URL cannot leave a stale slug.

    Derived rather than configured so the slug can never drift from the checkout the
    design loop actually spawns sessions in.
    """
    r = proc.capture(["git", "-C", root, "config", "--get", f"remote.{remote}.url"])
    if r.returncode != 0:
        return None
    return _slug_from_remote_url((r.stdout or "").strip())


@lru_cache(maxsize=8)
def _slug_from_remote_url(url: str) -> str | None:
    match = _REMOTE_SLUG.search(url)
    return f"{match['owner']}/{match['repo']}" if match else None


def design_slug(cfg) -> str | None:
    """GitHub ``owner/repo`` for the configured design repo, or None when unset/unresolvable."""
    if not cfg.design_repo:
        return None
    try:
        root = cfg.repo_root(cfg.design_repo)
    except ValueError:
        return None
    return _slug_for_checkout(str(root), cfg.repo_remote(cfg.design_repo))


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, value)


def _cutoff_date(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()


def require_gh(cfg) -> bool:
    if shutil.which("gh") is None:
        journal.log(cfg, "gh CLI not found — the design loop needs it to read merged PRs; skipping tick")
        return False
    if proc.capture(["gh", "auth", "status"]).returncode != 0:
        journal.log(cfg, "gh not authenticated (run 'gh auth login') — skipping tick")
        return False
    return True


def _merge_sha(raw: dict) -> str:
    commit = raw.get("mergeCommit") or {}
    return str(commit.get("oid") or commit.get("sha") or "")


def _file_paths(raw: dict) -> list[str]:
    paths: list[str] = []
    for f in raw.get("files") or []:
        if isinstance(f, str):
            paths.append(f)
        elif isinstance(f, dict):
            path = f.get("path") or f.get("filename") or f.get("name")
            if path:
                paths.append(str(path))
    return paths


def _comment_bodies(raw: dict) -> list[str]:
    bodies: list[str] = []
    for c in raw.get("comments") or []:
        if isinstance(c, str):
            bodies.append(c)
        elif isinstance(c, dict):
            body = c.get("body")
            if body:
                bodies.append(str(body))
    return bodies


def _parse_pr(raw: dict) -> MergedPR | None:
    try:
        number = int(raw["number"])
    except (KeyError, TypeError, ValueError):
        return None
    merge_sha = _merge_sha(raw)
    if not merge_sha:
        return None
    return MergedPR(
        number=number,
        title=str(raw.get("title") or ""),
        url=str(raw.get("url") or ""),
        merge_sha=merge_sha,
        merged_at=str(raw.get("mergedAt") or ""),
        files=[],
        comments=[],
    )


def _paginated_api_array(cfg, endpoint: str) -> list[dict] | None:
    r = proc.capture(["gh", "api", "--paginate", "--slurp", endpoint])
    if r.returncode != 0 or not r.stdout.strip():
        journal.log(cfg, f"could not read paginated GitHub API endpoint {endpoint}")
        return None
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        journal.log(cfg, f"gh returned invalid paginated JSON for {endpoint}")
        return None
    if not isinstance(payload, list):
        return None
    items: list[dict] = []
    pages = payload if all(isinstance(page, list) for page in payload) else [payload]
    for page in pages:
        if not isinstance(page, list):
            return None
        items.extend(item for item in page if isinstance(item, dict))
    return items


def _hydrate_pr(cfg, pr: MergedPR, slug: str) -> MergedPR | None:
    files = _paginated_api_array(cfg, f"repos/{slug}/pulls/{pr.number}/files")
    comments = _paginated_api_array(cfg, f"repos/{slug}/issues/{pr.number}/comments")
    if files is None or comments is None:
        journal.log(cfg, f"design-{pr.number}: could not hydrate files/comments — skipping")
        return None
    pr.files = _file_paths({"files": files})
    pr.comments = _comment_bodies({"comments": comments})
    return pr


def list_recent_merged_prs(cfg) -> list[MergedPR]:
    """Read merged PRs in the configured design repo through ``gh`` over a bounded window."""
    slug = design_slug(cfg)
    if slug is None:
        journal.log(
            cfg,
            "design loop: no design repo configured (set [settings] design_repo in the "
            "repo map) — skipping tick",
        )
        return []
    lookback = _int_env("AGENT_LOOP_DESIGN_LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
    limit = _int_env("AGENT_LOOP_DESIGN_SCAN_LIMIT", DEFAULT_SCAN_LIMIT)
    r = proc.capture(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            slug,
            "--state",
            "merged",
            "--limit",
            str(limit),
            "--search",
            f"merged:>={_cutoff_date(lookback)}",
            "--json",
            "number,mergeCommit,mergedAt,url,title",
        ]
    )
    if r.returncode != 0 or not r.stdout.strip():
        journal.log(cfg, f"could not list merged PRs for {slug}; skipping tick")
        return []
    try:
        payload = json.loads(r.stdout)
    except json.JSONDecodeError:
        journal.log(cfg, f"gh returned invalid JSON for {slug}; skipping tick")
        return []
    if not isinstance(payload, list):
        return []
    prs = [pr for raw in payload if isinstance(raw, dict) for pr in [_parse_pr(raw)] if pr is not None]
    hydrated: list[MergedPR] = []
    for pr in prs:
        full = _hydrate_pr(cfg, pr, slug)
        if full is not None:
            hydrated.append(full)
    return hydrated


def has_design_marker(pr: MergedPR) -> bool:
    markers = (f"{MARKER_PREFIX}{pr.merge_sha}", f"{CLAIM_PREFIX}{pr.merge_sha}")
    return any(marker in body for body in pr.comments for marker in markers)


def touches_ui(files: list[str]) -> bool:
    for path in files:
        lower = path.lower()
        if lower.endswith(_UI_EXTENSIONS):
            return True
        if lower.startswith(_UI_PATH_PREFIXES) and any(part in lower for part in _UI_PATH_PARTS):
            return True
        parts = lower.replace("\\", "/").split("/")
        if any(part in _UI_PATH_PARTS for part in parts):
            return True
    return False


def candidate_prs(prs: list[MergedPR]) -> list[MergedPR]:
    return [pr for pr in prs if not has_design_marker(pr) and touches_ui(pr.files)]


def claim_design_review(cfg, pr: MergedPR) -> DesignClaim | None:
    marker = f"{CLAIM_PREFIX}{pr.merge_sha}"
    body = (
        f"{marker}\n\n"
        f"Design review dispatched by agent-loop for PR #{pr.number}. "
        f"This spawn-time claim prevents duplicate post-merge review sessions if the review exits "
        f"before writing {MARKER_PREFIX}{pr.merge_sha}."
    )
    result = proc.run_gated(
        cfg,
        ["gh", "api", f"repos/{design_slug(cfg)}/issues/{pr.number}/comments", "-f", f"body={body}"],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    if not proc.succeeded(result):
        journal.log(cfg, f"design-{pr.number}: could not write post-merge review claim marker — skipping spawn")
        return None
    comment_id: str | None = None
    stdout = getattr(result, "stdout", "") if result is not None else ""
    if stdout:
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict) and payload.get("id"):
            comment_id = str(payload["id"])
    journal.log(cfg, f"design-{pr.number}: claimed design review with {marker}")
    return DesignClaim(marker=marker, comment_id=comment_id)


def rollback_design_claim(cfg, pr: MergedPR, claim: DesignClaim) -> None:
    if not claim.comment_id:
        journal.log(cfg, f"design-{pr.number}: spawn failed after {claim.marker}; no claim id to delete")
        return
    result = proc.run_gated(
        cfg,
        ["gh", "api", "-X", "DELETE", f"repos/{design_slug(cfg)}/issues/comments/{claim.comment_id}"],
    )
    if proc.succeeded(result):
        journal.log(cfg, f"design-{pr.number}: removed stale post-merge review claim {claim.marker}")
    else:
        journal.log(cfg, f"design-{pr.number}: could not remove stale post-merge review claim {claim.marker}")


def spawn_design_review(cfg, pr: MergedPR) -> bool:
    key = session_key(pr)
    if proc.has_session(key):
        journal.log(cfg, f"{key}: live post-merge review session already in flight — skipping")
        return False
    if not cfg.design_repo:
        journal.log(cfg, f"{key}: no design repo configured — skipping")
        return False
    root = cfg.repo_root(cfg.design_repo)
    if not root.is_dir():
        journal.log(cfg, f"{key}: design repo path missing ({root}) — skipping")
        return False
    claim = claim_design_review(cfg, pr)
    if claim is None:
        return False
    journal.log(cfg, f"{key}: candidate {pr.url or '#'+str(pr.number)} -> post-merge review")
    try:
        spawned = spawn.tmux_spawn(
            cfg, key, root, f"{workflows.design_review(pr.number, pr.url)}\n\n{tracker.resolve(cfg).state_contract()}"
        )
    except Exception as exc:
        journal.log(cfg, f"{key}: post-merge review spawn raised ({exc})")
        rollback_design_claim(cfg, pr, claim)
        return False
    if not spawned:
        rollback_design_claim(cfg, pr, claim)
        return False
    return True


def session_key(pr: MergedPR) -> str:
    return f"design-{pr.number}"


def live_design_sessions() -> set[str]:
    r = proc.capture(["tmux", "list-sessions", "-F", "#S"])
    if r.returncode != 0:
        return set()
    return {line.strip() for line in r.stdout.splitlines() if line.strip().startswith("design-")}


def run_tick(cfg) -> None:
    if not require_gh(cfg):
        return

    hint = rate_limited_now(cfg)
    if hint is not None:
        journal.log(cfg, f"Claude usage outage — holding this tick, no post-merge review spawns ({hint})")
        return

    prs = list_recent_merged_prs(cfg)
    candidates = candidate_prs(prs)
    # Review oldest merges first so that when the nightly cap holds overflow, the deferred PRs are
    # the newest — the ones with the most runway left before they age out of the lookback window
    # (AGENT_LOOP_DESIGN_LOOKBACK_DAYS). ISO-8601 mergedAt sorts chronologically as text.
    candidates.sort(key=lambda pr: pr.merged_at or "")
    cap = max(0, cfg.concurrency)
    live_sessions = live_design_sessions()
    for pr in candidates:
        key = session_key(pr)
        if key not in live_sessions and proc.has_session(key):
            live_sessions.add(key)
    live = len(live_sessions)
    slots = max(0, cap - live)
    skipped = len(prs) - len(candidates)
    journal.log(
        cfg,
        f"design scan: merged={len(prs)} candidates={len(candidates)} skipped={skipped} "
        f"cap={cap} live={live} slots={slots}",
    )
    if slots <= 0:
        journal.log(cfg, "no free post-merge review slots")
        return

    spawned = 0
    for pr in candidates:
        if session_key(pr) in live_sessions or proc.has_session(session_key(pr)):
            continue
        if spawned >= slots:
            break
        if spawn_design_review(cfg, pr):
            spawned += 1
    if len(candidates) > spawned:
        journal.log(
            cfg,
            f"post-merge review cap held {len(candidates) - spawned} candidate(s) over the -c={cap} "
            "safety cap for the next run",
        )
    journal.log(cfg, f"spawned {spawned} post-merge review session(s) this tick")
