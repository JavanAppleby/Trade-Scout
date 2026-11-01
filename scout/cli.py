"""`python -m scout <command>` (DESIGN 2.1, 8). Every command and argument is declared here; each
handler's module is imported only when its command runs, so the CLI works before every unit exists.

Exit codes: 0 ok, 1 runtime failure, 2 bad input or config, 3 not built (or Scout is running).
"""

from __future__ import annotations

import argparse
import importlib
import sys
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from scout import __version__

EXIT_OK, EXIT_FAILED, EXIT_BAD_INPUT, EXIT_NOT_BUILT = 0, 1, 2, 3
SCOUT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = SCOUT_DIR / "config.toml"

# command -> (module, handler); every handler is `def cli_<x>(ctx, args) -> int`
DISPATCH: dict[str, tuple[str, str]] = {
    "run": ("scout.app", "cli_run"),
    "brief": ("scout.brief", "cli_brief"),
    "levels add": ("scout.levels_add", "cli_levels_add"),
    "levels score": ("scout.levels_score", "cli_levels_score"),
    "check": ("scout.verify_check", "cli_check"),
    "verify": ("scout.verify", "cli_verify"),
    "answers": ("scout.answers_cli", "cli_answers"),
}

__all__ = ["DEFAULT_CONFIG", "DISPATCH", "build_parser", "main"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m scout",
                                     description="Scout: market context for the owner's bots. It never trades.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG), help="Scout's config.toml (default: %(default)s)")
    parser.add_argument("--version", action="version", version=f"Scout {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    sub.add_parser("run", help="run Scout until Ctrl+C (or a STOP file)")

    p = sub.add_parser("brief", help="write the morning brief now")
    p.add_argument("--date", help="ET trading date YYYY-MM-DD (default: today)")

    levels = sub.add_parser("levels", help="Jason's level files")
    lsub = levels.add_subparsers(dest="levels_command", metavar="<add|score>")
    p = lsub.add_parser("add", help="validate and add one of Jason's level files")
    p.add_argument("file", nargs="?", help="the file (default: the newest file in inbox\\jason)")
    p.add_argument("--yes", action="store_true", help="add without asking")
    p = lsub.add_parser("score", help="score past level files against daily bars (report only)")
    p.add_argument("--bars", choices=("radar", "csv"), default="radar")
    p.add_argument("--csv-dir", help="folder of <SYMBOL>.csv daily bars (with --bars csv)")
    p.add_argument("--since", help="first session date YYYY-MM-DD")

    p = sub.add_parser("check", help="this week's verification sample (owner's hand checks)")
    p.add_argument("--week", help="ISO week YYYY-Www (default: this week)")

    p = sub.add_parser("verify", help="write the verification report")
    p.add_argument("--from", dest="date_from", help="first date YYYY-MM-DD")
    p.add_argument("--to", dest="date_to", help="last date YYYY-MM-DD")

    p = sub.add_parser("answers", help="print the reader's answers for symbols (writes nothing)")
    p.add_argument("symbols", nargs="+", metavar="SYMBOL")
    p.add_argument("--as-of", help="aware ISO time, e.g. 2026-11-02T00:45:00Z (default: now)")
    return parser


def _command(args: argparse.Namespace) -> str | None:
    if args.command == "levels":
        sub = getattr(args, "levels_command", None)
        return f"levels {sub}" if sub else None
    return args.command


def _handler(command: str) -> Callable[[Any, argparse.Namespace], int] | None:
    module_name, attr = DISPATCH[command]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name != module_name:
            raise
        return None
    return getattr(module, attr, None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:  # --help, --version, or a usage error
        return int(exc.code or 0)
    command = _command(args)
    if command is None:
        parser.print_help()
        return EXIT_BAD_INPUT
    handler = _handler(command)
    if handler is None:
        print(f"not built yet: {DISPATCH[command][0]}")
        return EXIT_NOT_BUILT
    from scout.app import build_context  # noqa: PLC0415
    from scout.clock import et_date  # noqa: PLC0415
    from scout.config import ConfigError  # noqa: PLC0415
    from scout.logs import close_logging, setup_logging  # noqa: PLC0415
    from scout.secrets import load_env  # noqa: PLC0415

    try:
        ctx = build_context(args.config)
    except ConfigError as exc:
        print(f"Scout: {exc}")
        return EXIT_BAD_INPUT
    load_env(ctx.paths)
    try:
        ctx.log = setup_logging(ctx.paths, ctx.cfg.log_level, et_date(ctx.clock.now()), ctx.cfg,
                                today=lambda: et_date(ctx.clock.now()))
        return int(handler(ctx, args))
    except KeyboardInterrupt:
        return EXIT_OK
    finally:
        close_logging()
