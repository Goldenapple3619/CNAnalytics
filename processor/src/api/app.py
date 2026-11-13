from __future__ import annotations

import asyncio
import json
import secrets
import time
from contextlib import contextmanager
from typing import Literal, Optional

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from ..config import Settings
from ..processor import Processor
from ..store import ALERT_STATUSES
from .dashboard import build_router as dashboard_router
from .timeparse import parse_time, pick_step

SEVERITY_ORDER = ("critical", "high", "medium", "low")


class AlertUpdate(BaseModel):
    status: Optional[Literal["open", "acknowledged", "resolved"]] = None
    note: Optional[str] = None


@contextmanager
def bad_request():
    """Report invalid user input (bad field name, time, interval ...) as HTTP 400."""
    try:
        yield
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


class EventFilters:
    """Query parameters shared by every endpoint that filters events."""
    def __init__(
        self,
        application: Optional[str] = None,
        source: Optional[str] = None,
        event_type: Optional[str] = None,
        user_id: Optional[str] = None,
        start: Optional[str] = Query(None, alias="from", description="epoch ms, ISO-8601 or -15m"),
        to: Optional[str] = Query(None, description="epoch ms, ISO-8601 or 'now'"),
        where: list[str] = Query([], description="repeatable field=value filter, e.g. where=ip=1.2.3.4 "
                                                  "or where=ip.country_code=FR"),
    ):
        self.application, self.source, self.event_type, self.user_id = application, source, event_type, user_id
        self.start_raw, self.end_raw, self.where_raw = start, to, where

    def resolve(self, default_start: Optional[str] = None, default_end: Optional[str] = None) -> dict:
        pairs = []
        for item in self.where_raw:
            field, sep, value = item.partition("=")
            if not sep or not field:
                raise ValueError(f"invalid where {item!r}: expected field=value")
            pairs.append((field, value))
        t = int(time.time() * 1000)
        return dict(application=self.application, source=self.source, event_type=self.event_type,
                    user_id=self.user_id,
                    start=parse_time(self.start_raw or default_start, t),
                    end=parse_time(self.end_raw or default_end, t), where=pairs)


