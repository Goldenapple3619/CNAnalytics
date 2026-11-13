"""Event model and field resolution.

A *field reference* is the string used in ``<key>`` elements, API filters, ``group_by`` ... It is
resolved in this order:

1. an exact dimension name                  ``ip``
2. ``dims.<name>``                          ``dims.device.os``
3. ``enriched.<as>.<path>``                 ``enriched.ip.country_code``
4. an event column                          ``user_id``, ``session_id``, ``event_type`` ...
5. ``<as>.<path>`` shorthand for enrichment ``ip.country_code``
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

COLUMNS = frozenset({
    "event_id", "application", "source", "event_type",
    "user_id", "session_id", "timestamp_ms", "received_at_ms",
})


@dataclass(frozen=True)
class RawEvent:
    """A row of the ingestion ``analytics`` table."""
    id: int
    event_id: str
    event_type: str
    timestamp_ms: int
    user_id: str
    session_id: str
    source: str
    application: str
    environment: str
    schema_version: int
    payload_json: str
    attributes: str
    received_at_ms: int


@dataclass
class Event:
    """A raw event after dimension extraction; enrichment fills ``enriched``."""
    source_row_id: int
    event_id: str
    application: str
    source: str
    event_type: str
    timestamp_ms: int
    received_at_ms: int
    user_id: str
    session_id: str
    dims: dict[str, Any] = field(default_factory=dict)
    enriched: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def get(self, ref: str, default: Any = None) -> Any:
        value = resolve(ref, self)
        return default if value is None else value

    def ts(self, time_field: str = "timestamp_ms") -> int:
        return self.received_at_ms if time_field == "received_at_ms" else self.timestamp_ms


def dig(obj: Any, path: str) -> Any:
    """Walk ``a.b.0.c`` through dicts and lists; ``None`` when any step is missing."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return cur


def resolve(ref: str, event: Event) -> Any:
    if ref in event.dims:
        return event.dims[ref]
    if ref.startswith("dims."):
        return event.dims.get(ref[5:])
    if ref.startswith("enriched."):
        return dig(event.enriched, ref[9:])
    if ref in COLUMNS:
        return getattr(event, ref)
    if "." in ref:
        return dig(event.enriched, ref)
    return None


def loads_object(text: Optional[str]) -> Optional[dict]:
    """Parse a JSON object column; ``None`` when empty or not a JSON object."""
    if not text:
        return None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def extract(key: str, payload: Optional[dict], attributes: Optional[dict], raw: RawEvent) -> Any:
    """Find the value of a declared dimension: payload, then attributes, then table columns."""
    for source in (payload, attributes):
        if source is None:
            continue
        if key in source:
            return source[key]
        if "." in key:
            found = dig(source, key)
            if found is not None:
                return found
    if key in COLUMNS or key in ("environment", "schema_version"):
        return getattr(raw, key, None)
    return None
