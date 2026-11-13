"""Base class of every detection (alert rule).

Same shape as an enrichment: built once per event with the ``<values>`` of the XML rule, used as
a context manager, then asked to ``evaluate`` the event::

    with RateAboveDetection(group_by="1.2.3.4", window="5m", threshold=50) as detection:
        finding = detection.evaluate(ctx)       # Finding | None

Because instances are short-lived, anything that must survive between events (counters, sliding
windows ...) goes in ``ctx.state``: a plain dict owned by the rule, kept in memory by the engine
and rebuilt on start-up by replaying recent events through ``evaluate`` with ``ctx.silent=True``
(findings returned while silent are discarded). ``retention_ms`` tells the engine how much history
that replay needs, ``sweep`` lets the rule drop stale state.

A returned :class:`Finding` becomes an alert. Findings of the same rule and group are merged into
one open alert while they keep arriving within the rule's ``cooldown``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Finding:
    message: str
    group_key: Optional[str] = None     # what the rule groups on, e.g. "ip"
    group_value: Any = None             # e.g. "1.2.3.4" - part of the alert identity
    details: dict = field(default_factory=dict)
    title: Optional[str] = None         # defaults to the rule name
    severity: Optional[str] = None      # defaults to the rule's severity attribute


@dataclass
class DetectionContext:
    event: Any                          # src.event.Event (dims + enriched are available)
    state: dict                         # per-rule memory, see module docstring
    ts_ms: int                          # event time according to PROCESSOR_TIME_FIELD
    keys: dict                          # argument name -> field path it was read from ({"group_by": "ip"})
    rule_name: str = ""
    silent: bool = False                # True while replaying history on start-up


class BaseDetection(object):
    def __init__(self, *values: Any, **options: Any):
        self.values: tuple = values
        self.options: dict = options

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        return False

    @classmethod
    def validate(cls, **options: Any) -> None:
        """Called when the XML is loaded with the rule's constant keyword values (already cast).
        Raise ``ValueError`` to reject a bad configuration before any event is processed."""

    @classmethod
    def retention_ms(cls, **options: Any) -> int:
        """History to replay at start-up. ``options`` are the rule's constant keyword values."""
        return 0

    @classmethod
    def sweep(cls, state: dict, now_ms: int, **options: Any) -> None:
        """Called periodically: drop state that can no longer influence a result."""

    def evaluate(self, ctx: DetectionContext) -> Optional[Finding]:
        raise NotImplementedError(f"{type(self).__name__}.evaluate() is not implemented")


export = BaseDetection
