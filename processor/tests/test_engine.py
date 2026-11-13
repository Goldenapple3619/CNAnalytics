import json
import sqlite3

from conftest import T0, write_row

CHROME = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
SEC = 1000


def fail(env, ts, ip="8.8.8.8", account="a", ua=CHROME, **kw):
    write_row(env.ingest_db, "login_fail", ts, payload={"ip": ip, "user_agent": ua, "account": account}, **kw)


def alerts(env):
    c = sqlite3.connect(env.processor_db)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM alerts ORDER BY id")]


def events(env):
    c = sqlite3.connect(env.processor_db)
    c.row_factory = sqlite3.Row
    return [dict(r) for r in c.execute("SELECT * FROM events ORDER BY id")]


def test_dimensions_enrichment_and_ignored_events(env, make_processor):
    write_row(env.ingest_db, "login_fail", T0, payload={"ip": "8.8.8.8", "user_agent": CHROME, "account": "a",
                                                        "attempts": "7", "secret": "x"})
    write_row(env.ingest_db, "unknown_type", T0, payload={"ip": "1.1.1.1"})
    write_row(env.ingest_db, "login_fail", T0, application="other", payload={})
    p = make_processor()
    assert p.drain() == 3
    assert (p.metrics["processed"], p.metrics["ignored"]) == (1, 2)

    (row,) = events(env)
    dims = json.loads(row["dims_json"])
    assert dims == {"ip": "8.8.8.8", "user_agent": CHROME, "account": "a", "attempts": 7}  # typed, "secret" dropped
    enriched = json.loads(row["enriched_json"])
    assert enriched["ip"]["country_code"] == "US"
    assert enriched["user_agent"]["browser"] == "Chrome"
    assert row["errors_json"] is None


def test_dimension_lookup_order_nested_attributes_and_columns(env, make_processor):
    write_row(env.ingest_db, "purchase", T0, user="u-9", payload={"total": "12.5", "device": {"os": "ios"}})
    write_row(env.ingest_db, "purchase", T0, payload={"total": "oops"}, attributes={"device.os": "x"})
    p = make_processor()
    p.drain()
    first, second = events(env)
    assert json.loads(first["dims_json"]) == {"total": 12.5, "device.os": "ios"}
    assert "total" not in json.loads(second["dims_json"])        # bad cast: dropped ...
    assert "cannot cast" in json.loads(second["errors_json"])[0]  # ... and recorded, event kept


def test_invalid_payload_is_recorded_not_fatal(env, make_processor):
    write_row(env.ingest_db, "login_fail", T0, raw_payload="not json")
    p = make_processor()
    p.drain()
    (row,) = events(env)
    assert "payload_json is not a JSON object" in json.loads(row["errors_json"])
    assert p.metrics["errors"] == 0


def test_rule_with_missing_key_is_skipped(env, make_processor):
    write_row(env.ingest_db, "login_fail", T0, payload={"account": "a"})  # no ip, no user_agent
    p = make_processor()
    p.drain()
    (row,) = events(env)
    assert json.loads(row["enriched_json"]) == {} and row["errors_json"] is None
    assert alerts(env) == []


def test_broken_enrichment_keeps_the_event(env, make_processor, monkeypatch):
    from services.enrichment import ua_parse

    def boom(self):
        raise RuntimeError("provider down")
    monkeypatch.setattr(ua_parse.UaParseEnrichment, "run", boom)
    fail(env, T0)
    p = make_processor()
    p.drain()
    (row,) = events(env)
    assert "provider down" in row["errors_json"]
    assert json.loads(row["enriched_json"])["ip"]["country_code"] == "US"  # other enrichments still ran


def test_alerts_fire_dedupe_and_refire_after_cooldown(env, make_processor):
    for i in range(5):                                   # threshold 3 / 1m: fires from the 4th event
        fail(env, T0 + i * SEC, account="same")
    p = make_processor()
    p.drain()
    (burst,) = [a for a in alerts(env) if a["rule_name"] == "burst"]
    assert burst["severity"] == "high" and burst["group_value"] == "8.8.8.8"
    assert burst["occurrences"] == 2 and burst["status"] == "open"
    assert p.metrics["alerts_created"] == 1

    fail(env, T0 + 10 * 60 * SEC)                        # 10 min later: cooldown (5m) elapsed ...
    for i in range(1, 4):
        fail(env, T0 + 10 * 60 * SEC + i * SEC)
    p.drain()
    assert len([a for a in alerts(env) if a["rule_name"] == "burst"]) == 2   # ... new alert


