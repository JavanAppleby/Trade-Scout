# Scout's inbox: format v1 (the contract for the capture tasks)

Status: owner decisions of 2026-11-01 (SCOUT_SPEC §18). This file is what the scheduled Claude
tasks' prompts cite. Paths below are relative to Scout's folder (`E:\Scout` for the install the
owner runs). All times are UTC ISO-8601 with `Z` unless a field says ET.

## 1. Who writes what

**Scheduled Claude tasks** (Claude Code desktop scheduled tasks) gather Jason's (Paper Gains)
material and the Paper Gains Discord news and scanner channels and write files into Scout's inbox. They use:
- Claude in Chrome, with the owner's logged-in Discord, for the Discord channels;
- WebFetch for YouTube transcripts (the Tuesday stream);
- an approved fetch of the weekly-plan newsletter PDF from `cdn.trendsetters.cloudflowventures.com`
  (that host and its `/outward-weekly/` path only), done by the helper Scout ships for the task,
  `tools\fetch_weekly_plan.py <href> <monday>`: it writes only `inbox\jason\raw\<monday>.weekly.txt`
  and ends with a last line "OK: ..." or "REFUSED: <reason>". The task runs it; the Scout program
  never does.

**Scout** reads those files as untrusted data. The Scout program itself never logs in to Discord,
YouTube or X and never fetches Jason's material (SCOUT_SPEC §2 rule 10). For the tasks to run,
**Chrome must be open and logged in to Discord, and the PC must be awake** (Claude Code desktop
running) at the scheduled times.

