"""Boundary tests: assert the EXACT argv handed to subprocess (tmux/gh) and the EXACT GraphQL
query + variables sent to Linear.

These guard the integration seam that the loop/smoke tests mock away — the same seam that hid
the proc.capture/stderr crash. A wrong tmux quoting, a wrong gh flag, or a wrong state name in a
query is caught here without any live services.
"""

from __future__ import annotations

import json
import subprocess
import shutil
import types

import pytest

from agentloop import github, spawn
from agentloop.linear import LinearClient
from agentloop.loops import debug, declutter, deploy, design, develop, dispatch

EXECUTION_KEY = "al-linear-aaaaaaaaaaaaaaaaaaaaaaaa"


@pytest.fixture(autouse=True)
def _pinned_design_slug(monkeypatch):
    """These tests assert gh call shapes, not slug derivation (covered in test_repos).

    Deriving it for real would need a checkout with an origin remote in every fixture.
    """
    monkeypatch.setattr(design, "design_slug", lambda cfg: "example-org/sample-repo")


# ─────────────────────────── tmux spawn argv ───────────────────────────────
def test_build_spawn_cmd_exact_string(make_cfg):
    cfg = make_cfg()
    cfg.harness = "claude"
    cfg.claude_bin = "claude"
    cfg.claude_flags = "--permission-mode auto"
    assert spawn.build_spawn_cmd(cfg, "/fix-bug TEAM-1") == "claude -p --permission-mode auto '/fix-bug TEAM-1'"


def test_build_spawn_cmd_keeps_deploy_dontask_flags_verbatim(make_cfg):
    # The #1 drift risk: CLAUDE_FLAGS (with embedded single quotes) must pass through RAW so
    # tmux's `sh -c` reparses it — no shlex.quote, only the prompt is single-quoted.
    cfg = make_cfg()
    cfg.harness = "claude"
    cfg.claude_flags = deploy.DEPLOY_CLAUDE_FLAGS_DEFAULT
    got = spawn.build_spawn_cmd(cfg, "/ship-it TEAM-2")
    assert got == (
        "claude -p "
        "--allowedTools 'Bash,mcp__linear__*,mcp__linear-server__*,Read,Edit,Write,Glob,Grep,TodoWrite' "
        "--permission-mode dontAsk "
        "'/ship-it TEAM-2'"
    )


def test_build_spawn_cmd_codex_exec(make_cfg):
    cfg = make_cfg()
    cfg.harness = "codex"
    cfg.harness_bin = "codex"
    cfg.harness_flags = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"
    assert spawn.build_spawn_cmd(cfg, "/build-feature TEAM-1") == (
        "codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check "
        "'/build-feature TEAM-1'"
    )


def test_build_spawn_cmd_codex_deploy_skill(make_cfg):
    cfg = make_cfg()
    cfg.harness = "codex"
    cfg.harness_bin = "codex"
    cfg.harness_flags = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"
    assert spawn.build_spawn_cmd(cfg, "/ship-it TEAM-2") == (
        "codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check "
        "'/ship-it TEAM-2'"
    )


def test_build_spawn_cmd_codex_custom_skill_passthrough(make_cfg):
    cfg = make_cfg()
    cfg.harness = "codex"
    cfg.harness_bin = "codex"
    cfg.harness_flags = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"
    assert spawn.build_spawn_cmd(cfg, "/custom-skill TEAM-1") == (
        "codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check "
        "'/custom-skill TEAM-1'"
    )


def test_tmux_spawn_argv(make_cfg, monkeypatch):
    cfg = make_cfg(dry_run=False)
    cfg.harness = "claude"
    cfg.claude_bin = "claude"
    cfg.claude_flags = "--permission-mode auto"
    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0)
    )

    spawn.tmux_spawn(cfg, EXECUTION_KEY, "/work/tree", "/fix-bug TEAM-1")

    assert calls[0] == [
        "tmux", "new-session", "-d", "-s", EXECUTION_KEY, "-c", "/work/tree",
        "claude -p --permission-mode auto '/fix-bug TEAM-1'",
    ]
    # pipe-pane taps output to a per-session log without redirecting stdout.
    assert calls[1][:5] == ["tmux", "pipe-pane", "-o", "-t", EXECUTION_KEY]
    assert calls[1][5].startswith("cat >> '") and f"/logs/debug/{EXECUTION_KEY}-" not in calls[1][5]
    assert calls[1][5].endswith(".log'")


