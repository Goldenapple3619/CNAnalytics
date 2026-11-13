# Analytics processor

Tails the `analytics` table written by the ingestion API, then for every event:

1. **extracts** the dimensions declared in `applications/<app>.xml`
2. **enriches** them (GeoIP, user agent, ... any python class you add)
3. **runs alert rules** (rate, distinct count, field match, ... any python class you add)
4. stores the result in its own database and **serves it over HTTP** for a dashboard

```
ingestion API ──► ../data/db.sqlite ──(read-only)──► processor ──► ../data/processor.sqlite ──► HTTP API ──► dashboard
                                                       ▲
                                              applications/*.xml   (hot reloaded)
                                              services/enrichment/*, services/detection/*
```

The ingestion database is never written to. Processed events and alerts live in a separate
file, so the ingestion API stays its only writer.

## Run

```bash
pip install -r requirements.txt
python main.py                        # processor + API on http://127.0.0.1:8080  (docs: /docs)
python main.py --check                # validate applications/*.xml and exit
python main.py --once                 # process the current backlog and exit
python main.py --no-api               # processor only

python tools/seed_demo.py --follow    # demo traffic (creates ../data/db.sqlite if missing)
python -m pytest tests
```

Flags: `--ingest-db --db --applications --host --port --log-level`. Everything also has a
`PROCESSOR_*` environment variable:

| variable | default | |
|---|---|---|
| `PROCESSOR_INGEST_DB` | `../data/db.sqlite` | ingestion database (opened read-only) |
| `PROCESSOR_DB` | `../data/processor.sqlite` | processor database |
| `PROCESSOR_APPLICATIONS_DIR` | `./applications` | |
| `PROCESSOR_API_HOST` / `_PORT` | `127.0.0.1` / `8080` | |
| `PROCESSOR_API_KEY` | *(none)* | when set, `/api/*` requires header `X-API-Key` |
| `PROCESSOR_CORS_ORIGINS` | `*` | comma separated list |
| `PROCESSOR_WEBHOOK_URL` | *(none)* | JSON `POST {"event":"alert.created","alert":{...}}` per new alert |
| `PROCESSOR_TIME_FIELD` | `timestamp_ms` | clock of detection windows: `timestamp_ms` or `received_at_ms` |
| `PROCESSOR_RETENTION_DAYS` | `30` | older events and resolved alerts are pruned (`0` = keep all) |
| `PROCESSOR_BATCH_SIZE`, `_POLL_INTERVAL`, `_RELOAD_CHECK_INTERVAL`, `_SWEEP_INTERVAL` | | tuning |
| `PROCESSOR_VPN_RANGES` | *(none)* | file of VPN/proxy CIDRs, one per line, used by `geoip` with `check_vpn` |

## Application files

One file per application, matched on the `application`, `source` and `event_type` columns.
See `applications/moonreward.xml`, which is commented as a reference.

```xml
<application name="moonreward">
  <source name="api"><events>
    <event name="connection_fail">                     <!-- name="*" matches any event of the source -->
      <dimensions>                                     <!-- what to keep from the event -->
        <key>ip</key>
        <key constructor="int">attempts</key>          <!-- optional cast -->
      </dimensions>

      <enrichment>                                     <!-- run in order -->
        <rule name="get geoip">
          <pysymbol>services.enrichment.geoip</pysymbol>
          <values>
            <key constructor="str">ip</key>            <!-- positional: value of the event field -->
            <const constructor="bool" kwargs="check_vpn">1</const>   <!-- keyword: literal -->
          </values>
          <as>ip</as>                                  <!-- stored in event.enriched["ip"] -->
        </rule>
      </enrichment>

      <alerts>
        <rule name="bruteforce" severity="high" cooldown="5m">
          <pysymbol>services.detection.rate_above</pysymbol>
          <values>
            <key   constructor="str" kwargs="group_by">ip</key>
            <const constructor="str" kwargs="window">5m</const>
            <const constructor="int" kwargs="threshold">50</const>
          </values>
        </rule>
      </alerts>
    </event>
  </events></source>
</application>
```

**Where dimension values come from:** `payload_json`, then `attributes`, then the table columns
(`user_id`, `session_id`...). A dotted key (`device.os`) walks nested JSON.

**Field references** (`<key>`, `group_by`, API `field=`/`where=`): an exact dimension (`ip`),
`dims.<name>`, `enriched.<as>.<path>`, a column (`user_id`), or the shorthand `<as>.<path>`
(`ip.country_code`). Enrichments can read the results of the rules above them.

**`<values>`** are the constructor arguments of the python class. `kwargs="x"` passes by keyword,
otherwise positionally. If a `<key>` is missing from the event the rule is skipped, unless
`optional="true"` (the class then receives `None`). Constructors: `str int float bool json list duration`
(`bool` reads text properly: `"0"` is false).

**Alert rule attributes:** `severity` (low/medium/high/critical), `cooldown`, `warmup`, `enabled="false"`.

The file is validated before use and **all** problems are reported with their XML path (unknown
module, wrong argument name, undeclared key, bad duration, ...). Files are hot reloaded; an invalid
edit is rejected and the previous definitions keep running.

## Built-in services

