"""Read-only GitHub Project selection, isolated from the executable loop config.

Every network document is a query. No loop, local checkout, claim, or mutation is
involved; eligibility is a snapshot, not a reservation of work.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

from agentloop.github_tracker import GitHubClient, GitHubError


@dataclass(frozen=True)
class PreviewProfile:
    owner: str
    number: int
    repositories: tuple[str, ...]
    assignee: str
    repository_routes: dict[str, str]
    status_field: str = "Status"
    ready_status: str = "Ready"
    label: str = "loop"
    inherit_parent: bool = False
    milestones: tuple[str, ...] = ()


def _repository(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[\w.-]+/[\w.-]+", value, re.ASCII) is not None


def load_profile(path: Path) -> PreviewProfile:
    """Reject unknown settings rather than silently weakening the scope."""
    try:
        data = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"could not read preview profile: {error}") from error
    if data.keys() - {"project", "selection", "repository_routes"}:
        raise ValueError("unknown preview profile section")
    project = data.get("project", {})
    selection = data.get("selection", {})
    routes = data.get("repository_routes", {})
    if not all(isinstance(table, dict) for table in (project, selection, routes)):
        raise ValueError("profile sections must be TOML tables")
    if project.keys() - {"owner", "number", "repositories", "status_field"}:
        raise ValueError("unknown project setting")
    if selection.keys() - {"assignee", "ready_status", "label", "inherit_parent", "milestones"}:
        raise ValueError("unknown selection setting")
    owner, number = project.get("owner"), project.get("number")
    repositories = project.get("repositories")
    assignee = selection.get("assignee")
    if not isinstance(owner, str) or not owner.strip() or type(number) is not int or number <= 0:
        raise ValueError("project requires a nonempty owner and positive number")
    if not isinstance(repositories, list) or not repositories or not all(_repository(repo) for repo in repositories):
        raise ValueError("project.repositories must be a nonempty list of owner/repository names")
    repositories = [repo.lower() for repo in repositories]
    if len(set(repositories)) != len(repositories):
        raise ValueError("project.repositories contains duplicates")
    if not isinstance(assignee, str) or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", assignee) is None:
        raise ValueError("selection.assignee must be a GitHub username")
    normalized_routes = {}
    for source, target in routes.items():
        if not _repository(source) or not _repository(target) or source.lower() in normalized_routes:
            raise ValueError("repository_routes requires unique source and target owner/repository names")
        normalized_routes[source.lower()] = target
    if set(normalized_routes) != set(repositories):
        raise ValueError("repository_routes must map every allowed issue repository exactly once")
    strings = {
        "status_field": project.get("status_field", "Status"),
        "ready_status": selection.get("ready_status", "Ready"),
        "label": selection.get("label", "loop"),
    }
    if any(not isinstance(value, str) or not value.strip() for value in strings.values()):
        raise ValueError("status_field, ready_status, and label must be nonempty strings")
    inherit = selection.get("inherit_parent", False)
    if type(inherit) is not bool:
        raise ValueError("selection.inherit_parent must be a boolean")
    milestones = selection.get("milestones", [])
    if not isinstance(milestones, list):
        raise ValueError("selection.milestones must be a list of owner/repository#number values")
    normalized_milestones = []
    for value in milestones:
        repo, sep, milestone = value.rpartition("#") if isinstance(value, str) else ("", "", "")
        if not sep or repo.lower() not in repositories or not milestone.isascii() or not milestone.isdecimal() or int(milestone) <= 0:
            raise ValueError("milestones must name an allowed issue repository and positive milestone number")
        normalized_milestones.append(f"{repo.lower()}#{int(milestone)}")
    return PreviewProfile(
        owner=owner, number=number, repositories=tuple(repositories), assignee=assignee,
        repository_routes=normalized_routes, inherit_parent=inherit,
        milestones=tuple(normalized_milestones), **strings,
    )


_ITEMS_QUERY = """
query($id: ID!, $status: String!, $after: String) {
  node(id: $id) {
    ... on ProjectV2 {
      items(first: 100, after: $after) {
        nodes {
          id type isArchived
          fieldValueByName(name: $status) {
            ... on ProjectV2ItemFieldSingleSelectValue { name }
          }
          content {
            ... on Issue {
              id number title url state
              repository { nameWithOwner }
              labels(first: 100) { nodes { name } pageInfo { hasNextPage endCursor } }
              assignees(first: 100) { nodes { login } pageInfo { hasNextPage endCursor } }
              milestone { number }
              parent { id }
              subIssues(first: 1) { totalCount }
              blockedBy(first: 100) { nodes { state } pageInfo { hasNextPage endCursor } }
            }
          }
        }
        pageInfo { hasNextPage endCursor }
      }
    }
  }
}
"""


@dataclass(frozen=True)
class Candidate:
    id: str
    identifier: str
    title: str
    url: str
    repository: str
    state: str
    status: str
    labels: tuple[str, ...]
    assignees: tuple[str, ...]
    milestone: str | None
    parent: str | None
    has_children: bool
    blocked: bool
    archived: bool


@dataclass(frozen=True)
class Decision:
    identifier: str
    url: str
    eligible: bool
    reasons: tuple[str, ...]
    code_repository: str | None
    work_root: str | None
    milestone: str | None

    def to_dict(self) -> dict:
        return asdict(self)


def _connection(node: dict, name: str, key: str) -> tuple[str, ...]:
    connection = node.get(name)
    GitHubClient._require_complete_connection(connection, name)
    nodes = connection.get("nodes")
    if not isinstance(nodes, list) or any(
        not isinstance(item, dict) or not isinstance(item.get(key), str) for item in nodes
    ):
        raise GitHubError(f"malformed preview {name}")
    return tuple(item[key] for item in nodes)


def _candidate(item: dict) -> Candidate:
    node = item.get("content")
    if not isinstance(node, dict) or any(
        not isinstance(node.get(key), str) or not node[key] for key in ("id", "title", "url", "state")
    ) or type(node.get("number")) is not int:
        raise GitHubError("malformed preview issue")
    repository = node.get("repository")
    if not isinstance(repository, dict) or not _repository(repository.get("nameWithOwner")):
        raise GitHubError("malformed preview issue repository")
    repo = repository["nameWithOwner"]
    status = item.get("fieldValueByName")
    if status is not None and (not isinstance(status, dict) or not isinstance(status.get("name"), str)):
        raise GitHubError("malformed preview issue status")
    if type(item.get("isArchived")) is not bool:
        raise GitHubError("malformed preview archive flag")
    sub_issues = node.get("subIssues")
    if not isinstance(sub_issues, dict) or type(sub_issues.get("totalCount")) is not int or sub_issues["totalCount"] < 0:
        raise GitHubError("malformed preview sub-issue count")
    if "parent" not in node or "milestone" not in node:
        raise GitHubError("missing preview parent or milestone metadata")
    parent, milestone = node["parent"], node["milestone"]
    if parent is not None and (not isinstance(parent, dict) or not isinstance(parent.get("id"), str)):
        raise GitHubError("malformed preview parent")
    if milestone is not None and (not isinstance(milestone, dict) or type(milestone.get("number")) is not int or milestone["number"] <= 0):
        raise GitHubError("malformed preview milestone")
    blockers = _connection(node, "blockedBy", "state")
    if node["state"] not in {"OPEN", "CLOSED"} or any(state not in {"OPEN", "CLOSED"} for state in blockers):
        raise GitHubError("malformed preview issue/blocker state")
    return Candidate(
        id=node["id"], identifier=f"{repo}#{node['number']}", title=node["title"], url=node["url"],
        repository=repo.lower(), state=node["state"], status=status["name"] if status else "",
        labels=_connection(node, "labels", "name"), assignees=_connection(node, "assignees", "login"),
        milestone=f"{repo.lower()}#{milestone['number']}" if milestone else None,
        parent=parent["id"] if parent else None, has_children=sub_issues["totalCount"] > 0,
        blocked="OPEN" in blockers, archived=item["isArchived"],
    )


def evaluate(profile: PreviewProfile, candidates: list[Candidate]) -> list[Decision]:
    """Explain every Issue item; inheritance is from the immediate parent only.

    Both parent and leaf must be visible in this Project. Partial hierarchy data
    cannot authorize work, and neither a foreign assignee nor a blocked parent
    can be overridden by a child label.
    """
    indexed = {issue.id: issue for issue in candidates}
    if len(indexed) != len(candidates):
        raise GitHubError("duplicate issue in preview Project")
    decisions = []
    user = profile.assignee.lower()
    for issue in candidates:
        reasons = []
        parent = indexed.get(issue.parent) if issue.parent else None
        inherited = None
        if issue.archived:
            reasons.append("archived")
        if issue.repository not in profile.repositories:
            reasons.append("repository_not_allowed")
        if issue.state != "OPEN":
            reasons.append("closed")
        if issue.status != profile.ready_status:
            reasons.append("not_ready")
        if issue.has_children:
            reasons.append("has_sub_issues")
        if issue.blocked:
            reasons.append("open_blocker")
        if any(assignee.lower() != user for assignee in issue.assignees):
            reasons.append("assigned_to_another_user")
        if issue.parent:
            if parent is None:
                reasons.append("parent_outside_project")
            else:
                if any(assignee.lower() != user for assignee in parent.assignees):
                    reasons.append("parent_assigned_to_another_user")
                if parent.blocked:
                    reasons.append("parent_open_blocker")
                if parent.archived or parent.state != "OPEN":
                    reasons.append("parent_inactive")
                if (
                    profile.inherit_parent and parent.repository in profile.repositories
                    and parent.status == profile.ready_status and not parent.archived
                    and parent.state == "OPEN" and not parent.blocked
                    and profile.label in parent.labels
                    and {name.lower() for name in parent.assignees} == {user}
                ):
                    inherited = parent
        if profile.label not in issue.labels and inherited is None:
            reasons.append("missing_loop_label")
        if user not in {name.lower() for name in issue.assignees} and inherited is None:
            reasons.append("not_assigned_to_operator")
        milestone = issue.milestone or (inherited.milestone if inherited else None)
        if profile.milestones and milestone not in profile.milestones:
            reasons.append("milestone_not_selected")
        route = profile.repository_routes.get(issue.repository)
        if not route:
            reasons.append("no_repository_route")
        decisions.append(Decision(
            identifier=issue.identifier, url=issue.url, eligible=not reasons, reasons=tuple(reasons),
            code_repository=route, work_root=inherited.identifier if inherited else None, milestone=milestone,
        ))
    return decisions


class PreviewClient:
    """Use the existing HTTP transport through a query-only boundary."""

    def __init__(self, token: str):
        self._transport = GitHubClient(SimpleNamespace(github_token=token, log_tag="preview"))

    def _read(self, document: str, variables: dict | None = None) -> dict:
        if re.match(r"\s*query\b", document) is None or re.search(r"\bmutation\b", document):
            raise GitHubError("preview accepts GraphQL queries only")
        return self._transport.query(document, variables)

    def run(self, profile: PreviewProfile) -> list[Decision]:
        response = self._read("query { viewer { login } }")
        login = ((response.get("data") or {}).get("viewer") or {}).get("login")
        if not isinstance(login, str) or login.lower() != profile.assignee.lower():
            raise GitHubError("authenticated GitHub user must match the profile assignee")
        response = self._read(
            "query($owner:String!,$number:Int!){repositoryOwner(login:$owner){"
            "...on Organization{projectV2(number:$number){id}}"
            "...on User{projectV2(number:$number){id}}}}",
            {"owner": profile.owner, "number": profile.number},
        )
        project = (((response.get("data") or {}).get("repositoryOwner") or {}).get("projectV2"))
        if not isinstance(project, dict) or not isinstance(project.get("id"), str):
            raise GitHubError("preview Project not found or inaccessible")
        fields = self._pages(
            "query($id:ID!,$after:String){node(id:$id){...on ProjectV2{fields(first:100,after:$after){"
            "nodes{...on ProjectV2SingleSelectField{name options{name}}}"
            "pageInfo{hasNextPage endCursor}}}}}",
            {"id": project["id"]}, "fields",
        )
        matches = [field for field in fields if field.get("name") == profile.status_field]
        if len(matches) != 1 or not isinstance(matches[0].get("options"), list):
            raise GitHubError("preview requires exactly one matching single-select status field")
        options = matches[0]["options"]
        if any(not isinstance(option, dict) or not isinstance(option.get("name"), str) for option in options):
            raise GitHubError("malformed preview status options")
        if not any(option["name"] == profile.ready_status for option in options):
            raise GitHubError("preview ready_status is absent from the selected Project field")
        items = self._pages(_ITEMS_QUERY, {"id": project["id"], "status": profile.status_field}, "items")
        return evaluate(profile, [
            _candidate(item) for item in items if item.get("type") == "ISSUE"
        ])

    def _pages(self, document: str, variables: dict, name: str) -> list[dict]:
        cursor = None
        seen: set[str] = set()
        nodes = []
        while True:
            response = self._read(document, {**variables, "after": cursor})
            connection = ((response.get("data") or {}).get("node") or {}).get(name)
            if not isinstance(connection, dict) or not isinstance(connection.get("nodes"), list):
                raise GitHubError(f"malformed preview {name} collection")
            if any(not isinstance(node, dict) for node in connection["nodes"]):
                raise GitHubError(f"inaccessible or malformed preview {name} item")
            nodes.extend(connection["nodes"])
            cursor = GitHubClient._next_cursor(connection, seen, f"preview {name}")
            if cursor is None:
                return nodes