def test_tmux_spawn_argv_codex(make_cfg, monkeypatch):
    cfg = make_cfg(dry_run=False)
    cfg.harness = "codex"
    cfg.harness_bin = "codex"
    cfg.harness_flags = "--ask-for-approval never --sandbox danger-full-access --skip-git-repo-check"
    calls = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0)
    )

    spawn.tmux_spawn(cfg, EXECUTION_KEY, "/work/tree", "/build-feature TEAM-1")

    assert calls[0] == [
        "tmux", "new-session", "-d", "-s", EXECUTION_KEY, "-c", "/work/tree",
        "codex --ask-for-approval never --sandbox danger-full-access exec --skip-git-repo-check '/build-feature TEAM-1'",
    ]
    assert calls[1][:5] == ["tmux", "pipe-pane", "-o", "-t", EXECUTION_KEY]


def test_tmux_spawn_surfaces_new_session_failure(make_cfg, monkeypatch):
    cfg = make_cfg(dry_run=False)
    cfg.harness = "claude"
    cfg.claude_bin = "claude"
    cfg.claude_flags = "--permission-mode auto"
    calls = []

    def _run(argv, **kw):
        calls.append(argv)
        return types.SimpleNamespace(returncode=1)

    monkeypatch.setattr(subprocess, "run", _run)

    assert spawn.tmux_spawn(cfg, EXECUTION_KEY, "/work/tree", "/fix-bug TEAM-1") is False
    assert calls == [
        [
            "tmux", "new-session", "-d", "-s", EXECUTION_KEY, "-c", "/work/tree",
            "claude -p --permission-mode auto '/fix-bug TEAM-1'",
        ]
    ]


def test_dispatch_prompt_hands_pr_follow_up_to_debug_loop(make_cfg):
    client = LinearClient(make_cfg())
    prompt = dispatch.dispatch_prompt("/fix-bug", "TEAM-1232", "Ready for Review", tracker_adapter=client)

    assert client.state_contract() in prompt
    assert client.blocked_contract("loop-blocked", "Needs Input") in prompt

    assert prompt.startswith("/fix-bug\n\n")
    assert "Do NOT use the Linear MCP connector" in prompt
    assert "Linear GraphQL API at https://api.linear.app/graphql" in prompt
    assert "move TEAM-1232 to Ready for Review (via the tracker state contract above), then exit" in prompt
    assert "Do not inspect CI, run triangulation, address review comments, merge" in prompt
    assert "The debug loop owns Ready for Review follow-up." in prompt
    assert "Done requires deployed evidence" in prompt
    assert "verify both halves exist on their configured base branches" in prompt


def test_headless_prompts_have_no_apostrophes(make_cfg):
    # Headless prompts are single-quoted into the Claude spawn command with no escaping
    # (harness.build_headless_cmd, `'{prompt}'`), so any apostrophe breaks the shell command and
    # the spawned session never starts. Keep every spawn-prompt string apostrophe-free.
    from agentloop.loops.dispatch import dispatch_prompt
    from agentloop.workflows import design_review

    client = LinearClient(make_cfg())
    for text in (
        client.state_contract(),
        client.blocked_contract("loop-blocked", "Needs Input"),
        dispatch_prompt("/fix-bug", "TEAM-1", "In Review", tracker_adapter=client),
        dispatch_prompt("/build-feature", "TEAM-2", "In Review", tracker_adapter=client),
        design_review(42, "https://example.test/pull/42"),
    ):
        assert "'" not in text


def test_dispatch_prompt_has_no_repository_specific_design_contract():
    prompt = dispatch.dispatch_prompt("/build-feature", "TEAM-1300", "In Review")

    assert "@sample/ui" not in prompt
    assert "design tokens" not in prompt


def test_dispatch_prompt_carries_configured_blocked_handoff():
    prompt = dispatch.dispatch_prompt(
        "/build-feature",
        "TEAM-1300",
        blocked_label="parked-work",
        needs_input_state="Human Review",
    )

    assert "team-scoped parked-work label" in prompt
    assert "attempt to move it to the configured Human Review state" in prompt
    assert "remove parked-work and explicitly requeue" in prompt


