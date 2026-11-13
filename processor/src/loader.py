"""Load and validate ``applications/*.xml``.

Validation is deliberately strict and happens *before* any event is processed: unknown
modules, wrong argument names, unknown constructors, keys that do not exist, bad durations...
are all reported together, with the XML path they come from.
"""
from __future__ import annotations

import hashlib
import inspect
import logging
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

from .casting import CONSTRUCTORS, cast
from .duration import parse_duration
from .event import COLUMNS
from .spec import (SEVERITIES, ApplicationSpec, DimensionSpec, EventSpec, Registry,
                   RuleSpec, ValueSpec)
from .symbols import SymbolError, resolve

log = logging.getLogger(__name__)

_TRUE = {"1", "true", "yes", "on"}
_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")


class SpecErrors(Exception):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("invalid application definition:\n  - " + "\n  - ".join(errors))


def _flag(element: ET.Element, attr: str, default: bool) -> bool:
    raw = element.get(attr)
    return default if raw is None else raw.strip().lower() in _TRUE


def _text(element: Optional[ET.Element]) -> str:
    return (element.text or "").strip() if element is not None else ""


class _Parser:
    def __init__(self, path: Path):
        self.path = path
        self.errors: list[str] = []

    def err(self, where: str, message: str) -> None:
        self.errors.append(f"{self.path.name}: {where}: {message}")

    def values(self, parent: Optional[ET.Element], where: str) -> tuple[ValueSpec, ...]:
        out: list[ValueSpec] = []
        if parent is None:
            return ()
        seen_kwargs: set[str] = set()
        for i, el in enumerate(parent):
            loc = f"{where}/values/{el.tag}[{i}]"
            if el.tag not in ("key", "const"):
                self.err(loc, "expected <key> or <const>")
                continue
            constructor = el.get("constructor", "str")
            if constructor not in CONSTRUCTORS:
                self.err(loc, f"unknown constructor {constructor!r} (allowed: {', '.join(CONSTRUCTORS)})")
                continue
            kwarg = el.get("kwargs")
            if kwarg:
                if not kwarg.isidentifier():
                    self.err(loc, f"kwargs={kwarg!r} is not a valid python identifier")
                    continue
                if kwarg in seen_kwargs:
                    self.err(loc, f"duplicate kwargs={kwarg!r}")
                    continue
                seen_kwargs.add(kwarg)
            elif seen_kwargs:
                self.err(loc, "positional value after a keyword value")
                continue
            raw = _text(el) if el.tag == "key" else (el.text or "").strip()
            if el.tag == "key" and not raw:
                self.err(loc, "<key> is empty")
                continue
            if el.tag == "const":
                try:
                    cast(constructor, raw)
                except Exception as exc:
                    self.err(loc, f"cannot cast {raw!r} with constructor {constructor!r}: {exc}")
                    continue
            out.append(ValueSpec(el.tag, raw, constructor, kwarg or None, _flag(el, "optional", False)))
        return tuple(out)

    def check_symbol(self, kind: str, symbol: str, values: tuple[ValueSpec, ...], where: str) -> bool:
        from services.detection.base import BaseDetection
        from services.enrichment.base import BaseEnrichment
        expected = BaseEnrichment if kind == "enrichment" else BaseDetection
        try:
            cls = resolve(symbol)
        except SymbolError as exc:
            self.err(where, str(exc))
            return False
        if not issubclass(cls, expected):
            self.err(where, f"{symbol!r} exports {cls.__name__}, which is not a {expected.__name__}")
            return False
        positional = sum(1 for v in values if not v.kwarg)
        kwargs = {v.kwarg: None for v in values if v.kwarg}
        try:
            inspect.signature(cls).bind(*([None] * positional), **kwargs)
        except TypeError as exc:
            self.err(where, f"arguments do not match {cls.__name__}(): {exc}")
            return False
        try:
            cls.validate(**{v.kwarg: cast(v.constructor, v.raw) for v in values if v.kind == "const" and v.kwarg})
        except Exception as exc:
            self.err(where, f"invalid configuration: {exc}")
            return False
        return True

    def check_keys(self, values: tuple[ValueSpec, ...], known_dims: set[str],
                   enriched: set[str], where: str) -> None:
        for v in values:
            if v.kind != "key" or v.optional:
                continue
            ref = v.raw
            ok = (ref in known_dims or ref in COLUMNS
                  or ref.startswith("dims.") and ref[5:] in known_dims
                  or ref.startswith("enriched.") and ref[9:].split(".")[0] in enriched
                  or "." in ref and ref.split(".")[0] in enriched)
            if not ok:
                hint = f"declared dimensions: {sorted(known_dims) or 'none'}"
                if enriched:
                    hint += f", enrichments: {sorted(enriched)}"
                self.err(where, f"<key>{ref}</key> is not a declared dimension, column or enrichment ({hint})")

    def rule(self, kind: str, el: ET.Element, idx: int, where: str,
             known_dims: set[str], enriched: set[str]) -> Optional[RuleSpec]:
        symbol = _text(el.find("pysymbol"))
        name = el.get("name") or (symbol.rsplit(".", 1)[-1] if symbol else f"rule{idx}")
        loc = f"{where}/{kind}[{name}]"
        if not symbol:
            self.err(loc, "missing <pysymbol>")
            return None
        before = len(self.errors)
        values = self.values(el.find("values"), loc)
        if len(self.errors) > before:
            return None  # value errors already reported

        as_name = None
        if kind == "enrichment":
            as_name = _text(el.find("as"))
            if not as_name:
                self.err(loc, "missing <as> (the key the enrichment result is stored under)")
                return None
            if not _NAME.match(as_name):
                self.err(loc, f"<as>{as_name}</as> must be a simple name (letters, digits, _ or -)")
                return None

        severity, cooldown_ms, warmup_ms = "medium", 300_000, None
        if kind == "alert":
            severity = (el.get("severity") or "medium").lower()
            if severity not in SEVERITIES:
                self.err(loc, f"severity {severity!r} must be one of {SEVERITIES}")
                return None
            try:
                cooldown_ms = parse_duration(el.get("cooldown", "5m"))
                if el.get("warmup") is not None:
                    warmup_ms = parse_duration(el.get("warmup"))
            except ValueError as exc:
                self.err(loc, str(exc))
                return None

        if not self.check_symbol(kind, symbol, values, loc):
            return None
        self.check_keys(values, known_dims, enriched, loc)
        return RuleSpec(kind, name, symbol, values, as_name, severity, cooldown_ms,
                        warmup_ms, _flag(el, "enabled", True))

    def application(self, root: ET.Element) -> Optional[ApplicationSpec]:
        if root.tag != "application":
            self.err("/", f"root element must be <application>, found <{root.tag}>")
            return None
        app = root.get("name", "").strip()
        if not app:
            self.err("application", "missing name attribute")
            return None

        events: list[EventSpec] = []
        seen: set[tuple[str, str]] = set()
        for source_el in root.findall("source"):
            source = source_el.get("name", "").strip()
            if not source:
                self.err(f"application[{app}]", "<source> without name attribute")
                continue
            for ev_el in source_el.findall("events/event"):
                name = ev_el.get("name", "").strip()
                where = f"application[{app}]/source[{source}]/event[{name}]"
                if not name:
                    self.err(where, "<event> without name attribute")
                    continue
                if (source, name) in seen:
                    self.err(where, "duplicate event")
                    continue
                seen.add((source, name))
                event = self.event(app, source, name, ev_el, where)
                if event:
                    events.append(event)
        if not events:
            self.err(f"application[{app}]", "defines no events")
        return ApplicationSpec(app, self.path.name, tuple(events))

    def event(self, app: str, source: str, name: str, el: ET.Element, where: str) -> Optional[EventSpec]:
        dims: list[DimensionSpec] = []
        for key_el in el.findall("dimensions/key"):
            key = _text(key_el)
            constructor = key_el.get("constructor")
            if not key:
                self.err(where, "empty <key> in <dimensions>")
            elif any(d.key == key for d in dims):
                self.err(where, f"duplicate dimension {key!r}")
            elif constructor and constructor not in CONSTRUCTORS:
                self.err(where, f"dimension {key!r}: unknown constructor {constructor!r}")
            else:
                dims.append(DimensionSpec(key, constructor))
        known = {d.key for d in dims}

        enrichment: list[RuleSpec] = []
        enriched: set[str] = set()
        names: set[str] = set()
        for i, rule_el in enumerate(el.findall("enrichment/rule")):
            rule = self.rule("enrichment", rule_el, i, where, known, enriched)
            if rule is None:
                continue
            if rule.name in names:
                self.err(where, f"duplicate enrichment rule name {rule.name!r}")
                continue
            names.add(rule.name)
            enrichment.append(rule)
            enriched.add(rule.as_name)

        alerts: list[RuleSpec] = []
        names = set()
        for i, rule_el in enumerate(el.findall("alerts/rule")):
            rule = self.rule("alert", rule_el, i, where, known, enriched)
            if rule is None:
                continue
            if rule.name in names:
                self.err(where, f"duplicate alert rule name {rule.name!r}")
                continue
            names.add(rule.name)
            alerts.append(rule)
        return EventSpec(app, source, name, tuple(dims), tuple(enrichment), tuple(alerts))