def create_app(processor: Processor, settings: Settings) -> FastAPI:
    app = FastAPI(title="Analytics processor", version="1.0.0",
                  description="Processed events, enrichment results, metrics and alerts.")
    app.add_middleware(CORSMiddleware, allow_origins=list(settings.cors_origins),
                       allow_methods=["*"], allow_headers=["*"])

    def store():
        return processor.store

    def now() -> int:
        return int(time.time() * 1000)

    async def authenticate(request: Request) -> None:
        if not settings.api_key:
            return
        supplied = request.headers.get("x-api-key", "")
        if not supplied and request.url.path.endswith("/alerts/stream"):
            supplied = request.query_params.get("api_key", "")
        if not supplied or not secrets.compare_digest(supplied, settings.api_key):
            raise HTTPException(401, "missing or invalid API key", headers={"WWW-Authenticate": "ApiKey"})

    @app.get("/health", tags=["system"])
    def health():
        st = processor.status()
        ok = st["running"] and st.get("source_available", False)
        body = {"status": "ok" if ok else "degraded", "running": st["running"],
                "source_available": st.get("source_available"), "lag": st.get("lag")}
        return JSONResponse(body, status_code=200 if st["running"] else 503)

    @app.get("/", include_in_schema=False)
    def index():
        return {"service": "analytics processor", "docs": "/docs", "api": "/api/v1"}

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])

    @api.get("/status", tags=["system"])
    def status():
        """Worker state, counters, ingestion lag."""
        return {**processor.status(), "now_ms": now()}

    @api.post("/reload", tags=["system"])
    def reload():
        """Re-read applications/*.xml (also done automatically when the files change)."""
        result = processor.reload()
        if not result["ok"]:
            raise HTTPException(422, {"message": "application definitions rejected, previous ones kept",
                                      "errors": result["errors"]})
        return result

    @api.get("/applications", tags=["schema"])
    def applications():
        """Applications / sources / events / dimensions / enrichments / alert rules, straight from the XML.
        A dashboard can build its filters and widgets from this."""
        return {"version": processor.registry_version, "applications": processor.engine.registry.describe()}

    @api.get("/events", tags=["events"])
    def events(f: EventFilters = Depends(), limit: int = Query(100, ge=1, le=1000),
               before_id: Optional[int] = Query(None, description="pagination: return events older than this id"),
               after_id: Optional[int] = Query(None, description="live tail: return events newer than this id")):
        """Newest first. Page with ``next_before_id``."""
        with bad_request():
            filters = f.resolve()
            items = store().query_events(limit=limit, before_id=before_id, after_id=after_id, **filters)
        return {"items": items, "next_before_id": items[-1]["id"] if len(items) == limit else None}

    @api.get("/events/{event_id}", tags=["events"])
    def event(event_id: str):
        found = store().get_event(event_id)
        if found is None:
            raise HTTPException(404, "event not found")
        return found

    @api.get("/metrics/summary", tags=["metrics"])
    def summary(f: EventFilters = Depends()):
        """Header numbers: events in the window (default last hour) by type, alerts, processor lag."""
        with bad_request():
            filters = f.resolve(default_start="-1h", default_end="now")
        by_type = store().top("event_type", 100, **filters)
        return {
            "from": filters["start"], "to": filters["end"],
            "events": {"total": sum(t["count"] for t in by_type), "by_type": by_type},
            "alerts": store().alert_counts(filters["start"], filters["end"]),
            "processor": {k: v for k, v in processor.status().items()
                          if k in ("running", "lag", "last_event_ts_ms", "processed", "ignored", "errors")},
        }

    @api.get("/metrics/timeseries", tags=["metrics"])
    def timeseries(f: EventFilters = Depends(),
                   interval: Optional[str] = Query(None, description="bucket size: 30s, 5m, 1h ... (auto when omitted)"),
                   group_by: Optional[str] = Query(None, description="field, e.g. event_type, ip, ip.country_code"),
                   top: int = Query(5, ge=1, le=20, description="with group_by: number of series kept")):
        """Event counts per time bucket, zero-filled so charts need no post-processing."""
        with bad_request():
            filters = f.resolve(default_start="-1h", default_end="now")
            step = pick_step(filters["start"], filters["end"], interval)
            series = store().timeseries(step, group_by=group_by, top=top, **filters)
        first = filters["start"] // step * step
        buckets = list(range(first, filters["end"], step))
        dense = []
        for s in series:
            counts = dict(s["points"])
            dense.append({"group": s["group"], "points": [[b, counts.get(b, 0)] for b in buckets]})
        if not dense and group_by is None:
            dense = [{"group": None, "points": [[b, 0] for b in buckets]}]
        return {"from": filters["start"], "to": filters["end"], "interval_ms": step, "series": dense}

    @api.get("/metrics/top", tags=["metrics"])
    def top_values(field: str = Query(..., description="e.g. ip, user_agent.browser, ip.country_code, user_id"),
                   f: EventFilters = Depends(), limit: int = Query(10, ge=1, le=100)):
        """Most frequent values of a field over the window (default last hour)."""
        with bad_request():
            filters = f.resolve(default_start="-1h", default_end="now")
            items = store().top(field, limit, **filters)
        return {"field": field, "from": filters["start"], "to": filters["end"], "items": items}

    @api.get("/alerts/stream", tags=["alerts"])
    async def alert_stream(request: Request, after_rev: Optional[int] = None):
        """Server-Sent Events: one ``alert`` event whenever an alert is created or updated.
        Resume with ``Last-Event-ID`` (automatic for EventSource) or ``?after_rev=``."""
        header = request.headers.get("last-event-id")
        start_rev = after_rev if after_rev is not None else (int(header) if header and header.isdigit() else None)

        async def generate():
            rev = start_rev if start_rev is not None else await asyncio.to_thread(store().max_alert_rev)
            yield "retry: 3000\n\n"
            quiet = 0
            while processor.running and not await request.is_disconnected():
                rows = await asyncio.to_thread(store().alerts_since_rev, rev)
                for alert in rows:
                    rev = max(rev, alert["rev"])
                    yield f"id: {alert['rev']}\nevent: alert\ndata: {json.dumps(alert)}\n\n"
                quiet = 0 if rows else quiet + 1
                if quiet and quiet % 15 == 0:
                    yield ": keepalive\n\n"
                await asyncio.sleep(1)

        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @api.get("/alerts", tags=["alerts"])
    def alerts(status: list[str] = Query([], description=f"any of {ALERT_STATUSES}"),
               severity: list[str] = Query([], description="any of low, medium, high, critical"),
               rule: Optional[str] = None, application: Optional[str] = None, source: Optional[str] = None,
               event_type: Optional[str] = None, group_value: Optional[str] = Query(None, description="e.g. the ip"),
               start: Optional[str] = Query(None, alias="from"), to: Optional[str] = None,
               limit: int = Query(100, ge=1, le=1000), offset: int = Query(0, ge=0)):
        """Alerts by last activity, newest first. ``from``/``to`` filter on ``last_seen_ms``."""
        with bad_request():
            for s in status:
                if s not in ALERT_STATUSES:
                    raise ValueError(f"status must be one of {ALERT_STATUSES}")
            for s in severity:
                if s not in SEVERITY_ORDER:
                    raise ValueError(f"severity must be one of {SEVERITY_ORDER}")
            t = now()
            items, total = store().list_alerts(
                limit=limit, offset=offset, status=status, severity=severity, rule=rule,
                application=application, source=source, event_type=event_type, group_value=group_value,
                start=parse_time(start, t), end=parse_time(to, t))
        return {"items": items, "total": total, "limit": limit, "offset": offset}

    @api.get("/alerts/{alert_id}", tags=["alerts"])
    def alert(alert_id: int):
        found = store().get_alert(alert_id)
        if found is None:
            raise HTTPException(404, "alert not found")
        return found

    @api.patch("/alerts/{alert_id}", tags=["alerts"])
    def update_alert(alert_id: int, body: AlertUpdate):
        """Acknowledge / resolve an alert, or attach a note. A resolved alert is never re-opened:
        the next matching finding creates a new one."""
        updated = store().update_alert(alert_id, status=body.status, note=body.note)
        if updated is None:
            raise HTTPException(404, "alert not found")
        return updated

    app.include_router(api)
    app.include_router(dashboard_router(processor, authenticate))
    return app