# ─────────────────────────── gh invocation ─────────────────────────────────
def test_pr_review_state_gh_argv(monkeypatch):
    captured = {}

    def _cap(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout='{"data":{}}')

    # github.py binds `from agentloop.proc import capture`, so patch the name on github.
    monkeypatch.setattr(github, "capture", _cap)
    github.pr_review_state("acme", "widgets", 42, frozenset({"greptile-apps"}))

    argv = captured["argv"]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert "-f" in argv and argv[argv.index("-f") + 1].startswith("query=")
    assert "owner=acme" in argv
    assert "repo=widgets" in argv
    assert "comments(last:100){nodes{body createdAt author{login}} pageInfo{hasPreviousPage startCursor}}" in argv[argv.index("-f") + 1]
    # PR number is passed with -F (typed field) so gh sends it as an Int, not a String.
    assert argv[argv.index("num=42") - 1] == "-F"


def test_pr_review_state_paginates_until_greptile_comment(monkeypatch):
    calls = []

    def _cap(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "data": {"repository": {"pullRequest": {
                        "comments": {"nodes": [{"author": {"login": "human"}}],
                                      "pageInfo": {"hasPreviousPage": True, "startCursor": "cursor-2"}}
                    }}}
                }),
            )
        return types.SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "data": {"repository": {"pullRequest": {
                    "comments": {"nodes": [{"author": {"login": "greptile-apps[bot]"}}],
                                  "pageInfo": {"hasPreviousPage": False, "startCursor": "cursor-1"}}
                }}}
            }),
        )

    monkeypatch.setattr(github, "capture", _cap)
    state = github.pr_review_state("acme", "widgets", 42, frozenset({"greptile-apps"}))

    assert len(calls) == 2
    assert "comments(last:100,before:$before)" in calls[1][calls[1].index("-f") + 1]
    assert "before=cursor-2" in calls[1]
    comments = state["data"]["repository"]["pullRequest"]["comments"]["nodes"]
    assert [c["author"]["login"] for c in comments] == ["greptile-apps[bot]", "human"]


def test_design_list_recent_merged_prs_gh_argv(monkeypatch, make_cfg):
    captured = {}

    def _cap(argv, **kw):
        captured["argv"] = argv
        return types.SimpleNamespace(returncode=0, stdout="[]")

    monkeypatch.setenv("AGENT_LOOP_DESIGN_LOOKBACK_DAYS", "14")
    monkeypatch.setenv("AGENT_LOOP_DESIGN_SCAN_LIMIT", "25")
    monkeypatch.setattr(design.proc, "capture", _cap)

    design.list_recent_merged_prs(make_cfg(log_tag="design"))

    argv = captured["argv"]
    assert argv[:3] == ["gh", "pr", "list"]
    assert argv[argv.index("--repo") + 1] == "example-org/sample-repo"
    assert argv[argv.index("--state") + 1] == "merged"
    assert argv[argv.index("--limit") + 1] == "25"
    assert argv[argv.index("--search") + 1].startswith("merged:>=")
    assert argv[argv.index("--json") + 1] == "number,mergeCommit,mergedAt,url,title"


def test_design_list_recent_merged_prs_hydrates_files_and_comments_with_pagination(monkeypatch, make_cfg):
    calls = []

    def _cap(argv, **kw):
        calls.append(argv)
        if argv[:3] == ["gh", "pr", "list"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "number": 42,
                            "mergeCommit": {"oid": "abc123"},
                            "mergedAt": "2026-07-10T00:00:00Z",
                            "url": "https://github.com/example-org/sample-repo/pull/42",
                            "title": "UI change",
                        }
                    ]
                ),
            )
        if argv == ["gh", "api", "--paginate", "--slurp", "repos/example-org/sample-repo/pulls/42/files"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps([[{"filename": "apps/api/jobs/sync.py"}], [{"filename": "apps/web/app/page.tsx"}]]),
            )
        if argv == ["gh", "api", "--paginate", "--slurp", "repos/example-org/sample-repo/issues/42/comments"]:
            return types.SimpleNamespace(
                returncode=0,
                stdout=json.dumps([[{"body": "first page"}], [{"body": "agent-loop-post-merge reviewed:abc123"}]]),
            )
        raise AssertionError(f"unexpected argv: {argv}")

    monkeypatch.setattr(design.proc, "capture", _cap)

    prs = design.list_recent_merged_prs(make_cfg(log_tag="design"))

    assert len(prs) == 1
    assert prs[0].files == ["apps/api/jobs/sync.py", "apps/web/app/page.tsx"]
    assert prs[0].comments == ["first page", "agent-loop-post-merge reviewed:abc123"]
    assert calls[1] == ["gh", "api", "--paginate", "--slurp", "repos/example-org/sample-repo/pulls/42/files"]
    assert calls[2] == ["gh", "api", "--paginate", "--slurp", "repos/example-org/sample-repo/issues/42/comments"]


