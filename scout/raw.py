"""Raw store (SCOUT_SPEC 6.1, 4.1 #4; DESIGN 2.2).

Each new item is kept as received under `state/raw/<source>/<ET date>/<HHMMSS>-<name>`, written once
(never overwritten: a clash gets a numbered name) and identified by its sha256, which facts carry as
`raw_sha256`. Raw items are kept for replay and relabelling and never republished.

Wire sources whose `terms_ok` is false (PR Newswire, until the owner reads its
terms) keep a reduced item only: title, link, guid and pubDate (`reduced_item`). The wire poller
always passes the reduced item for such a source; `RawStore(paths, cfg)` also reduces any body it is
given for one, as a second guard.
"""

from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from scout.clock import et_date, to_et
from scout.config import ScoutConfig
from scout.paths import ScoutPaths
from scout.store import sha256_bytes

__all__ = ["RawStore", "keeps_full_body", "reduced_item"]

_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_MAX_NAME = 80


def keeps_full_body(cfg: ScoutConfig | None, source: str) -> bool:
    """False for a wire source whose terms are not yet read (SS 4.1 #4)."""
    if cfg is None:
        return True
    try:
        src = cfg.source(source)
    except KeyError:
        return True
    return src.terms_ok or src.kind != "wire"


def reduced_item(title: str | None, link: str | None, guid: str | None, pub_date: str | None) -> bytes:
    """An RSS <item> holding only title, link, guid and pubDate."""
    item = ET.Element("item")
    for tag, value in (("title", title), ("link", link), ("guid", guid), ("pubDate", pub_date)):
        ET.SubElement(item, tag).text = value or ""
    return ET.tostring(item, encoding="utf-8")


def _reduce_body(body: bytes) -> bytes:
    """Keep title/link/guid/pubDate of every <item> in an RSS fragment or page; nothing else."""
    from scout.edgar_parse import SchemaError, safe_xml_root  # noqa: PLC0415 - avoid an import cycle

    try:
        root = safe_xml_root(body)
    except SchemaError:
        return b"<reduced/>"
    items = [root] if root.tag == "item" else root.iter("item")
    out = [reduced_item(*((it.findtext(t) or "").strip() for t in ("title", "link", "guid", "pubDate")))
           for it in items]
    return b"<items>" + b"".join(out) + b"</items>" if len(out) != 1 else out[0]


class RawStore:
    """implements interfaces.RawSink."""

    def __init__(self, paths: ScoutPaths, cfg: ScoutConfig | None = None) -> None:
        self.paths = paths
        self.cfg = cfg

    def folder(self, source: str, at: datetime) -> Path:
        return self.paths.raw / _clean(source) / et_date(at).isoformat()

    def save(self, source: str, name: str, body: bytes, at: datetime) -> str:
        """Write `body` once; returns its sha256 (of the bytes stored)."""
        if not keeps_full_body(self.cfg, source):
            body = _reduce_body(body)
        digest = sha256_bytes(body)
        folder = self.paths.writable(self.folder(source, at))
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"{to_et(at):%H%M%S}-{_clean(name)}"
        for n in range(1000):
            target = self.paths.writable(folder / (stem if n == 0 else _numbered(stem, n)))
            try:
                with open(target, "xb") as fh:  # never overwrite
                    fh.write(body)
                return digest
            except FileExistsError:
                try:
                    if sha256_bytes(target.read_bytes()) == digest:
                        return digest  # the same item stored already
                except OSError:
                    pass
        raise OSError(f"raw store: no free name for {stem}")  # pragma: no cover

    def latest(self, source: str, suffix: str) -> bytes | None:
        """The newest stored item of `source` whose name ends with `suffix` (None when none)."""
        base = self.paths.raw / _clean(source)
        try:
            days = sorted((d for d in base.iterdir() if d.is_dir()), reverse=True)
        except OSError:
            return None
        for day in days:
            try:
                names = sorted((p for p in day.iterdir() if p.name.endswith(suffix)), reverse=True)
            except OSError:
                continue
            for p in names:
                try:
                    return p.read_bytes()
                except OSError:
                    continue
        return None


def _clean(name: str) -> str:
    cleaned = _SAFE.sub("_", name).strip("._") or "item"
    return cleaned[:_MAX_NAME]


def _numbered(stem: str, n: int) -> str:
    root, ext = os.path.splitext(stem)
    return f"{root}-{n}{ext}"
