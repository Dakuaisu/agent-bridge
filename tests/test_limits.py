"""Every limit and auth message here was observed on this machine (account names anonymized)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import pytest

from agent_bridge import limits
from agent_bridge.clock import IST
from agent_bridge.limits import classify, parse_reset

UTC = timezone.utc


def _zones_known(*names: str) -> bool:
    try:
        for name in names:
            ZoneInfo(name)
    except ZoneInfoNotFoundError:
        return False
    return True


needs_tz = pytest.mark.skipif(
    not _zones_known("Asia/Kolkata", "America/New_York", "America/Los_Angeles"),
    reason="needs a tz database; without one the reset time is ignored (test_an_unknown_zone_is_logged_not_silent)",
)


@needs_tz
def test_claude_session_limit_later_today() -> None:
    now = datetime(2026, 10, 2, 14, 25, 33, tzinfo=UTC)  # 19:55 IST
    text = 'claude CLI error (success): "You\'ve hit your session limit · resets 9:40pm (Asia/Calcutta)"'
    assert parse_reset(text, now) == datetime(2026, 10, 2, 21, 40, tzinfo=IST)
    assert classify(text) == "session_limit"


@needs_tz
def test_claude_session_limit_rolls_to_tomorrow() -> None:
    now = datetime(2026, 10, 2, 16, 21, 12, tzinfo=UTC)  # 21:51 IST
    text = "You've hit your session limit · resets 2:40am (Asia/Calcutta)"
    assert parse_reset(text, now) == datetime(2026, 10, 3, 2, 40, tzinfo=IST)


@needs_tz
def test_weekly_limit_with_a_date() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=IST)
    text = "You've hit your weekly limit · resets Oct 8, 2pm (Asia/Calcutta)"
    assert parse_reset(text, now) == datetime(2026, 10, 8, 14, 0, tzinfo=IST)
    assert classify(text) == "session_limit"


@needs_tz
def test_24_hour_clock_and_other_zones() -> None:
    now = datetime(2026, 10, 6, 12, 0, tzinfo=IST)
    when = parse_reset("limit reached · resets 21:40 (America/New_York)", now)
    assert when is not None and when.utcoffset() == timedelta(hours=5, minutes=30)
    assert when.astimezone(UTC).hour == 1  # 21:40 EDT is 01:40 UTC


@pytest.mark.parametrize(
    ("text", "delta"),
    [
        ("[claude-pool] Claude pool exhausted — next account free in 1h 45m (acct-b); 3 disabled", timedelta(hours=1, minutes=45)),
        ("[claude-pool] Claude pool exhausted — next account free in 20m (acct-c); 3 disabled", timedelta(minutes=20)),
        ("[claude-pool] Claude pool exhausted — next account free in 12h 29m (acct-b); 3 disabled", timedelta(hours=12, minutes=29)),
        ("[claude-pool] acct-c: Account usage window rejected by Anthropic; resets in 16613 seconds; rotating account", timedelta(seconds=16613)),
        ("[claude-pool] acct-b: Account usage window rejected by Anthropic; resets in 44912 seconds; rotating account", timedelta(seconds=44912)),
    ],
)
def test_pool_messages(text: str, delta: timedelta) -> None:
    now = datetime(2026, 10, 4, 5, 1, 26, tzinfo=IST)
    assert parse_reset(text, now) == now + delta
    assert classify(text) == "session_limit"


def test_epoch_and_iso_and_retry_after() -> None:
    now = datetime(2026, 10, 2, 12, 0, tzinfo=IST)
    assert parse_reset("Claude AI usage limit reached|1790882321", now) == datetime.fromtimestamp(1790882321, tz=IST)
    assert parse_reset("usage limit; resets at 2026-10-03T02:40:00+05:30", now) == datetime(2026, 10, 3, 2, 40, tzinfo=IST)
    assert parse_reset("429 Too Many Requests, retry-after: 120", now) == now + timedelta(seconds=120)


def test_no_reset_in_text() -> None:
    assert parse_reset("supervisor timed out after 1800s", datetime(2026, 10, 2, tzinfo=IST)) is None
    assert parse_reset("resets 9:40pm (Not/AZone)", datetime(2026, 10, 2, tzinfo=IST)) is None


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("Failed to authenticate: OAuth session expired and could not be refreshed", "auth"),
        ("API Error: 401 {\"type\":\"error\",\"error\":{\"type\":\"authentication_error\"}}", "auth"),
        ("Invalid API key · Please run /login", "auth"),
        ("Token refresh failed (400): invalid_grant", "auth"),
        ("API Error: 529 Overloaded", "rate_limited"),
        ("rate_limit_error: Number of requests has exceeded your rate limit", "rate_limited"),
        ("Third-party apps now draw from your extra usage", "session_limit"),
        ("Unrecognized flag: --attach in command opencode run", "other"),
        ("builder returned no output", "other"),
    ],
)
def test_classify(text: str, kind: str) -> None:
    assert classify(text) == kind


def _without_backward_links(monkeypatch: pytest.MonkeyPatch) -> None:
    """A tz database without the 'backward' links (some minimal Linux images): legacy names are missing."""
    real = limits.ZoneInfo
    missing = {"Asia/Calcutta", "US/Pacific", "Europe/Kiev"}

    def zone(name: str):
        if name in missing:
            raise ZoneInfoNotFoundError(f"No time zone found with key {name}")
        return real(name)

    monkeypatch.setattr(limits, "ZoneInfo", zone)


@needs_tz
def test_a_legacy_zone_name_parses_without_the_backward_links(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_backward_links(monkeypatch)
    now = datetime(2026, 10, 6, 20, 0, tzinfo=IST)
    assert parse_reset("You've hit your session limit · resets 9:40pm (Asia/Calcutta)", now) == datetime(2026, 10, 6, 21, 40, tzinfo=IST)
    pacific = parse_reset("limit reached · resets 6am (US/Pacific)", now)
    assert pacific is not None and pacific.astimezone(ZoneInfo("America/Los_Angeles")).hour == 6


def test_an_unknown_zone_is_logged_not_silent(caplog: pytest.LogCaptureFixture) -> None:
    now = datetime(2026, 10, 6, 20, 0, tzinfo=IST)
    with caplog.at_level(logging.WARNING, logger="agent_bridge.limits"):
        assert parse_reset("You've hit your session limit · resets 9:40pm (Mars/Olympus_Mons)", now) is None
    assert "Mars/Olympus_Mons" in caplog.text
