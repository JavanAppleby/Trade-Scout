"""scout.raw (SCOUT_SPEC 6.1, 4.1 #4): items kept as received, written once, sha256; wire sources
without `terms_ok` keep title, link, guid and pubDate only."""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

from helpers_wire import wire_config

from scout.raw import RawStore, keeps_full_body, reduced_item

AT = datetime(2026, 11, 3, 2, 30, 5, tzinfo=timezone.utc)  # 21:30:05 ET on 2026-11-02
ITEM = (b"<item><title>Acme (NASDAQ: ACME) wins</title><link>https://x/1</link><guid>g1</guid>"
        b"<pubDate>Mon, 02 Nov 2026 13:30:00 +0000</pubDate><description>Full body text</description></item>")


def test_save_path_sha_and_once(paths, write_audit):
    store = RawStore(paths)
    with write_audit.watching(paths.root):
        digest = store.save("edgar_atom", "0001-1.xml", b"<entry/>", AT)
    assert digest == hashlib.sha256(b"<entry/>").hexdigest()
    target = paths.raw / "edgar_atom" / "2026-11-02" / "213005-0001-1.xml"
    assert target.read_bytes() == b"<entry/>"
    assert store.save("edgar_atom", "0001-1.xml", b"<entry/>", AT) == digest  # same item: no second file
    assert len(list(target.parent.iterdir())) == 1


def test_a_clash_is_never_overwritten(paths):
    store = RawStore(paths)
    store.save("prn", "a.xml", b"one", AT)
    store.save("prn", "a.xml", b"two", AT)
    folder = paths.raw / "prn" / "2026-11-02"
    assert sorted(p.read_bytes() for p in folder.iterdir()) == [b"one", b"two"]
    assert (folder / "213005-a-1.xml").read_bytes() == b"two"


def test_unsafe_names_are_cleaned(paths):
    RawStore(paths).save("../../evil", "..\\..\\x?.xml", b"x", AT)
    files = [p for p in paths.raw.rglob("*") if p.is_file()]
    assert len(files) == 1 and paths.raw in files[0].parents and ".." not in files[0].name


def test_reduced_item_keeps_four_fields_only():
    el = ET.fromstring(reduced_item("T", "https://l", "g", "Mon"))
    assert [c.tag for c in el] == ["title", "link", "guid", "pubDate"]
    assert [c.text for c in el] == ["T", "https://l", "g", "Mon"]


def test_store_with_config_reduces_wire_bodies(scout_dir, paths):
    cfg = wire_config(scout_dir)
    store = RawStore(paths, cfg)
    store.save("prnewswire", "i.xml", ITEM, AT)
    store.save("edgar_atom", "e.xml", ITEM, AT)
    prn = next((paths.raw / "prnewswire").rglob("*.xml")).read_bytes()
    edgar = next((paths.raw / "edgar_atom").rglob("*.xml")).read_bytes()
    assert b"Full body text" not in prn and b"<guid>g1</guid>" in prn and b"Acme" in prn
    assert edgar == ITEM
    assert store.save("prnewswire", "bad.xml", b"<not xml", AT)  # unparseable -> marker only
    assert not keeps_full_body(cfg, "prnewswire") and keeps_full_body(cfg, "nasdaq_halts")
    assert keeps_full_body(None, "prnewswire") and keeps_full_body(cfg, "unknown")


def test_terms_ok_keeps_full_body(scout_dir, paths):
    cfg = wire_config(scout_dir, sources={"prnewswire": {"terms_ok": True}})
    RawStore(paths, cfg).save("prnewswire", "i.xml", ITEM, AT)
    assert next((paths.raw / "prnewswire").rglob("*.xml")).read_bytes() == ITEM


def test_latest(paths):
    store = RawStore(paths)
    assert store.latest("edgar_tickers", "map.json") is None
    store.save("edgar_tickers", "map.json", b"old", datetime(2026, 10, 30, 12, tzinfo=timezone.utc))
    store.save("edgar_tickers", "map.json", b"new", AT)
    store.save("edgar_tickers", "other.txt", b"zzz", AT)
    assert store.latest("edgar_tickers", "map.json") == b"new"
