"""Secrets: Scout's own .env and redaction (SCOUT_SPEC 2.8, DESIGN 2.1).

`load_env` reads only `.env` in Scout's own folder and sets only the names in ALLOWED_ENV that are
not already set. It never logs or returns a value. Scout never reads any other bot's `.env`, any
`conf\\` folder or any token file. `redact` masks Anthropic keys, any `x-api-key` value and e-mail
addresses (the contact email included) in text bound for a log.
"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from scout.config import ScoutConfig
    from scout.paths import ScoutPaths

ALLOWED_ENV = ("ANTHROPIC_API_KEY",)
MASK = "<redacted>"
_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]+")
_HEADER = re.compile(r"(?i)(x-api-key['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")

__all__ = ["ALLOWED_ENV", "MASK", "load_env", "redact"]


def load_env(paths: ScoutPaths) -> list[str]:
    """Set allowed variables from Scout's .env (never overriding the environment). Returns the
    names set (never the values). A missing or unreadable file sets nothing."""
    try:
        text = paths.env_file.read_text(encoding="utf-8-sig")
    except OSError:
        return []
    done: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, value = line.split("=", 1)
        name = name.strip().removeprefix("export ").strip()
        value = value.strip().strip('"').strip("'")
        if name in ALLOWED_ENV and value and not os.environ.get(name):
            os.environ[name] = value
            done.append(name)
    return done


def redact(text: str, cfg: ScoutConfig | None = None) -> str:
    """`text` with keys, x-api-key values and e-mail addresses masked."""
    out = _KEY.sub(MASK, str(text))
    out = _HEADER.sub(lambda m: m.group(1) + MASK, out)
    if cfg is not None and cfg.contact_email:
        out = out.replace(cfg.contact_email, MASK)
    out = _EMAIL.sub(MASK, out)
    for name in ALLOWED_ENV:
        value = os.environ.get(name)
        if value and len(value) >= 8:
            out = out.replace(value, MASK)
    return out