def test_design_claim_marker_posts_github_issue_comment_before_spawn(monkeypatch, make_cfg):
    calls = []
    cfg = make_cfg(log_tag="design", dry_run=False)
    pr = design.MergedPR(
        number=42,
        title="UI change",
        url="https://github.com/example-org/sample-repo/pull/42",
        merge_sha="abc123",
        merged_at="2026-07-10T00:00:00Z",
        files=["apps/web/app/page.tsx"],
        comments=[],
    )

    def _run_gated(cfg, argv, **kw):
        calls.append(("run_gated", argv))
        return types.SimpleNamespace(returncode=0)

    def _tmux_spawn(cfg, key, cwd, prompt):
        calls.append(("tmux_spawn", key, prompt))
        return True

    monkeypatch.setattr(design.proc, "run_gated", _run_gated)
    monkeypatch.setattr(design.proc, "has_session", lambda key: False)
    monkeypatch.setattr(design.spawn, "tmux_spawn", _tmux_spawn)

    assert design.spawn_design_review(cfg, pr) is True

    assert calls[0][0] == "run_gated"
    argv = calls[0][1]
    assert argv[:3] == ["gh", "api", "repos/example-org/sample-repo/issues/42/comments"]
    assert "-f" in argv
    body = argv[argv.index("-f") + 1]
    assert "body=agent-loop-design-claimed:abc123" in body
    assert "agent-loop-design-reviewed:abc123" in body
    assert calls[1][:2] == ("tmux_spawn", "design-42")
    assert calls[1][2].startswith("Review the merged pull request #42")
    assert "Do NOT use the Linear MCP connector" in calls[1][2]


def test_design_claim_marker_rolls_back_when_spawn_fails(monkeypatch, make_cfg):
    calls = []
    cfg = make_cfg(log_tag="design", dry_run=False)
    pr = design.MergedPR(
        number=42,
        title="UI change",
        url="https://github.com/example-org/sample-repo/pull/42",
        merge_sha="abc123",
        merged_at="2026-07-10T00:00:00Z",
        files=["apps/web/app/page.tsx"],
        comments=[],
    )

    def _run_gated(cfg, argv, **kw):
        calls.append(("run_gated", argv))
        if argv[:3] == ["gh", "api", "repos/example-org/sample-repo/issues/42/comments"]:
            return types.SimpleNamespace(returncode=0, stdout='{"id": 987}')
        return types.SimpleNamespace(returncode=0)

    def _tmux_spawn(cfg, key, cwd, prompt):
        calls.append(("tmux_spawn", key, prompt))
        return False

    monkeypatch.setattr(design.proc, "run_gated", _run_gated)
    monkeypatch.setattr(design.proc, "has_session", lambda key: False)
    monkeypatch.setattr(design.spawn, "tmux_spawn", _tmux_spawn)

    assert design.spawn_design_review(cfg, pr) is False

    assert calls[0][0] == "run_gated"
    assert calls[1][:2] == ("tmux_spawn", "design-42")
    assert calls[1][2].startswith("Review the merged pull request #42")
    assert "Do NOT use the Linear MCP connector" in calls[1][2]
    assert calls[2] == (
        "run_gated",
        ["gh", "api", "-X", "DELETE", "repos/example-org/sample-repo/issues/comments/987"],
    )


