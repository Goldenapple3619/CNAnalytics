"""Resolve ``<pysymbol>`` values to the class a service module exports."""
from __future__ import annotations

import importlib
from functools import lru_cache

# Only code living under these packages can be referenced from an XML file.
ALLOWED_PREFIXES = ("services.",)


class SymbolError(Exception):
    pass


@lru_cache(maxsize=None)
def resolve(symbol: str) -> type:
    if not symbol.startswith(ALLOWED_PREFIXES):
        raise SymbolError(f"{symbol!r}: only modules under {', '.join(ALLOWED_PREFIXES)} may be used")
    try:
        module = importlib.import_module(symbol)
    except ModuleNotFoundError as exc:
        raise SymbolError(f"{symbol!r}: module not found ({exc})") from exc
    except Exception as exc:  # broken import inside the service
        raise SymbolError(f"{symbol!r}: import failed ({type(exc).__name__}: {exc})") from exc
    exported = getattr(module, "export", None)
    if not isinstance(exported, type):
        raise SymbolError(f"{symbol!r}: module must define `export = <class>`")
    return exported
