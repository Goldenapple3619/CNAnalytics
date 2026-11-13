"""Constructors usable in the XML (`constructor="int"`, ...).

Unlike Python's builtins these are safe on strings coming from XML: ``bool("0")``
is ``True`` in Python, but ``cast("bool", "0")`` is ``False``.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .duration import parse_duration

_TRUE = {"1", "true", "yes", "y", "on", "t"}
_FALSE = {"0", "false", "no", "n", "off", "f", ""}


def _to_str(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _to_int(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(f"cannot cast {value!r} to int without loss")
        return int(value)
    text = str(value).strip()
    try:
        return int(text)
    except ValueError:
        number = float(text)
        if not number.is_integer():
            raise ValueError(f"cannot cast {value!r} to int without loss") from None
        return int(number)


def _to_float(value: Any) -> float:
    return float(value)


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    raise ValueError(f"cannot cast {value!r} to bool")


def _to_json(value: Any) -> Any:
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def _to_list(value: Any) -> list:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [part.strip() for part in str(value).split(",") if part.strip()]


CONSTRUCTORS: dict[str, Callable[[Any], Any]] = {
    "str": _to_str,
    "int": _to_int,
    "float": _to_float,
    "bool": _to_bool,
    "json": _to_json,
    "list": _to_list,
    "duration": parse_duration
}


def cast(constructor: str, value: Any) -> Any:
    try:
        return CONSTRUCTORS[constructor](value)
    except KeyError:
        raise ValueError(f"unknown constructor {constructor!r}") from None