def parse_file(path: Path) -> tuple[Optional[ApplicationSpec], list[str]]:
    parser = _Parser(path)
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        return None, [f"{path.name}: not well-formed XML ({exc})"]
    app = parser.application(root)
    return app, parser.errors


def load_registry(directory: Path) -> Registry:
    """Parse every ``*.xml`` of ``directory``. Raises :class:`SpecErrors` listing *all* problems."""
    directory = Path(directory)
    files = sorted(directory.glob("*.xml")) if directory.is_dir() else []
    if not files:
        log.warning("no application definitions found in %s - every event will be ignored", directory)

    apps: list[ApplicationSpec] = []
    errors: list[str] = []
    digest = hashlib.sha1()
    for path in files:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
        app, errs = parse_file(path)
        errors.extend(errs)
        if app is not None:
            if any(a.name == app.name for a in apps):
                errors.append(f"{path.name}: application {app.name!r} is already defined in {next(a.path for a in apps if a.name == app.name)}")
            else:
                apps.append(app)
    if errors:
        raise SpecErrors(errors)
    return Registry(tuple(apps), digest.hexdigest()[:12])


def fingerprint(directory: Path) -> tuple:
    """Cheap change detector for hot reload (names + mtimes + sizes)."""
    directory = Path(directory)
    if not directory.is_dir():
        return ()
    return tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in sorted(directory.glob("*.xml")))
