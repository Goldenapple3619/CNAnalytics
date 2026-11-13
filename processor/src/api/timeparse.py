"""Time parameters accepted by the API: epoch milliseconds, ISO-8601, or relative (``-15m``, ``-2h``)."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from ..duration import parse_duration

_DIGITS = re.compile(r"^\d{9,}$")
_NICE_STEPS = [10_000, 30_000, 60_000, 300_000, 900_000, 3_600_000, 21_600_000, 86_400_000]


def parse_time(value: Optional[str], now_ms: int, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    value = value.strip()
    if value == "now":
        return now_ms
    if value.startswith("-"):
        return now_ms - parse_duration(value[1:])
    if _DIGITS.match(value):
        return int(value)
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"invalid time {value!r}: use epoch ms, ISO-8601 or a relative value like -15m") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return int(moment.timestamp() * 1000)


def pick_step(start_ms: int, end_ms: int, interval: Optional[str], max_buckets: int = 2000) -> int:
    """Requested interval, or the smallest 'nice' one giving at most ~120 points."""
    if interval:
        step = parse_duration(interval)
        if step <= 0:
            raise ValueError("interval must be positive")
        if (end_ms - start_ms) / step > max_buckets:
            raise ValueError(f"interval too small for this range (more than {max_buckets} points)")
        return step
    span = max(end_ms - start_ms, 1)
    return next((s for s in _NICE_STEPS if span / s <= 120), _NICE_STEPS[-1])
