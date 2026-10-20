"""decide() — classify a PR's gh review state into exactly one action."""

from __future__ import annotations

from agentloop.loops.debug import DebugConfig
from agentloop.loops.debug import decide as _decide

#: Both bot gates ship as presets but default to off, so a fresh install needs a human
#: approving review. These tests are about the preset behavior and opt in explicitly.
SCORE_BOTS = frozenset({"greptile-apps"})
APPROVAL_REACTORS = frozenset({"chatgpt-codex-connector"})


def decide(
    pr_json,
    require_approval,
    greptile_min_score=4,
    approval_reactors=APPROVAL_REACTORS,
    score_bot_authors=SCORE_BOTS,
):
    return _decide(
        pr_json, require_approval, greptile_min_score, approval_reactors, score_bot_authors
    )


def _pr(**pr):
    return {"data": {"repository": {"pullRequest": pr}}}


def _state(
    state="OPEN",
    checks="SUCCESS",
    head="2020-01-01T00:00:00Z",
    threads=None,
    decision="APPROVED",
    body=None,
    reactions=None,
    comments=None,
):
    return _pr(
        state=state,
        reviewDecision=decision,
        headRefName="feature/x",
        body=body,
        reactions={"nodes": reactions or []},
        comments={"nodes": comments or []},
        commits={"nodes": [{"commit": {"committedDate": head, "statusCheckRollup": {"state": checks}}}]},
        reviewThreads={"nodes": threads or []},
    )


def _thread(resolved, created, outdated=False):
    return {"isResolved": resolved, "isOutdated": outdated, "comments": {"nodes": [{"createdAt": created}]}}


def test_pr_none_is_error():
    assert decide(_pr() and {"data": {"repository": {"pullRequest": None}}}, True) == "error"


def test_merged():
    assert decide(_state(state="MERGED"), True) == "merged"


def test_closed():
    assert decide(_state(state="CLOSED"), True) == "closed"


def test_fix_when_unresolved_thread_newer_than_head():
    st = _state(head="2020-01-01T00:00:00Z", threads=[_thread(False, "2020-01-02T00:00:00Z")])
    assert decide(st, True) == "fix"


def test_fix_when_unresolved_current_thread_older_than_head():
    st = _state(head="2020-01-05T00:00:00Z", threads=[_thread(False, "2020-01-02T00:00:00Z")])
    assert decide(st, True) == "fix"


def test_no_fix_when_unresolved_outdated_thread_older_than_head():
    st = _state(head="2020-01-05T00:00:00Z", threads=[_thread(False, "2020-01-02T00:00:00Z", outdated=True)])
    assert decide(st, True) == "wait"


def test_resolved_thread_ignored_and_approves():
    st = _state(threads=[_thread(True, "2020-02-02T00:00:00Z")])
    assert decide(st, True) == "approve"


def test_approve_requires_approval_when_flag_set():
    st = _state(decision="REVIEW_REQUIRED", threads=[])
    assert decide(st, True) == "wait"  # green + no threads but no approving review


def test_approve_without_approval_when_flag_unset():
    st = _state(decision="REVIEW_REQUIRED", threads=[], body="not a Greptile review")
    assert decide(st, False) == "approve"  # green + no threads is enough


def _reaction(login="chatgpt-codex-connector[bot]", created="2020-01-02T00:00:00Z"):
    return {"content": "THUMBS_UP", "user": {"login": login}, "createdAt": created}


def _greptile_comment(score, created="2020-01-02T00:00:00Z", login="greptile-apps[bot]"):
    return {
        "body": f"<h3>Greptile Summary</h3><h3>Confidence Score: {score}/5</h3>",
        "createdAt": created,
        "author": {"login": login},
    }


def test_approve_with_greptile_score_in_fresh_allowlisted_comment_only():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[_greptile_comment(5)],
        reactions=[_reaction()],
    )
    assert decide(st, True) == "approve"


def test_approve_with_greptile_score_and_fresh_codex_reaction():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 5/5</h3>",
        reactions=[_reaction()],
    )
    assert decide(st, True) == "approve"


def test_legacy_body_score_still_approves():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 5/5</h3>",
        reactions=[_reaction()],
    )
    assert decide(st, True) == "approve"


def test_greptile_comment_wins_over_legacy_body_score():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 5/5</h3>",
        comments=[_greptile_comment(4)],
        reactions=[_reaction()],
    )
    assert decide(st, True, greptile_min_score=5) == "wait"


def test_newest_allowlisted_greptile_comment_wins():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[
            _greptile_comment(5, created="2020-01-02T00:00:00Z"),
            _greptile_comment(3, created="2020-01-03T00:00:00Z"),
        ],
        reactions=[_reaction(created="2020-01-04T00:00:00Z")],
    )
    assert decide(st, True) == "wait"


def test_approve_at_default_greptile_threshold_with_fresh_codex_reaction():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>",
        reactions=[_reaction()],
    )
    assert decide(st, True) == "approve"


def test_debug_config_reads_score_and_reactor_allowlist():
    dcfg = DebugConfig.from_env(
        None,
        environ={
            "AGENT_LOOP_DEBUG_GREPTILE_MIN_SCORE": "5",
            "AGENT_LOOP_DEBUG_APPROVAL_REACTORS": " ChatGPT-Codex-Connector[bot], another-bot ",
            "AGENT_LOOP_DEBUG_APPROVAL_WAIT_MINUTES": "90",
        },
    )
    assert dcfg.greptile_min_score == 5
    assert dcfg.approval_reactors == {"chatgpt-codex-connector", "another-bot"}
    assert dcfg.approval_wait_minutes == 90


