"""Alert on every event whose field satisfies a condition (stateless).

XML - flag logins from VPN addresses (uses the geoip enrichment result)::

    <pysymbol>services.detection.field_match</pysymbol>
    <values>
        <key   constructor="bool" kwargs="field">ip.is_vpn</key>
        <const constructor="str"  kwargs="op">eq</const>
        <const constructor="bool" kwargs="value">1</const>
        <key   constructor="str"  kwargs="group_by" optional="true">ip</key>   <!-- alert identity -->
    </values>

Operators: eq, ne, in, not_in, gt, gte, lt, lte, contains, startswith, regex.
``in`` / ``not_in`` expect ``constructor="list"`` (comma separated) for ``value``.
Without ``group_by`` the alert identity is the matched field value.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Any, Optional

from .base import BaseDetection, DetectionContext, Finding

OPERATORS = ("eq", "ne", "in", "not_in", "gt", "gte", "lt", "lte", "contains", "startswith", "regex")


@lru_cache(maxsize=256)
def _regex(pattern: str) -> "re.Pattern":
    return re.compile(pattern)


def _same(a: Any, b: Any) -> bool:
    return a == b or str(a) == str(b)


def _compare(op: str, actual: Any, expected: Any) -> bool:
    if op == "eq":
        return _same(actual, expected)
    if op == "ne":
        return not _same(actual, expected)
    if op == "in":
        return any(_same(actual, item) for item in expected)
    if op == "not_in":
        return not any(_same(actual, item) for item in expected)
    if op in ("gt", "gte", "lt", "lte"):
        a, b = float(actual), float(expected)
        return {"gt": a > b, "gte": a >= b, "lt": a < b, "lte": a <= b}[op]
    if op == "contains":
        return str(expected) in str(actual)
    if op == "startswith":
        return str(actual).startswith(str(expected))
    return _regex(str(expected)).search(str(actual)) is not None


class FieldMatchDetection(BaseDetection):
    def __init__(self, field=None, *, op: str = "eq", value: Any = None, group_by: Any = None):
        super().__init__()
        self.field = field
        self.op = op
        self.value = value
        self.group_by = group_by

    @classmethod
    def validate(cls, *, op: str = "eq", value: Any = None, **_) -> None:
        if op not in OPERATORS:
            raise ValueError(f"op must be one of {', '.join(OPERATORS)}")
        if op == "regex" and value is not None:
            _regex(str(value))
        if op in ("in", "not_in") and value is not None and not isinstance(value, list):
            raise ValueError(f"op={op} needs value with constructor=\"list\"")

    def evaluate(self, ctx: DetectionContext) -> Optional[Finding]:
        if self.field is None:
            return None
        try:
            if not _compare(self.op, self.field, self.value):
                return None
        except (TypeError, ValueError):
            return None  # e.g. numeric comparison on a non-numeric value: simply no match
        field_name = ctx.keys.get("field")
        group_key = ctx.keys.get("group_by") if self.group_by is not None else field_name
        group_value = self.group_by if self.group_by is not None else self.field
        return Finding(
            message=(f"{field_name} {self.op} {self.value!r}" if self.op == "eq"
                     else f"{field_name} {self.op} {self.value!r} (value: {self.field!r})"),
            group_key=group_key,
            group_value=group_value,
            details={"field": field_name, "op": self.op, "expected": self.value, "actual": self.field},
        )


export = FieldMatchDetection
