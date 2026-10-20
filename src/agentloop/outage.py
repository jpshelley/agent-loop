"""outage — usage-outage detection, log-only (no Linear, no Claude).

Ports the usage-outage block of ``agent-loop-lib.sh`` (lib:65-145). When Claude usage is
exhausted, a spawned ``claude -p`` session exits immediately with a "You've hit your session
limit · resets <time>" banner — by Linear state alone that's indistinguishable from a crash.
These helpers read the session LOGS to tell the two apart, so the loops can stop spawning INTO
an outage and re-queue victims for automatic resume. Zero API calls, zero Claude usage.
"""

from __future__ import annotations

import re
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from agentloop.identity import validate_execution_key

# Signature of a usage-limit exit (lib:76). ASCII-only on purpose. Matched case-insensitively
# against the ANSI-stripped log tail.
OUTAGE_REGEX = re.compile(
    r"hit your (session|usage) limit"
    r"|(session|usage|5-hour|weekly) limit reached"
    r"|resets [0-9]{1,2}:[0-9]{2} ?[apAP][mM]",
    re.IGNORECASE,
)

_ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_RESETS_LINE = re.compile(
    r"resets [0-9]{1,2}:[0-9]{2} ?[apAP][mM]( \([A-Za-z_/]+\))?", re.IGNORECASE
)
_CLOCK = re.compile(r"[0-9]{1,2}:[0-9]{2} ?[apAP][mM]", re.IGNORECASE)
_TZ_PAREN = re.compile(r"\(([A-Za-z_/]+)\)")

_TAIL_BYTES = 4000


def strip_ansi(s: str) -> str:
    """Strip ANSI escape sequences (banners are wrapped in cursor/color codes) — lib:79."""
    return _ANSI.sub("", s)


def _read_tail(path: Path) -> str | None:
    """Return the last ``_TAIL_BYTES`` of a file as text, or None if unreadable (lib:85)."""
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - _TAIL_BYTES))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None


def log_tail_is_outage(path: Path) -> bool:
    """True if the tail of a log file carries the usage-limit signature (lib:82-86)."""
    body = _read_tail(path)
    if body is None:
        return False
    return OUTAGE_REGEX.search(strip_ansi(body)) is not None


def newest_key_log(cfg, key: str) -> Path | None:
    """Newest per-session log for KEY across develop/dispatch/deploy folders + the legacy
    flat location. Debug fix sessions are still excluded: declutter handles build-session zombies,
    not in-review fix attempts."""
    validate_execution_key(key)
    candidates: list[Path] = []
    candidates += cfg.log_dir.glob(f"develop/{key}-*.log")
    candidates += cfg.log_dir.glob(f"dispatch/{key}-*.log")
    candidates += cfg.log_dir.glob(f"deploy/{key}-*.log")
    legacy = cfg.log_dir / f"{key}.log"
    if legacy.exists():
        candidates.append(legacy)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def key_outage_log(cfg, key: str) -> bool:
    """True if KEY's most recent session ended on a usage limit (an outage victim, lib:94-97)."""
    f = newest_key_log(cfg, key)
    return f is not None and log_tail_is_outage(f)


def outage_reset_epoch(path: Path, ref: float = 0.0) -> float | None:
    """Best-effort epoch that a usage-limit banner says it resets (lib:99-119).

    Parses "resets 2:10pm (America/Chicago)". Returns None on ANY parse failure so callers
    fall back to the mtime backoff. Preserves the two subtle rules:
      * Only trust an IANA zone (contains '/'); a bare abbreviation ('CDT') is ambiguous, so
        ignore it and fall back to the machine-local zone (what the banner prints in anyway).
      * Day-wrap: if the reset lands before the cap was hit, it's the next day (+86400).
    """
    body = _read_tail(path)
    if body is None:
        return None
    body = strip_ansi(body)

    matches = list(_RESETS_LINE.finditer(body))
    if not matches:
        return None
    line = matches[-1].group(0)  # last "resets ..." occurrence (lib:105 `tail -1`)

    cm = _CLOCK.search(line)
    if not cm:
        return None
    clock = cm.group(0).replace(" ", "").upper()  # e.g. "2:10PM"

    tzm = _TZ_PAREN.search(line)
    zone_name = tzm.group(1) if tzm else ""
    use_iana = "/" in zone_name
    tz = ZoneInfo(zone_name) if use_iana else None  # tz=None -> machine-local

    # Anchor the reset on the cap-hit day (log mtime), in the chosen zone.
    if ref and ref > 0:
        ref_dt = datetime.fromtimestamp(ref, tz) if tz else datetime.fromtimestamp(ref)
    else:
        ref_dt = datetime.now(tz) if tz else datetime.now()
    day = ref_dt.date()

    try:
        clock_dt = datetime.strptime(clock, "%I:%M%p")
    except ValueError:
        return None

    reset_dt = datetime(
        day.year, day.month, day.day, clock_dt.hour, clock_dt.minute, tzinfo=tz
    )
    # For a naive datetime (local zone), .timestamp() interprets it as local time (DST-correct);
    # for an aware datetime (IANA zone) it converts from that zone. Both match bash `date`.
    r = reset_dt.timestamp()

    if ref and ref > 0 and r < ref:  # reset is AFTER the cap was hit -> next day (lib:117)
        r += 86400
    return r


def rate_limited_now(cfg) -> str | None:
    """Global outage gate for the spawning loops (lib:121-145).

    Returns a short human "hold hint" string if we are currently rate-limited, else None.
    Rate-limited == the MOST RECENT build-class loop session log
    (develop/dispatch/debug/deploy/design) is a usage-limit exit AND we're still inside its
    window — until the banner's stated reset time when parseable, else ``outage_backoff`` from
    the log's mtime. Lets exactly one retry through per window (past the window -> None), so
    retries self-space instead of hammering.
    """
    candidates: list[Path] = []
    for sub in ("develop", "dispatch", "debug", "deploy", "design"):
        candidates += cfg.log_dir.glob(f"{sub}/*.log")
    if not candidates:
        return None
    newest = max(candidates, key=lambda p: p.stat().st_mtime)
    if not log_tail_is_outage(newest):
        return None

    now = time.time()
    mtime = newest.stat().st_mtime
    reset = outage_reset_epoch(newest, mtime)
    if reset is not None:
        if now >= reset:  # past the stated reset -> allow a retry
            return None
        return f"until {datetime.fromtimestamp(reset).strftime('%H:%M')} (banner reset)"

    if now - mtime >= cfg.outage_backoff:  # backoff window elapsed -> allow a retry
        return None
    left = int((cfg.outage_backoff - (now - mtime)) // 60)
    return f"for up to ~{left}m (backoff; no reset time in banner)"
