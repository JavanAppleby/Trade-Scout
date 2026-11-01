"""scout_reader: the stdlib-only reader of Scout's files (SCOUT_SPEC 3, 10.3; DESIGN 3).

Radar and Pilot install it at a pinned tag
(`pip install "git+https://github.com/JavanAppleby/Trade-Scout.git@<tag>#subdirectory=scout_reader"`).
Every public function answers "true", "false" or "unknown" with evidence and never raises into the
caller. The six functions, `Reader` and `hold_allowed` live in `scout_reader.api` and are loaded on
first use (PEP 562), so `import scout_reader` stays cheap.
"""

from __future__ import annotations

from typing import Any

__version__ = "0.1.0"

__all__ = [
    "Reader", "event_before_next_session", "fresh_catalyst", "halt_open", "hold_allowed", "jason_tags",
    "market_context", "news_headline",
]


def __getattr__(name: str) -> Any:
    if name in __all__:
        try:
            from scout_reader import api  # noqa: PLC0415 - lazy by design
        except ImportError as exc:  # the api module is built by a later unit
            raise AttributeError(f"scout_reader.{name} is not built yet") from exc
        return getattr(api, name)
    raise AttributeError(f"module 'scout_reader' has no attribute {name!r}")
