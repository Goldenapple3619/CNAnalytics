from __future__ import annotations

from typing import Any

class BaseEnrichment(object):
    def __init__(self, *values: Any, **options: Any):
        self.values: tuple = values
        self.options: dict = options

    def __enter__(self):
        return self

    def __exit__(self, exception_type, exception_value, exception_traceback):
        return False

    @classmethod
    def validate(cls, **options: Any) -> None:
        """Called when the XML is loaded with the rule's constant keyword values (already cast).
        Raise ``ValueError`` to reject a bad configuration before any event is processed."""

    def run(self) -> Any:
        """Return the enrichment result (a JSON-serialisable dict, or ``None`` for nothing)."""
        raise NotImplementedError(f"{type(self).__name__}.run() is not implemented")


export = BaseEnrichment
