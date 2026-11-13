"""Alert when a group produces more than ``threshold`` events within ``window``.

XML (from the example application)::

    <pysymbol>services.detection.rate_above</pysymbol>
    <values>
        <key   constructor="str" kwargs="group_by">ip</key>      <!-- value of the dimension for this event -->
        <const constructor="str" kwargs="window">5m</const>
        <const constructor="int" kwargs="threshold">50</const>
    </values>

State: per group, a sorted list of the timestamps still inside the window (capped, so a flood
cannot exhaust memory). Late / out-of-order events are inserted at their place.
"""
from __future__ import annotations

from bisect import bisect_right, insort
from functools import lru_cache
from typing import Optional

from src.duration import format_duration, parse_duration

from .base import BaseDetection, DetectionContext, Finding

MAX_TRACKED = 100_000  # timestamps kept per group; the reported count saturates here


@lru_cache(maxsize=256)
def _ms(window: str) -> int:
    return parse_duration(window)


class RateAboveDetection(BaseDetection):
    def __init__(self, group_by=None, *, window: str = "5m", threshold: int = 50):
        super().__init__()
        self.group_by = group_by
        self.window = window
        self.threshold = int(threshold)

    @classmethod
    def validate(cls, *, window: str = "5m", threshold: int = 50, **_) -> None:
        if _ms(window) <= 0:
            raise ValueError("window must be positive")
        if int(threshold) < 0:
            raise ValueError("threshold must be >= 0")

    @classmethod
    def retention_ms(cls, *, window: str = "5m", **_) -> int:
        return _ms(window)

    @classmethod
    def sweep(cls, state: dict, now_ms: int, *, window: str = "5m", **_) -> None:
        cutoff = now_ms - _ms(window)
        groups = state.get("groups", {})
        for key in [k for k, stamps in groups.items() if not stamps or stamps[-1] <= cutoff]:
            del groups[key]

    def evaluate(self, ctx: DetectionContext) -> Optional[Finding]:
        if self.group_by is None:
            return None
        window_ms = _ms(self.window)
        stamps = ctx.state.setdefault("groups", {}).setdefault(self.group_by, [])
        insort(stamps, ctx.ts_ms)
        cutoff = stamps[-1] - window_ms
        del stamps[:bisect_right(stamps, cutoff)]          # keep (newest - window, newest]
        if len(stamps) > MAX_TRACKED:
            del stamps[:len(stamps) - MAX_TRACKED]

        count = len(stamps)
        if count <= self.threshold:
            return None
        key = ctx.keys.get("group_by")
        window = format_duration(window_ms)
        return Finding(
            message=f"{count} events in {window} for {key}={self.group_by} (threshold {self.threshold})",
            group_key=key,
            group_value=self.group_by,
            details={"count": count, "threshold": self.threshold, "window": window,
                     "window_ms": window_ms, "first_ts": stamps[0], "last_ts": stamps[-1]},
        )


export = RateAboveDetection
