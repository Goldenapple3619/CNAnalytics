from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

from .event import COLUMNS, Event

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS events (
    id              INTEGER PRIMARY KEY,
    source_row_id   INTEGER NOT NULL,
    event_id        TEXT    NOT NULL UNIQUE,
    application     TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    event_type      TEXT    NOT NULL,
    timestamp_ms    INTEGER NOT NULL,
    received_at_ms  INTEGER NOT NULL,
    user_id         TEXT    NOT NULL,
    session_id      TEXT    NOT NULL,
    dims_json       TEXT    NOT NULL,
    enriched_json   TEXT    NOT NULL,
    errors_json     TEXT,
    processed_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_type_ts ON events (application, source, event_type, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_ts      ON events (timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_events_user    ON events (user_id);

CREATE TABLE IF NOT EXISTS alerts (
    id             INTEGER PRIMARY KEY,
    rev            INTEGER NOT NULL,
    fingerprint    TEXT    NOT NULL,
    rule_id        TEXT    NOT NULL,
    rule_name      TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    application    TEXT    NOT NULL,
    source         TEXT    NOT NULL,
    event_type     TEXT    NOT NULL,
    severity       TEXT    NOT NULL,
    status         TEXT    NOT NULL DEFAULT 'open',
    title          TEXT    NOT NULL,
    message        TEXT    NOT NULL,
    group_key      TEXT,
    group_value    TEXT,
    details_json   TEXT    NOT NULL,
    first_event_id TEXT    NOT NULL,
    last_event_id  TEXT    NOT NULL,
    first_seen_ms  INTEGER NOT NULL,
    last_seen_ms   INTEGER NOT NULL,
    occurrences    INTEGER NOT NULL DEFAULT 1,
    note           TEXT,
    created_at_ms  INTEGER NOT NULL,
    updated_at_ms  INTEGER NOT NULL,
    acknowledged_at_ms INTEGER,
    resolved_at_ms     INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_fp     ON alerts (fingerprint, status);
CREATE INDEX IF NOT EXISTS idx_alerts_rev    ON alerts (rev);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON alerts (status, last_seen_ms);
CREATE INDEX IF NOT EXISTS idx_alerts_app    ON alerts (application, last_seen_ms);
"""

ALERT_STATUSES = ("open", "acknowledged", "resolved")
_SEGMENT = re.compile(r"^[A-Za-z0-9_\-]+$")
_DIM_KEY = re.compile(r"^[A-Za-z0-9_.\-]+$")


def now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class AlertCandidate:
    """What the engine hands to the store when a rule fires."""
    rule_id: str
    rule_name: str
    symbol: str
    application: str
    source: str
    event_type: str
    severity: str
    title: str
    message: str
    group_key: Optional[str]
    group_value: Optional[str]
    details: dict
    event_id: str
    ts_ms: int
    cooldown_ms: int

    @property
    def fingerprint(self) -> str:
        raw = f"{self.rule_id}|{self.group_key}|{self.group_value}"
        return hashlib.sha1(raw.encode()).hexdigest()[:16]


def field_sql(field: str) -> tuple[str, list]:
    """SQL expression (+ bound params) reading ``field`` from the events table.

    Mirrors :func:`src.event.resolve`; every path segment is validated, and the JSON path is
    passed as a bound parameter, so user input never reaches the SQL text.
    """
    if field in COLUMNS:
        return field, []
    if field.startswith("dims."):
        key = field[5:]
        if not _DIM_KEY.match(key):
            raise ValueError(f"invalid field {field!r}")
        return "json_extract(dims_json, ?)", [f'$."{key}"']
    if field.startswith("enriched."):
        parts = field[9:].split(".")
        if not all(_SEGMENT.match(p) for p in parts):
            raise ValueError(f"invalid field {field!r}")
        return "json_extract(enriched_json, ?)", ["$." + ".".join(f'"{p}"' for p in parts)]
    if not _DIM_KEY.match(field):
        raise ValueError(f"invalid field {field!r}")
    if "." not in field:
        return "json_extract(dims_json, ?)", [f'$."{field}"']
    parts = field.split(".")
    if not all(_SEGMENT.match(p) for p in parts):
        raise ValueError(f"invalid field {field!r}")
    return ("COALESCE(json_extract(dims_json, ?), json_extract(enriched_json, ?))",
            [f'$."{field}"', "$." + ".".join(f'"{p}"' for p in parts)])

class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._wlock = threading.RLock()
        self._local = threading.local()
        self._w = self._connect()
        self._w.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Bring a database created by an older version up to date (CREATE IF NOT EXISTS never alters tables)."""
        have = {r["name"] for r in self._w.execute("PRAGMA table_info(alerts)")}
        for column in ("acknowledged_at_ms", "resolved_at_ms"):
            if column not in have:
                self._w.execute(f"ALTER TABLE alerts ADD COLUMN {column} INTEGER")

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def reader(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect()
        return conn

    def close(self) -> None:
        with self._wlock:
            self._w.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._wlock:
            self._w.execute("BEGIN IMMEDIATE")
            try:
                yield self._w
            except BaseException:
                self._w.execute("ROLLBACK")
                raise
            else:
                self._w.execute("COMMIT")

    def get_cursor(self) -> int:
        row = self.reader().execute("SELECT value FROM meta WHERE key = 'cursor'").fetchone()
        return int(row[0]) if row else 0

    def reset_cursor(self, value: int = 0) -> None:
        with self._tx() as c:
            self._set_cursor(c, value)

    @staticmethod
    def _set_cursor(c: sqlite3.Connection, value: int) -> None:
        c.execute("INSERT INTO meta (key, value) VALUES ('cursor', ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (str(value),))

    def write_batch(self, items: list[tuple[Optional[Event], list[AlertCandidate]]], cursor: int) -> list[dict]:
        """Atomically store events + alerts and advance the cursor. Returns the *newly created* alerts."""
        created: list[dict] = []
        stamp = now_ms()
        with self._tx() as c:
            for event, candidates in items:
                if event is None:
                    continue
                cur = c.execute(
                    "INSERT OR IGNORE INTO events (source_row_id, event_id, application, source, event_type,"
                    " timestamp_ms, received_at_ms, user_id, session_id, dims_json, enriched_json,"
                    " errors_json, processed_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (event.source_row_id, event.event_id, event.application, event.source, event.event_type,
                     event.timestamp_ms, event.received_at_ms, event.user_id, event.session_id,
                     json.dumps(event.dims, default=str), json.dumps(event.enriched, default=str),
                     json.dumps(event.errors) if event.errors else None, stamp))
                if cur.rowcount == 0:      # already stored (cursor was reset): never alert twice
                    continue
                for cand in candidates:
                    alert, is_new = self._upsert_alert(c, cand, stamp)
                    if is_new:
                        created.append(alert)
            self._set_cursor(c, cursor)
        return created

    @staticmethod
    def _next_rev(c: sqlite3.Connection) -> int:
        return c.execute("SELECT COALESCE(MAX(rev), 0) + 1 FROM alerts").fetchone()[0]

    def _upsert_alert(self, c: sqlite3.Connection, a: AlertCandidate, stamp: int) -> tuple[dict, bool]:
        row = c.execute(
            "SELECT id, last_seen_ms FROM alerts WHERE fingerprint = ? AND status IN ('open', 'acknowledged') "
            "ORDER BY last_seen_ms DESC LIMIT 1", (a.fingerprint,)).fetchone()
        details = json.dumps(a.details, default=str)
        if row is not None and a.ts_ms - row["last_seen_ms"] <= a.cooldown_ms:
            c.execute(
                "UPDATE alerts SET last_seen_ms = MAX(last_seen_ms, ?), occurrences = occurrences + 1,"
                " message = ?, details_json = ?, last_event_id = ?, severity = ?, rev = ?, updated_at_ms = ?"
                " WHERE id = ?",
                (a.ts_ms, a.message, details, a.event_id, a.severity, self._next_rev(c), stamp, row["id"]))
            return self._alert_by_id(c, row["id"]), False
        cur = c.execute(
            "INSERT INTO alerts (rev, fingerprint, rule_id, rule_name, symbol, application, source, event_type,"
            " severity, status, title, message, group_key, group_value, details_json, first_event_id,"
            " last_event_id, first_seen_ms, last_seen_ms, occurrences, created_at_ms, updated_at_ms)"
            " VALUES (?,?,?,?,?,?,?,?,?, 'open', ?,?,?,?,?,?,?,?,?,1,?,?)",
            (self._next_rev(c), a.fingerprint, a.rule_id, a.rule_name, a.symbol, a.application, a.source,
             a.event_type, a.severity, a.title, a.message, a.group_key,
             None if a.group_value is None else str(a.group_value), details, a.event_id, a.event_id,
             a.ts_ms, a.ts_ms, stamp, stamp))
        return self._alert_by_id(c, cur.lastrowid), True

    def update_alert(self, alert_id: int, status: Optional[str] = None, note: Optional[str] = None) -> Optional[dict]:
        if status is not None and status not in ALERT_STATUSES:
            raise ValueError(f"status must be one of {ALERT_STATUSES}")
        with self._tx() as c:
            if c.execute("SELECT 1 FROM alerts WHERE id = ?", (alert_id,)).fetchone() is None:
                return None
            sets, params = ["rev = ?", "updated_at_ms = ?"], [self._next_rev(c), now_ms()]
            if status is not None:
                sets.append("status = ?")
                params.append(status)
                stamp = now_ms()
                if status == "acknowledged":
                    sets.append("acknowledged_at_ms = COALESCE(acknowledged_at_ms, ?)")
                    params.append(stamp)
                elif status == "resolved":
                    sets.append("resolved_at_ms = ?")
                    params.append(stamp)
            if note is not None:
                sets.append("note = ?")
                params.append(note)
            c.execute(f"UPDATE alerts SET {', '.join(sets)} WHERE id = ?", (*params, alert_id))
            return self._alert_by_id(c, alert_id)

    def prune(self, before_ms: int) -> tuple[int, int]:
        with self._tx() as c:
            events = c.execute("DELETE FROM events WHERE timestamp_ms < ?", (before_ms,)).rowcount
            alerts = c.execute("DELETE FROM alerts WHERE status = 'resolved' AND last_seen_ms < ?",
                               (before_ms,)).rowcount
        return events, alerts

    @staticmethod
    def _event_dict(row: sqlite3.Row) -> dict:
        return {
            "id": row["id"], "event_id": row["event_id"], "application": row["application"],
            "source": row["source"], "event_type": row["event_type"],
            "timestamp_ms": row["timestamp_ms"], "received_at_ms": row["received_at_ms"],
            "user_id": row["user_id"], "session_id": row["session_id"],
            "dims": json.loads(row["dims_json"]), "enriched": json.loads(row["enriched_json"]),
            "errors": json.loads(row["errors_json"]) if row["errors_json"] else [],
        }

    @staticmethod
    def _alert_dict(row: sqlite3.Row) -> dict:
        d = dict(row)
        d["details"] = json.loads(d.pop("details_json"))
        d.pop("fingerprint", None)
        return d

    def _alert_by_id(self, c: sqlite3.Connection, alert_id: int) -> dict:
        return self._alert_dict(c.execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone())

    @staticmethod
    def _type_clause(application: str, source: str, event_type: str) -> tuple[str, list]:
        """Match one event type; ``"*"`` (XML wildcard) means any source / any type."""
        clauses, params = ["application = ?"], [application]
        if source != "*":
            clauses.append("source = ?")
            params.append(source)
        if event_type != "*":
            clauses.append("event_type = ?")
            params.append(event_type)
        return " AND ".join(clauses), params

    def latest_ts(self, application: str, source: str, event_type: str) -> Optional[int]:
        clause, params = self._type_clause(application, source, event_type)
        return self.reader().execute(f"SELECT MAX(timestamp_ms) FROM events WHERE {clause}", params).fetchone()[0]

    def replay(self, application: str, source: str, event_type: str, since_ms: int,
               chunk: int = 5000) -> Iterator[Event]:
        """Stored events of one type since ``since_ms``, oldest first (keyset-paginated)."""
        clause, params = self._type_clause(application, source, event_type)
        last = (since_ms, 0)
        while True:
            rows = self.reader().execute(
                f"SELECT * FROM events WHERE {clause} AND (timestamp_ms, id) > (?, ?) "
                f"ORDER BY timestamp_ms, id LIMIT ?", (*params, last[0], last[1], chunk)).fetchall()
            if not rows:
                return
            for row in rows:
                yield Event(row["source_row_id"], row["event_id"], row["application"], row["source"],
                            row["event_type"], row["timestamp_ms"], row["received_at_ms"], row["user_id"],
                            row["session_id"], json.loads(row["dims_json"]), json.loads(row["enriched_json"]))
            last = (rows[-1]["timestamp_ms"], rows[-1]["id"])

    @staticmethod
    def _filters(application=None, source=None, event_type=None, user_id=None, start=None, end=None,
                 where: Optional[list[tuple[str, str]]] = None, after_id=None, before_id=None) -> tuple[str, list]:
        clauses: list[str] = []
        params: list[Any] = []
        for column, value in (("application", application), ("source", source),
                              ("event_type", event_type), ("user_id", user_id)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if start is not None:
            clauses.append("timestamp_ms >= ?")
            params.append(start)
        if end is not None:
            clauses.append("timestamp_ms < ?")
            params.append(end)
        if after_id is not None:
            clauses.append("id > ?")
            params.append(after_id)
        if before_id is not None:
            clauses.append("id < ?")
            params.append(before_id)
        for field, value in where or []:
            expr, p = field_sql(field)
            clauses.append(f"CAST({expr} AS TEXT) = ?")
            params.extend([*p, value])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def query_events(self, limit: int = 100, **filters) -> list[dict]:
        where, params = self._filters(**filters)
        rows = self.reader().execute(
            f"SELECT * FROM events{where} ORDER BY id DESC LIMIT ?", (*params, limit)).fetchall()
        return [self._event_dict(r) for r in rows]

    def get_event(self, event_id: str) -> Optional[dict]:
        row = self.reader().execute("SELECT * FROM events WHERE event_id = ?", (event_id,)).fetchone()
        return self._event_dict(row) if row else None

    def count_events(self, **filters) -> int:
        where, params = self._filters(**filters)
        return self.reader().execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()[0]

    def top(self, field: str, limit: int = 10, **filters) -> list[dict]:
        expr, p = field_sql(field)
        where, params = self._filters(**filters)
        where += (" AND " if where else " WHERE ") + f"{expr} IS NOT NULL"
        rows = self.reader().execute(
            f"SELECT {expr} AS value, COUNT(*) AS count FROM events{where} "
            f"GROUP BY value ORDER BY count DESC, value LIMIT ?", (*p, *params, *p, limit)).fetchall()
        return [{"value": r["value"], "count": r["count"]} for r in rows]

    def timeseries(self, step_ms: int, group_by: Optional[str] = None, top: int = 5, **filters) -> list[dict]:
        """Sparse ``[bucket_ts, count]`` series, one per group (or a single series without ``group_by``)."""
        where, params = self._filters(**filters)
        if group_by is None:
            rows = self.reader().execute(
                f"SELECT (timestamp_ms / ?) * ? AS bucket, COUNT(*) AS n FROM events{where} "
                f"GROUP BY bucket ORDER BY bucket", (step_ms, step_ms, *params)).fetchall()
            return [{"group": None, "points": [[r["bucket"], r["n"]] for r in rows]}]

        groups = [t["value"] for t in self.top(group_by, top, **filters)]
        if not groups:
            return []
        expr, p = field_sql(group_by)
        marks = ",".join("?" * len(groups))
        rows = self.reader().execute(
            f"SELECT {expr} AS g, (timestamp_ms / ?) * ? AS bucket, COUNT(*) AS n FROM events{where} "
            f"{'AND' if where else 'WHERE'} {expr} IN ({marks}) GROUP BY g, bucket ORDER BY bucket",
            (*p, step_ms, step_ms, *params, *p, *groups)).fetchall()
        series: dict[Any, list] = {g: [] for g in groups}
        for r in rows:
            series[r["g"]].append([r["bucket"], r["n"]])
        return [{"group": g, "points": pts} for g, pts in series.items()]

    def list_alerts(self, limit: int = 100, offset: int = 0, status: Optional[list[str]] = None,
                    severity: Optional[list[str]] = None, rule: Optional[str] = None,
                    application: Optional[str] = None, source: Optional[str] = None,
                    event_type: Optional[str] = None, group_value: Optional[str] = None,
                    start: Optional[int] = None, end: Optional[int] = None) -> tuple[list[dict], int]:
        clauses, params = [], []
        for column, values in (("status", status), ("severity", severity)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' * len(values))})")
                params.extend(values)
        for column, value in (("rule_name", rule), ("application", application), ("source", source),
                              ("event_type", event_type), ("group_value", group_value)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        if start is not None:
            clauses.append("last_seen_ms >= ?")
            params.append(start)
        if end is not None:
            clauses.append("last_seen_ms < ?")
            params.append(end)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        conn = self.reader()
        total = conn.execute(f"SELECT COUNT(*) FROM alerts{where}", params).fetchone()[0]
        rows = conn.execute(
            f"SELECT * FROM alerts{where} ORDER BY last_seen_ms DESC, id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset)).fetchall()
        return [self._alert_dict(r) for r in rows], total

    def get_alert(self, alert_id: int) -> Optional[dict]:
        row = self.reader().execute("SELECT * FROM alerts WHERE id = ?", (alert_id,)).fetchone()
        return self._alert_dict(row) if row else None

    def alerts_since_rev(self, rev: int, limit: int = 100) -> list[dict]:
        rows = self.reader().execute(
            "SELECT * FROM alerts WHERE rev > ? ORDER BY rev LIMIT ?", (rev, limit)).fetchall()
        return [self._alert_dict(r) for r in rows]

    def max_alert_rev(self) -> int:
        return self.reader().execute("SELECT COALESCE(MAX(rev), 0) FROM alerts").fetchone()[0]

    def alert_counts(self, start: Optional[int] = None, end: Optional[int] = None) -> dict:
        clauses, params = [], []
        if start is not None:
            clauses.append("last_seen_ms >= ?")
            params.append(start)
        if end is not None:
            clauses.append("last_seen_ms < ?")
            params.append(end)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self.reader().execute(
            f"SELECT status, severity, COUNT(*) AS n FROM alerts{where} GROUP BY status, severity", params).fetchall()
        out: dict = {"by_status": {}, "by_severity": {}, "total": 0}
        for r in rows:
            out["by_status"][r["status"]] = out["by_status"].get(r["status"], 0) + r["n"]
            out["by_severity"][r["severity"]] = out["by_severity"].get(r["severity"], 0) + r["n"]
            out["total"] += r["n"]
        return out

    def event_scopes(self) -> list[tuple[str, str, str]]:
        """Every (application, source, event_type) present in the stored events."""
        rows = self.reader().execute(
            "SELECT DISTINCT application, source, event_type FROM events ORDER BY application, source, event_type")
        return [(r[0], r[1], r[2]) for r in rows]

    def count_distinct(self, column: str, **filters) -> int:
        if column not in COLUMNS:
            raise ValueError(f"invalid column {column!r}")
        where, params = self._filters(**filters)
        return self.reader().execute(f"SELECT COUNT(DISTINCT {column}) FROM events{where}", params).fetchone()[0]

    def count_with_errors(self, **filters) -> int:
        where, params = self._filters(**filters)
        where += (" AND " if where else " WHERE ") + "errors_json IS NOT NULL"
        return self.reader().execute(f"SELECT COUNT(*) FROM events{where}", params).fetchone()[0]

    def by_source(self, **filters) -> dict[tuple[str, str], dict]:
        """``{(application, source): {"count", "last_ts"}}`` for the filtered events."""
        where, params = self._filters(**filters)
        rows = self.reader().execute(
            f"SELECT application, source, COUNT(*) AS n, MAX(timestamp_ms) AS last_ts FROM events{where} "
            f"GROUP BY application, source", params).fetchall()
        return {(r["application"], r["source"]): {"count": r["n"], "last_ts": r["last_ts"]} for r in rows}

    def histogram(self, start: int, end: int, n: int, group: tuple[str, ...] = (), **filters) -> dict[tuple, list[int]]:
        """Event counts in ``n`` equal slices of ``[start, end)``, optionally per ``group`` columns.
        Always returns the key ``()`` when ``group`` is empty, so callers need no special case."""
        for column in group:
            if column not in COLUMNS:
                raise ValueError(f"invalid column {column!r}")
        filters = {**filters, "start": start, "end": end}
        where, params = self._filters(**filters)
        cols = "".join(f"{c}, " for c in group)
        by = (", " + ", ".join(group)) if group else ""
        rows = self.reader().execute(
            f"SELECT {cols}CAST((timestamp_ms - ?) * ? / ? AS INTEGER) AS slot, COUNT(*) AS n FROM events{where} "
            f"GROUP BY slot{by}", (start, n, max(end - start, 1), *params)).fetchall()
        out: dict[tuple, list[int]] = {} if group else {(): [0] * n}
        for r in rows:
            key = tuple(r[c] for c in group)
            if 0 <= r["slot"] < n:
                out.setdefault(key, [0] * n)[r["slot"]] = r["n"]
        return out

    def event_groups(self, start: int, end: int, trend_slots: int = 12, limit: int = 200, **filters) -> list[dict]:
        """One row per (application, source, event_type): count, first/last seen, trend and the latest event."""
        filters = {**filters, "start": start, "end": end}
        where, params = self._filters(**filters)
        conn = self.reader()
        rows = conn.execute(
            f"SELECT application, source, event_type, COUNT(*) AS n, MIN(timestamp_ms) AS first_ts, "
            f"MAX(timestamp_ms) AS last_ts, MAX(id) AS last_id FROM events{where} "
            f"GROUP BY application, source, event_type ORDER BY n DESC, event_type LIMIT ?",
            (*params, limit)).fetchall()
        if not rows:
            return []
        trend = self.histogram(start, end, trend_slots, ("application", "source", "event_type"), **{
            k: v for k, v in filters.items() if k not in ("start", "end")})
        marks = ",".join("?" * len(rows))
        samples = {r["id"]: self._event_dict(r) for r in conn.execute(
            f"SELECT * FROM events WHERE id IN ({marks})", [r["last_id"] for r in rows])}
        return [{
            "application": r["application"], "source": r["source"], "event_type": r["event_type"],
            "count": r["n"], "first_ts": r["first_ts"], "last_ts": r["last_ts"],
            "trend": trend.get((r["application"], r["source"], r["event_type"]), [0] * trend_slots),
            "sample": samples.get(r["last_id"]),
        } for r in rows]

    # alerts ---------------------------------------------------------------
    @staticmethod
    def _alert_scope(application=None, source=None, status=None) -> tuple[list[str], list]:
        clauses, params = [], []
        for column, value in (("application", application), ("source", source), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                params.append(value)
        return clauses, params

    def open_alert_counts(self) -> dict[tuple[str, str], dict[str, int]]:
        """``{(application, source): {severity: n}}`` for alerts that are still open (firing)."""
        rows = self.reader().execute(
            "SELECT application, source, severity, COUNT(*) AS n FROM alerts WHERE status = 'open' "
            "GROUP BY application, source, severity").fetchall()
        out: dict[tuple[str, str], dict[str, int]] = {}
        for r in rows:
            out.setdefault((r["application"], r["source"]), {})[r["severity"]] = r["n"]
        return out

    def alerts_raised(self, start: int, end: int, **scope) -> int:
        clauses, params = self._alert_scope(**scope)
        clauses += ["first_seen_ms >= ?", "first_seen_ms < ?"]
        return self.reader().execute(
            f"SELECT COUNT(*) FROM alerts WHERE {' AND '.join(clauses)}", (*params, start, end)).fetchone()[0]

    def alert_buckets(self, step_ms: int, start: int, end: int, **scope) -> dict[str, dict[int, int]]:
        """``{severity: {bucket_ts: new alerts}}`` (alerts are placed where they first fired)."""
        clauses, params = self._alert_scope(**scope)
        clauses += ["first_seen_ms >= ?", "first_seen_ms < ?"]
        rows = self.reader().execute(
            f"SELECT severity, (first_seen_ms / ?) * ? AS bucket, COUNT(*) AS n FROM alerts "
            f"WHERE {' AND '.join(clauses)} GROUP BY severity, bucket", (step_ms, step_ms, *params, start, end)).fetchall()
        out: dict[str, dict[int, int]] = {}
        for r in rows:
            out.setdefault(r["severity"], {})[r["bucket"]] = r["n"]
        return out

    def alert_spans(self, start: int, end: int, **scope) -> list[tuple[str, str, str, int, int]]:
        """``(application, source, severity, first_seen_ms, last_seen_ms)`` of alerts active in ``[start, end)``."""
        clauses, params = self._alert_scope(**scope)
        clauses += ["first_seen_ms < ?", "last_seen_ms >= ?"]
        rows = self.reader().execute(
            f"SELECT application, source, severity, first_seen_ms, last_seen_ms FROM alerts "
            f"WHERE {' AND '.join(clauses)}", (*params, end, start)).fetchall()
        return [tuple(r) for r in rows]
