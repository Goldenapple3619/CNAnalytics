import json
import sqlite3
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402

INGEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS analytics (
    id INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE, event_type TEXT NOT NULL,
    timestamp_ms INTEGER NOT NULL, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
    source TEXT NOT NULL, application TEXT NOT NULL, environment TEXT NOT NULL,
    schema_version INTEGER NOT NULL, payload_json TEXT NOT NULL, attributes TEXT NOT NULL,
    received_at_ms INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_analytics_type_ts ON analytics (event_type, timestamp_ms);
CREATE INDEX IF NOT EXISTS idx_analytics_user ON analytics (user_id);
"""

T0 = 1_800_000_000_000  # a fixed "now" for deterministic windows

APP_XML = """<application name="shop">
  <source name="api"><events>
    <event name="login_fail">
      <dimensions><key>ip</key><key>user_agent</key><key>account</key><key constructor="int">attempts</key></dimensions>
      <enrichment>
        <rule name="geo"><pysymbol>services.enrichment.geoip</pysymbol>
          <values><key constructor="str">ip</key><const constructor="bool" kwargs="check_vpn">1</const></values>
          <as>ip</as></rule>
        <rule name="ua"><pysymbol>services.enrichment.ua_parse</pysymbol>
          <values><key>user_agent</key></values><as>user_agent</as></rule>
      </enrichment>
      <alerts>
        <rule name="burst" severity="high" cooldown="5m"><pysymbol>services.detection.rate_above</pysymbol>
          <values><key kwargs="group_by">ip</key><const kwargs="window">1m</const>
                  <const constructor="int" kwargs="threshold">3</const></values></rule>
        <rule name="stuffing" severity="critical" cooldown="5m"><pysymbol>services.detection.distinct_above</pysymbol>
          <values><key kwargs="group_by">ip</key><key kwargs="distinct">account</key>
                  <const kwargs="window">1m</const><const constructor="int" kwargs="threshold">2</const></values></rule>
        <rule name="bot" severity="low" cooldown="5m"><pysymbol>services.detection.field_match</pysymbol>
          <values><key constructor="bool" kwargs="field">user_agent.is_automated</key>
                  <const kwargs="op">eq</const><const constructor="bool" kwargs="value">1</const>
                  <key kwargs="group_by">ip</key></values></rule>
      </alerts>
    </event>
    <event name="purchase">
      <dimensions><key constructor="float">total</key><key>device.os</key></dimensions>
    </event>
  </events></source>
</application>
"""


def write_row(path: Path, event_type="login_fail", ts=T0, user="u1", payload=None, application="shop",
              source="api", attributes=None, raw_payload=None, event_id=None):
    conn = sqlite3.connect(path)
    conn.executescript(INGEST_SCHEMA)
    conn.execute(
        "INSERT INTO analytics (event_id, event_type, timestamp_ms, user_id, session_id, source, application,"
        " environment, schema_version, payload_json, attributes, received_at_ms) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (event_id or str(uuid.uuid4()), event_type, ts, user, "s1", source, application, "test", 1,
         raw_payload if raw_payload is not None else json.dumps(payload or {}),
         json.dumps(attributes or {}), ts + 10))
    conn.commit()
    conn.close()


@pytest.fixture
def env(tmp_path):
    apps = tmp_path / "applications"
    apps.mkdir()
    (apps / "shop.xml").write_text(APP_XML)
    settings = Settings().with_overrides(
        ingest_db=tmp_path / "ingest.sqlite", processor_db=tmp_path / "proc.sqlite",
        applications_dir=apps, poll_interval=0.05, reload_check_interval=0)
    return settings


@pytest.fixture
def make_processor(env):
    created = []

    def factory(settings=None):
        from src.processor import Processor
        p = Processor(settings or env)
        created.append(p)
        return p

    yield factory
    for p in created:
        p.stop()
        if p.store:
            p.store.close()
