from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from services.detection.base import DetectionContext, Finding

from .casting import cast
from .config import Settings
from .event import Event, RawEvent, extract, loads_object
from .spec import SEVERITIES, EventSpec, Registry, RuleSpec, ValueSpec
from .store import AlertCandidate, Store
from .store import now_ms as wall_clock_ms
from .symbols import resolve

log = logging.getLogger(__name__)


def rule_id(spec: EventSpec, rule: RuleSpec) -> str:
    return f"{spec.key}#{rule.name}"

@dataclass
class RuleRuntime:
    rule_id: str
    digest: str
    cls: type
    const: dict
    state: dict = field(default_factory=dict)
    last_ts: int = 0
    spec: Optional[EventSpec] = None
    rule: Optional[RuleSpec] = None


def build_args(values: tuple[ValueSpec, ...], event: Event) -> Optional[tuple[list, dict, dict]]:
    args: list = []
    kwargs: dict = {}
    keys: dict = {}

    for index, spec in enumerate(values):
        if spec.kind == "key":
            raw = event.get(spec.raw)
            if raw is None:
                if not spec.optional:
                    return None
                value = None
            else:
                value = cast(spec.constructor, raw)
            keys[spec.kwarg or index] = spec.raw
        else:
            value = cast(spec.constructor, spec.raw)
        if spec.kwarg:
            kwargs[spec.kwarg] = value
        else:
            args.append(value)
    return args, kwargs, keys


