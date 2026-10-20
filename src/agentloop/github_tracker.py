"""GitHub Issues + Projects v2 tracker adapter.

The selected Project is the workflow authority.  Issue open/closed state remains
separate: only open Issues from the one configured repository and carrying the
operator's opt-in label are admitted.  All Project writes name that one project
and item explicitly, so the adapter cannot update an unrelated project.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Sequence

from agentloop import journal
from agentloop.tracker import Comment, Issue, TrackerError

_ENDPOINT = "https://api.github.com/graphql"
_CI_FIX_MARKER = re.compile(r"agent-loop-ci-fix:([0-9a-f]{7,40})")
_APPROVAL_WAIT_MARKER = re.compile(r"agent-loop-approval-wait:([0-9a-f]{7,40})")
_APPROVAL_WAIT_HANDOFF_MARKER = re.compile(r"agent-loop-approval-handoff:([0-9a-f]{7,40})")

_PROJECT_ITEMS_QUERY = """
query($id: ID!, $status: String!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 25, after: $after) {
        nodes {
          id
          type
          isArchived
          fieldValueByName(name: $status) {
            ... on ProjectV2ItemFieldSingleSelectValue {
              name
              optionId
            }
          }
          content {
            ... on Issue {
              id
              number
              title
              url
              body
              state
              repository { id nameWithOwner }
              labels(first: 100) { nodes { name } }
              subIssues(first: 1) { totalCount }
              blockedBy(first: 100) {
                nodes { state }
                pageInfo { hasNextPage endCursor }
              }
              closedByPullRequestsReferences(first: 100, includeClosedPrs: true) {
                nodes { url }
                pageInfo { hasNextPage endCursor }
              }
              timelineItems(first: 100, itemTypes: [CROSS_REFERENCED_EVENT]) {
                nodes {
                  ... on CrossReferencedEvent {
                    source { ... on PullRequest { url } }
                  }
                }
                pageInfo { hasNextPage endCursor }
              }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""

class GitHubError(TrackerError):
    """Raised when GitHub's GraphQL API cannot provide a reliable answer."""


class GitHubClient:
    """A deliberately single-Project, single-repository GitHub queue."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.project_id = ""
        self.status_field_id = ""
        self.status_options: dict[str, str] = {}
        self._items: dict[str, str] = {}
        self._repositories: dict[str, tuple[str, str]] = {}
        self._labels: dict[tuple[str, str], str] = {}

    def query(self, document: str, variables: dict | None = None) -> dict:
        payload = json.dumps({"query": document, "variables": variables or {}}).encode()
        req = urllib.request.Request(
            _ENDPOINT,
            data=payload,
            headers={
                "Authorization": f"bearer {self.cfg.github_token}",
                "Content-Type": "application/json",
                "User-Agent": "agentloop",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as response:
                code = response.getcode()
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as error:
            code = error.code
            body = error.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as error:
            journal.log(self.cfg, f"GitHub API error (network): {error.reason}")
            raise GitHubError(str(error.reason)) from error
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if not (200 <= code < 300) or (isinstance(data, dict) and data.get("errors")):
            journal.log(self.cfg, f"GitHub API error (HTTP {code})")
            raise GitHubError(f"HTTP {code}")
        if not isinstance(data, dict):
            raise GitHubError("malformed response returned by GitHub")
        return data

    def resolve_scope(self) -> bool:
        project = self._project()
        self.project_id = project["id"]
        self._load_status_field()
        return True

    def ensure_scope(self, scope: str) -> None:
        if not self.project_id:
            self.resolve_scope()
        if scope != self.project_id:
            raise GitHubError("issue does not belong to the configured GitHub Project")

    def _project(self) -> dict:
        response = self.query(
            "query($owner:String!,$number:Int!){repositoryOwner(login:$owner){"
            "...on Organization{projectV2(number:$number){id title}}"
            "...on User{projectV2(number:$number){id title}}}}",
            {"owner": self.cfg.github_owner, "number": self.cfg.github_project_number},
        )
        owner = (response.get("data") or {}).get("repositoryOwner")
        if not isinstance(owner, dict):
            raise GitHubError("GitHub project owner was not found or is not accessible")
        candidates = [value for key, value in owner.items() if key == "projectV2"]
        project = candidates[0] if candidates else None
        if not isinstance(project, dict) or not isinstance(project.get("id"), str) or not project["id"]:
            raise GitHubError("configured GitHub Project was not found or is not accessible")
        return project

    def _load_status_field(self) -> None:
        cursor: str | None = None
        seen: set[str] = set()
        wanted = getattr(self.cfg, "github_status_field", "Status")
        selected: dict | None = None
        while True:
            response = self.query(
                "query($id:ID!,$after:String){node(id:$id){...on ProjectV2{fields(first:100,after:$after){"
                "nodes{...on ProjectV2SingleSelectField{id name options{id name}}}"
                "pageInfo{hasNextPage endCursor}}}}}",
                {"id": self.project_id, "after": cursor},
            )
            fields = (((response.get("data") or {}).get("node") or {}).get("fields"))
            if not isinstance(fields, dict) or not isinstance(fields.get("nodes"), list):
                raise GitHubError("malformed GitHub Project field collection")
            for field in fields["nodes"]:
                if isinstance(field, dict) and field.get("name") == wanted:
                    if selected is not None:
                        raise GitHubError(f"multiple GitHub Project fields are named '{wanted}'")
                    selected = field
            next_cursor = self._next_cursor(fields, seen, "Project field")
            if next_cursor is None:
                break
            cursor = next_cursor
        if not isinstance(selected, dict) or not isinstance(selected.get("id"), str):
            raise GitHubError(f"GitHub Project has no single-select '{wanted}' field")
        options = selected.get("options")
        if not isinstance(options, list):
            raise GitHubError(f"GitHub Project '{wanted}' field has malformed options")
        mapped: dict[str, str] = {}
        for option in options:
            if not isinstance(option, dict) or not isinstance(option.get("id"), str) or not isinstance(option.get("name"), str):
                raise GitHubError(f"GitHub Project '{wanted}' field has malformed option")
            if option["name"] in mapped:
                raise GitHubError(f"GitHub Project '{wanted}' has duplicate option '{option['name']}'")
            mapped[option["name"]] = option["id"]
        needed = {
            self.cfg.state_queue, self.cfg.state_todo, self.cfg.state_active,
            self.cfg.state_review, self.cfg.state_approved, self.cfg.state_needs_input,
        }
        absent = sorted(name for name in needed if name not in mapped)
        if absent:
            raise GitHubError("GitHub Project Status is missing configured option(s): " + ", ".join(absent))
        self.status_field_id = selected["id"]
        self.status_options = mapped

    @staticmethod
    def _next_cursor(connection: dict, seen: set[str], name: str) -> str | None:
        page = connection.get("pageInfo")
        if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
            raise GitHubError(f"malformed {name} pagination")
        if not page["hasNextPage"]:
            return None
        cursor = page.get("endCursor")
        if not isinstance(cursor, str) or not cursor or cursor in seen:
            raise GitHubError(f"missing or repeated {name} pagination cursor")
        seen.add(cursor)
        return cursor

    def issues_in_states(
        self, states: Sequence[str], *, include_comments: bool = False,
        newest_comments: bool = False,
    ) -> list[Issue]:
        if not self.project_id:
            self.resolve_scope()
        unknown = [name for name in states if name not in self.status_options]
        if unknown:
            raise GitHubError("unknown configured GitHub Project Status: " + ", ".join(unknown))
        wanted = set(states)
        rows: list[Issue] = []
        seen_issues: set[str] = set()
        cursor: str | None = None
        cursors: set[str] = set()
        while True:
            response = self.query(
                _PROJECT_ITEMS_QUERY,
                {"id": self.project_id, "status": getattr(self.cfg, "github_status_field", "Status"), "after": cursor},
            )
            items = (((response.get("data") or {}).get("node") or {}).get("items"))
            if not isinstance(items, dict) or not isinstance(items.get("nodes"), list):
                raise GitHubError("malformed GitHub Project item collection")
            for item in items["nodes"]:
                issue = self._normalize_item(item, include_comments, newest_comments)
                if issue is not None:
                    if issue.id in seen_issues:
                        raise GitHubError("GitHub Issue appears more than once in the configured Project")
                    seen_issues.add(issue.id)
                    if self._item_status(item) in wanted:
                        rows.append(issue)
            next_cursor = self._next_cursor(items, cursors, "Project item")
            if next_cursor is None:
                return rows
            cursor = next_cursor

    def _item_status(self, item: dict) -> str:
        value = item.get("fieldValueByName") if isinstance(item, dict) else None
        if not isinstance(value, dict) or not isinstance(value.get("name"), str):
            return ""
        return value["name"]

    def _normalize_item(self, item: object, include_comments: bool, newest_comments: bool) -> Issue | None:
        if not isinstance(item, dict) or item.get("isArchived") is True or item.get("type") != "ISSUE":
            return None
        item_id = item.get("id")
        node = item.get("content")
        if not isinstance(item_id, str) or not isinstance(node, dict):
            raise GitHubError("malformed GitHub Project issue item")
        issue_id, number, title, url, state = (node.get(key) for key in ("id", "number", "title", "url", "state"))
        repository = node.get("repository")
        if not isinstance(issue_id, str) or not isinstance(number, int) or not isinstance(title, str) or not isinstance(url, str):
            raise GitHubError("malformed GitHub Issue in Project")
        if not isinstance(repository, dict) or not isinstance(repository.get("id"), str) or not isinstance(repository.get("nameWithOwner"), str):
            raise GitHubError("GitHub Issue has malformed repository")
        allowed = {value.lower() for value in self.cfg.github_repositories}
        if repository["nameWithOwner"].lower() not in allowed:
            return None
        if state != "OPEN":
            return None
        labels_connection = node.get("labels")
        labels_nodes = labels_connection.get("nodes") if isinstance(labels_connection, dict) else None
        if not isinstance(labels_nodes, list) or any(not isinstance(label, dict) or not isinstance(label.get("name"), str) for label in labels_nodes):
            raise GitHubError(f"GitHub Issue {url} has malformed labels")
        labels = tuple(label["name"] for label in labels_nodes)
        if self.cfg.project_label not in labels:
            return None
        self._items[issue_id] = item_id
        self._repositories[issue_id] = (repository["id"], repository["nameWithOwner"])
        comments = self._comments(issue_id, newest_comments) if include_comments else ()
        closing = node.get("closedByPullRequestsReferences") or {}
        timeline = node.get("timelineItems") or {}
        closing_nodes = closing.get("nodes") if isinstance(closing, dict) else None
        timeline_nodes = timeline.get("nodes") if isinstance(timeline, dict) else None
        if not isinstance(closing_nodes, list) or not isinstance(timeline_nodes, list):
            raise GitHubError(f"GitHub Issue {url} has malformed pull-request references")
        self._require_complete_connection(closing, f"GitHub Issue {url} closing pull-request references")
        self._require_complete_connection(timeline, f"GitHub Issue {url} timeline pull-request references")
        urls = []
        for pull in closing_nodes:
            if not isinstance(pull, dict) or not isinstance(pull.get("url"), str):
                raise GitHubError(f"GitHub Issue {url} has malformed closing pull-request reference")
            urls.append(pull["url"])
        for event in timeline_nodes:
            source = event.get("source") if isinstance(event, dict) else None
            if isinstance(source, dict) and isinstance(source.get("url"), str):
                urls.append(source["url"])
        links = tuple(dict.fromkeys(urls))
        sub_issues = node.get("subIssues") or {}
        blocked_by = node.get("blockedBy") or {}
        blocker_nodes = blocked_by.get("nodes") if isinstance(blocked_by, dict) else None
        if not isinstance(sub_issues, dict) or not isinstance(blocker_nodes, list):
            raise GitHubError(f"GitHub Issue {url} has malformed dependency metadata")
        self._require_complete_connection(blocked_by, f"GitHub Issue {url} blockers")
        if any(not isinstance(blocker, dict) or blocker.get("state") not in {"OPEN", "CLOSED"} for blocker in blocker_nodes):
            raise GitHubError(f"GitHub Issue {url} has malformed blocker")
        return Issue(
            id=issue_id, identifier=f"{repository['nameWithOwner']}#{number}", scope=self.project_id,
            title=title, labels=labels, linked_pr_urls=links, comments=comments,
            has_children=bool(sub_issues.get("totalCount", 0)),
            blocked_by_open_issue=any(blocker["state"] == "OPEN" for blocker in blocker_nodes),
        )

    @staticmethod
    def _require_complete_connection(connection: object, name: str) -> None:
        """Refuse a partial decision-bearing nested collection.

        A later open blocker or linked pull request must not look like an absent
        one. Fetching every nested page per Project item would be needlessly
        expensive for the first slice, so a collection beyond its explicit bound
        stops the tick before any mutation instead.
        """
        page = connection.get("pageInfo") if isinstance(connection, dict) else None
        if not isinstance(page, dict) or not isinstance(page.get("hasNextPage"), bool):
            raise GitHubError(f"{name} has malformed pagination")
        if page["hasNextPage"]:
            raise GitHubError(f"{name} exceeds the supported pagination bound")

    def _comments(self, issue_id: str, newest: bool = False) -> tuple[Comment, ...]:
        rows: list[Comment] = []
        cursor: str | None = None
        cursors: set[str] = set()
        while True:
            response = self.query(
                "query($id:ID!,$after:String){node(id:$id){...on Issue{comments(first:100,after:$after){"
                "nodes{body createdAt} pageInfo{hasNextPage endCursor}}}}}",
                {"id": issue_id, "after": cursor},
            )
            comments = (((response.get("data") or {}).get("node") or {}).get("comments"))
            nodes = comments.get("nodes") if isinstance(comments, dict) else None
            if not isinstance(nodes, list):
                raise GitHubError("malformed GitHub Issue comment collection")
            for node in nodes:
                if not isinstance(node, dict) or not isinstance(node.get("body"), str) or not isinstance(node.get("createdAt"), str):
                    raise GitHubError("malformed GitHub Issue comment")
                rows.append(Comment(node["body"], node["createdAt"]))
            next_cursor = self._next_cursor(comments, cursors, "Issue comment")
            if next_cursor is None:
                return tuple(reversed(rows)) if newest else tuple(rows)
            cursor = next_cursor

    def _mutate(self, document: str, variables: dict, name: str) -> bool:
        if self.cfg.dry_run:
            journal.log(self.cfg, f"DRY-RUN would: GitHub {name}")
            return True
        try:
            response = self.query(document, variables)
        except GitHubError:
            return False
        if not isinstance((response.get("data") or {}).get(name), dict):
            journal.log(self.cfg, f"GitHub {name} returned malformed data")
            return False
        return True

    def move_state(self, issue_id: str, scope: str, state_name: str) -> bool:
        self.ensure_scope(scope)
        item = self._items.get(issue_id)
        option = self.status_options.get(state_name)
        if not item or not option:
            journal.log(self.cfg, f"GitHub issue/status is not cached for '{state_name}' — skipping move")
            return False
        document = (
            "mutation($p:ID!,$i:ID!,$f:ID!,$o:String!){updateProjectV2ItemFieldValue("
            "input:{projectId:$p,itemId:$i,fieldId:$f,value:{singleSelectOptionId:$o}}){projectV2Item{id}}}"
        )
        variables = {"p": self.project_id, "i": item, "f": self.status_field_id, "o": option}
        if self.cfg.dry_run:
            journal.log(self.cfg, "DRY-RUN would: GitHub updateProjectV2ItemFieldValue")
            return True
        try:
            response = self.query(document, variables)
        except GitHubError:
            return False
        changed = ((response.get("data") or {}).get("updateProjectV2ItemFieldValue") or {}).get("projectV2Item")
        if not isinstance(changed, dict) or changed.get("id") != item:
            journal.log(self.cfg, "GitHub status update did not confirm the selected Project item")
            return False
        return True

    def _label(self, issue_id: str, label_name: str, create: bool) -> str:
        repo = self._repositories.get(issue_id)
        if not repo:
            raise GitHubError("GitHub Issue repository is not cached")
        key = (repo[0], label_name)
        if key in self._labels:
            return self._labels[key]
        response = self.query(
            "query($id:ID!,$name:String!){node(id:$id){...on Repository{labels(first:100,query:$name){nodes{id name}}}}}",
            {"id": repo[0], "name": label_name},
        )
        nodes = ((((response.get("data") or {}).get("node") or {}).get("labels") or {}).get("nodes"))
        if not isinstance(nodes, list):
            raise GitHubError("malformed GitHub repository label collection")
        for node in nodes:
            if isinstance(node, dict) and node.get("name") == label_name and isinstance(node.get("id"), str):
                self._labels[key] = node["id"]
                return node["id"]
        if not create:
            return ""
        if self.cfg.dry_run:
            return "dry-run-label"
        response = self.query(
            "mutation($r:ID!,$n:String!){createLabel(input:{repositoryId:$r,name:$n,color:\"0E8A16\"}){label{id}}}",
            {"r": repo[0], "n": label_name},
        )
        label = (((response.get("data") or {}).get("createLabel") or {}).get("label"))
        if not isinstance(label, dict) or not isinstance(label.get("id"), str):
            raise GitHubError(f"could not create GitHub label '{label_name}'")
        self._labels[key] = label["id"]
        return label["id"]

    def ensure_required_label(self, issue_id: str, scope: str, label: str) -> bool:
        self.ensure_scope(scope)
        try:
            label_id = self._label(issue_id, label, create=True)
        except GitHubError:
            return False
        return self._add_label(issue_id, label_id)

    def add_label(self, issue_id: str, scope: str, label_name: str) -> bool:
        self.ensure_scope(scope)
        try:
            label_id = self._label(issue_id, label_name, create=False)
        except GitHubError:
            return False
        if not label_id:
            journal.log(self.cfg, f"GitHub label '{label_name}' is unavailable — skipping")
            return True
        return self._add_label(issue_id, label_id)

    def _add_label(self, issue_id: str, label_id: str) -> bool:
        return self._mutate(
            "mutation($issue:ID!,$labels:[ID!]!){addLabelsToLabelable(input:{labelableId:$issue,labelIds:$labels}){labelable{__typename}}}",
            {"issue": issue_id, "labels": [label_id]}, "addLabelsToLabelable",
        )

    def add_comment(self, issue_id: str, body: str) -> bool:
        return self._mutate(
            "mutation($id:ID!,$body:String!){addComment(input:{subjectId:$id,body:$body}){commentEdge{node{id}}}}",
            {"id": issue_id, "body": body}, "addComment",
        )

    def assign_issue(self, issue_id: str) -> bool:
        if not self.cfg.assignee_id:
            return True
        return self._mutate(
            "mutation($i:ID!,$a:[ID!]!){addAssigneesToAssignable(input:{assignableId:$i,assigneeIds:$a}){assignable{__typename}}}",
            {"i": issue_id, "a": [self.cfg.assignee_id]}, "addAssigneesToAssignable",
        )

    def ci_fix_markers(self, issue_id: str) -> list[str]:
        try:
            return [sha for comment in self._comments(issue_id) for sha in _CI_FIX_MARKER.findall(comment.body)]
        except GitHubError:
            return []

    def add_ci_fix_marker(self, issue_id: str, sha: str, pr_num: int) -> bool:
        return self.add_comment(issue_id, (
            f"🔧 The **debug** loop is attempting an automated CI fix for the failing checks on PR #{pr_num} "
            f"(commit `{sha[:12]}`).\n\n<sub>agent-loop-ci-fix:{sha}</sub>"
        ))

    def approval_wait_markers(self, issue_id: str) -> tuple[dict[str, str], set[str]]:
        try:
            comments = self._comments(issue_id)
        except GitHubError:
            return {}, set()
        started: dict[str, str] = {}
        handoffs: set[str] = set()
        for comment in comments:
            for sha in _APPROVAL_WAIT_MARKER.findall(comment.body):
                if sha not in started or comment.created_at < started[sha]:
                    started[sha] = comment.created_at
            handoffs.update(_APPROVAL_WAIT_HANDOFF_MARKER.findall(comment.body))
        return started, handoffs

    def add_approval_wait_marker(self, issue_id: str, sha: str, pr_num: int, missing: list[str]) -> bool:
        return self.add_comment(issue_id, (
            f"⏳ The **debug** loop started an approval wait for PR #{pr_num} (commit `{sha[:12]}`). "
            f"It is green with no unresolved review threads, but is missing: {'; '.join(missing)}.\n\n"
            f"<sub>agent-loop-approval-wait:{sha}</sub>"
        ))

    def add_approval_wait_escalation(self, issue_id: str, sha: str, pr_num: int, minutes: int, missing: list[str], review_state: str, needs_input_state: str) -> bool:
        return self.add_comment(issue_id, (
            f"⚠️ The **debug** loop waited {minutes} minutes for approval on PR #{pr_num} "
            f"(commit `{sha[:12]}`), missing: {'; '.join(missing)}. The ticket is being handed from "
            f"**{review_state}** to **{needs_input_state}**.\n\n"
            f"<sub>agent-loop-approval-handoff:{sha}</sub>"
        ))

    def state_contract(self) -> str:
        return (
            "GitHub state contract: the configured GitHub Project Status field is the loop workflow store. "
            "Use the configured GitHub token and Project only; do not update another Project. If a write fails, "
            "report it and leave the issue's status unchanged."
        )

    def blocked_contract(self, blocked_label: str, needs_input_state: str) -> str:
        return (
            "Blocked-work safety contract: apply the repository-scoped " + blocked_label +
            " label, write one blocked-verdict comment, then move the selected GitHub Project item to " +
            needs_input_state + ". If any write fails, leave the label in place and stop."
        )

    def classifier_context(self) -> str:
        return "GitHub Issue"