def test_design_claim_marker_rolls_back_when_spawn_raises(monkeypatch, make_cfg):
    calls = []
    cfg = make_cfg(log_tag="design", dry_run=False)
    pr = design.MergedPR(
        number=43,
        title="UI change",
        url="https://github.com/example-org/sample-repo/pull/43",
        merge_sha="def456",
        merged_at="2026-07-10T00:00:00Z",
        files=["apps/web/app/page.tsx"],
        comments=[],
    )

    def _run_gated(cfg, argv, **kw):
        calls.append(("run_gated", argv))
        if argv[:3] == ["gh", "api", "repos/example-org/sample-repo/issues/43/comments"]:
            return types.SimpleNamespace(returncode=0, stdout='{"id": 988}')
        return types.SimpleNamespace(returncode=0)

    def _tmux_spawn(cfg, key, cwd, prompt):
        calls.append(("tmux_spawn", key, prompt))
        raise RuntimeError("tmux unavailable")

    monkeypatch.setattr(design.proc, "run_gated", _run_gated)
    monkeypatch.setattr(design.proc, "has_session", lambda key: False)
    monkeypatch.setattr(design.spawn, "tmux_spawn", _tmux_spawn)

    assert design.spawn_design_review(cfg, pr) is False

    assert calls[1][:2] == ("tmux_spawn", "design-43")
    assert calls[1][2].startswith("Review the merged pull request #43")
    assert "Do NOT use the Linear MCP connector" in calls[1][2]
    assert calls[2] == (
        "run_gated",
        ["gh", "api", "-X", "DELETE", "repos/example-org/sample-repo/issues/comments/988"],
    )


# ─────────────────── Linear mutation query + variables ──────────────────────
def _recording_client(make_cfg):
    cfg = make_cfg(dry_run=False)
    client = LinearClient(cfg)
    sent = []
    client.query = lambda q, v=None: sent.append((q, v)) or {"data": {"mutation": {"success": True}}}
    return client, sent


def test_move_state_mutation_and_variables(make_cfg):
    client, sent = _recording_client(make_cfg)
    client.state_id["team-1|In Progress"] = "sid-9"
    ok = client.move_state("iss-1", "team-1", "In Progress")
    assert ok is True
    q, v = sent[0]
    assert q == "mutation($id:String!,$s:String!){issueUpdate(id:$id,input:{stateId:$s}){success}}"
    assert v == {"id": "iss-1", "s": "sid-9"}


def test_move_state_unknown_state_does_not_call_api(make_cfg):
    client, sent = _recording_client(make_cfg)
    ok = client.move_state("iss-1", "team-1", "Nonexistent")
    assert ok is False
    assert sent == []  # no state id -> no mutation issued


def test_add_label_mutation_and_variables(make_cfg):
    client, sent = _recording_client(make_cfg)
    client.label_id["team-1|loop-wip"] = "lid-3"
    client.add_label("iss-1", "team-1", "loop-wip")
    q, v = sent[0]
    assert q == "mutation($id:String!,$l:String!){issueAddLabel(id:$id,labelId:$l){success}}"
    assert v == {"id": "iss-1", "l": "lid-3"}


def test_add_comment_mutation_and_variables(make_cfg):
    client, sent = _recording_client(make_cfg)
    client.add_comment("iss-1", "hello **world**")
    q, v = sent[0]
    assert q == "mutation($id:String!,$b:String!){commentCreate(input:{issueId:$id,body:$b}){success}}"
    assert v == {"id": "iss-1", "b": "hello **world**"}


def test_mutation_success_false_is_reported_to_callers(make_cfg):
    cfg = make_cfg(dry_run=False)
    client = LinearClient(cfg)
    client.query = lambda q, v=None: {"data": {"commentCreate": {"success": False}}}

    assert client.add_approval_wait_escalation(
        "iss-1", "abc123def456", 56, 60, ["no Codex 👍 reaction"], "In Review", "Needs Input",
    ) is False