| `pysymbol` | |
|---|---|
| `services.enrichment.geoip` | country / cidr / private-range flag, `is_vpn` when `check_vpn` (offline `geoip2fast` + optional CIDR list) |
| `services.enrichment.ua_parse` | browser, os, device, `is_bot`, `is_automated` (regex based, no dependency) |
| `services.detection.rate_above` | `group_by`, `window`, `threshold` - more than N events per group in a window |
| `services.detection.distinct_above` | `group_by`, `distinct`, `window`, `threshold` - more than N distinct values (credential stuffing) |
| `services.detection.field_match` | `field`, `op`, `value`, optional `group_by` - stateless condition on every event |

### Adding your own

A service is a module under `services/` that exports a class with `export = ...` (only modules
under `services.` can be referenced from XML).

```python
# services/enrichment/account_age.py
from .base import BaseEnrichment

class AccountAge(BaseEnrichment):
    def __init__(self, account_id, *, unit: str = "days"):   # <values> map onto this signature
        super().__init__(account_id)
        self.account_id, self.unit = account_id, unit

    @classmethod
    def validate(cls, *, unit="days", **_):                  # optional: reject bad XML at load time
        if unit not in ("days", "hours"): raise ValueError("unit must be days or hours")

    def run(self):                                           # JSON-serialisable result, or None
        return {"age": lookup(self.account_id, self.unit)}

export = AccountAge
```

A detection derives `BaseDetection` and implements `evaluate(ctx) -> Finding | None`. Instances are
created per event; keep memory in `ctx.state` (a dict owned by the rule). Declare `retention_ms()`
so the engine can rebuild that state from stored events after a restart, and `sweep()` to
drop stale groups. See `services/detection/rate_above.py`.

## Alerts

A finding creates an alert identified by *rule + group* (e.g. `bruteforce` + `ip=1.2.3.4`). While
findings keep arriving within the rule's `cooldown` they update the **same** open alert
(`occurrences`, `last_seen_ms`, `details`) instead of creating a new one. An alert you resolve is
never re-opened: the next finding creates a new alert. New alerts are logged and, if configured,
POSTed to the webhook. Status: `open` → `acknowledged` → `resolved` (via the API). The time of the acknowledgement and of the resolution is kept (`acknowledged_at_ms`, `resolved_at_ms`; databases from an earlier version get the two columns added at start-up).

## HTTP API (`/api/v1`, interactive docs at `/docs`)

Times (`from`, `to`) accept epoch ms, ISO-8601 or relative values (`-15m`, `-2h`, `now`).
Common filters: `application source event_type user_id from to` and repeatable
`where=field=value` (e.g. `where=ip.country_code=FR`).

| endpoint | |
|---|---|
| `GET /health` *(no key needed)* | liveness, 503 when the worker is down |
| `GET /status` | counters, cursor, ingestion lag |
| `POST /reload` | re-read the XML files |
| `GET /applications` | apps / events / dimensions / enrichments / alert rules - lets a dashboard build itself |
| `GET /events`, `GET /events/{event_id}` | newest first; `before_id` pages back, `after_id` tails live |
| `GET /metrics/summary` | totals by event type, alert counts, processor state (default last hour) |
| `GET /metrics/timeseries` | `interval`, `group_by`, `top`; zero-filled `[[bucket_ms, count], ...]` per series |
| `GET /metrics/top?field=` | most frequent values of any dimension or enriched field |
| `GET /alerts`, `GET /alerts/{id}` | filter by `status severity rule application group_value from to` |
| `PATCH /alerts/{id}` | `{"status": "acknowledged", "note": "..."}` |
| `GET /alerts/stream` | Server-Sent Events, one message per created/updated alert (`Last-Event-ID` resumes) |

### Dashboard endpoints (`/api/v1/dashboard`)

Finished widgets for the frontend in `../dashboard`: `GET /apps` (applications with their sources,
firing counts and health), `/overview`, `/sources`, `/events`, `/alerts` (all take `app`, `source`
and, except `/apps` and `/alerts`, `range=1h|6h|24h|7d|30d`) and `POST /alerts/{id}/ack|resolve`.
They are built from the same store as the endpoints above and share the API key. The response shapes
are documented in `../dashboard/README.md`; the code is `src/api/dashboard.py`. Sources are the
`<source>` names of the XML plus any source seen in stored events (a `name="*"` source is filled in
from the data).

With `PROCESSOR_API_KEY` set, send `X-API-Key`; the SSE stream also accepts `?api_key=` since
`EventSource` cannot set headers.

## Operational notes

* **Exactly-once-ish:** events, alerts and the read cursor are committed in one transaction per batch;
  `event_id` is unique, so a replay never duplicates or re-alerts.
* **Restart:** detection windows live in memory and are rebuilt by replaying stored events (silently)
  for the history each rule declares. A changed rule is rebuilt on hot reload; untouched rules keep their state.
* **Ingestion DB deleted/replaced/reset** is detected and the cursor restarts from 0.
* **Failures are contained:** a broken enrichment or rule is recorded in the event's `errors`
  and never loses the event or blocks the batch.
* **Throughput:** ~14k events/s on one thread with 2 enrichments and 4 rules per event (measured on 50k
  events). One worker thread by design: detection state is in memory and ordered.
* **Limits to know:** GeoIP is country level (no city) and `is_vpn` is `null` ("unknown") without a CIDR
  list; the UA parser is a triage heuristic, not a fingerprint.
