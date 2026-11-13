import time

import pytest
from fastapi.testclient import TestClient

from conftest import write_row
from src.api.app import create_app

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
SEC = 1000
BASE = "/api/v1/dashboard"

# a second application with two sources, to check events are separated per app and per source
BLOG_XML = """<application name="blog">
  <source name="web"><events>
    <event name="page_view"><dimensions><key>path</key></dimensions></event>
  </events></source>
  <source name="worker"><events>
    <event name="job_done"><dimensions><key>job</key></dimensions></event>
    <event name="job_failed"><dimensions><key>job</key></dimensions></event>
  </events></source>
</application>
"""


@pytest.fixture
def api(env, make_processor):
    (env.applications_dir / "blog.xml").write_text(BLOG_XML)
    now = int(time.time() * 1000)
    for i in range(6):   # burst alert on shop/api
        write_row(env.ingest_db, "login_fail", now - 60_000 + i * SEC,
                  payload={"ip": "8.8.8.8", "user_agent": CHROME, "account": "a"}, user=f"u{i % 2}")
    write_row(env.ingest_db, "purchase", now - 20_000, payload={"total": 10})
    for i in range(4):
        write_row(env.ingest_db, "page_view", now - 90_000 + i * SEC, application="blog", source="web",
                  payload={"path": "/"}, user="reader")
    write_row(env.ingest_db, "job_done", now - 40_000, application="blog", source="worker", payload={"job": "x"})
    write_row(env.ingest_db, "job_failed", now - 30_000, application="blog", source="worker", payload={"job": "x"})
    p = make_processor()
    p.drain()
    p.running = True
    return TestClient(create_app(p, env))


def test_apps_nest_sources_under_their_application(api):
    apps = {a["id"]: a for a in api.get(f"{BASE}/apps").json()}
    assert set(apps) == {"shop", "blog"}
    assert [s["id"] for s in apps["blog"]["sources"]] == ["web", "worker"]
    assert apps["blog"]["sources"][1]["eventTypes"] == ["job_done", "job_failed"]
    assert [s["id"] for s in apps["shop"]["sources"]] == ["api"]
    # the burst/stuffing alerts belong to shop/api only
    assert apps["shop"]["firingAlerts"] >= 1 and apps["shop"]["sources"][0]["firingAlerts"] == apps["shop"]["firingAlerts"]
    assert apps["shop"]["status"] in ("degraded", "down")
    assert apps["blog"]["firingAlerts"] == 0 and apps["blog"]["status"] == "ok"
    assert all(s["status"] == "ok" for s in apps["blog"]["sources"])


def test_sources_are_separated_per_app(api):
    rows = api.get(f"{BASE}/sources?app=blog").json()
    assert {r["name"]: r["events"] for r in rows} == {"web": 4, "worker": 2}
    assert all(r["app"] == "blog" and len(r["health"]) == 48 and len(r["spark"]) == 24 for r in rows)
    assert sum(sum(r["spark"]) for r in rows) == 6
    one = api.get(f"{BASE}/sources?app=blog&source=worker").json()
    assert [r["name"] for r in one] == ["worker"] and one[0]["eventTypes"] == 2
    allrows = api.get(f"{BASE}/sources").json()
    assert {(r["app"], r["name"]) for r in allrows} == {("shop", "api"), ("blog", "web"), ("blog", "worker")}
    api_row = next(r for r in allrows if r["app"] == "shop")
    assert api_row["firingAlerts"] >= 1 and any(c["status"] != "ok" for c in api_row["health"])


def test_overview_scopes_series(api):
    everything = api.get(f"{BASE}/overview?range=1h").json()
    assert [k["id"] for k in everything["kpis"]] == ["events", "alerts", "firing", "users"]
    assert everything["kpis"][0]["value"] == 13
    assert [s["name"] for s in everything["traffic"]["series"]] == ["blog", "shop"]       # by app
    assert len({len(s["values"]) for s in everything["traffic"]["series"]} | {len(everything["traffic"]["times"])}) == 1

    in_app = api.get(f"{BASE}/overview?app=blog&range=1h").json()
    assert in_app["kpis"][0]["value"] == 6
    assert [s["name"] for s in in_app["traffic"]["series"]] == ["web", "worker"]           # by source
    assert [s["name"] for s in in_app["eventMix"]["series"]] == ["job_done", "job_failed", "page_view"]

    in_source = api.get(f"{BASE}/overview?app=blog&source=worker&range=1h").json()
    assert in_source["kpis"][0]["value"] == 2
    assert [s["name"] for s in in_source["traffic"]["series"]] == ["Events"]
    assert [s["name"] for s in in_source["alerts"]["series"]] == ["Critical", "High", "Medium", "Low"]

    shop = api.get(f"{BASE}/overview?app=shop&range=1h").json()
    assert shop["kpis"][1]["value"] >= 1 and sum(sum(s["values"]) for s in shop["alerts"]["series"]) >= 1


