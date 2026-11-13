"""Alert when a group touches more than ``threshold`` *distinct* values within ``window``.

Typical use: credential stuffing - one ip trying many different accounts::

    <pysymbol>services.detection.distinct_above</pysymbol>
    <values>
        <key   constructor="str" kwargs="group_by">ip</key>
        <key   constructor="str" kwargs="distinct">attempted_account_id</key>
        <const constructor="str" kwargs="window">10m</const>
        <const constructor="int" kwargs="threshold">20</const>
    </values>

State: per group an insertion-ordered ``{value: last_seen_ts}`` (re-inserted on every sighting, so
the oldest sighting is always first and expiry is a pop from the front).
"""

from __future__ import annotations

from typing import Optional

from src.duration import format_duration

from .base import DetectionContext, Finding
from .rate_above import RateAboveDetection, _ms

MAX_TRACKED = 50_000

class DistinctAboveDetection(RateAboveDetection):
    def __init__(self, group_by=None, distinct=None, *, window: str = "5m", threshold: int = 20):
        super().__init__(group_by, window=window, threshold=threshold)
        self.distinct = distinct

    @classmethod
    def sweep(cls, state: dict, now_ms: int, *, window: str = "5m", **_) -> None:
        cutoff = now_ms - _ms(window)
        groups = state.get("groups", {})

        for key, seen in list(groups.items()):
            while seen and next(iter(seen.values())) <= cutoff:
                del seen[next(iter(seen))]
            if not seen:
                del groups[key]

    def evaluate(self, ctx: DetectionContext) -> Optional[Finding]:
        if self.group_by is None or self.distinct is None:
            return None

        window_ms = _ms(self.window)
        clock = ctx.state["clock"] = max(ctx.state.get("clock", 0), ctx.ts_ms)
        cutoff = clock - window_ms

        if ctx.ts_ms <= cutoff:
            return None

        seen: dict = ctx.state.setdefault("groups", {}).setdefault(self.group_by, {})
        previous = seen.pop(self.distinct, None)
        seen[self.distinct] = ctx.ts_ms if previous is None else max(ctx.ts_ms, previous)

        while seen:
            oldest = next(iter(seen))
            if seen[oldest] > cutoff:
                break
            del seen[oldest]

        while len(seen) > MAX_TRACKED:
            del seen[next(iter(seen))]

        count = len(seen)

        if count <= self.threshold:
            return None

        group_field = ctx.keys.get("group_by")
        distinct_field = ctx.keys.get("distinct")
        window = format_duration(window_ms)

        return Finding(
            message=(f"{count} distinct {distinct_field} in {window} for "
                     f"{group_field}={self.group_by} (threshold {self.threshold})"),
            group_key=group_field,
            group_value=self.group_by,
            details={"distinct_field": distinct_field, "distinct_count": count,
                     "threshold": self.threshold, "window": window, "window_ms": window_ms,
                     "sample": list(seen)[-10:]},
        )


export = DistinctAboveDetection
