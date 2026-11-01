"""Atomic writes, append-only JSONL and the partial-line-tolerant reader (SCOUT_SPEC 10)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scout import store
from scout.paths import OwnFolderError, ScoutPaths
from scout_reader.records import read_jsonl


def test_atomic_write_json_and_no_temp_left(paths: ScoutPaths) -> None:
    target = paths.state / "x.json"
    store.atomic_write_json(paths, target, {"b": 1, "a": "é"})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": "é", "b": 1}
    store.atomic_write_json(paths, target, {"a": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"a": 2}
    assert [p.name for p in target.parent.iterdir() if p.name.endswith(".tmp")] == []


def test_atomic_write_retries_a_blocked_replace(paths: ScoutPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    real = os.replace
    calls = {"n": 0}

    def flaky(src, dst):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("a reader holds it")
        return real(src, dst)

    monkeypatch.setattr(store.os, "replace", flaky)
    pauses: list[float] = []
    store.atomic_write_text(paths, paths.state / "y.txt", "hello", pause=pauses.append)
    assert (paths.state / "y.txt").read_text(encoding="utf-8") == "hello"
    assert calls["n"] == 3 and pauses == [store.REPLACE_PAUSE_S] * 2


def test_atomic_write_gives_up_and_cleans_the_temp(paths: ScoutPaths, monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(src, dst):  # noqa: ANN001
        raise PermissionError("held")

    monkeypatch.setattr(store.os, "replace", blocked)
    with pytest.raises(PermissionError):
        store.atomic_write_text(paths, paths.state / "z.txt", "x", pause=lambda s: None)
    assert list(paths.state.glob(".z.txt.*")) == [] and not (paths.state / "z.txt").exists()


def test_append_jsonl_requires_format_and_appends_lines(paths: ScoutPaths) -> None:
    f = paths.facts / "2026-11-02.jsonl"
    with pytest.raises(ValueError, match="format"):
        store.append_jsonl(paths, f, {"a": 1})
    with pytest.raises(ValueError, match="format"):
        store.append_jsonl(paths, f, {"format": True})
    store.append_jsonl(paths, f, {"format": 1, "title": "line\nbreak"})
    store.append_jsonl(paths, f, {"format": 1, "n": 2})
    raw = f.read_bytes()
    assert raw.count(b"\n") == 2 and raw.endswith(b"\n")
    assert [r.get("n") for r in store.read_jsonl(f)] == [None, 2]


def test_reader_ignores_partial_last_line_and_garbage(paths: ScoutPaths) -> None:
    f = paths.facts / "2026-11-02.jsonl"
    f.write_bytes(b'{"format":1,"n":1}\nnot json\n[1,2]\n\xff\xfe\n{"format":1,"n":2}\n{"format":1,"n":3')
    assert [r["n"] for r in read_jsonl(f)] == [1, 2]
    assert read_jsonl(paths.facts / "missing.jsonl") == []


def test_append_after_a_crash_starts_a_new_line(paths: ScoutPaths) -> None:
    f = paths.facts / "2026-11-02.jsonl"
    f.write_bytes(b'{"format":1,"n":1}\n{"format":1,"n":')  # a crashed half line
    store.append_jsonl(paths, f, {"format": 1, "n": 3})
    assert [r["n"] for r in read_jsonl(f)] == [1, 3]


def test_writes_outside_scout_are_refused(paths: ScoutPaths, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere.json"
    with pytest.raises(OwnFolderError):
        store.atomic_write_json(paths, outside, {})
    with pytest.raises(OwnFolderError):
        store.append_jsonl(paths, paths.root / ".." / "x.jsonl", {"format": 1})
    assert not outside.exists()


def test_hash_helpers() -> None:
    assert store.sha256_text("a") == store.sha256_bytes(b"a")
    assert store.canonical_json({"b": 1, "a": [1, 2]}) == '{"a":[1,2],"b":1}'
