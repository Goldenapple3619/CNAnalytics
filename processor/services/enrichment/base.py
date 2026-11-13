"""Base class of every enrichment.

An enrichment is built once per event with the arguments declared in the XML ``<values>``
(positional ``<key>``/``<const>`` first, then ``kwargs=`` ones), used as a context manager,
and asked for its result::

    with GeoIpEnrichment("1.2.3.4", check_vpn=True) as enrichment:
        result = enrichment.run()      # JSON-serialisable dict

The result is stored in ``event.enriched[<as>]``. Two rules with the same ``<as>`` are merged,
so several rules can enrich the same dimension.

Expensive resources (databases, caches, HTTP sessions) must live at module level, not in the
instance: instances are short-lived. Use ``__exit__`` to release per-call resources.
"""
from __future__ import annotations

from typing import Any


class BaseEnrichment(object):
    def __init__(self, *values: Any, **options: Any):
        self.values: tuple = values
        self.options: dict = options

    @classmethod
    def validate(cls, **options: Any) -> None:
        """Called when the XML is loaded with the rule's constant keyword values (already cast).
        Raise ``ValueError`` to reject a bad configuration before any event is processed."""

    def run(self) -> Any:
        """Return the enrichment result (a JSON-serialisable dict, or ``None`` for nothing)."""
        raise NotImplementedError(f"{type(self).__name__}.run() is not implemented")


export = BaseEnrichment
