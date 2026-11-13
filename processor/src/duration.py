from __future__ import annotations

import re

_UNITS_MS = {"ms": 1, "s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*(ms|s|m|h|d|w)?\s*$", re.IGNORECASE)


def parse_duration(value: str | int | float) -> int:
    if isinstance(value, bool):
        raise ValueError(f"invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        return int(value * 1000)
    match = _RE.match(str(value))
    if not match:
        raise ValueError(f"invalid duration: {value!r} (expected e.g. 30s, 5m, 1h, 2d)")
    amount, unit = match.groups()
    return int(float(amount) * _UNITS_MS[(unit or "s").lower()])


def format_duration(ms: int) -> str:
    for unit in ("w", "d", "h", "m", "s"):
        size = _UNITS_MS[unit]
        if ms >= size and ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"