def test_wait_with_low_greptile_score_even_with_codex_reaction():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[_greptile_comment(3)],
        reactions=[_reaction()],
    )
    assert decide(st, True) == "wait"


def test_wait_with_greptile_score_but_no_codex_reaction():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>",
    )
    assert decide(st, True) == "wait"


def test_wait_with_codex_reaction_older_than_head():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>",
        reactions=[_reaction(created="2019-12-31T23:59:59Z")],
    )
    assert decide(st, True) == "wait"


def test_wait_with_reaction_from_non_allowlisted_login():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<!-- greptile_comment --><h3>Confidence Score: 4/5</h3>",
        reactions=[_reaction(login="greptile-app[bot]")],
    )
    assert decide(st, True) == "wait"


def test_wait_with_missing_greptile_marker():
    st = _state(
        decision="REVIEW_REQUIRED",
        body="<h3>Confidence Score: 5/5</h3>",
        reactions=[_reaction()],
    )
    assert decide(st, True) == "wait"


def test_wait_with_greptile_comment_from_non_allowlisted_author():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[_greptile_comment(5, login="someone-with-read-access")],
        reactions=[_reaction()],
    )
    assert decide(st, True) == "wait"


def test_wait_with_greptile_comment_older_than_head():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[_greptile_comment(5, created="2019-12-31T23:59:59Z")],
        reactions=[_reaction()],
    )
    assert decide(st, True) == "wait"


def test_wait_with_unparseable_allowlisted_greptile_comment():
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[{
            "body": "<h3>Greptile Summary</h3>",
            "createdAt": "2020-01-02T00:00:00Z",
            "author": {"login": "greptile-apps[bot]"},
        }],
        reactions=[_reaction()],
    )
    assert decide(st, True) == "wait"


def test_wait_with_no_greptile_comment_or_legacy_body_score():
    st = _state(decision="REVIEW_REQUIRED", comments=[], reactions=[_reaction()])
    assert decide(st, True) == "wait"


def test_formal_approval_still_approves_without_bot_signals():
    assert decide(_state(decision="APPROVED", body=None, reactions=[]), True) == "approve"


def test_merge_state_blocker_takes_precedence_over_approve():
    st = _state(threads=[])
    pr = st["data"]["repository"]["pullRequest"]
    pr["mergeable"] = "MERGEABLE"
    pr["mergeStateStatus"] = "BEHIND"

    assert decide(st, True) == "merge_fix"


def test_conflicting_pr_takes_precedence_over_approve():
    st = _state(threads=[])
    pr = st["data"]["repository"]["pullRequest"]
    pr["mergeable"] = "CONFLICTING"
    pr["mergeStateStatus"] = "DIRTY"

    assert decide(st, True) == "merge_fix"


def test_ci_fix_takes_precedence_over_unstable_merge_state():
    st = _state(checks="FAILURE", threads=[])
    pr = st["data"]["repository"]["pullRequest"]
    pr["mergeStateStatus"] = "UNSTABLE"

    assert decide(st, True) == "ci_fix"


def test_wait_when_checks_not_success():
    st = _state(checks="PENDING", threads=[])
    assert decide(st, True) == "wait"


def test_empty_open_threads_max_is_none_not_fix():
    # No open threads at all -> newest is None -> never "fix".
    st = _state(threads=[_thread(True, "2099-01-01T00:00:00Z")])
    assert decide(st, False) == "approve"


def test_ci_fix_when_checks_failure_and_no_open_threads():
    # TEAM-1231's exact shape: CI red, all review threads resolved -> the old "wait" dead zone.
    st = _state(checks="FAILURE", threads=[_thread(True, "2020-02-02T00:00:00Z")])
    assert decide(st, False) == "ci_fix"


def test_ci_fix_on_error_state():
    st = _state(checks="ERROR", threads=[])
    assert decide(st, True) == "ci_fix"


def test_fix_takes_precedence_over_ci_fix():
    # Unresolved review comment newer than head AND red CI -> fix (triangulate handles CI too).
    st = _state(checks="FAILURE", head="2020-01-01T00:00:00Z", threads=[_thread(False, "2020-01-02T00:00:00Z")])
    assert decide(st, True) == "fix"


def test_pending_checks_stay_wait_not_ci_fix():
    # CI still running is not a failure — must keep waiting, never spawn a CI fix.
    assert decide(_state(checks="PENDING", threads=[]), True) == "wait"
    assert decide(_state(checks="EXPECTED", threads=[]), True) == "wait"


def test_bot_gates_are_off_by_default_so_a_human_must_approve():
    # Out of the box neither bot is configured, so a green, thread-free PR with a perfect
    # score and a fresh reaction still waits for a human. Opting in is what unlocks it.
    st = _state(
        decision="REVIEW_REQUIRED",
        comments=[_greptile_comment(5)],
        reactions=[_reaction()],
    )
    assert _decide(st, True) == "wait"
    assert decide(st, True) == "approve"


def test_default_gates_still_approve_on_a_human_review():
    # With no bots configured, GitHub's own approval remains the way through the gate.
    assert _decide(_state(decision="APPROVED"), True) == "approve"
