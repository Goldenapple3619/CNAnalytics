import pytest

from src.casting import cast
from src.duration import format_duration, parse_duration
from src.loader import SpecErrors, load_registry
from src.spec import Registry


def test_example_application_is_valid():
    from src.config import ROOT
    registry = load_registry(ROOT / "applications")
    assert registry.lookup("moonreward", "api", "connection_fail") is not None


def test_durations():
    assert parse_duration("500ms") == 500
    assert parse_duration("5m") == 300_000
    assert parse_duration("2h") == 7_200_000
    assert parse_duration("30") == 30_000
    assert format_duration(300_000) == "5m"
    with pytest.raises(ValueError):
        parse_duration("5x")


def test_bool_cast_reads_text_not_truthiness():
    assert cast("bool", "0") is False
    assert cast("bool", "1") is True
    assert cast("bool", "false") is False
    assert cast("int", "50") == 50
    assert cast("int", 3.0) == 3
    with pytest.raises(ValueError):
        cast("int", 3.5)
    assert cast("list", "a, b ,c") == ["a", "b", "c"]


def _load(tmp_path, xml):
    (tmp_path / "a.xml").write_text(xml)
    return load_registry(tmp_path)


def _event(inner, dims="<key>ip</key>"):
    return (f'<application name="a"><source name="s"><events><event name="e">'
            f"<dimensions>{dims}</dimensions>{inner}</event></events></source></application>")


def _alert(values, symbol="services.detection.rate_above", attrs=""):
    return f"<alerts><rule {attrs}><pysymbol>{symbol}</pysymbol><values>{values}</values></rule></alerts>"


@pytest.mark.parametrize("xml, fragment", [
    ("<application", "not well-formed"),
    ("<foo/>", "root element must be <application>"),
    (_event(_alert("<key kwargs='group_by'>nope</key>")), "<key>nope</key> is not a declared dimension"),
    (_event(_alert("<key kwargs='group_by'>ip</key><const kwargs='window'>5x</const>")), "invalid duration"),
    (_event(_alert("<key kwargs='gruop_by'>ip</key>")), "arguments do not match"),
    (_event(_alert("<key kwargs='group_by'>ip</key>", symbol="services.detection.nothing")), "module not found"),
    (_event(_alert("<key kwargs='group_by'>ip</key>", symbol="os.path")), "only modules under"),
    (_event(_alert("<key kwargs='group_by'>ip</key>", symbol="services.enrichment.geoip")), "not a BaseDetection"),
    (_event(_alert("<key kwargs='group_by'>ip</key>", attrs="severity='urgent'")), "severity"),
    (_event(_alert("<key constructor='nope' kwargs='group_by'>ip</key>")), "unknown constructor"),
    (_event(_alert("<const kwargs='op'>zz</const>", symbol="services.detection.field_match")), "op must be one of"),
    (_event("<enrichment><rule><pysymbol>services.enrichment.ua_parse</pysymbol>"
            "<values><key>ip</key></values></rule></enrichment>"), "missing <as>"),
    (_event("", dims="<key>ip</key><key>ip</key>"), "duplicate dimension"),
])
def test_invalid_definitions_are_reported(tmp_path, xml, fragment):
    with pytest.raises(SpecErrors) as exc:
        _load(tmp_path, xml)
    assert fragment in str(exc.value)


def test_all_errors_reported_together(tmp_path):
    xml = _event(_alert("<key kwargs='group_by'>nope</key><const kwargs='window'>5x</const>"))
    with pytest.raises(SpecErrors) as exc:
        _load(tmp_path, xml)
    assert len(exc.value.errors) >= 1  # first problem per rule; more rules -> more errors


def test_enrichment_result_usable_as_key_only_after_its_rule(tmp_path):
    enrich = ("<enrichment><rule name='g'><pysymbol>services.enrichment.geoip</pysymbol>"
              "<values><key>ip</key></values><as>ip</as></rule></enrichment>")
    ok = _event(enrich + _alert("<key kwargs='group_by'>ip.country_code</key>"))
    assert isinstance(_load(tmp_path, ok), Registry)
    with pytest.raises(SpecErrors):
        _load(tmp_path, _event(_alert("<key kwargs='group_by'>ip.country_code</key>")))


def test_wildcard_event_lookup(tmp_path):
    xml = ('<application name="a"><source name="s"><events><event name="*"><dimensions><key>x</key></dimensions>'
           '</event><event name="known"><dimensions><key>y</key></dimensions></event></events></source></application>')
    reg = _load(tmp_path, xml)
    assert reg.lookup("a", "s", "known").dimensions[0].key == "y"
    assert reg.lookup("a", "s", "anything").dimensions[0].key == "x"
    assert reg.lookup("a", "other", "anything") is None
    assert reg.lookup("zzz", "s", "known") is None
