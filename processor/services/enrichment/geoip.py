"""GeoIP enrichment.

XML::

    <pysymbol>services.enrichment.geoip</pysymbol>
    <values>
        <key constructor="str">ip</key>
        <const constructor="bool" kwargs="check_vpn">1</const>
    </values>

Result (stored under ``<as>``)::

    {"ip": "8.8.8.8", "valid": true, "is_private": false,
     "country_code": "US", "country": "United States", "city": null,
     "cidr": "8.8.0.0/15", "asn_name": null,
     "is_vpn": null, "vpn_reason": null}          # the last two only when check_vpn is true

Data sources (all optional, the enrichment degrades gracefully):

* ``geoip2fast`` (pip) - bundled offline country database, no licence key needed.
* ``PROCESSOR_VPN_RANGES`` - path of a text file with one CIDR per line (``#`` comments) of known
  VPN / proxy ranges. ``is_vpn`` is ``true`` when the ip matches, ``false`` when it does not, and
  ``null`` when no VPN data is configured at all (unknown, not "no").

To plug a more precise provider (MaxMind city db, ipinfo ...) replace :func:`_lookup`.
"""
from __future__ import annotations

import bisect
import ipaddress
import logging
import os
import threading
import warnings
from functools import lru_cache
from typing import Optional

from .base import BaseEnrichment

log = logging.getLogger(__name__)

_ASN_HINTS = ("vpn", "proxy", "tor exit", "hosting", "datacenter", "data center",
              "digitalocean", "ovh", "hetzner", "linode", "vultr", "choopa")


# --------------------------------------------------------------------------- backends
_lock = threading.Lock()
_geo = None
_geo_loaded = False


def _backend():
    global _geo, _geo_loaded
    if _geo_loaded:
        return _geo
    with _lock:
        if not _geo_loaded:
            try:
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")
                    from geoip2fast import GeoIP2Fast
                    _geo = GeoIP2Fast(verbose=False)
            except Exception as exc:
                log.warning("geoip2fast unavailable (%s) - geo fields will be null", exc)
            _geo_loaded = True
    return _geo


class _CidrSet:
    """Merged, sorted ranges + bisect: O(log n) membership for large VPN lists."""

    def __init__(self, cidrs: list[str]):
        self.starts: dict[int, list[int]] = {4: [], 6: []}
        self.ends: dict[int, list[int]] = {4: [], 6: []}
        nets: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
        for text in cidrs:
            try:
                net = ipaddress.ip_network(text, strict=False)
            except ValueError:
                log.warning("ignoring invalid CIDR %r", text)
                continue
            nets[net.version].append((int(net.network_address), int(net.broadcast_address)))
        for version, ranges in nets.items():
            merged: list[list[int]] = []
            for start, end in sorted(ranges):
                if merged and start <= merged[-1][1] + 1:
                    merged[-1][1] = max(merged[-1][1], end)
                else:
                    merged.append([start, end])
            self.starts[version] = [r[0] for r in merged]
            self.ends[version] = [r[1] for r in merged]

    def __len__(self) -> int:
        return len(self.starts[4]) + len(self.starts[6])

    def __contains__(self, ip: ipaddress._BaseAddress) -> bool:
        starts = self.starts[ip.version]
        i = bisect.bisect_right(starts, int(ip)) - 1
        return i >= 0 and int(ip) <= self.ends[ip.version][i]


_vpn_sets: dict[str, Optional[_CidrSet]] = {}


def _vpn_ranges() -> Optional[_CidrSet]:
    path = os.environ.get("PROCESSOR_VPN_RANGES", "")
    if not path:
        return None
    if path not in _vpn_sets:
        with _lock:
            if path not in _vpn_sets:
                try:
                    with open(path, encoding="utf-8") as fh:
                        lines = [ln.split("#", 1)[0].strip() for ln in fh]
                    _vpn_sets[path] = _CidrSet([ln for ln in lines if ln])
                    log.info("loaded %d VPN ranges from %s", len(_vpn_sets[path]), path)
                except OSError as exc:
                    log.warning("cannot read PROCESSOR_VPN_RANGES=%s: %s", path, exc)
                    _vpn_sets[path] = None
    return _vpn_sets[path]


# --------------------------------------------------------------------------- lookup
@lru_cache(maxsize=65_536)
def _lookup(ip_text: str, check_vpn: bool) -> dict:
    result: dict = {"ip": ip_text, "valid": True, "is_private": False,
                    "country_code": None, "country": None, "city": None,
                    "cidr": None, "asn_name": None}
    try:
        ip = ipaddress.ip_address(ip_text.strip())
    except ValueError:
        result.update(valid=False)
        return result

    result["ip"] = str(ip)
    result["is_private"] = bool(ip.is_private or ip.is_loopback or ip.is_link_local
                                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    asn_name = ""
    geo = None if result["is_private"] else _backend()
    if geo is not None:
        found = geo.lookup(str(ip))
        if not getattr(found, "is_private", False):
            result["country_code"] = (found.country_code or None) if found.country_code not in ("", "--") else None
            result["country"] = found.country_name or None
            city = getattr(found, "city", None)
            result["city"] = (getattr(city, "name", None) or None) if city is not None else None
            result["cidr"] = found.cidr or None
            asn_name = found.asn_name or ""
            result["asn_name"] = asn_name or None

    if check_vpn:
        ranges = _vpn_ranges()
        is_vpn: Optional[bool] = None
        reason = None
        if ranges is not None:
            is_vpn = ip in ranges
            reason = "range" if is_vpn else None
        if asn_name and any(hint in asn_name.lower() for hint in _ASN_HINTS):
            is_vpn, reason = True, "asn"
        elif asn_name and is_vpn is None:
            is_vpn = False
        result.update(is_vpn=is_vpn, vpn_reason=reason)
    return result


class GeoIpEnrichment(BaseEnrichment):
    def __init__(self, *values, check_vpn: bool = False):
        super().__init__(*values)
        self.check_vpn = bool(check_vpn)

    def run(self) -> Optional[dict]:
        if not self.values or self.values[0] in (None, ""):
            return None
        return dict(_lookup(str(self.values[0]), self.check_vpn))  # copy: the cache is shared


export = GeoIpEnrichment
