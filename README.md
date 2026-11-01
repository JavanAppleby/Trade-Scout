# Scout

Scout is the market-context bot of the owner's trading system. It reads public sources (SEC EDGAR,
press-release wires, Nasdaq trading halts, the nowflation.com macro calendar, the BLS and FOMC
schedules, the Cboe VIX close), records each fact with the time Scout first saw it, and answers
questions for the other bots. Every answer is `true`, `false` or `unknown`, with the evidence.
It also writes a morning brief for the owner before the US open.

Spec: `docs/SCOUT_SPEC.md`. Design: `docs/DESIGN.md`.

**What Scout is not.** It never trades and installs no broker code. It sends no Webull requests.
It never writes outside its own folder; it only reads Radar's and Pilot's files. The Claude API is
off unless the owner turns it on, and even then its labels are evidence only: they never change an
answer.

## Install (outside US market hours)

    git clone https://github.com/JavanAppleby/Trade-Scout.git E:\Scout
    cd /d E:\Scout
    py -3.13 -m venv .venv
    .venv\Scripts\python.exe -m pip install -e ".[dev]"
    copy config.example.toml config.toml

Then open `config.toml` and set:

- `contact_email`: your e-mail address, sent to the SEC and the other sources in Scout's
  User-Agent. **While it is empty Scout makes no web requests at all.**
- `radar_dir` (for example `'E:\Radar'`) and, once Pilot exists, `pilot_dir`. Scout only reads them.

Never commit `config.toml` or `.env`. A misspelt key in `config.toml` stops Scout with a message
naming the key.

To use the Claude API (optional, off by default): create a dedicated Anthropic workspace with a
spend limit, put `ANTHROPIC_API_KEY=...` in a file named `.env` in Scout's folder, and set
`llm_enabled = true`. Scout also stops itself at `llm_daily_cost_cap_usd` and
`llm_monthly_cost_cap_usd`.

## Launchers (double-click in Scout's folder)

- **Start Scout**: runs Scout until you press Ctrl+C in its window. If Scout is already running in
  another window, it says so and starts nothing; that Scout keeps running.
- **Scout Brief**: writes the morning brief now and opens it in Notepad. Safe while Scout runs.
- **Add Levels**: drop one of Jason's level files on it (or put the file in `inbox\jason` and
  double-click). Scout checks the file, shows any warnings and asks before adding it. It never
  changes a file it has already added.
- **Update Scout**: downloads and installs the latest version. Stop Scout first: while Scout is
  running, Update Scout says so and changes nothing.

## Commands

    python -m scout run                       # gather facts and answer, until Ctrl+C
    python -m scout brief [--date YYYY-MM-DD] # write the morning brief now
    python -m scout levels add <file> [--yes] # add one of Jason's level files
    python -m scout levels score              # score past level files (report only)
    python -m scout check [--week YYYY-Www]   # this week's hand-check sample
    python -m scout verify                    # the verification report
    python -m scout answers SYMBOL... [--as-of ISO-time]   # print answers (writes nothing)
    python -m scout --version

All of these are built in v1. (A command whose module is missing says "not built yet" and exits with code 3.)

## Folders

`state\` (facts, source health, answers, heartbeat and the other files the bots read), `logs\`,
`reports\` (the brief and the verification report), `inbox\jason\` (where you drop level files).
All are created on first run and ignored by git.

To stop Scout without its window, put a file named `STOP` in Scout's folder. In Explorer, New > Text
Document renamed to STOP is really `STOP.txt` when extensions are hidden; Scout accepts either name.
Or in PowerShell, from Scout's folder:

    New-Item STOP -ItemType File

Scout stops within a few seconds and deletes the file itself, so the next Start Scout runs normally.
A STOP file already in the folder when Scout starts is deleted (the log says so) and Scout runs.

## Tests

    .venv\Scripts\python.exe -m pytest -q

Tests never touch the network, real keys or the Anthropic API.

## CI

`.github/workflows/ci.yml` runs the tests on every push to `main` and every pull request (Ubuntu
with Python 3.11, 3.12 and 3.13, Windows with 3.13), plus a boundary check that no Webull or Trade
Guardian package is declared, installed or importable.

## scout_reader

`scout_reader\` is a small separate package (Python standard library only) that Radar and Pilot
install to read Scout's files. See `scout_reader\README.md`.