def test_approval_wait_markers_follow_comment_pagination(make_cfg):
    client, sent = _recording_client(make_cfg)
    responses = iter([
        {"data": {"issue": {"comments": {"nodes": [
            {"body": "agent-loop-approval-wait:abc1234", "createdAt": "2026-08-08T10:00:00Z"},
        ], "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"}}}}},
        {"data": {"issue": {"comments": {"nodes": [
            {"body": "agent-loop-approval-handoff:def5678", "createdAt": "2026-08-08T11:00:00Z"},
        ], "pageInfo": {"hasNextPage": False, "endCursor": "cursor-2"}}}}},
    ])
    client.query = lambda q, v=None: sent.append((q, v)) or next(responses)

    started, handoffs = client.approval_wait_markers("iss-1")

    assert started == {"abc1234": "2026-08-08T10:00:00Z"}
    assert handoffs == {"def5678"}
    assert len(sent) == 2
    assert sent[0][1] == {"id": "iss-1", "after": None}
    assert sent[1][1] == {"id": "iss-1", "after": "cursor-1"}


# ─────────────── each loop's list query sends the right filter ──────────────
class _QueryRecorder(LinearClient):
    def __init__(self, nodes=None):
        super().__init__(None)
        self.loop_ids = ["p1", "p2"]
        self.sent = []
        self._nodes = nodes or []

    def query(self, q, v=None):
        self.sent.append((q, v))
        return {"data": {"issues": {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": self._nodes}}}


def test_count_active_query_variables(make_cfg):
    c = _QueryRecorder()
    dispatch.count_active(c, make_cfg())
    q, v = c.sent[0]
    assert "issues(first:25,after:$after" in q
    assert "pageInfo{hasNextPage endCursor}" in q
    assert "state:{name:{in:$s}}" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["Todo", "In Progress", "In Review"]}


def test_list_ready_query_variables(make_cfg):
    c = _QueryRecorder()
    dispatch.list_ready(c, make_cfg())
    q, v = c.sent[0]
    assert "children(first:1)" in q  # umbrella detection field
    assert "attachments(first:15)" in q  # PR-backed queued issues are repaired, not dispatched
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["Backlog"]}


def test_list_queued_prs_query_variables(make_cfg):
    c = _QueryRecorder()
    dispatch.list_queued_prs(c, make_cfg())
    q, v = c.sent[0]
    assert "attachments(first:15)" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["Backlog"]}


def test_list_development_query_variables(make_cfg):
    c = _QueryRecorder()
    develop.list_development(c, make_cfg())
    q, v = c.sent[0]
    assert "attachments(first:15)" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["Todo"]}


def test_list_active_development_query_variables(make_cfg):
    c = _QueryRecorder()
    develop.list_active_development(c, make_cfg())
    q, v = c.sent[0]
    assert "attachments(first:15)" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["In Progress"]}


def test_list_active_development_keeps_pr_backed_rows_for_capacity(make_cfg):
    c = _QueryRecorder(
        nodes=[
            {
                "id": "id-1",
                "identifier": "TEAM-1",
                "title": "Active with PR",
                "team": {"id": "team"},
                "labels": {"nodes": [{"name": "loop-wip"}, {"name": "repo:sample-repo"}]},
                "attachments": {"nodes": [{"url": "https://github.com/o/r/pull/1"}]},
            }
        ]
    )

    rows = develop.list_active_development(c, make_cfg())

    assert [r.identifier for r in rows] == ["TEAM-1"]


def test_list_approved_query_variables(make_cfg):
    c = _QueryRecorder()
    deploy.list_approved(c, make_cfg())
    q, v = c.sent[0]
    assert "attachments(first:15)" in q
    assert "comments(last:100)" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["Approved"]}


def test_pr_deploy_gate_state_gh_argv(monkeypatch):
    calls = []
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/gh")
    monkeypatch.setattr(
        subprocess, "run",
        lambda argv, **kw: calls.append(argv) or types.SimpleNamespace(returncode=0, stdout=b'{"data":{}}', stderr=b""),
    )

    github.pr_deploy_gate_state("acme", "widgets", 43)

    argv = calls[0]
    assert argv[:3] == ["gh", "api", "graphql"]
    assert "-f" in argv and "owner=acme" in argv and "repo=widgets" in argv
    assert "comments(last:100){nodes{body createdAt author{login}} pageInfo{hasPreviousPage startCursor}}" in argv[argv.index("-f") + 1]
    assert "-F" in argv and "num=43" in argv


def test_pr_deploy_gate_state_missing_gh_returns_none(monkeypatch):
    def missing(*_args, **_kwargs):
        raise FileNotFoundError("gh")

    monkeypatch.setattr(subprocess, "run", missing)

    assert github.pr_deploy_gate_state("acme", "widgets", 43) is None