The task prompts are in `tasks/<task-id>/SKILL.md` (schedules, install and the retirement of
the old platform's tasks: `tasks/README.md`).

The tasks write only into `inbox\jason\` and `inbox\discord\`, and run only the three Scout
commands in §5 plus (daily task only) `tools\fetch_weekly_plan.py`. They never write anywhere else
under Scout's folder.

```
inbox\
  jason\<session_date>.<source_kind>.json          level files (§2); written as .json.tmp then renamed; added by `levels add`
  jason\<file>.rejected.txt                        written by Scout when it refuses a level file
  jason\raw\<monday>.weekly.txt                    weekly-plan PDF text (tools\fetch_weekly_plan.py); Scout never reads raw\
  jason\raw\<tuesday>.tuesday.txt                  Tuesday-stream transcript (the Tuesday task); Scout never reads raw\
  discord\<channel>\<YYYYMMDDTHHMMSSZ>.jsonl       capture files (§3); read by the running Scout
  discord\_cursor.json, discord\_cursor_jason.json the two Discord tasks' own cursors; Scout ignores them
```

## 2. Level files: `inbox\jason\<session_date>.<source_kind>.json`

The decided level format (SCOUT_SPEC §5.1; the loader is `scout/levels.py`). One file per read.

Top level:

| Key | Required | Meaning |
|---|---|---|
| `session_date` | yes | ISO date the read is for (daily: the card's date; weekly: the week's Monday; tuesday: the stream's date) |
| `source_kind` | yes for auto-add | `"daily"`, `"weekly"` or `"tuesday"` |
| `expires_default` | yes for daily and weekly | daily: the session date; weekly: the week's last trading day (`week_end`); tuesday: leave out (session_date + 7) |
| `setups` | yes | list of setups (below) |
| `source` | recommended | object or string: title, channel, message_id, permalink, posted_at, captured_at, PDF pages, video URL |
| `market` | optional | one line of market context |
| other keys | ignored | e.g. `caveats`; the loader ignores unknown top-level keys |

The file name must equal `<session_date>.<source_kind>.json` (auto-add refuses a mismatch).
**Write it in one step**: write `<name>.json.tmp`, then rename it to `<name>.json` (PowerShell
`Move-Item` without `-Force`). Scout ignores `*.tmp`, so `levels add --inbox` from another run never
reads half a file.

A setup: `symbol`, `direction` (`long | short | neutral | avoid`), `attribution` (`jason | amit |
unclear`; Jason's own posts are `jason`), optional `confidence`, `horizon`, `expires` (ISO date),
`conditionals` (`[{if, then}]`, prose), and `levels`: a list of `{kind, price, note}` with optional
`price_alternatives` (list) and `ambiguous` (bool). `kind` is one of `support, resistance, trigger,
target, invalidation, re-entry`. `price` is a string written exactly as printed (`"10.50"`,
`"$1,250"`, `"65-70"` for a zone). Never guess a digit: leave out an illegible level and say so in
the setup's `confidence` or the file's `source`. Keep each level's wording in `note` (the quote).

**Daily card** (#daily-watchlist image; SCOUT_SPEC §16 Q12 mapping), one setup per symbol,
`direction: "neutral"`, `expires` = the session date:

| Card field | Level `kind` |
|---|---|
| long trigger | `trigger` |
| long target 1, 2 | `target` (two levels) |
| short trigger | `invalidation` |
| short target 1, 2 | `support` (two levels) |

**Weekly plan** (the PDF's Technical Analysis pages): the same kinds, `direction` as printed,
`expires` = `week_end`; a stated trigger is `trigger`, targets `target`, invalidation
`invalidation`. Thesis text goes in `confidence` or a level's `note`.

**Tuesday stream** (YouTube transcript): as before (the 2026-09-15 reference read).

Examples: `tests/fixtures/scout_inbox/jason/2026-11-02.daily.json` and `2026-11-02.weekly.json`
(invented values).

## 3. Discord capture files: `inbox\discord\<channel>\<YYYYMMDDTHHMMSSZ>.jsonl`

One file per channel per task run, named by the capture time in UTC (`20261102T133000Z.jsonl`).
`<channel>` is the channel's name as Discord shows it, one of `discord_channels` (default:
`daily-watchlist`, `weekly-trade-plan`, `free-analysis`, `brianna-news`, `stockmktnewz`,
`major-headlines`, `earnings-whispers`, `market-movers`, `open-outcrier`, and the SCANNERS channels
`15m-scanner`, `daily-scanner`, `weekly-scanner`; never `market-edge-chat`).

**Write it atomically**: write `<name>.jsonl.tmp` in the same folder, then rename it to
`<name>.jsonl` (PowerShell: `Move-Item -LiteralPath <tmp> -Destination <final>`). Scout ignores
`*.tmp` and names starting with `.`. Never append to or rewrite a capture file after the rename;
write a new file next run. UTF-8, one JSON object per line, every line ending in `\n`.

**Write a file for every channel the run actually read, even when nothing is new** (an empty
file means "read, nothing new"): Scout uses capture times to show whether the capture runs on
schedule (§6). A channel the run did **not** read (skipped, header mismatch, no message id readable)
gets **no** file, not even an empty one, so an outage shows as `stale` instead of looking healthy.

### 3.1 One line = one Discord post (format 1)

| Field | Type | Rule |
|---|---|---|
| `format` | int | `1` |
| `channel` | str | the folder's channel name |
| `message_id` | str | the Discord message id (digits, a snowflake). It must agree with `posted_at` |
| `permalink` | str | `https://discord.com/channels/<server_id>/<channel_id>/<message_id>` |
| `posted_at` | str | the post's time from Discord's timestamp (`<time datetime=...>`), UTC `...Z`; within `discord_posted_at_tolerance_s` (120 s) of the time inside `message_id` |
| `captured_at` | str | when the task read it, UTC `...Z` |
| `author` | str | display name, at most 100 characters |
| `text` | str | the post text verbatim (embed title and description included); may be `""`. Scout cuts it to `discord_text_max_chars` (2000) |
| `image_text` | str or null | the task's transcription of an attached image (tables, cards); null when none. Cut to `discord_image_text_max_chars` (4000) |
| `kind` | str | `post`, `earnings_schedule`, `levels_card`, `recap` or `scanner_alert` (below) |
| `scanner` | object | required for `scanner_alert` and refused for every other kind (§3.3) |
| `earnings` | list | required (at least one entry) for `earnings_schedule`, allowed for `levels_card`, refused for the others, and allowed only in `discord_schedule_channels` (default `daily-watchlist`, `open-outcrier`, `market-movers`). A `levels_card` with neither an AM ER nor a PM ER line leaves the key out (`[]` is read as no key) |

No other keys are allowed (a line with an unknown key is refused). Every string must be valid
UTF-8 text: an escape for a lone surrogate (such as `\udcff`) is refused (`text_not_utf8`).

`kind`:
- `levels_card`: the #daily-watchlist "Daily Watchlist: MM/DD" card. Put the card's "AM ER" and
  "PM ER" lists in `earnings` (`pre` and `post` on the card's date).
- `earnings_schedule`: a structured earnings list: #open-outcrier "Top Earnings Mon 11/2 Pre: $A $B"
  (one entry; "Aft" is `post`; "None" is an entry with `tickers: []`), or #market-movers' weekly
  "Earnings Reports Calendar" when the table is legible (one entry per day and session).
- `recap`: a recap card (PT hits, weekly recap tables).
- `scanner_alert`: a "PaperGains Scanner" card in #15m-scanner, #daily-scanner or #weekly-scanner
  (3.3). Allowed only in those three folders.
- `post`: everything else (headlines, chart commentary, reactions, economic calendar; a non-card post
  in a scanner channel).

`earnings` entry: `{"date": "YYYY-MM-DD", "session": "pre" | "post" | "unknown", "tickers": ["ACME", ...]}`.
`date` is the ET calendar date, with the year resolved by the task; it must lie between the post's
ET date less one day and `discord_schedule_max_ahead_days` (14) after it. `session`: "Pre", "BMO",
"AM ER" -> `pre`; "Aft", "AMC", "PM ER" -> `post`; anything else `unknown`. Tickers are plain
symbols (a leading `$` is removed; `BRK.B` becomes `BRK-B`). At most 40 entries, 400 tickers each.

Example (invented):

```json
{"format": 1, "channel": "open-outcrier", "message_id": "1566804069580804242", "permalink": "https://discord.com/channels/111111111111111111/222222222222222202/1566804069580804242", "posted_at": "2026-11-02T13:20:00Z", "captured_at": "2026-11-02T13:29:50Z", "author": "Open Outcrier", "text": "Top Earnings Tue 11/3 Pre: $DELT $EPSI", "image_text": null, "kind": "earnings_schedule", "earnings": [{"date": "2026-11-03", "session": "pre", "tickers": ["DELT", "EPSI"]}]}
```

More in `tests/fixtures/scout_inbox/discord/`.

### 3.2 What Scout does with a line

Each line is checked on its own; a bad line is refused with a reason
(`state\discord\rejects\<ET date>.jsonl` and the file's health record) and the rest of the file is
kept. Reasons are fixed strings and never carry capture text: an unknown key is named only when it
is a plain key of at most 20 characters (`unknown_key:extra`; otherwise `?`; at most three, then
`+N`). A file that raises an unexpected error while it is read is booked as refused
(`ingest_error:<type>`) and the poll goes on to the next file. A last line with no newline is read only if it is a complete JSON object. Posts older than
`discord_max_post_age_days` (14) or in the future are refused. A post already stored (same channel
and message id) is skipped, so re-capturing overlapping posts is harmless.

Each new post becomes facts in `state\facts\` with `first_seen_at` = the time Scout read it (the
as-of rule):
- kind `discord`, one per ticker the text names (`$TICKER` cashtags, "(NASDAQ: XXX)" tags, and in
  `discord_prefix_ticker_channels`, default `brianna-news`, a leading "TICKER - headline"); one fact
  with no ticker when none. Source `discord:<channel>`, class from Scout's deterministic title
  classifier, url = permalink, published_at = posted_at. **Record and brief only**: no gate reads it
  (gate 6 reads only wire and filing facts).
- kind `earnings_schedule`, one per (date, session, ticker) of `earnings`. **Gate 5 may use it only
  to answer `true`** (an earnings event inside the window from the hold check to the next open
  blocks a hold). Its absence never makes gate 5 `false` and never clears an `unknown`
  (`discord_gate5_enabled`, default true).
- kind `scanner`, one per ticker of a `scanner_alert` card **instead of** `discord` facts. Source
  `discord:<channel>`, class `scanner_alert`, `extra` with `timeframe`, `pattern`, `state`, the card's
  tickers and text; url = permalink, published_at = posted_at. **Record and brief only**: neither gate
  5 nor gate 6 reads it. The brief lists the cards since the prior close by timeframe and pattern;
  `scout_reader`'s `scanner_alerts(S, as_of)` describes them for Radar's later tags.

Scout never edits, moves or deletes an inbox file. What it has read is in
`state\discord\files.jsonl`; a raw copy of each file is kept under `state\raw\discord\`. The owner
may delete old capture files; Scout skips files whose name time is older than 15 days anyway.

### 3.3 Scanner cards (`scanner_alert`, owner 2026-11-01)

The SCANNERS category's bot ("PaperGains Scanner") posts text-only embed cards: a title
"<Pattern> - <Timeframe> (<state>)" (the parenthesised state may be missing), then "Tickers", one
ticker per line, then "Disclaimer". Examples as observed 2026-11-01: "Outside Bar - 15m (In Force)",
"Inside Bar - 15m", "Failed 2D - 15m", "Outside Bar - Daily (Current Candle)",
"Outside Bar - Weekly (Current Candle)". One card = one line with `kind: "scanner_alert"`, `text` the
card verbatim (title, "Tickers", the tickers, "Disclaimer" and its text), and:

    "scanner": {"timeframe": "15m" | "daily" | "weekly", "pattern": "<as printed>",
                "state": "<as printed in the parentheses>" | null, "tickers": ["GOOGL", ...]}

| Field | Rule |
|---|---|
| `timeframe` | must agree with the folder: `15m-scanner` -> `"15m"`, `daily-scanner` -> `"daily"`, `weekly-scanner` -> `"weekly"` (else `scanner:timeframe_not_channel`) |
| `pattern` | the title's text before " - <Timeframe>", as printed (e.g. `"Outside Bar"`, `"Failed 2D"`); 1-80 characters |
| `state` | the text inside the title's parentheses, as printed (`"In Force"`, `"Current Candle"`), or `null` when the title has none; 1-40 characters |
| `tickers` | the card's tickers in printed order, 1-200, plain symbols validated like every other ticker (a leading `$` is removed; `BRK.B` becomes `BRK-B`) |

No other keys inside `scanner`. `earnings` is refused on a `scanner_alert`. A post in a scanner
channel that is not a card (an announcement) is `post`. Example (invented):

```json
{"format": 1, "channel": "15m-scanner", "message_id": "1566810361036800009", "permalink": "https://discord.com/channels/111111111111111111/222222222222222301/1566810361036800009", "posted_at": "2026-11-02T13:45:00Z", "captured_at": "2026-11-02T13:49:30Z", "author": "PaperGains Scanner", "text": "Outside Bar - 15m (In Force)
Tickers
ACME
KAPA
LAMB
Disclaimer
Invented test card: not advice.", "image_text": null, "kind": "scanner_alert", "scanner": {"timeframe": "15m", "pattern": "Outside Bar", "state": "In Force", "tickers": ["ACME", "KAPA", "LAMB"]}}
```

More in `tests/fixtures/scout_inbox/scanners/`.

## 4. Session placement for gate 5

On the listed ET date D: `pre` = from midnight to D's 09:30 open; `post` = from D's close (16:00,
13:00 on an early close) to 20:00; `unknown` = all of D (on a non-trading day every session is all
of D). The entry blocks when that interval overlaps [hold check, next open). Example: at the 19:45
check on Monday, "PM ER" Monday and "Pre" Tuesday block; "AM ER" Monday and "Aft" Tuesday do not.

## 5. Commands the tasks run (from Scout's folder)

    .venv\Scripts\python.exe -m scout discord check inbox\discord\<channel>\<name>.jsonl.tmp
    .venv\Scripts\python.exe -m scout levels add --yes inbox\jason\<session_date>.<source_kind>.json
    .venv\Scripts\python.exe -m scout levels add --inbox

- `discord check <file>` checks a capture file exactly as Scout will read it and writes nothing:
  exit 0 and "OK: N post(s) accepted", or exit 2 with one "line N: <reason>" per refused line and a
  last line "REFUSED: ...". Run it on the `.tmp` file before the rename and fix what it names.
- `levels add --yes <file>` adds one level file without asking. It refuses (exit 2, last line
  "REFUSED: <file>: <reason>", and `<file>.rejected.txt` next to it): a file the loader cannot read;
  a missing or unknown `source_kind`; a file with no setup left after validation; a name like
  `<date>.<kind>.json` that disagrees with the file's own `session_date` and `source_kind`; a
  different file already added for that date and kind (Scout never replaces an added file: a
  correction needs the owner). The same file again exits 0 ("Already added"). The note's sha256 is
  that of the bytes that were checked (the file is read once), so a file refused while half written
  is tried again once complete. A file that could not be read at all (missing, or locked for a
  moment by a scanner) is refused **without** a note, so the next run or `--inbox` retries it.
- `levels add --inbox` adds every `*.json` in `inbox\jason` not already added (by sha256) and not
  already refused in the same form, refusing any name that is not `<session_date>.<source_kind>.json`.
  It prints one line per file and "Inbox: A added, R refused, ..."; exit 2 when it refused one.
  Idempotent: safe to run after every write.

All three are safe while Scout runs (no lock is taken; Scout's own files in `state\levels`
appear in one step; the tasks' level and capture files appear in one step because the tasks write
`*.tmp` and rename, and Scout ignores `*.tmp`).

The daily task also runs `.venv\Scripts\python.exe tools\fetch_weekly_plan.py "<href>" <monday>`
(§1): exit 0 with a last line "OK: ..." or exit 2 with a last line "REFUSED: <reason>" for every
failure (refused URL, network or HTTP error, not a PDF, too large, failed write); never a traceback.

## 6. Health

`discord_inbox` on Scout's heartbeat: `ok` while the capture-hours time (`discord_capture_hours_et`,
default 04:00-20:00 ET, trading days) since the newest capture file of a `discord_cadence_channels` channel (default: the six MARKET NEWS channels the news task reads every run) is at most
`discord_expected_every_min` (40); else `stale` with the reason. An empty file counts as a
capture (read, nothing new); a file with lines of which **none** was accepted does not (it stored
nothing; its refusals are in the brief). It is shown to the owner (brief and heartbeat) and **never
read by any gate**. The brief's "Paper Gains Discord" section lists each
channel's newest capture (the three scanner channels included) and today's refused lines by reason.
`scout_reader.scanner_alerts` answers `unknown` instead of `false` while `discord_inbox` is not `ok`
(a missing capture never reads as "not on a scanner"); that is a description, not a gate.
