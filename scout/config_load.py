"""Reading and coercing config.toml values (DESIGN 2.1, 6). Pure helpers: every function returns
plain values or raises ConfigError naming the key. `scout.config` builds the frozen dataclasses.

Rules (Radar's pattern): an unknown key, a value of the wrong type, or a table where a value
belongs is an error. The contact email is never echoed in a message.
"""

from __future__ import annotations

import math
import tomllib
from collections.abc import Mapping
from datetime import date, time
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from scout.clock import parse_hhmm

__all__ = [
    "ConfigError", "as_bool", "as_date_list", "as_float", "as_int", "as_str", "as_str_list", "as_table", "as_time",
    "as_time_list", "check_keys", "is_subfolder", "read_toml",
]


class ConfigError(ValueError):
    """Invalid or unreadable Scout config.toml. The message names the key."""


def is_subfolder(value: str) -> bool:
    """True for a relative folder with no drive, no root and no "..", judged by Windows' path rules
    and by POSIX's alike. The bots run on Windows, so "C:/x", "C:x" and a backslash ".." must be
    refused wherever the config is checked (the tests also run on Linux, where the native rules
    would let them through)."""
    if not value.strip():
        return False
    for flavour in (PureWindowsPath, PurePosixPath):
        p = flavour(value)
        if p.is_absolute() or p.drive or p.anchor or ".." in p.parts:
            return False
    return True


def as_bool(name: str, value: Any) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name}: expected true/false, got {_shown(value)}")
    return value


def as_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{name}: expected a whole number, got {_shown(value)}")
    return value


def as_float(name: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{name}: expected a number, got {_shown(value)}")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(f"{name}: must be finite, got {value!r}")
    return result


def as_str(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{name}: expected a string, got {_shown(value)}")
    return value


def as_time(name: str, value: Any) -> time:
    if isinstance(value, time):
        return value
    try:
        return parse_hhmm(value)
    except ValueError as exc:
        raise ConfigError(f"{name}: {exc}") from None


def _list(name: str, value: Any) -> list[Any]:
    if isinstance(value, (str, bytes, Mapping)) or not isinstance(value, (list, tuple)):
        raise ConfigError(f"{name}: expected a list, got {_shown(value)}")
    return list(value)


def as_str_list(name: str, value: Any, *, unique: bool = True) -> tuple[str, ...]:
    out: list[str] = []
    for item in _list(name, value):
        if not isinstance(item, str) or not item.strip():
            raise ConfigError(f"{name}: items must be non-empty strings, got {_shown(item)}")
        if unique and item in out:
            raise ConfigError(f"{name}: {item!r} is listed twice")
        out.append(item)
    return tuple(out)


def as_time_list(name: str, value: Any) -> tuple[time, ...]:
    return tuple(as_time(name, v) for v in _list(name, value))


def as_date_list(name: str, value: Any) -> tuple[date, ...]:
    out: list[date] = []
    for item in _list(name, value):
        if isinstance(item, date) and not hasattr(item, "hour"):
            day = item
        elif isinstance(item, str):
            try:
                day = date.fromisoformat(item.strip())
            except ValueError:
                raise ConfigError(f"{name}: expected dates 'YYYY-MM-DD', got {item!r}") from None
        else:
            raise ConfigError(f"{name}: expected dates 'YYYY-MM-DD', got {_shown(item)}")
        if day in out:
            raise ConfigError(f"{name}: {day} is listed twice")
        out.append(day)
    return tuple(sorted(out))


def as_table(name: str, value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"{name}: expected a table [{name}], got {_shown(value)}")
    return value


def check_keys(name: str, data: Mapping[str, Any], allowed: set[str] | frozenset[str]) -> None:
    unknown = sorted(set(data) - set(allowed))
    if unknown:
        where = f"{name}." if name else ""
        raise ConfigError(f"unknown config key(s): {', '.join(where + k for k in unknown)}")


def read_toml(path: Path | str) -> dict[str, Any]:
    """Parse `path` as TOML (a BOM from Windows editors is tolerated)."""
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        raise ConfigError(f"config file not found: {path} (copy config.example.toml to config.toml)") from None
    except OSError as exc:
        raise ConfigError(f"{path}: cannot be read ({type(exc).__name__})") from None
    try:
        return tomllib.loads(raw.decode("utf-8-sig"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: not valid TOML: {exc}") from None


def _shown(value: Any) -> str:
    """A short description of a bad value: tables and long strings by type only."""
    if isinstance(value, Mapping):
        return "a table"
    if isinstance(value, str):
        return "a string"
    return repr(value) if len(repr(value)) <= 40 else f"a {type(value).__name__}"
