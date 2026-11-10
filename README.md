# Scout

Scout is the market-context bot of the owner's trading system. It reads public sources (SEC EDGAR,
press-release wires, Nasdaq trading halts, the nowflation.com macro calendar, the BLS and FOMC
schedules, the Cboe VIX close, the BLS jobs and CPI results), records each fact with the time Scout first saw it, and answers
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

Then create `config.toml` holding **only the keys you change**, for example:

    contact_email = "you@example.com"
    radar_dir = 'E:\Radar'

- `contact_email`: your e-mail address, sent to the SEC and the other sources in Scout's
  User-Agent. **While it is empty Scout makes no web requests at all.**
- `radar_dir` and, once Pilot exists, `pilot_dir`. Scout only reads them.

Every other key keeps the default in `config.example.toml`, which documents them all. Do not copy
the whole example: a copied key pins today's value, so later updates to that default (keyword lists,
source addresses) would never reach you.

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
  changes a file it has already added. (The scheduled Claude tasks add files themselves; see below.)
- **Add Read**: note your own read of a stock for the thesis-read paper test (asks the ticker, long
  or short, strength 1 to 5, and why). Nothing trades on it. See "Thesis reads" below.
- **Update Scout**: downloads and installs the latest version. Stop Scout first: while Scout is
  running, Update Scout says so and changes nothing.

## Commands

    python -m scout run                       # gather facts and answer, until Ctrl+C
    python -m scout brief [--date YYYY-MM-DD] # write the morning brief now
    python -m scout levels add <file> [--yes] # add one of Jason's level files
    python -m scout levels add --inbox        # add every new valid file in inbox\jason (no question)
    python -m scout levels score              # score past level files (report only)
    python -m scout discord check <file>      # check one Discord capture file (writes nothing)
    python -m scout reads add [TICKER long|short STRENGTH [WHY]]   # note your own read (asks when given nothing)
    python -m scout check [--week YYYY-Www]   # this week's hand-check sample
    python -m scout verify                    # the verification report
    python -m scout answers SYMBOL... [--as-of ISO-time]   # print answers (writes nothing)
    python -m scout --version

All of these are built in v1. (A command whose module is missing says "not built yet" and exits with code 3.)

## Folders

`state\` (facts, source health, answers, heartbeat and the other files the bots read), `logs\`,
`reports\` (the brief and the verification report), `inbox\jason\` (level files),
`inbox\discord\` (Discord capture files), and, once thesis reads are on, `state\reads\` (the reads)
and `inbox\reads\` (your read notes). All are created on first run and ignored by git.

## How often Scout polls (the polling pace, 0.2.4)

Scout slows each news and filing feed (each SEC form, PR Newswire, Stock Titan) to how often it
really has something new. For every hour of a trading day, and once for weekends and holidays, it
works out from the last 7 days how long that feed typically goes between new items, and polls about
twice in that time. It works this out again every day at 03:30 ET, so the timings follow the feeds.
It never polls faster than before, never so slowly that a burst could push items off a feed's page,
and in trading hours never waits longer than 2 minutes for a wire or 5 minutes for an SEC filing that
could block a Radar buy (30 minutes at night and at weekends). The halt feed is never slowed. The
morning brief's "Polling pace" section shows each feed's timings. The settings are the `[pace]`
table in `config.example.toml`; `enabled = false` there puts every feed back on its fixed timing.

After 20:00 ET on a trading day Scout reads Nasdaq's list of that day's halts once (0.3.1). It only
fills in halts that ended, and resumptions, that Scout missed during the day; it never adds a halt
that is still open, so it can never stop a Radar buy. If that read fails, Scout tries again every 15
minutes (`halts_dated_retry_s`), not every minute.

## Thesis reads (a paper test; off until you turn it on, 0.3.0)

You asked (10 Nov 2026) for a live paper test of reading everything together: for each stock Radar
finds, Radar packs up the evidence (chart bars, the quote and its sizes, the day so far, the market),
and Claude readers write a "read" before anything happens: direction, strength 1 to 5, reasons, the
price that would prove it wrong. A strong read is looked at again every few minutes until it fades.
A scorer later checks whether strong reads beat weak ones. **No bot acts on a read**, and no Scout
answer, gate or block reads them. The rules are in `E:\Market_Data\research\reads\READS_TEST.md`.

To turn it on, add one line to `config.toml`: `reads_enabled = true` (reads also need
`llm_enabled = true` and the API key). Your choice of 11 Nov 2026 is the default: reader R1 (Claude
Haiku 5.5, medium effort) reads every find and looks again while its read is strong; reader R2
(Claude Opus 5.5, low effort) reads a random 15% of finds, once. Each reader works on its own, with
up to 3 calls at once (`reads_workers_per_reader`), so the slower R2 never holds up R1 and a burst of
finds at the open is read before it goes stale. Reads have their own budget, with their own records
in `state\reads\ledger\`: they stop for the day at $1.50 and for the month at $20 (R2 at $0.75 a day
and $12 a month, so it can never use up R1's), apart from the other Claude jobs' $1.00 a day, and
neither budget ever stops the other. The morning brief's "Thesis reads" section counts the reads and
shows their spend. If Scout stops, packs it had not finished are picked up at the next start (they
get a "stale" line when too old to read).

Use **Add Read** to note your own reads. The note waits in `inbox\reads\` until Scout runs with reads
on; the command tells you when it is waiting. With `reads_enabled = false` the running Scout does
nothing for reads at all.

## Jason's material and the Paper Gains Discord (scheduled Claude tasks)

Scout itself never logs in to Discord, YouTube or X and never downloads Jason's material. Scheduled
Claude tasks in Claude Code desktop do that and leave files in Scout's inbox; Scout reads them as
untrusted data. The file format and the commands the tasks run are in `docs/INBOX.md`.

- **What the tasks need:** the PC awake and Claude Code desktop running at the scheduled times, and
  Chrome open with the Claude in Chrome extension and **logged in to Discord** (and to nothing the
  tasks should not use). The weekly plan's PDF is fetched only from
  `cdn.trendsetters.cloudflowventures.com`.
- **Level files** (the daily card, the weekly plan, the Tuesday stream) go to
  `inbox\jason\<session_date>.<source_kind>.json` and are added at once with
  `python -m scout levels add --yes <file>` or `levels add --inbox`. A refused file stays in the
  inbox with a `<file>.rejected.txt` note beside it saying why; Scout never replaces a file it has
  already added, so a correction for the same day needs you.
- **Discord captures** (the channels under MARKET NEWS, MAIN STAGE and SCANNERS, not market-edge-chat) go to
  `inbox\discord\<channel>\<time>.jsonl`. The running Scout reads new files every minute and stores
  each post as a fact. They appear in the brief's "Paper Gains Discord" section. Only an earnings
  list (open-outcrier's "Top Earnings", the card's AM/PM ER, market-movers' calendar) reaches an
  answer, and only to block a hold. Nothing from Discord can allow a hold. Scanner cards (#15m-scanner,
  #daily-scanner, #weekly-scanner) are listed in the brief's "Paper Gains scanners" lines and reach no answer.
- **Is it working?** The brief and the heartbeat show `discord_inbox`: `ok` while a capture arrives at
  least every `discord_expected_every_min` (40) minutes between `discord_capture_hours_et` (04:00 and
  20:00 ET) on trading days, else `stale`, with each channel's last capture and today's refused lines.

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