class Engine:
    def __init__(self, store: Store, settings: Settings):
        self.store = store
        self.settings = settings
        self.registry = Registry()
        self._runtimes: dict[str, RuleRuntime] = {}

    def apply(self, registry: Registry) -> dict:
        """Install a new registry. Detection state of unchanged rules is kept; new or changed
        rules are rebuilt by replaying recent stored events (silently)."""
        runtimes: dict[str, RuleRuntime] = {}
        to_warm: list[tuple[EventSpec, RuleSpec, RuleRuntime]] = []

        for spec in registry.events():
            for rule in spec.alerts:
                if not rule.enabled:
                    continue
                rid = rule_id(spec, rule)
                previous = self._runtimes.get(rid)
                if previous is not None and previous.digest == rule.digest():
                    previous.spec, previous.rule = spec, rule
                    runtimes[rid] = previous
                    continue
                runtime = RuleRuntime(rid, rule.digest(), resolve(rule.symbol), rule.const_kwargs(),
                                      spec=spec, rule=rule)
                runtimes[rid] = runtime
                to_warm.append((spec, rule, runtime))
        self._runtimes = runtimes
        self.registry = registry

        replayed = 0
        for spec, rule, runtime in to_warm:
            replayed += self._warm_up(spec, rule, runtime)
        return {"rules": len(runtimes), "rebuilt": len(to_warm), "replayed_events": replayed}

    def _warm_up(self, spec: EventSpec, rule: RuleSpec, runtime: RuleRuntime) -> int:
        try:
            retention = rule.warmup_ms if rule.warmup_ms is not None else runtime.cls.retention_ms(**runtime.const)
            if retention <= 0:
                return 0
            latest = self.store.latest_ts(spec.application, spec.source, spec.name)
            if latest is None:
                return 0
            count = 0
            for event in self.store.replay(spec.application, spec.source, spec.name, latest - retention):
                if self.registry.lookup(event.application, event.source, event.event_type) is not spec:
                    continue
                self._evaluate(rule, runtime, event, silent=True)
                count += 1
            if count:
                log.info("rule %s: rebuilt state from %d stored events", runtime.rule_id, count)
            return count
        except Exception:
            log.exception("warm-up of rule %s failed (it will start with an empty state)", runtime.rule_id)
            return 0

    def sweep(self) -> None:
        for runtime in self._runtimes.values():
            try:
                runtime.cls.sweep(runtime.state, runtime.last_ts, **runtime.const)
            except Exception:
                log.exception("sweep of rule %s failed", runtime.rule_id)

    def tick(self, now_ms: Optional[int] = None) -> list[dict]:
        """Run the time-driven part of every rule (``cls.tick``), e.g. absence of events, and store the
        resulting alerts. ``now_ms`` is the wall clock. Returns the *newly created* alerts."""
        now = wall_clock_ms() if now_ms is None else now_ms
        candidates: list[AlertCandidate] = []
        for runtime in self._runtimes.values():
            try:
                findings = runtime.cls.tick(runtime.state, now, **runtime.const)
            except Exception:
                log.exception("tick of rule %s failed", runtime.rule_id)
                continue
            for finding in findings:
                spec = runtime.spec
                candidates.append(self._build_candidate(
                    spec, runtime.rule, runtime, finding, spec.source, spec.name, finding.event_id or "", now))
        return self.store.write_alerts(candidates) if candidates else []

    def process(self, raw: RawEvent) -> tuple[Optional[Event], list[AlertCandidate]]:
        """Returns ``(None, [])`` when no application definition matches the event."""
        spec = self.registry.lookup(raw.application, raw.source, raw.event_type)
        if spec is None:
            return None, []
        event = self._extract(raw, spec)
        self._enrich(event, spec)
        return event, self._detect(event, spec)

    @staticmethod
    def _extract(raw: RawEvent, spec: EventSpec) -> Event:
        event = Event(raw.id, raw.event_id, raw.application, raw.source, raw.event_type,
                      raw.timestamp_ms, raw.received_at_ms, raw.user_id, raw.session_id)
        payload = loads_object(raw.payload_json)
        if payload is None:
            event.errors.append("payload_json is not a JSON object")
        attributes = loads_object(raw.attributes)
        for dim in spec.dimensions:
            value = extract(dim.key, payload, attributes, raw)
            if value is None:
                continue
            if dim.constructor:
                try:
                    value = cast(dim.constructor, value)
                except Exception as exc:
                    event.errors.append(f"dimension {dim.key}: cannot cast {value!r} to {dim.constructor} ({exc})")
                    continue
            event.dims[dim.key] = value
        return event

    @staticmethod
    def _enrich(event: Event, spec: EventSpec) -> None:
        for rule in spec.enrichment:
            if not rule.enabled:
                continue
            try:
                built = build_args(rule.values, event)
                if built is None:
                    continue
                args, kwargs, _ = built
                with resolve(rule.symbol)(*args, **kwargs) as enrichment:
                    result = enrichment.run()
            except Exception as exc:
                log.debug("enrichment %s failed", rule.name, exc_info=True)
                event.errors.append(f"enrichment {rule.name}: {type(exc).__name__}: {exc}")
                continue
            if result is None:
                continue
            current = event.enriched.get(rule.as_name)
            if isinstance(current, dict) and isinstance(result, dict):
                current.update(result)
            else:
                event.enriched[rule.as_name] = result

    def _detect(self, event: Event, spec: EventSpec) -> list[AlertCandidate]:
        candidates: list[AlertCandidate] = []
        for rule in spec.alerts:
            runtime = self._runtimes.get(rule_id(spec, rule))
            if runtime is None:
                continue
            try:
                finding = self._evaluate(rule, runtime, event, silent=False)
            except Exception as exc:
                log.debug("alert rule %s failed", rule.name, exc_info=True)
                event.errors.append(f"alert {rule.name}: {type(exc).__name__}: {exc}")
                continue
            if finding is not None:
                candidates.append(self._candidate(spec, rule, runtime, finding, event))
        return candidates

    def _evaluate(self, rule: RuleSpec, runtime: RuleRuntime, event: Event, silent: bool) -> Optional[Finding]:
        built = build_args(rule.values, event)
        if built is None:
            return None
        args, kwargs, keys = built
        ts = event.ts(self.settings.time_field)
        runtime.last_ts = max(runtime.last_ts, ts)
        ctx = DetectionContext(event, runtime.state, ts, keys, rule.name, silent)
        with runtime.cls(*args, **kwargs) as detection:
            return detection.evaluate(ctx)

    def _candidate(self, spec: EventSpec, rule: RuleSpec, runtime: RuleRuntime,
                   finding: Finding, event: Event) -> AlertCandidate:
        return self._build_candidate(spec, rule, runtime, finding, event.source, event.event_type,
                                     event.event_id, event.ts(self.settings.time_field))

    @staticmethod
    def _build_candidate(spec: EventSpec, rule: RuleSpec, runtime: RuleRuntime, finding: Finding,
                         source: str, event_type: str, event_id: str, ts_ms: int) -> AlertCandidate:
        severity = finding.severity if finding.severity in SEVERITIES else rule.severity
        return AlertCandidate(
            rule_id=runtime.rule_id, rule_name=rule.name, symbol=rule.symbol,
            application=spec.application, source=source, event_type=event_type,
            severity=severity, title=finding.title or rule.name, message=finding.message,
            group_key=finding.group_key,
            group_value=None if finding.group_value is None else str(finding.group_value),
            details=finding.details, event_id=event_id, ts_ms=ts_ms, cooldown_ms=rule.cooldown_ms)
