"""linear — the Linear GraphQL client and higher-level issue operations.

Ports the Linear block of ``agent-loop-lib.sh`` (lib:216-303): the ``linear()`` HTTP helper,
per-team state/label caches, project resolution, and the state/label/comment/assignee mutators.
Replaces ``curl`` + ``jq`` with stdlib ``urllib`` + ``json``. All mutators route through the
DRY_RUN gate and preserve the bash soft-fail branches (label missing -> log+skip, etc.).
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Sequence

from agentloop import journal
from agentloop.tracker import Comment, Issue, TrackerError

_ENDPOINT = "https://api.linear.app/graphql"

# Machine-readable token the debug loop embeds in its CI-fix marker comments so it can tell,
# on a later tick, which failing head commit(s) it has already spawned a fix for. Keyed by the
# full commit SHA so a fresh push (new SHA) earns a fresh attempt while a stuck SHA does not.
_CI_FIX_MARKER = re.compile(r"agent-loop-ci-fix:([0-9a-f]{7,40})")
_APPROVAL_WAIT_MARKER = re.compile(r"agent-loop-approval-wait:([0-9a-f]{7,40})")
_APPROVAL_WAIT_HANDOFF_MARKER = re.compile(r"agent-loop-approval-handoff:([0-9a-f]{7,40})")


class LinearError(TrackerError):
    """Raised when a Linear call fails (non-2xx or a GraphQL ``errors`` array)."""


class LinearClient:
    def __init__(self, cfg):
        self.cfg = cfg
        # Per-team caches of workflow-state and label IDs (team-scoped in Linear), keyed
        # "<teamId>|<name>" (lib:234-250). Resolved lazily via ensure_team_meta.
        self.state_id: dict[str, str] = {}
        self.label_id: dict[str, str] = {}
        self._team_done: set[str] = set()
        # JSON array of loop project ids, set by resolve_loop_projects (lib:255).
        self.loop_ids: list[str] = []

    def resolve_scope(self) -> bool:
        self.resolve_loop_projects()
        return bool(self.loop_ids)

    def ensure_scope(self, scope: str) -> None:
        self.ensure_team_meta(scope)

    def ensure_required_label(self, issue_id: str, scope: str, label: str) -> bool:
        """Create and apply a required label, failing closed if creation failed.

        Optional add_label calls retain their historical missing-label soft failure.
        Required ownership and blocked labels must actually be available before a
        live transition can proceed.
        """
        self.ensure_label(scope, label)
        if not self.cfg.dry_run and not self.label_id.get(f"{scope}|{label}"):
            journal.log(self.cfg, f"label '{label}' unavailable in team {scope}")
            return False
        return self.add_label(issue_id, scope, label)

    def issues_in_states(
        self, states: Sequence[str], *, include_comments: bool = False,
        newest_comments: bool = False,
    ) -> list[Issue]:
        """Read normalized issues in the resolved loop projects.

        Small issue pages keep the rich projection under the API complexity limit.
        Read every issue page so capacity and blocked-worktree protection cannot
        silently omit work. Nested collections remain bounded at 30 labels,
        15 attachments/relations, and optionally 100 comments.
        """
        comments = ""
        if include_comments:
            direction = "last" if newest_comments else "first"
            comments = f"comments({direction}:100){{nodes{{body createdAt}}}}"
        query = (
            "query($ids:[ID!],$s:[String!],$after:String){issues(first:25,after:$after,filter:{"
            "project:{id:{in:$ids}},state:{name:{in:$s}}}){nodes{"
            "id identifier title priority team{id} startedAt "
            "labels(first:30){nodes{name}}"
            "attachments(first:15){nodes{url}}"
            "children(first:1){nodes{id}}"
            "inverseRelations(first:15){nodes{type issue{state{type}}}}"
            + comments + "}pageInfo{hasNextPage endCursor}}}"
        )
        rows: list[Issue] = []
        cursor: str | None = None
        seen_cursors: set[str] = set()
        while True:
            resp = self.query(query, {"ids": self.loop_ids, "s": list(states), "after": cursor})
            data = resp.get("data") if isinstance(resp, dict) else None
            issues = data.get("issues") if isinstance(data, dict) else None
            if not isinstance(issues, dict) or not isinstance(issues.get("nodes"), list):
                raise LinearError("malformed issue collection returned by Linear")
            page_info = issues.get("pageInfo")
            if not isinstance(page_info, dict) or not isinstance(page_info.get("hasNextPage"), bool):
                raise LinearError("malformed issue pagination returned by Linear")
            rows.extend(self._normalize_issue(node) for node in issues["nodes"])
            if not page_info["hasNextPage"]:
                return rows
            next_cursor = page_info.get("endCursor")
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen_cursors:
                raise LinearError("missing or repeated issue pagination cursor returned by Linear")
            seen_cursors.add(next_cursor)
            cursor = next_cursor

    @staticmethod
    def _normalize_issue(node: dict) -> Issue:
        if not isinstance(node, dict) or not node.get("id") or not node.get("identifier"):
            raise LinearError("malformed issue returned by Linear")

        def nodes(field: str) -> list[dict]:
            if field == "labels":
                connection = node.get(field)
                if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                    raise LinearError(f"malformed labels on Linear issue {node['identifier']}")
            connection = node.get(field) or {}
            values = connection.get("nodes", []) if isinstance(connection, dict) else None
            if not isinstance(values, list) or any(not isinstance(value, dict) for value in values):
                raise LinearError(f"malformed {field} on Linear issue {node['identifier']}")
            return values

        label_nodes = nodes("labels")
        if any(not isinstance(label.get("name"), str) or not label["name"] for label in label_nodes):
            raise LinearError(f"malformed label on Linear issue {node['identifier']}")
        labels = tuple(label["name"] for label in label_nodes)
        urls = tuple(
            attachment["url"] for attachment in nodes("attachments")
            if "github.com/" in (attachment.get("url") or "")
            and "/pull/" in (attachment.get("url") or "")
        )
        # In inverseRelations, issue is the blocker (relatedIssue is this ticket).
        blockers = [relation for relation in nodes("inverseRelations") if relation.get("type") == "blocks"]
        blocked = any(
            (((relation.get("issue") or {}).get("state") or {}).get("type"))
            not in ("completed", "canceled")
            for relation in blockers
        )
        return Issue(
            id=node["id"], identifier=node["identifier"],
            scope=(node.get("team") or {}).get("id", ""),
            title=node.get("title") or "", labels=labels, linked_pr_urls=urls,
            comments=tuple(
                Comment(body=comment.get("body") or "", created_at=comment.get("createdAt") or "")
                for comment in nodes("comments")
            ),
            priority_rank=node.get("priority") or 99,
            has_children=bool(nodes("children")), blocked_by_open_issue=blocked,
            started_at=node.get("startedAt") or "",
        )

    def state_contract(self) -> str:
        return (
            "Linear state contract: Linear is the state store for the loops — every status move, comment, "
            "and "
            "evidence write must land in Linear or the ticket strands and the pipeline stalls. Do ALL "
            "Linear reads and writes with the LINEAR_API_KEY (already in the environment, or source "
            "~/.agent-loop/env) against the Linear GraphQL API at https://api.linear.app/graphql. Do NOT "
            "use the Linear MCP connector: its OAuth token may be expired/unauthorized, and a silent MCP "
            "failure leaves the issue in the wrong state. If a Linear write fails, retry via the API key and "
            "report it — never exit leaving Linear unupdated."
        )

    def blocked_contract(self, blocked_label: str, needs_input_state: str) -> str:
        return (
            "Blocked-work safety contract: never return a blocked ticket to Todo or Backlog as a fallback. "
            f"First ensure the team-scoped {blocked_label} label exists through the Linear GraphQL API and add it "
            "to the issue. Then write one blocked-verdict comment with the current origin/main SHA and the marker "
            "<sub>agent-loop-blocked:<sha></sub>, and attempt to move it to the configured "
            f"{needs_input_state} state. "
            f"If that state is absent or the move fails, leave the {blocked_label} label in place and stop; the loop "
            f"excludes that label from all pickups. Do not stack duplicate blocked comments. After the blocking "
            f"condition changes, remove {blocked_label} and explicitly requeue the ticket."
        )

    def classifier_context(self) -> str:
        return "Linear issue"

    # ── low-level GraphQL POST (lib:217-232) ────────────────────────────────
    def query(self, query: str, variables: dict | None = None) -> dict:
        """POST a GraphQL query. Raises :class:`LinearError` on non-2xx HTTP or a body that
        carries an ``errors`` array (Linear often returns 400 with the real reason, incl.
        "Authentication required")."""
        payload = json.dumps({"query": query, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            _ENDPOINT,
            data=payload,
            headers={
                "Authorization": self.cfg.linear_api_key,
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                code = resp.getcode()
                body = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # A 400-with-errors must be surfaced, not swallowed — read the body for the reason.
            code = e.code
            body = e.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            journal.log(self.cfg, f"Linear API error (network): {e.reason}")
            raise LinearError(str(e.reason)) from e

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if not (200 <= code < 300) or (isinstance(data, dict) and "errors" in data):
            detail = (
                json.dumps(data.get("errors")) if isinstance(data, dict) and "errors" in data else body
            )
            journal.log(self.cfg, f"Linear API error (HTTP {code}): {detail}")
            raise LinearError(f"HTTP {code}")
        return data if isinstance(data, dict) else {}

    # ── per-team metadata cache (lib:236-250) ───────────────────────────────
    def ensure_team_meta(self, team: str) -> None:
        if team in self._team_done:
            return
        meta = self.query(
            "query($t:String!){team(id:$t){states{nodes{id name}} labels{nodes{id name}}}}",
            {"t": team},
        )
        t = ((meta.get("data") or {}).get("team")) or {}
        for node in (t.get("states") or {}).get("nodes", []):
            self.state_id[f"{team}|{node['name']}"] = node["id"]
        for node in (t.get("labels") or {}).get("nodes", []):
            self.label_id[f"{team}|{node['name']}"] = node["id"]
        self._team_done.add(team)

    # ── project resolution (lib:255-265) ────────────────────────────────────
    def resolve_loop_projects(self) -> None:
        """Set ``self.loop_ids`` to the ids of projects carrying the loop label. Fetch up to 100 +
        filter client-side (a project's labels connection is reliable; a server-side project
        filter is not)."""
        resp = self.query(
            "query{projects(first:100){nodes{id name labels(first:25){nodes{name}}}}}"
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        projects = data.get("projects") if isinstance(data, dict) else None
        if not isinstance(projects, dict) or not isinstance(projects.get("nodes"), list):
            raise LinearError("malformed project collection returned by Linear")
        nodes = projects["nodes"]
        for node in nodes:
            if not isinstance(node, dict) or not node.get("id") or not isinstance(node.get("name"), str):
                raise LinearError("malformed project returned by Linear")
            labels = node.get("labels")
            if not isinstance(labels, dict) or not isinstance(labels.get("nodes"), list):
                raise LinearError("malformed project labels returned by Linear")
            if any(not isinstance(label, dict) or not isinstance(label.get("name"), str)
                   for label in labels["nodes"]):
                raise LinearError("malformed project label returned by Linear")
        label = self.cfg.project_label
        matched = [
            n for n in nodes if any(l.get("name") == label for l in (n.get("labels") or {}).get("nodes", []))
        ]
        self.loop_ids = [n["id"] for n in matched]
        names = ", ".join(n["name"] for n in matched)
        journal.log(self.cfg, f"loop projects [{label}]: {names or '<none>'}")

    # ── mutators (DRY_RUN-gated, soft-fail preserved) ───────────────────────
    def _mutate(self, query: str, variables: dict) -> bool:
        """Run a mutation through the DRY_RUN gate. Returns True on success (or dry-run).

        Linear mutation payloads can return HTTP 200 while their ``success`` field is false.
        Treat that as a failed mutation so callers that depend on an earlier write (notably the
        approval-wait handoff comment) do not perform their next state transition.
        """
        if self.cfg.dry_run:
            journal.log(self.cfg, f"DRY-RUN would: linear {query.split('(')[0].strip()}")
            return True
        try:
            response = self.query(query, variables)
        except LinearError:
            return False
        payloads = (response.get("data") or {}).values()
        result = next(iter(payloads), None)
        if not isinstance(result, dict) or result.get("success") is not True:
            journal.log(self.cfg, "Linear mutation returned success=false or malformed data")
            return False
        return True

    def move_state(self, issue_id: str, team: str, state_name: str) -> bool:
        """Move an issue to a workflow state (lib:267-272). False if the state id is unknown."""
        sid = self.state_id.get(f"{team}|{state_name}")
        if not sid:
            journal.log(self.cfg, f"no state id for '{state_name}' in team {team} — skipping move")
            return False
        return self._mutate(
            "mutation($id:String!,$s:String!){issueUpdate(id:$id,input:{stateId:$s}){success}}",
            {"id": issue_id, "s": sid},
        )

    def add_label(self, issue_id: str, team: str, label_name: str) -> bool:
        """Apply a label (lib:274-279). Missing label -> log + skip (return True, like bash)."""
        lid = self.label_id.get(f"{team}|{label_name}")
        if not lid:
            journal.log(
                self.cfg, f"label '{label_name}' not found in team {team} — create it once; skipping"
            )
            return True
        return self._mutate(
            "mutation($id:String!,$l:String!){issueAddLabel(id:$id,labelId:$l){success}}",
            {"id": issue_id, "l": lid},
        )

    def add_comment(self, issue_id: str, body: str) -> bool:
        return self._mutate(
            "mutation($id:String!,$b:String!){commentCreate(input:{issueId:$id,body:$b}){success}}",
            {"id": issue_id, "b": body},
        )

    # ── debug CI-fix idempotency markers (comment-based) ────────────────────
    def ci_fix_markers(self, issue_id: str) -> list[str]:
        """Return the failing head SHAs the debug loop has already recorded a CI-fix attempt for
        on this issue, parsed from its marker comments. Read-only (runs even under DRY_RUN).

        On any read failure returns ``[]`` — fail toward re-attempting a fix, never toward a
        premature Needs-Input escalation (a session in flight is still guarded by has_session)."""
        try:
            resp = self.query(
                "query($id:String!){issue(id:$id){comments(first:100){nodes{body}}}}",
                {"id": issue_id},
            )
        except LinearError:
            return []
        nodes = ((((resp.get("data") or {}).get("issue")) or {}).get("comments") or {}).get("nodes", [])
        shas: list[str] = []
        for n in nodes:
            shas.extend(_CI_FIX_MARKER.findall(n.get("body") or ""))
        return shas

    def add_ci_fix_marker(self, issue_id: str, sha: str, pr_num: int) -> bool:
        """Post the CI-fix marker comment for a failing head ``sha`` (DRY_RUN-gated via add_comment).
        The trailing ``agent-loop-ci-fix:<sha>`` token is what :meth:`ci_fix_markers` reads back."""
        short = sha[:12]
        body = (
            f"🔧 The **debug** loop is attempting an automated CI fix for the failing checks on "
            f"PR #{pr_num} (commit `{short}`). If CI is still red on this same commit next cycle — "
            f"or after repeated attempts — I'll move this ticket to **{self.cfg.state_needs_input}** "
            f"for a human.\n\n<sub>agent-loop-ci-fix:{sha}</sub>"
        )
        return self.add_comment(issue_id, body)

    # ── debug approval-wait idempotency markers ─────────────────────────────
    def approval_wait_markers(self, issue_id: str) -> tuple[dict[str, str], set[str]]:
        """Return first-seen approval waits and durable handoffs, keyed by head SHA.

        Linear's server timestamp makes the first marker a durable start time across loop ticks;
        the handoff marker prevents duplicate explanatory comments if a state mutation retries.
        Read failure safely restarts the timer rather than escalating without a known wait.
        """
        nodes: list[dict] = []
        cursor: str | None = None
        while True:
            try:
                resp = self.query(
                    "query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after){"
                    "nodes{body createdAt} pageInfo{hasNextPage endCursor}}}}",
                    {"id": issue_id, "after": cursor},
                )
            except LinearError:
                return {}, set()
            comments = ((((resp.get("data") or {}).get("issue")) or {}).get("comments") or {})
            nodes.extend(comments.get("nodes", []))
            page_info = comments.get("pageInfo") or {}
            next_cursor = page_info.get("endCursor")
            if not page_info.get("hasNextPage") or not next_cursor or next_cursor == cursor:
                break
            cursor = next_cursor

        started: dict[str, str] = {}
        handoffs: set[str] = set()
        for node in nodes:
            body = node.get("body") or ""
            created_at = node.get("createdAt") or ""
            for sha in _APPROVAL_WAIT_MARKER.findall(body):
                if sha not in started or created_at < started[sha]:
                    started[sha] = created_at
            handoffs.update(_APPROVAL_WAIT_HANDOFF_MARKER.findall(body))
        return started, handoffs

    def add_approval_wait_marker(self, issue_id: str, sha: str, pr_num: int, missing: list[str]) -> bool:
        body = (
            f"⏳ The **debug** loop started an approval wait for PR #{pr_num} (commit `{sha[:12]}`). "
            f"It is green with no unresolved review threads, but is missing: {'; '.join(missing)}. "
            "A fresh push starts a new wait window.\n\n"
            f"<sub>agent-loop-approval-wait:{sha}</sub>"
        )
        return self.add_comment(issue_id, body)

    def add_approval_wait_escalation(
        self, issue_id: str, sha: str, pr_num: int, minutes: int, missing: list[str],
        review_state: str, needs_input_state: str,
    ) -> bool:
        body = (
            f"⚠️ The **debug** loop waited {minutes} minutes for approval on PR #{pr_num} "
            f"(commit `{sha[:12]}`). It is green with no unresolved review threads, but is missing: "
            f"{'; '.join(missing)}. The ticket is being handed from **{review_state}** "
            f"to **{needs_input_state}** for a human. This loop only watches **{review_state}**; "
            f"after the missing signal arrives, move the ticket back there to re-evaluate it.\n\n"
            f"<sub>agent-loop-approval-handoff:{sha}</sub>"
        )
        return self.add_comment(issue_id, body)

    def ensure_label(self, team: str, label_name: str) -> None:
        """Ensure a team-scoped label exists (creating it once if missing) and cache its id
        (lib:289-297). Used for the loop-wip marker so a missing label can't silently make
        count_active return 0 and over-dispatch."""
        if self.label_id.get(f"{team}|{label_name}"):
            return
        if self.cfg.dry_run:
            journal.log(self.cfg, f"DRY-RUN would create label '{label_name}' in team {team}")
            return
        try:
            resp = self.query(
                "mutation($n:String!,$t:String!){issueLabelCreate(input:{name:$n,teamId:$t})"
                "{success issueLabel{id}}}",
                {"n": label_name, "t": team},
            )
        except LinearError:
            return
        result = ((resp.get("data") or {}).get("issueLabelCreate")) or {}
        if not isinstance(result, dict) or result.get("success") is not True:
            journal.log(self.cfg, f"could not create label '{label_name}' in team {team}")
            return
        lid = result.get("issueLabel") or {}
        if lid.get("id"):
            self.label_id[f"{team}|{label_name}"] = lid["id"]
            journal.log(self.cfg, f"created label '{label_name}' in team {team}")

    def assign_issue(self, issue_id: str) -> bool:
        """Reassign an issue (no-op unless assignee_id is set) — lib:299-303."""
        if not self.cfg.assignee_id:
            return True
        return self._mutate(
            "mutation($id:String!,$a:String!){issueUpdate(id:$id,input:{assigneeId:$a}){success}}",
            {"id": issue_id, "a": self.cfg.assignee_id},
        )