def test_resolved_alert_is_not_reopened(env, make_processor):
    for i in range(5):
        fail(env, T0 + i * SEC)
    p = make_processor()
    p.drain()
    burst = next(a for a in alerts(env) if a["rule_name"] == "burst")
    p.store.update_alert(burst["id"], status="resolved")
    fail(env, T0 + 6 * SEC)
    p.drain()
    bursts = [a for a in alerts(env) if a["rule_name"] == "burst"]
    assert len(bursts) == 2 and bursts[0]["status"] == "resolved" and bursts[1]["status"] == "open"


def test_credential_stuffing_and_bot_rules(env, make_processor):
    for i in range(3):
        fail(env, T0 + i * SEC, ip="9.9.9.9", account=f"acc{i}", ua="curl/8.0")
    p = make_processor()
    p.drain()
    names = {a["rule_name"] for a in alerts(env)}
    assert {"stuffing", "bot"} <= names


def test_cursor_survives_restart_and_state_is_rebuilt(env, make_processor):
    for i in range(4):
        fail(env, T0 + i * SEC)                          # 4 events: burst fires on the 4th
    p1 = make_processor()
    p1.drain()
    assert len(alerts(env)) >= 1
    n_events = len(events(env))
    p1.stop()
    p1.store.close()

    fail(env, T0 + 5 * SEC)                              # one more event after the restart
    p2 = make_processor()
    assert p2.drain() == 1                               # nothing reprocessed
    assert len(events(env)) == n_events + 1
    burst = [a for a in alerts(env) if a["rule_name"] == "burst"]
    # Detection state was rebuilt from stored events: the new event continued the SAME alert
    # (a cold state would have seen 1 event, below the threshold, and not fired at all).
    assert len(burst) == 1 and burst[0]["occurrences"] == 2


def test_reload_keeps_unchanged_rule_state(env, make_processor):
    for i in range(3):
        fail(env, T0 + i * SEC)
    p = make_processor()
    p.drain()
    xml = (env.applications_dir / "shop.xml")
    xml.write_text(xml.read_text().replace('severity="low"', 'severity="medium"'))   # only the bot rule changes
    result = p.reload()
    assert result["ok"] and result["changed"] and result["rebuilt"] == 1
    fail(env, T0 + 3 * SEC)                              # 4th event: burst still counts the 3 before
    p.drain()
    assert any(a["rule_name"] == "burst" for a in alerts(env))


def test_invalid_reload_keeps_running_definitions(env, make_processor):
    fail(env, T0 - SEC)
    p = make_processor()
    p.drain()
    version = p.registry_version
    (env.applications_dir / "shop.xml").write_text("<application name='shop'><oops")
    result = p.reload()
    assert result["ok"] is False and p.registry_version == version
    fail(env, T0)
    assert p.drain() == 1 and p.metrics["processed"] == 2  # still processing with the old definitions


def test_ingest_db_missing_then_created(env, make_processor):
    from src.reader import SourceUnavailable
    import pytest
    p = make_processor()
    with pytest.raises(SourceUnavailable):
        p.drain()
    fail(env, T0)
    assert p.drain() == 1


def test_ingest_table_reset_restarts_from_zero(env, make_processor):
    for i in range(3):
        fail(env, T0 + i * SEC)
    p = make_processor()
    p.drain()
    env.ingest_db.unlink()
    for i in range(2):
        fail(env, T0 + 100 * SEC + i * SEC, account=f"new{i}")
    assert p.drain() == 2
    assert len(events(env)) == 5


def test_worker_thread_processes_live_rows(env, make_processor):
    import time
    p = make_processor()
    p.start()
    fail(env, T0)
    deadline = time.time() + 5
    while time.time() < deadline and p.metrics["processed"] < 1:
        time.sleep(0.05)
    assert p.metrics["processed"] == 1
    p.stop()
    assert p.running is False
