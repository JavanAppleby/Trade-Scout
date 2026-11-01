"""The double-click .bat files (README): static checks, since tests never run cmd."""

from __future__ import annotations

from pathlib import Path

from scout.app import EXIT_ALREADY_RUNNING
from scout.cli import EXIT_BAD_INPUT, EXIT_FAILED, EXIT_NOT_BUILT, EXIT_OK

ROOT = Path(__file__).resolve().parent.parent


def _lines(name: str) -> list[str]:
    raw = (ROOT / name).read_bytes()
    assert b"\r\n" in raw and b"\n" not in raw.replace(b"\r\n", b""), f"{name} must use CRLF line ends"
    return [x.strip() for x in raw.decode("ascii").split("\r\n")]


def _depths(lines: list[str]) -> list[int]:
    """The block depth each line runs at (a line ending in "(" opens a block, ")" alone closes one)."""
    depth, out = 0, []
    for line in lines:
        if line.startswith(")"):
            depth -= 1
        out.append(depth)
        if line.endswith("("):
            depth += 1
    assert depth == 0
    return out


def test_update_runs_git_pull_inside_one_block_that_exits() -> None:
    """Regression (third review): `git pull` replaced the running .bat, and cmd went on reading the
    new text at the old byte offset (mid-line, a second pull, or the pip step skipped). Everything
    from `git pull` on must be one block, read whole before it runs, that ends in `exit /b`."""
    lines = _lines("Update Scout.bat")
    depths = _depths(lines)
    pull = next(i for i, x in enumerate(lines) if x.startswith("git pull"))
    assert depths[pull] == 1
    opened = max(i for i in range(pull) if depths[i] == 0 and lines[i] == "(")
    closed = next(i for i in range(pull, len(lines)) if depths[i] == 0 and lines[i].startswith(")"))
    assert all(depths[i] >= 1 for i in range(opened + 1, closed))
    assert lines[closed - 1].lower().startswith("exit /b")
    assert not [x for x in lines[closed + 1:] if x], "nothing may follow the block"
    body = lines[opened:closed]
    assert any("pip install" in x for x in body) and any("Updated." in x for x in body)


def test_start_scout_says_when_another_scout_is_running() -> None:
    """Regression (third review): a refused second start printed "Scout has stopped", as if the
    owner's running Scout had stopped."""
    assert EXIT_ALREADY_RUNNING not in (EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_NOT_BUILT)
    text = "\n".join(_lines("Start Scout.bat"))
    assert f'if "%RC%"=="{EXIT_ALREADY_RUNNING}" (' in text
    block = text[text.index(f'"{EXIT_ALREADY_RUNNING}" ('):]
    assert "already running" in block.split(")")[0]
    assert block.split(")")[0].strip().endswith(f"exit /b {EXIT_ALREADY_RUNNING}")
