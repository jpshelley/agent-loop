"""Usage-outage detection — the crown-jewel parser. Log-tail signatures, reset-epoch parsing
(timezone + day-wrap + IANA-only rule), and the rate_limited_now window logic."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from agentloop import outage


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


OUTAGE_TAILS = [
    "You've hit your session limit · resets 2:10pm (America/Chicago)",
    "\x1b[31mYou've hit your usage limit\x1b[0m",  # wrapped in ANSI color codes
    "weekly limit reached, try again later",
    "5-hour limit reached",
    "resets 11:59pm",
]
NON_OUTAGE_TAILS = [
    "PR opened successfully",
    "Task complete. Exiting.",
    "error: something unrelated happened",
]


@pytest.mark.parametrize("tail", OUTAGE_TAILS)
def test_log_tail_is_outage_positive(tmp_path, tail):
    f = _write(tmp_path / "s.log", "lots of earlier output\n" + tail)
    assert outage.log_tail_is_outage(f) is True


@pytest.mark.parametrize("tail", NON_OUTAGE_TAILS)
def test_log_tail_is_outage_negative(tmp_path, tail):
    f = _write(tmp_path / "s.log", tail)
    assert outage.log_tail_is_outage(f) is False


def test_strip_ansi():
    assert outage.strip_ansi("\x1b[1;31mhit your session limit\x1b[0m") == "hit your session limit"


def test_reset_epoch_basic_iana(tmp_path):
    # ref = 2025-06-01 18:00 UTC (= 13:00 America/Chicago, CDT). Banner resets 2:10pm same day.
    ref = datetime(2025, 6, 1, 18, 0, tzinfo=timezone.utc).timestamp()
    f = _write(tmp_path / "s.log", "resets 2:10pm (America/Chicago)")
    got = outage.outage_reset_epoch(f, ref)
    expected = datetime(2025, 6, 1, 14, 10, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert got == expected
    assert got > ref  # reset after the cap was hit -> no day-wrap


def test_reset_epoch_day_wrap(tmp_path):
    # ref = 2025-06-01 04:00 UTC (= 2025-05-31 23:00 Chicago). Banner resets 1:00am -> that clock
    # on the Chicago ref-date (05-31 01:00) is BEFORE the cap, so it wraps to the next day.
    ref = datetime(2025, 6, 1, 4, 0, tzinfo=timezone.utc).timestamp()
    f = _write(tmp_path / "s.log", "resets 1:00am (America/Chicago)")
    got = outage.outage_reset_epoch(f, ref)
    expected = datetime(2025, 6, 1, 1, 0, tzinfo=ZoneInfo("America/Chicago")).timestamp()
    assert got == expected
    assert got > ref


def test_reset_epoch_bare_abbreviation_falls_back_to_local(tmp_path):
    # A bare 'CDT' (no '/') is ambiguous -> ignored, parsed in the machine-local zone. We assert
    # it equals the naive-local interpretation of the same clock on ref's local date.
    ref = datetime(2025, 6, 1, 18, 0, tzinfo=timezone.utc).timestamp()
    f = _write(tmp_path / "s.log", "resets 2:10pm (CDT)")
    got = outage.outage_reset_epoch(f, ref)
    local_day = datetime.fromtimestamp(ref).date()
    expected = datetime(local_day.year, local_day.month, local_day.day, 14, 10).timestamp()
    # Account for the possible day-wrap in local zone as well.
    assert got in (expected, expected + 86400)


def test_reset_epoch_no_banner_returns_none(tmp_path):
    f = _write(tmp_path / "s.log", "usage limit reached")  # outage, but no parseable reset time
    assert outage.outage_reset_epoch(f, 1000.0) is None


def test_rate_limited_now_inside_window(make_cfg):
    cfg = make_cfg()
    f = _write(cfg.log_dir / "develop" / "TEAM-1-x.log", "You've hit your session limit")
    # Fresh mtime, no reset banner -> inside the backoff window -> a hold hint.
    hint = outage.rate_limited_now(cfg)
    assert hint is not None
    assert "backoff" in hint


def test_rate_limited_now_considers_legacy_dispatch_logs(make_cfg):
    cfg = make_cfg()
    _write(cfg.log_dir / "dispatch" / "TEAM-1-x.log", "You've hit your session limit")

    hint = outage.rate_limited_now(cfg)

    assert hint is not None
    assert "backoff" in hint


def test_rate_limited_now_considers_design_logs(make_cfg):
    cfg = make_cfg()
    _write(cfg.log_dir / "design" / "design-99-x.log", "You've hit your session limit")

    hint = outage.rate_limited_now(cfg)

    assert hint is not None
    assert "backoff" in hint


def test_rate_limited_now_past_backoff(make_cfg):
    cfg = make_cfg()
    f = _write(cfg.log_dir / "develop" / "TEAM-1-x.log", "usage limit reached")
    old = os.stat(f).st_mtime - (cfg.outage_backoff + 60)
    os.utime(f, (old, old))
    assert outage.rate_limited_now(cfg) is None  # window elapsed -> allow a retry


def test_rate_limited_now_no_logs(make_cfg):
    assert outage.rate_limited_now(make_cfg()) is None


def test_newest_key_log_ignores_debug_folder(make_cfg):
    cfg = make_cfg()
    key = "al-linear-999999999999999999999999"
    _write(cfg.log_dir / "debug" / f"{key}-x.log", "hit your session limit")
    # Only debug/ has a log for this key; newest_key_log looks at build-spawn folders/flat only.
    assert outage.newest_key_log(cfg, key) is None
