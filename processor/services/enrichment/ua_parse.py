"""Lightweight User-Agent parser (no dependency).

XML::

    <pysymbol>services.enrichment.ua_parse</pysymbol>
    <values><key constructor="str">user_agent</key></values>
    <as>user_agent</as>

Result::

    {"kind": "browser|bot|library|unknown", "browser": "Chrome", "browser_version": "120.0.0.0",
     "os": "Windows", "os_version": "10", "device": "desktop|mobile|tablet|unknown",
     "is_bot": false, "is_automated": false, "empty": false}

``is_bot``       - crawlers / headless browsers.
``is_automated`` - bots *and* plain HTTP libraries (curl, python-requests, Go-http-client ...):
                   anything that is clearly not a person behind a browser.
This is regex based and meant for triage, not for exact fingerprinting.
"""
from __future__ import annotations

import re
from functools import lru_cache
from typing import Optional

from .base import BaseEnrichment

_BOTS = re.compile(
    r"bot|crawl|spider|slurp|headless|phantomjs|selenium|puppeteer|playwright|lighthouse|"
    r"facebookexternalhit|preview|monitor|uptime", re.I)

# (regex, name) - order matters: first match wins.
_LIBRARIES = [
    (re.compile(r"curl/([\d.]+)", re.I), "curl"),
    (re.compile(r"wget/([\d.]+)", re.I), "wget"),
    (re.compile(r"python-requests/([\d.]+)", re.I), "python-requests"),
    (re.compile(r"python-urllib/([\d.]+)", re.I), "python-urllib"),
    (re.compile(r"python-httpx/([\d.]+)", re.I), "httpx"),
    (re.compile(r"aiohttp/([\d.]+)", re.I), "aiohttp"),
    (re.compile(r"go-http-client/([\d.]+)", re.I), "go-http-client"),
    (re.compile(r"okhttp/([\d.]+)", re.I), "okhttp"),
    (re.compile(r"axios/([\d.]+)", re.I), "axios"),
    (re.compile(r"node-fetch/([\d.]+)", re.I), "node-fetch"),
    (re.compile(r"java/([\d._]+)", re.I), "java"),
    (re.compile(r"libwww-perl/([\d.]+)", re.I), "libwww-perl"),
    (re.compile(r"postmanruntime/([\d.]+)", re.I), "postman"),
]

# (regex, name) - order matters: Edge/Opera/Samsung also say "Chrome", Chrome also says "Safari".
_BROWSERS = [
    (re.compile(r"edgios?/([\d.]+)|edga?/([\d.]+)|edg/([\d.]+)", re.I), "Edge"),
    (re.compile(r"opr/([\d.]+)|opera/([\d.]+)|opios/([\d.]+)", re.I), "Opera"),
    (re.compile(r"samsungbrowser/([\d.]+)", re.I), "Samsung Internet"),
    (re.compile(r"firefox/([\d.]+)|fxios/([\d.]+)", re.I), "Firefox"),
    (re.compile(r"(?:chrome|crios)/([\d.]+)", re.I), "Chrome"),
    (re.compile(r"version/([\d.]+).*safari/", re.I), "Safari"),
    (re.compile(r"msie ([\d.]+)|trident/.*rv:([\d.]+)", re.I), "Internet Explorer"),
]

_OS = [
    (re.compile(r"windows nt ([\d.]+)", re.I), "Windows", {"10.0": "10/11", "6.3": "8.1", "6.2": "8", "6.1": "7"}),
    (re.compile(r"android ([\d.]+)", re.I), "Android", {}),
    (re.compile(r"(?:iphone|cpu) os ([\d_]+)", re.I), "iOS", {}),
    (re.compile(r"ipad.*os ([\d_]+)", re.I), "iPadOS", {}),
    (re.compile(r"mac os x ([\d_.]+)", re.I), "macOS", {}),
    (re.compile(r"cros [^ ]+ ([\d.]+)", re.I), "ChromeOS", {}),
    (re.compile(r"(linux)", re.I), "Linux", {}),
]


def _first_group(match: "re.Match") -> Optional[str]:
    return next((g for g in match.groups() if g), None)


@lru_cache(maxsize=16_384)
def _parse(ua: str) -> dict:
    result = {"kind": "unknown", "browser": None, "browser_version": None, "os": None,
              "os_version": None, "device": "unknown", "is_bot": False, "is_automated": False,
              "empty": not ua.strip()}
    if result["empty"]:
        result["is_automated"] = True  # real browsers always send a user agent
        return result

    for pattern, name, aliases in _OS:
        match = pattern.search(ua)
        if match:
            result["os"] = name
            if name != "Linux":
                version = match.group(1).replace("_", ".")
                result["os_version"] = aliases.get(version, version)
            break

    lowered = ua.lower()
    if "ipad" in lowered or "tablet" in lowered or ("android" in lowered and "mobile" not in lowered):
        result["device"] = "tablet"
    elif "mobile" in lowered or "iphone" in lowered or "android" in lowered:
        result["device"] = "mobile"
    elif result["os"] in ("Windows", "macOS", "Linux", "ChromeOS"):
        result["device"] = "desktop"

    if _BOTS.search(ua):
        result.update(kind="bot", is_bot=True, is_automated=True)
        return result

    for pattern, name in _LIBRARIES:
        match = pattern.search(ua)
        if match:
            result.update(kind="library", browser=name, browser_version=match.group(1), is_automated=True)
            return result

    for pattern, name in _BROWSERS:
        match = pattern.search(ua)
        if match:
            result.update(kind="browser", browser=name, browser_version=(_first_group(match) or "").replace("_", ".") or None)
            return result
    return result


class UaParseEnrichment(BaseEnrichment):
    def run(self) -> Optional[dict]:
        if not self.values or self.values[0] is None:
            return None
        return dict(_parse(str(self.values[0])))


export = UaParseEnrichment
