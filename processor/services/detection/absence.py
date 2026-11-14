"""Absence detection (dead-man switch): alert when a group produces NO event for ``window``.

XML::

    <pysymbol>services.detection.absence</pysymbol>
    <values>
        <key   constructor="str" kwargs="group_by" optional="true">instance_id</key>
        <const constructor="str" kwargs="window">2m</const>
        <const constructor="str" kwargs="forget" optional="true">1h</const>
    </values>

* ``group_by`` (key): value of the dimension that identifies the thing that must keep reporting. Each distinct
  value is watched separately. Without it the whole event stream is one group.
* ``window``: how long a group may stay silent (``10s 1m 5m 2h 7d`` ...).
* ``forget`` (optional, default ``max(1h, 10 x window)``): a group silent for longer than this is dropped, so
  retired instances do not keep alerting forever. Must be longer than ``window``.

State: per group ``{"last": ts_ms, "alerted_at": ts_ms | None}``. ``evaluate`` only records "seen at"; nothing can
fire there, because the rule is about events that did not come. The engine must call ``tick`` on a timer: it returns
a Finding for every group silent longer than ``window`` and repeats it once per ``window`` while the silence lasts.
A group that reports again stops alerting.

Safety nets: nothing fires during the first ``window`` after start-up (the processor itself may have been down),
and state is rebuilt by replaying ``2 x window`` of history (see ``retention_ms``).
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from src.duration import format_duration, parse_duration

from .base import BaseDetection, DetectionContext, Finding

MIN_FORGET_MS = 3_600_000


@lru_cache(maxsize=256)
def _ms(value: str) -> int:
    return parse_duration(value)


def _forget_ms(window_ms: int, forget: Optional[str]) -> int:
    return _ms(forget) if forget is not None else max(MIN_FORGET_MS, 10 * window_ms)


class AbsenceDetection(BaseDetection):
    def __init__(self, group_by=None, *, window: str = "5m", forget: Optional[str] = None):
        super().__init__()
        self.group_by = group_by
        self.window = window
        self.forget = forget

    @classmethod
    def validate(cls, *, window: str = "5m", forget: Optional[str] = None, **_) -> None:
        window_ms = _ms(window)
        if window_ms <= 0:
            raise ValueError("window must be positive")
        if forget is not None and _ms(forget) <= window_ms:
            raise ValueError("forget must be longer than window")

    @classmethod
    def retention_ms(cls, *, window: str = "5m", **_) -> int:
        return 2 * _ms(window)

    @classmethod
    def sweep(cls, state: dict, now_ms: int, *, window: str = "5m", forget: Optional[str] = None, **_) -> None:
        cutoff = now_ms - _forget_ms(_ms(window), forget)
        groups = state.get("groups", {})

        for key in [k for k, rec in groups.items() if rec["last"] < cutoff]:
            del groups[key]

    @classmethod
    def tick(cls, state: dict, now_ms: int, *, window: str = "5m", forget: Optional[str] = None, **_) -> list[Finding]:
        window_ms = _ms(window)
        armed_at = state.setdefault("armed_at", now_ms + window_ms)

        if now_ms < armed_at:
            return []

        key = state.get("group_key")
        window_txt = format_duration(window_ms)
        findings: list[Finding] = []

        for group, rec in state.get("groups", {}).items():
            silent = now_ms - rec["last"]

            if silent <= window_ms:
                continue
            if rec["alerted_at"] is not None and now_ms - rec["alerted_at"] < window_ms:
                continue
            rec["alerted_at"] = now_ms

            what = f"{key}={group}" if key else "event stream"
            findings.append(Finding(
                message=f"No event from {what} for {format_duration(silent)} (allowed: {window_txt})",
                group_key=key,
                group_value=group if key else None,
                details={"last_seen_ms": rec["last"], "silent_for_ms": silent,
                         "window": window_txt, "window_ms": window_ms},
                event_id=rec.get("event_id"),
            ))
        return findings

    def evaluate(self, ctx: DetectionContext) -> Optional[Finding]:
        key = ctx.keys.get("group_by")

        if key is None:
            group = "*"
        elif self.group_by is None:
            return None
        else:
            group = str(self.group_by)

        ctx.state["group_key"] = key
        event_id = getattr(ctx.event, "event_id", None)
        rec = ctx.state.setdefault("groups", {}).setdefault(
            group, {"last": ctx.ts_ms, "alerted_at": None, "event_id": event_id})
        if ctx.ts_ms >= rec["last"]:
            rec["last"] = ctx.ts_ms
            rec["event_id"] = event_id
        rec["alerted_at"] = None
        return None


export = AbsenceDetection