def test_deploy_blockers_detect_unresolved_current_thread():
    st = {"data": {"repository": {"pullRequest": {
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "reviewDecision": None,
        "commits": {"nodes": [{"commit": {"statusCheckRollup": {"state": "SUCCESS"}}}]},
        "reviewThreads": {"nodes": [{"isResolved": False, "isOutdated": False}]},
    }}}}
    assert deploy.deploy_blockers(st) == ["1 unresolved current review thread(s)"]


def test_list_in_review_query_variables(make_cfg):
    c = _QueryRecorder()
    debug.list_in_review(c, make_cfg())
    q, v = c.sent[0]
    assert "attachments(first:15)" in q  # PR-url source
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["In Review"]}


def test_list_in_review_skips_blocked_pr(make_cfg):
    node = {
        "id": "id-1",
        "identifier": "TEAM-1",
        "team": {"id": "team"},
        "labels": {"nodes": [{"name": "loop-wip"}, {"name": "loop-blocked"}]},
        "attachments": {"nodes": [{"url": "https://github.com/o/r/pull/1"}]},
    }

    assert debug.list_in_review(_QueryRecorder([node]), make_cfg()) == []


def test_list_stuck_query_variables(make_cfg):
    c = _QueryRecorder()
    declutter.list_stuck(c, make_cfg())
    q, v = c.sent[0]
    assert "startedAt" in q  # grace-window field
    assert "startedAtlabels" not in q
    assert "startedAt labels" in q
    assert v == {"ids": ["p1", "p2"], "after": None, "s": ["In Progress"]}


def test_list_stuck_flags_pr_backed_issues(make_cfg):
    """PR-backed In-Progress issues are no longer dropped — they come back flagged has_pr=True so
    declutter_one advances them to In Review instead of reaping them to Needs Input."""
    old = "2000-01-01T00:00:00+00:00"  # well past any grace window (offset form parses on 3.9+ too)

    def node(ident, pr):
        return {
            "id": f"id-{ident}", "identifier": ident, "team": {"id": "t"}, "startedAt": old,
            "labels": {"nodes": [{"name": "loop-wip"}]},
            "attachments": {"nodes": ([{"url": "https://github.com/o/r/pull/1"}] if pr else [])},
        }

    c = _QueryRecorder([node("TEAM-A", True), node("TEAM-B", False)])
    rows = {r.identifier: r for r in declutter.list_stuck(c, make_cfg())}
    assert set(rows) == {"TEAM-A", "TEAM-B"}  # PR-backed one is NOT dropped
    assert rows["TEAM-A"].has_pr is True
    assert rows["TEAM-B"].has_pr is False


def test_worktree_holding_branch(tmp_path):
    """worktree_holding_branch finds the worktree that has a branch checked out — the lookup the
    debug fix path uses to reclaim a PR branch a build-skill worktree left checked out elsewhere."""
    import os
    from pathlib import Path

    from agentloop import worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    git("init", "-q")
    git("commit", "--allow-empty", "-q", "-m", "init")
    git("branch", "feature/x")
    # Simulate the build skill's leftover worktree: feature/x checked out in a SIBLING dir.
    other = tmp_path / "skill-worktrees" / "team-9"
    git("worktree", "add", "-q", str(other), "feature/x")

    held = worktree.worktree_holding_branch(repo, "feature/x")
    assert held is not None and os.path.samefile(held, other)  # found the holder
    git("branch", "feature/y")
    assert worktree.worktree_holding_branch(repo, "feature/y") is None  # not checked out anywhere
    assert worktree.worktree_holding_branch(repo, "no-such-branch") is None


def test_dispatch_branch_name_uses_lowercase_linear_key(make_cfg):
    from agentloop.loops import dispatch

    # Pin the namespace: the default now derives from the invoking user, and this test
    # is about the key/slug half of the branch name.
    cfg = make_cfg(branch_prefix="alice/")
    key = "al-linear-0123456789abcdef01234567"
    assert dispatch.branch_name(cfg, key, "Unify Loop Worktrees!") == f"alice/{key}-unify-loop-worktrees"
