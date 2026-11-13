from __future__ import annotations

import time
from typing import Callable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from ..processor import Processor
from ..store import Store

HOUR, DAY = 3_600_000, 86_400_000

RANGES = {
    "1h": (HOUR, 60_000),
    "6h": (6 * HOUR, 300_000),
    "24h": (DAY, 900_000),
    "7d": (7 * DAY, 2 * HOUR),
    "30d": (30 * DAY, 12 * HOUR),
}
SEVERITIES = ("critical", "high", "medium", "low")
SEVERITY_COLOR = {"critical": "var(--st-crit)", "high": "var(--st-serious)",
                  "medium": "var(--st-warn)", "low": "#8fc1ff"}
STATE_TO_STATUS = {"firing": "open", "acknowledged": "acknowledged", "resolved": "resolved"}
STATUS_TO_STATE = {v: k for k, v in STATE_TO_STATUS.items()}
SPARK_POINTS, HEALTH_CELLS, TREND_POINTS, MAX_SERIES = 24, 48, 12, 8
HEALTH_RANK = {"ok": 0, "degraded": 1, "down": 2}
HEALTH_NAME = {v: k for k, v in HEALTH_RANK.items()}


def health_of_severity(severity: str) -> str:
    """A firing alert degrades the health of its source: critical = down, high/medium = degraded."""
    return {"critical": "down", "high": "degraded", "medium": "degraded"}.get(severity, "ok")


def worst_health(severities) -> str:
    return HEALTH_NAME[max((HEALTH_RANK[health_of_severity(s)] for s in severities), default=0)]