def test_events_view_groups_by_event_type(api):
    body = api.get(f"{BASE}/events?app=blog&range=1h").json()
    assert {g["name"]: (g["source"], g["count"]) for g in body["groups"]} == {
        "page_view": ("web", 4), "job_done": ("worker", 1), "job_failed": ("worker", 1)}
    assert [(s["name"], s["count"]) for s in body["bySource"]] == [("web", 4), ("worker", 2)]
    group = body["groups"][0]
    assert len(group["trend"]) == 12 and group["sample"]["dims"] == {"path": "/"}
    only = api.get(f"{BASE}/events?app=shop&type=login_fail&range=1h").json()
    assert [g["name"] for g in only["groups"]] == ["login_fail"]
    assert only["groups"][0]["sample"]["enriched"]["ip"]["country_code"] == "US"


def test_alert_lifecycle_and_filters(api):
    body = api.get(f"{BASE}/alerts?state=firing").json()
    assert body["counts"]["high"] >= 1 and body["items"]
    first = body["items"][0]
    assert first["app"] == "shop" and first["source"] == "api" and first["state"] == "firing"
    assert api.get(f"{BASE}/alerts?app=blog").json()["items"] == []
    assert api.get(f"{BASE}/alerts?severity=high&state=firing").json()["items"]

    acked = api.post(f"{BASE}/alerts/{first['id']}/ack").json()
    assert acked["state"] == "acknowledged" and acked["acknowledgedAt"]
    assert first["id"] not in [a["id"] for a in api.get(f"{BASE}/alerts?state=firing").json()["items"]]
    assert first["id"] in [a["id"] for a in api.get(f"{BASE}/alerts?state=acknowledged").json()["items"]]

    done = api.post(f"{BASE}/alerts/{first['id']}/resolve").json()
    assert done["state"] == "resolved" and done["resolvedAt"] >= done["acknowledgedAt"]
    assert api.post(f"{BASE}/alerts/{first['id']}/ack").status_code == 409
    assert api.post(f"{BASE}/alerts/99999/ack").status_code == 404
    assert [a["state"] for a in api.get(f"{BASE}/alerts").json()["items"]] == ["resolved"]   # no state = every state


@pytest.mark.parametrize("url", [
    f"{BASE}/overview?range=2y", f"{BASE}/sources?range=nope", f"{BASE}/alerts?state=burning",
    f"{BASE}/alerts?severity=urgent",
])
def test_bad_input_is_a_400(api, url):
    assert api.get(url).status_code == 400


def test_dashboard_requires_the_api_key(env, make_processor):
    p = make_processor(env.with_overrides(api_key="s3cret"))
    p.open()
    c = TestClient(create_app(p, p.settings))
    assert c.get(f"{BASE}/apps").status_code == 401
    assert c.get(f"{BASE}/apps", headers={"X-API-Key": "s3cret"}).status_code == 200


def test_old_alert_tables_are_migrated(tmp_path):
    import sqlite3
    from src.store import Store
    db = tmp_path / "old.sqlite"
    Store(db).close()
    conn = sqlite3.connect(db)                       # pretend this file was made before the two columns existed
    conn.execute("ALTER TABLE alerts DROP COLUMN acknowledged_at_ms")
    conn.execute("ALTER TABLE alerts DROP COLUMN resolved_at_ms")
    conn.commit()
    conn.close()
    store = Store(db)
    cols = {r["name"] for r in store.reader().execute("PRAGMA table_info(alerts)")}
    assert {"acknowledged_at_ms", "resolved_at_ms"} <= cols
    store.close()
