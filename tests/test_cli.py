"""The command line (DESIGN 2.1, 8)."""

from __future__ import annotations

from pathlib import Path

import pytest

from scout import cli


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--version"]) == 0
    assert "Scout 0.3.1" in capsys.readouterr().out


def test_a_missing_handler_module_exits_3(monkeypatch: pytest.MonkeyPatch,
                                          capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setitem(cli.DISPATCH, "verify", ("scout.no_such_module", "cli_verify"))
    assert cli.main(["verify"]) == 3
    assert "not built yet: scout.no_such_module" in capsys.readouterr().out


@pytest.mark.parametrize("command", sorted(cli.DISPATCH))
def test_every_handler_resolves(command: str) -> None:
    assert callable(cli._handler(command)), command


def test_usage_errors_exit_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 2
    assert cli.main(["levels"]) == 2
    assert cli.main(["no-such-command"]) == 2
    assert cli.main(["answers"]) == 2  # at least one symbol
    capsys.readouterr()


def test_bad_config_exits_2(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["--config", str(tmp_path / "missing.toml"), "run"]) == 2
    assert "config file not found" in capsys.readouterr().out
    bad = tmp_path / "config.toml"
    bad.write_text("bogus_key = 1\n", encoding="utf-8")
    assert cli.main(["--config", str(bad), "run"]) == 2
    assert "bogus_key" in capsys.readouterr().out


def parser_args(argv: list[str]):
    return cli.build_parser().parse_args(argv)


def test_every_command_is_dispatched() -> None:
    assert set(cli.DISPATCH) == {"run", "brief", "levels add", "levels score", "check", "verify", "answers",
                                 "discord check", "reads add"}
    assert parser_args(["reads", "add", "ABCD", "long", "4", "why"]).fields == ["ABCD", "long", "4", "why"]
    assert parser_args(["reads", "add"]).fields == []
    assert parser_args(["levels", "add", "--inbox"]).inbox is True
    assert parser_args(["discord", "check", "x.jsonl"]).file == "x.jsonl"
    parser = cli.build_parser()
    assert parser.parse_args(["answers", "AB", "CD", "--as-of", "2026-11-02T00:45:00Z"]).symbols == ["AB", "CD"]
    assert parser.parse_args(["levels", "add", "f.json", "--yes"]).yes is True