def build_router(processor: Processor, authenticate: Callable) -> APIRouter:
    router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"], dependencies=[Depends(authenticate)])

    def store() -> Store:
        return processor.store

    def window(range_id: str) -> tuple[int, int, int, int]:
        """``(start, end, previous_start, step)`` for a range id, ending now."""
        if range_id not in RANGES:
            raise HTTPException(400, f"range must be one of {', '.join(RANGES)}")
        span, step = RANGES[range_id]
        end = int(time.time() * 1000)
        return end - span, end, end - 2 * span, step

    def catalogue() -> dict[str, dict[str, set[str]]]:
        """``{application: {source: {event types}}}`` from the XML, plus anything seen in the data.
        A ``*`` wildcard in the XML is not a name, so it is skipped (the data supplies the real ones)."""
        out: dict[str, dict[str, set[str]]] = {}
        for app in processor.engine.registry.describe():
            sources = out.setdefault(app["name"], {})
            for src in app["sources"]:
                if src["name"] == "*":
                    continue
                sources.setdefault(src["name"], set()).update(e["name"] for e in src["events"] if e["name"] != "*")
        for application, source, event_type in store().event_scopes():
            out.setdefault(application, {}).setdefault(source, set()).add(event_type)
        return out

    def scope_sources(app: Optional[str], source: Optional[str]) -> list[tuple[str, str]]:
        return [(a, s) for a, sources in catalogue().items() if app in (None, a)
                for s in sources if source in (None, s)]

    def dense(points: list, times: list[int], per_minute: float = 0, end: Optional[int] = None) -> list[float]:
        """Fill a sparse ``[[bucket, n]]`` list onto ``times``. With ``per_minute`` the counts become
        events/minute; the unfinished last bucket is divided by the time it really covered (at least
        half a bucket, so a bucket that just opened does not spike)."""
        lookup = dict(points)
        step = (times[1] - times[0]) if len(times) > 1 else 60_000
        values = []
        for t in times:
            n = lookup.get(t, 0)
            if per_minute:
                covered = step if end is None or t + step <= end else max(end - t, step / 2)
                n = round(n * 60_000 / covered, 2)
            values.append(n)
        return values

    def times_for(start: int, end: int, step: int) -> list[int]:
        return list(range(start // step * step, end, step))

    def count_series(group_by: Optional[str], f: dict, start: int, end: int, step: int, rate: bool = False) -> dict:
        times = times_for(start, end, step)
        rows = store().timeseries(step, group_by=group_by, top=MAX_SERIES, **f, start=start, end=end)
        series = [{"id": str(r["group"] if r["group"] is not None else "events"),
                   "name": str(r["group"] if r["group"] is not None else "Events"),
                   "values": dense(r["points"], times, 1 if rate else 0, end)} for r in rows]
        series.sort(key=lambda s: s["name"])        # stable order = stable colours
        if not series:
            series = [{"id": "events", "name": "Events", "values": [0] * len(times)}]
        return {"times": times, "series": series}

    def kpi(id_: str, label: str, value: float, previous: Optional[float], spark: Optional[list] = None,
            higher_is_better: Optional[bool] = None) -> dict:
        return {"id": id_, "label": label, "unit": "count", "value": value, "previous": previous,
                "spark": spark or [], "higherIsBetter": higher_is_better}

    def alert_item(a: dict) -> dict:
        d = a.get("details") or {}
        metric = None
        if "distinct_count" in d:
            metric = {"label": f"Distinct {d.get('distinct_field', 'values')} in {d.get('window', 'window')}",
                      "unit": "count", "value": d["distinct_count"], "threshold": d.get("threshold")}
        elif "count" in d:
            metric = {"label": f"Events in {d.get('window', 'window')}", "unit": "count",
                      "value": d["count"], "threshold": d.get("threshold")}
        state = STATUS_TO_STATE[a["status"]]
        return {
            "id": a["id"], "severity": a["severity"], "state": state, "title": a["title"],
            "description": a["message"], "rule": a["rule_name"],
            "app": a["application"], "source": a["source"], "eventType": a["event_type"],
            "group": {"key": a["group_key"], "value": a["group_value"]} if a["group_key"] else None,
            "firedAt": a["first_seen_ms"], "lastSeenAt": a["last_seen_ms"], "occurrences": a["occurrences"],
            "acknowledgedAt": a.get("acknowledged_at_ms"), "acknowledgedBy": None,
            "resolvedAt": a.get("resolved_at_ms"), "note": a["note"], "metric": metric,
        }

    def firing_counts(app: Optional[str] = None, source: Optional[str] = None) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITIES, 0)
        for (a, s), by_sev in store().open_alert_counts().items():
            if app in (None, a) and source in (None, s):
                for sev, n in by_sev.items():
                    counts[sev] = counts.get(sev, 0) + n
        return counts

    def health_cells(spans: list, start: int, end: int) -> dict[tuple[str, str], list[dict]]:
        """Per source, ``HEALTH_CELLS`` blocks holding the worst state of any alert active in the block."""
        width = (end - start) / HEALTH_CELLS
        ranks: dict[tuple[str, str], list[int]] = {}
        for application, source, severity, first, last in spans:
            cells = ranks.setdefault((application, source), [0] * HEALTH_CELLS)
            lo = max(int((first - start) // width), 0)
            hi = min(int((last - start) // width), HEALTH_CELLS - 1)
            rank = HEALTH_RANK[health_of_severity(severity)]
            for i in range(lo, hi + 1):
                cells[i] = max(cells[i], rank)
        return {k: [{"t": int(start + i * width), "status": HEALTH_NAME[r]} for i, r in enumerate(cells)]
                for k, cells in ranks.items()}

    def empty_health(start: int, end: int) -> list[dict]:
        width = (end - start) / HEALTH_CELLS
        return [{"t": int(start + i * width), "status": "ok"} for i in range(HEALTH_CELLS)]

    @router.get("/apps")
    def apps():
        """Sidebar: every application with its sources, firing alert counts and current health."""
        open_by_scope = store().open_alert_counts()
        out = []
        for application, sources in catalogue().items():
            entries, app_sev = [], []
            for source in sorted(sources):
                by_sev = open_by_scope.get((application, source), {})
                sev_list = [sev for sev, n in by_sev.items() for _ in range(n)]
                app_sev += sev_list
                entries.append({"id": source, "name": source, "status": worst_health(sev_list),
                                "firingAlerts": len(sev_list), "eventTypes": sorted(sources[source])})
            out.append({"id": application, "name": application, "status": worst_health(app_sev),
                        "firingAlerts": len(app_sev), "sources": entries})
        return out

    @router.get("/overview")
    def overview(app: Optional[str] = None, source: Optional[str] = None, range_id: str = Query("24h", alias="range")):
        start, end, prev_start, step = window(range_id)
        scope = dict(application=app, source=source)
        alert_scope = dict(application=app, source=source)
        st = store()

        events_now = st.count_events(**scope, start=start, end=end)
        events_prev = st.count_events(**scope, start=prev_start, end=start)
        users_now = st.count_distinct("user_id", **scope, start=start, end=end)
        users_prev = st.count_distinct("user_id", **scope, start=prev_start, end=start)
        raised_now = st.alerts_raised(start, end, **alert_scope)
        raised_prev = st.alerts_raised(prev_start, start, **alert_scope)
        raised_spark = [0] * SPARK_POINTS
        width = (end - start) / SPARK_POINTS
        for by_bucket in st.alert_buckets(int(width) or 1, start, end, **alert_scope).values():
            for bucket, n in by_bucket.items():
                raised_spark[min(max(int((bucket - start) // width), 0), SPARK_POINTS - 1)] += n
        kpis = [
            kpi("events", "Events", events_now, events_prev, st.histogram(start, end, SPARK_POINTS, **scope)[()]),
            kpi("alerts", "Alerts raised", raised_now, raised_prev, raised_spark, higher_is_better=False),
            kpi("firing", "Firing now", sum(firing_counts(app, source).values()), None, higher_is_better=False),
            kpi("users", "Active users", users_now, users_prev),
        ]

        group = "application" if app is None else "source" if source is None else None
        traffic = count_series(group, scope, start, end, step, rate=True)
        mix = count_series("event_type", scope, start, end, step, rate=True)

        times = times_for(start, end, step)
        by_sev = st.alert_buckets(step, start, end, **alert_scope)
        alerts_chart = {"times": times, "series": [
            {"id": sev, "name": sev.capitalize(), "color": SEVERITY_COLOR[sev],
             "values": [by_sev.get(sev, {}).get(t, 0) for t in times]} for sev in SEVERITIES]}

        return {"kpis": kpis, "traffic": traffic, "alerts": alerts_chart, "eventMix": mix}

    @router.get("/sources")
    def sources(app: Optional[str] = None, source: Optional[str] = None, range_id: str = Query("24h", alias="range")):
        start, end, _, _ = window(range_id)
        st = store()
        scope = dict(application=app, source=source)
        stats = st.by_source(**scope, start=start, end=end)
        last_seen = st.by_source(**scope)
        sparks = st.histogram(start, end, SPARK_POINTS, ("application", "source"), **scope)
        health = health_cells(st.alert_spans(start, end, **scope), start, end)
        open_by_scope = st.open_alert_counts()
        catalog = catalogue()

        rows = []
        for application, name in scope_sources(app, source):
            key = (application, name)
            by_sev = open_by_scope.get(key, {})
            sev_list = [sev for sev, n in by_sev.items() for _ in range(n)]
            count = stats.get(key, {}).get("count", 0)
            minutes = (end - start) / 60_000
            rows.append({
                "id": name, "name": name, "app": application,
                "status": worst_health(sev_list), "events": count,
                "perMinute": round(count / minutes, 2), "spark": sparks.get(key, [0] * SPARK_POINTS),
                "eventTypes": len(catalog[application][name]),
                "firingAlerts": len(sev_list), "worstSeverity": next((s for s in SEVERITIES if by_sev.get(s)), None),
                "lastEventAt": last_seen.get(key, {}).get("last_ts"),
                "health": health.get(key) or empty_health(start, end),
            })
        return rows

    @router.get("/events")
    def events(app: Optional[str] = None, source: Optional[str] = None, range_id: str = Query("24h", alias="range"),
               type: Optional[str] = None):
        start, end, prev_start, step = window(range_id)
        st = store()
        scope = dict(application=app, source=source, event_type=type)

        total = st.count_events(**scope, start=start, end=end)
        kpis = [
            kpi("events", "Events", total, st.count_events(**scope, start=prev_start, end=start),
                st.histogram(start, end, SPARK_POINTS, **scope)[()]),
            kpi("users", "Active users", st.count_distinct("user_id", **scope, start=start, end=end),
                st.count_distinct("user_id", **scope, start=prev_start, end=start)),
            kpi("types", "Event types", st.count_distinct("event_type", **scope, start=start, end=end), None),
            kpi("errors", "Processing errors", st.count_with_errors(**scope, start=start, end=end),
                st.count_with_errors(**scope, start=prev_start, end=start), higher_is_better=False),
        ]

        timeline = count_series("event_type", scope, start, end, step)
        by_source = sorted(st.by_source(**scope, start=start, end=end).items(), key=lambda kv: -kv[1]["count"])
        groups = []
        for g in st.event_groups(start, end, TREND_POINTS, **scope):
            sample = g["sample"] or {}
            groups.append({
                "id": f"{g['application']}/{g['source']}/{g['event_type']}",
                "name": g["event_type"], "app": g["application"], "source": g["source"],
                "count": g["count"], "trend": g["trend"], "firstSeen": g["first_ts"], "lastSeen": g["last_ts"],
                "sample": {"eventId": sample.get("event_id"), "userId": sample.get("user_id"),
                           "sessionId": sample.get("session_id"), "timestamp": sample.get("timestamp_ms"),
                           "dims": sample.get("dims") or {}, "enriched": sample.get("enriched") or {},
                           "errors": sample.get("errors") or []},
            })
        return {
            "kpis": kpis, "timeline": timeline,
            "bySource": [{"id": s, "name": s, "app": a, "count": v["count"]} for (a, s), v in by_source],
            "groups": groups,
        }

    @router.get("/alerts")
    def alerts(app: Optional[str] = None, source: Optional[str] = None, state: Optional[str] = None,
               severity: Optional[str] = None, limit: int = Query(100, ge=1, le=1000)):
        if state is not None and state not in STATE_TO_STATUS:
            raise HTTPException(400, f"state must be one of {', '.join(STATE_TO_STATUS)}")
        if severity is not None and severity not in SEVERITIES:
            raise HTTPException(400, f"severity must be one of {', '.join(SEVERITIES)}")
        items, _ = store().list_alerts(
            limit=1000, application=app, source=source,
            status=[STATE_TO_STATUS[state]] if state else None, severity=[severity] if severity else None)
        if state == "firing":                      # most severe first, then newest
            items.sort(key=lambda a: (SEVERITIES.index(a["severity"]) if a["severity"] in SEVERITIES else 9,
                                      -a["last_seen_ms"]))
        return {"counts": firing_counts(app, source), "items": [alert_item(a) for a in items[:limit]]}

    def transition(alert_id: int, status: str) -> dict:
        current = store().get_alert(alert_id)
        if current is None:
            raise HTTPException(404, "alert not found")
        if current["status"] == "resolved":
            raise HTTPException(409, "this alert is already resolved")
        return alert_item(store().update_alert(alert_id, status=status))

    @router.post("/alerts/{alert_id}/ack")
    def acknowledge(alert_id: int):
        return transition(alert_id, "acknowledged")

    @router.post("/alerts/{alert_id}/resolve")
    def resolve(alert_id: int):
        return transition(alert_id, "resolved")

    return router
