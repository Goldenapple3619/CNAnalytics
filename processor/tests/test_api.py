import time

import pytest
from fastapi.testclient import TestClient

from conftest import T0, write_row
from src.api.app import create_app

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
SEC = 1000


@pytest.fixture
def api(env, make_processor):
    now = int(time.time() * 1000)
    for i in range(6):   # 6 failures from one ip -> burst alert; recent so default windows include them
        write_row(env.ingest_db, "login_fail", now - 60_000 + i * SEC,
                  payload={"ip": "8.8.8.8", "user_agent": CHROME, "account": "a"})
    write_row(env.ingest_db, "login_fail", now - 30_000, payload={"ip": "81.2.69.142", "user_agent": "curl/8.0", "account": "b"})
    write_row(env.ingest_db, "purchase", now - 20_000, payload={"total": 10})
    p = make_processor()
    p.drain()
    p.running = True  # the SSE loop only runs while the processor does
    client = TestClient(create_app(p, env))
    client.now = now
    return client


def test_health_status_applications(api):
    assert api.get("/health").json()["status"] in ("ok", "degraded")
    assert api.get("/api/v1/status").json()["processed"] == 8
    apps = api.get("/api/v1/applications").json()["applications"]
    events = {e["name"]: e for e in apps[0]["sources"][0]["events"]}
    assert [d["key"] for d in events["login_fail"]["dimensions"]][:2] == ["ip", "user_agent"]
    assert {a["name"] for a in events["login_fail"]["alerts"]} == {"burst", "stuffing", "bot"}


def test_events_filters_and_pagination(api):
    body = api.get("/api/v1/events?event_type=login_fail&limit=3").json()
    assert len(body["items"]) == 3 and body["next_before_id"] == body["items"][-1]["id"]
    page2 = api.get(f"/api/v1/events?event_type=login_fail&limit=10&before_id={body['next_before_id']}").json()
    assert len(page2["items"]) == 4 and page2["next_before_id"] is None
    by_ip = api.get("/api/v1/events?where=ip=81.2.69.142").json()["items"]
    assert len(by_ip) == 1 and by_ip[0]["enriched"]["user_agent"]["browser"] == "curl"
    by_enriched = api.get("/api/v1/events?where=ip.country_code=US&where=event_type=login_fail").json()["items"]
    assert len(by_enriched) == 6
    one = api.get(f"/api/v1/events/{by_ip[0]['event_id']}").json()
    assert one["dims"]["account"] == "b"
    assert api.get("/api/v1/events/nope").status_code == 404


def test_metrics(api):
    s = api.get("/api/v1/metrics/summary").json()
    assert s["events"]["total"] == 8
    assert {t["value"]: t["count"] for t in s["events"]["by_type"]} == {"login_fail": 7, "purchase": 1}
    assert s["alerts"]["total"] >= 1

    top = api.get("/api/v1/metrics/top?field=ip&event_type=login_fail").json()["items"]
    assert top[0] == {"value": "8.8.8.8", "count": 6}
    top = api.get("/api/v1/metrics/top?field=ip.country_code").json()["items"]
    assert top[0]["value"] == "US"

    ts = api.get("/api/v1/metrics/timeseries?from=-5m&interval=1m&group_by=event_type").json()
    assert ts["interval_ms"] == 60_000
    totals = {s["group"]: sum(p[1] for p in s["points"]) for s in ts["series"]}
    assert totals == {"login_fail": 7, "purchase": 1}
    assert all(len(s["points"]) == len(ts["series"][0]["points"]) for s in ts["series"])   # dense & aligned
    plain = api.get("/api/v1/metrics/timeseries?from=-5m&interval=1m").json()
    assert sum(p[1] for p in plain["series"][0]["points"]) == 8


@pytest.mark.parametrize("url", [
    "/api/v1/metrics/top?field=ip%27%3B%20DROP%20TABLE%20events",
    "/api/v1/events?where=garbage",
    "/api/v1/events?from=yesterday-ish",
    "/api/v1/metrics/timeseries?interval=1s&from=-30d",
    "/api/v1/alerts?severity=urgent",
    "/api/v1/alerts?status=gone",
])
def test_bad_input_is_a_400_not_a_500(api, url):
    assert api.get(url).status_code == 400


def test_sql_injection_attempt_does_not_touch_data(api):
    api.get("/api/v1/events?where=ip=1'%20OR%20'1'='1")
    assert api.get("/api/v1/status").json()["processed"] == 8
    assert len(api.get("/api/v1/events?where=ip=1'%20OR%20'1'='1").json()["items"]) == 0


def test_alert_lifecycle(api):
    items = api.get("/api/v1/alerts?severity=high").json()["items"]
    assert items and items[0]["rule_name"] == "burst" and items[0]["details"]["count"] >= 4
    alert_id = items[0]["id"]
    assert api.get(f"/api/v1/alerts/{alert_id}").json()["group_value"] == "8.8.8.8"

    r = api.patch(f"/api/v1/alerts/{alert_id}", json={"status": "acknowledged", "note": "on it"})
    assert r.status_code == 200 and r.json()["status"] == "acknowledged" and r.json()["note"] == "on it"
    assert api.patch(f"/api/v1/alerts/{alert_id}", json={"status": "bogus"}).status_code == 422
    assert api.patch("/api/v1/alerts/99999", json={"status": "resolved"}).status_code == 404
    assert api.get("/api/v1/alerts?status=open&severity=high").json()["total"] == 0
    assert api.get("/api/v1/alerts?status=acknowledged").json()["total"] == 1


def test_alert_stream_over_real_server(env, make_processor):
    """SSE needs a real server: TestClient cannot consume an endless response."""
    import socket
    import httpx
    from src.api.server import ApiServer

    now = int(time.time() * 1000)
    for i in range(5):
        write_row(env.ingest_db, "login_fail", now - 5_000 + i * 100, payload={"ip": "8.8.8.8", "user_agent": CHROME, "account": "a"})
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    p = make_processor(env.with_overrides(api_port=port))
    p.start()
    server = ApiServer(p, p.settings)
    server.start()
    try:
        deadline = time.time() + 5
        while time.time() < deadline and p.metrics["alerts_created"] < 1:
            time.sleep(0.05)
        lines = []
        with httpx.stream("GET", f"http://127.0.0.1:{port}/api/v1/alerts/stream?after_rev=0", timeout=5) as r:
            assert r.headers["content-type"].startswith("text/event-stream")
            for line in r.iter_lines():
                lines.append(line)
                if line.startswith("data:"):
                    break
        assert "event: alert" in lines
        assert httpx.get(f"http://127.0.0.1:{port}/health").json()["running"] is True
    finally:
        server.stop()


def test_reload_endpoint(api, env):
    assert api.post("/api/v1/reload").json()["changed"] is False
    (env.applications_dir / "shop.xml").write_text("<application name='shop'><broken")
    r = api.post("/api/v1/reload")
    assert r.status_code == 422 and r.json()["detail"]["errors"]


def test_api_key(env, make_processor):
    p = make_processor(env.with_overrides(api_key="s3cret"))
    p.open()
    c = TestClient(create_app(p, p.settings))
    assert c.get("/health").status_code in (200, 503)                     # health stays open
    assert c.get("/api/v1/status").status_code == 401
    assert c.get("/api/v1/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert c.get("/api/v1/status", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert c.get("/api/v1/status?api_key=s3cret").status_code == 401      # query key only for the SSE stream
