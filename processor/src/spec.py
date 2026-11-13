"""Immutable in-memory representation of an ``applications/*.xml`` file."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Optional

from .casting import cast

SEVERITIES = ("low", "medium", "high", "critical")

@dataclass(frozen=True)
class ValueSpec:
    """One argument of a rule: ``<key>`` (read from the event) or ``<const>`` (literal)."""
    kind: str
    raw: str
    constructor: str = "str"
    kwarg: Optional[str] = None
    optional: bool = False


@dataclass(frozen=True)
class DimensionSpec:
    key: str
    constructor: Optional[str] = None


@dataclass(frozen=True)
class RuleSpec:
    kind: str
    name: str
    symbol: str
    values: tuple[ValueSpec, ...] = ()
    as_name: Optional[str] = None
    severity: str = "medium"
    cooldown_ms: int = 300_000
    warmup_ms: Optional[int] = None
    enabled: bool = True

    def const_kwargs(self) -> dict:
        """Constant keyword values (already cast) - what class-level hooks can know without an event."""
        return {v.kwarg: cast(v.constructor, v.raw) for v in self.values if v.kind == "const" and v.kwarg}

    def digest(self) -> str:
        return hashlib.sha1(repr(self).encode()).hexdigest()[:12]


@dataclass(frozen=True)
class EventSpec:
    application: str
    source: str
    name: str
    dimensions: tuple[DimensionSpec, ...] = ()
    enrichment: tuple[RuleSpec, ...] = ()
    alerts: tuple[RuleSpec, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.application}/{self.source}/{self.name}"


@dataclass(frozen=True)
class ApplicationSpec:
    name: str
    path: str
    events: tuple[EventSpec, ...] = ()


@dataclass(frozen=True)
class Registry:
    applications: tuple[ApplicationSpec, ...] = ()
    version: str = ""
    _index: dict = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self):
        for app in self.applications:
            for ev in app.events:
                self._index[(ev.application, ev.source, ev.name)] = ev

    def lookup(self, application: str, source: str, event_type: str) -> Optional[EventSpec]:
        """Exact match first, then ``name="*"`` wildcards (event, then source)."""
        idx = self._index
        return (idx.get((application, source, event_type))
                or idx.get((application, source, "*"))
                or idx.get((application, "*", event_type))
                or idx.get((application, "*", "*")))

    def events(self) -> list[EventSpec]:
        return list(self._index.values())

    def describe(self) -> list[dict]:
        """JSON-friendly view, used by the API so a dashboard can build itself from the XML."""
        from .duration import format_duration
        out = []
        for app in self.applications:
            sources: dict[str, list[dict]] = {}
            for ev in app.events:
                sources.setdefault(ev.source, []).append({
                    "name": ev.name,
                    "dimensions": [{"key": d.key, "type": d.constructor or "auto"} for d in ev.dimensions],
                    "enrichment": [
                        {"name": r.name, "symbol": r.symbol, "as": r.as_name, "enabled": r.enabled}
                        for r in ev.enrichment
                    ],
                    "alerts": [
                        {
                            "name": r.name, "symbol": r.symbol, "severity": r.severity,
                            "cooldown": format_duration(r.cooldown_ms), "enabled": r.enabled,
                            "arguments": [
                                {"name": v.kwarg, "kind": v.kind, "value": v.raw}
                                for v in r.values
                            ],
                        }
                        for r in ev.alerts
                    ],
                })
            out.append({
                "name": app.name,
                "file": app.path,
                "sources": [{"name": s, "events": evs} for s, evs in sources.items()],
            })
        return out
