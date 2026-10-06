"""Classify failure text and parse when a usage limit resets (docs/DESIGN.md 4.2, 4.3, 7.2).

Every format here was seen on this machine: Claude Code's error results, the
account-pool plugin's serve.log lines, and opencode's retry status messages.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

AUTH_PATTERNS = [
    r"failed to authenticate",
    r"oauth (?:token|session) (?:has )?expired",
    r"please run /login",
    r"invalid api key",
    r"invalid x-api-key",
    r"authentication_error",
    r"\bnot logged in\b",
    # A bare "401" is too common ("line 401"): only an HTTP status, or 401 next to an auth word, counts.
    r"(?:http|status|error|code)\D{0,12}\b401\b",
    r"\b401\b(?=[^\n]{0,40}(?:unauthori[sz]ed|authenticat|invalid|token|credential|log ?in))",
    r"\binvalid_grant\b",
]
BILLING_PATTERNS = [
    r"credit balance is too low",
    r"insufficient (?:credit|credits|balance|funds|quota)",
    r"billing (?:error|issue|problem)",
    r"payment required",
]
SESSION_LIMIT_PATTERNS = [
    r"hit your (?:\w+[ -])?limit",
    r"usage limit",
    r"limit reached",
    r"pool exhausted",
    r"usage window rejected",
    r"out of (?:usage|credits)",
    r"extra usage",
]
RATE_PATTERNS = [r"\b429\b", r"rate[ _-]?limit", r"overloaded", r"\b529\b", r"too many requests"]

_AUTH = re.compile("|".join(AUTH_PATTERNS), re.IGNORECASE)
_BILLING = re.compile("|".join(BILLING_PATTERNS), re.IGNORECASE)
_SESSION = re.compile("|".join(SESSION_LIMIT_PATTERNS), re.IGNORECASE)
_RATE = re.compile("|".join(RATE_PATTERNS), re.IGNORECASE)

_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_CLOCK_RESET = re.compile(
    r"resets?\s+(?:at\s+|on\s+)?"
    r"(?:(?P<mon>[A-Za-z]{3,9})\.?\s+(?P<day>\d{1,2}),?\s+(?:at\s+)?)?"
    r"(?P<h>\d{1,2})(?::(?P<m>\d{2}))?\s*(?P<ampm>am|pm)?\s*"
    r"\((?P<tz>[A-Za-z_]+(?:/[A-Za-z_+-]+)*)\)",
    re.IGNORECASE,
)
_IN_DURATION = re.compile(
    r"(?:free|resets?|retry|again|available)\s+in\s+(?P<dur>(?:\d+\s*(?:d|h|m|s|days?|hours?|minutes?|mins?|seconds?|secs?)\b[\s,]*)+)",
    re.IGNORECASE,
)
_RETRY_AFTER = re.compile(r"retry-after:?\s*(\d+)", re.IGNORECASE)
_EPOCH = re.compile(r"limit reached\|(\d{9,11})", re.IGNORECASE)
_ISO_RESET = re.compile(r"resets?\s+(?:at\s+)?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2})?(?:Z|[+-]\d{2}:?\d{2})?)", re.IGNORECASE)
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}


def _duration_seconds(text: str) -> int:
    total = 0
    for n, unit in re.findall(r"(\d+)\s*([a-z]+)", text.lower()):
        key = "m" if unit.startswith("min") else unit[0]
        total += int(n) * _UNIT_SECONDS[key]
    return total


def parse_reset(text: str, now: datetime) -> datetime | None:
    """When the limit described in `text` resets, or None if it does not say."""
    if m := _EPOCH.search(text):
        return datetime.fromtimestamp(int(m.group(1)), tz=now.tzinfo)
    if m := _ISO_RESET.search(text):
        try:
            when = datetime.fromisoformat(m.group(1).replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            when = None
        if when is not None:
            return when if when.tzinfo else when.replace(tzinfo=now.tzinfo)
    if m := _CLOCK_RESET.search(text):
        when = _clock_reset(m, now)
        if when is not None:
            return when
    if m := _IN_DURATION.search(text):
        seconds = _duration_seconds(m.group("dur"))
        if seconds:
            return now + timedelta(seconds=seconds)
    if m := _RETRY_AFTER.search(text):
        return now + timedelta(seconds=int(m.group(1)))
    return None


def _clock_reset(m: re.Match[str], now: datetime) -> datetime | None:
    try:
        tz = ZoneInfo(m.group("tz"))
    except (ZoneInfoNotFoundError, ValueError):
        return None
    hour = int(m.group("h"))
    minute = int(m.group("m") or 0)
    ampm = (m.group("ampm") or "").lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    local_now = now.astimezone(tz)
    if m.group("mon"):
        month = _MONTHS.get(m.group("mon")[:3].lower())
        if month is None:
            return None
        when = local_now.replace(month=month, day=int(m.group("day")), hour=hour, minute=minute, second=0, microsecond=0)
        if when < local_now - timedelta(days=1):
            when = when.replace(year=when.year + 1)
        return when.astimezone(now.tzinfo)
    when = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if when <= local_now:
        when += timedelta(days=1)
    return when.astimezone(now.tzinfo)


def classify(text: str) -> str:
    """'auth', 'billing', 'session_limit', 'rate_limited' or 'other'. Auth wins: a dead login looks like nothing else."""
    if _AUTH.search(text):
        return "auth"
    if _BILLING.search(text):
        return "billing"
    if _SESSION.search(text):
        return "session_limit"
    if _RATE.search(text):
        return "rate_limited"
    return "other"
