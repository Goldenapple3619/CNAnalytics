from types import SimpleNamespace

import pytest

from services.detection.base import DetectionContext
from services.detection.distinct_above import DistinctAboveDetection
from services.detection.field_match import FieldMatchDetection
from services.detection.rate_above import RateAboveDetection
from services.enrichment.geoip import GeoIpEnrichment, _CidrSet
from services.enrichment.ua_parse import UaParseEnrichment

SEC = 1000


def ctx(state, ts, **keys):
    return DetectionContext(event=SimpleNamespace(), state=state, ts_ms=ts, keys=keys)


def feed_rate(state, ts, group="1.2.3.4", window="10s", threshold=3):
    with RateAboveDetection(group, window=window, threshold=threshold) as d:
        return d.evaluate(ctx(state, ts, group_by="ip"))


def test_rate_above_fires_only_above_threshold_inside_window():
    state = {}
    assert [feed_rate(state, i * SEC) for i in range(3)] == [None, None, None]
    finding = feed_rate(state, 3 * SEC)
    assert finding and finding.details["count"] == 4 and finding.group_value == "1.2.3.4"
    assert finding.group_key == "ip"


def test_rate_above_window_slides():
    state = {}
    for i in range(4):
        feed_rate(state, i * SEC)
    assert feed_rate(state, 30 * SEC) is None  # old ones expired, only 1 left


def test_rate_above_groups_are_independent():
    state = {}
    for i in range(10):
        feed_rate(state, i * 100, group="a")
    assert feed_rate(state, 1000, group="b") is None


def test_rate_above_handles_out_of_order_events():
    state = {}
    for ts in (5 * SEC, 1 * SEC, 3 * SEC):
        feed_rate(state, ts)
    finding = feed_rate(state, 4 * SEC)
    assert finding and finding.details["count"] == 4


def test_rate_above_sweep_drops_stale_groups():
    state = {}
    feed_rate(state, 0, group="old")
    feed_rate(state, 100 * SEC, group="new")
    RateAboveDetection.sweep(state, 100 * SEC, window="10s")
    assert list(state["groups"]) == ["new"]


def test_distinct_above_counts_distinct_values_not_events():
    state = {}

    def feed(ts, account):
        with DistinctAboveDetection("ip1", account, window="10s", threshold=2) as d:
            return d.evaluate(ctx(state, ts, group_by="ip", distinct="account"))

    assert feed(0, "a") is None
    assert feed(1 * SEC, "a") is None      # same account again: still 1 distinct
    assert feed(2 * SEC, "b") is None
    finding = feed(3 * SEC, "c")
    assert finding and finding.details["distinct_count"] == 3
    assert feed(60 * SEC, "d") is None     # window expired


@pytest.mark.parametrize("op, actual, expected, hit", [
    ("eq", True, True, True), ("eq", "FR", "FR", True), ("ne", "FR", "DE", True),
    ("in", "FR", ["FR", "DE"], True), ("not_in", "FR", ["FR", "DE"], False),
    ("gt", 5, 3, True), ("gte", 3, 3, True), ("lt", 5, 3, False),
    ("contains", "hello world", "wor", True), ("startswith", "abc", "ab", True),
    ("regex", "acc-123", r"^acc-\d+$", True), ("gt", "abc", 3, False),
])
def test_field_match_operators(op, actual, expected, hit):
    with FieldMatchDetection(actual, op=op, value=expected) as d:
        finding = d.evaluate(ctx({}, 0, field="f"))
    assert bool(finding) is hit


def test_ua_parse():
    chrome = UaParseEnrichment("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36").run()
    assert (chrome["browser"], chrome["os"], chrome["device"], chrome["is_automated"]) == \
           ("Chrome", "Windows", "desktop", False)
    edge = UaParseEnrichment("Mozilla/5.0 (Windows NT 10.0) AppleWebKit/537.36 Chrome/119.0 Safari/537.36 Edg/119.0.0.0").run()
    assert edge["browser"] == "Edge"
    iphone = UaParseEnrichment("Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) AppleWebKit/605.1.15 "
                               "(KHTML, like Gecko) Version/17.2 Mobile/15E148 Safari/604.1").run()
    assert (iphone["browser"], iphone["os"], iphone["os_version"], iphone["device"]) == ("Safari", "iOS", "17.2", "mobile")
    assert UaParseEnrichment("python-requests/2.31.0").run()["is_automated"] is True
    assert UaParseEnrichment("Googlebot/2.1").run()["is_bot"] is True
    assert UaParseEnrichment("").run()["empty"] is True


def test_geoip_public_private_invalid():
    public = GeoIpEnrichment("8.8.8.8").run()
    assert public["country_code"] == "US" and public["is_private"] is False
    assert "is_vpn" not in public                          # only with check_vpn
    assert GeoIpEnrichment("10.1.2.3").run()["is_private"] is True
    assert GeoIpEnrichment("not-an-ip").run()["valid"] is False
    assert GeoIpEnrichment("8.8.8.8", check_vpn=True).run()["is_vpn"] is None   # no VPN data = unknown
    assert GeoIpEnrichment(None).run() is None


def test_geoip_vpn_ranges(tmp_path, monkeypatch):
    from services.enrichment import geoip
    geoip._lookup.cache_clear()
    geoip._vpn_sets.clear()
    ranges = tmp_path / "vpn.txt"
    ranges.write_text("# comment\n8.8.8.0/24\n2001:db8::/32\n")
    monkeypatch.setenv("PROCESSOR_VPN_RANGES", str(ranges))
    assert GeoIpEnrichment("8.8.8.8", check_vpn=True).run()["is_vpn"] is True
    assert GeoIpEnrichment("1.1.1.1", check_vpn=True).run()["is_vpn"] is False
    geoip._lookup.cache_clear()
    geoip._vpn_sets.clear()


def test_cidr_set_merges_and_bisects():
    import ipaddress
    s = _CidrSet(["10.0.0.0/24", "10.0.1.0/24", "192.168.0.0/16", "bogus"])
    assert ipaddress.ip_address("10.0.1.200") in s
    assert ipaddress.ip_address("10.0.2.1") not in s
    assert ipaddress.ip_address("192.168.5.5") in s
    assert ipaddress.ip_address("::1") not in s
