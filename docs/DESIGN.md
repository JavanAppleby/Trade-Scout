# Scout v1: design

Spec: `docs/SCOUT_SPEC.md` (APPROVED 2026-11-01; every §16 answer is its bold default). The spec
is the contract; this file fixes how the code meets it: packages, modules, public signatures, file
records, config keys, the main loop, time, HTTP, and which test proves which spec rule. "SS x" below
means SCOUT_SPEC section x. Build steps 1-5 of SS 15 are in scope; steps 6-11 and phases 2-4 are not.

Plain rules for every module:
- Scout never trades. No module imports `webull*`, `trade_guardian`, `radar` or `pilot`.
- Every write goes through `ScoutPaths.writable()` (section 4). Nothing writes outside Scout's folder.
- Every stored time is UTC ISO-8601 with `Z` (`scout.clock.iso`). ET and Brisbane appear in reports only.
- All tunables come from `ScoutConfig` (section 6). No per-ticker code anywhere.
- Modules stay under ~600 lines. Tests never touch the network (fixtures, fakes, `ManualClock`).
- No secret, token, API key or `contact_email` is ever logged, printed or written to a file.

## 0. Decisions this design makes (where the spec leaves a choice)

| # | Decision | Why |
|---|---|---|
| D1 | The calendar copy lives in `scout_reader/scout_reader/calendar.py` (with SS 3's provenance header); `scout/calendar.py` re-exports it and adds the local `Session` enum and session helpers. | `scout_reader` must compute gate windows (prior close, next open) and must stay stdlib-only, so it needs the calendar too. One copy, not two. |
| D2 | ET conversion uses a stdlib US-Eastern DST rule (`scout_reader/_et.py`: second Sunday of March 02:00 to first Sunday of November 02:00, UTC-4 / UTC-5), not `zoneinfo`. | `zoneinfo` needs the `tzdata` package on Windows, which is not stdlib. A test compares `_et` with `zoneinfo` for every hour of 2026-2027. |
| D3 | The gate logic lives in `scout_reader` (pure functions over records). Scout's own answers job calls the same functions on its own folder. | One implementation; Radar, Pilot and Scout get identical answers; replay is a pure read. |
| D4 | Scout writes four extra files beyond SS 10.2 (additive, allowed by SS 10): `beats\<date>.jsonl` (one line per heartbeat), `params.jsonl` (the reader-relevant config, appended when it changes), `edgar\due\<date>.jsonl` (EDGAR due-window records) and `jason\snapshots.jsonl` (the merged current Jason setups). | The reader must answer "as of t" for any past t. The single `scout.heartbeat` only says "now"; config values and Jason's parsed files must be visible to a stdlib reader with their own `first_seen_at`. |
| D5 | `fact_id` = sha256 of `"{source}|{guid}|{ticker}"` (ticker empty when none). | SS 6.3: one release naming several tickers gives one fact per ticker, so source and guid alone collide. |
| D6 | EDGAR "getcurrent" is polled once per form type (`type=<form>&count=100`), each form a separate feed for overlap. | Market-wide getcurrent with Form 4 traffic can pass 100 items within a poll; per-form feeds keep the overlap rule meaningful. Whether `type=424B` matches all 424B* forms is UNVERIFIED; unit `wire` confirms it from a fixture. |
| D7 | A poll interval is a gap when it is longer than `max(source_max_gap_s, 2 x S)`, S the larger `spacing_s` of the two good polls (owner, 2026-11-04: the 04:00 ET switch), plus each failed poll's own `spacing_s` between them (1 to 3, the later poll overlapping). Owner, 2026-11-04 (0.2.2): when the later poll overlaps by a seen id (or is a 304), the interval is covered, failed polls included, up to `source_overlap_max_gap_s` (1800 s); see section 20. Only the feeds in params `gate6_feeds` count (not EDGAR's 10-Q and 10-K). Each source has `min_spacing_s` (04:00-20:00 ET) and `idle_spacing_s` (outside). | SS 6.2 sets 180 s, but SS 4.1 polls EDGAR every 10 min overnight. Without this, gate 6 is `unknown` every Monday. Overlap still has to hold on every poll. |
| D8 | Scout has no price feed in v1. J1's "tickers under `scan_max_price`" uses the price where one is known (a Radar find); otherwise every wire or filing fact with an exchange-listed ticker is eligible, bounded by `j1_max_calls_per_day`. | SS 8.2 assumes a price Scout does not have. Flagged for the owner in the brief's LLM section. |
| D9 | LLM calls send no `fallbacks` parameter; a refusal is stored as `unlabelled`. | SS 8.2: "Sonnet and Opus calls handle refusals and fallbacks as unlabelled." |
| D10 | `jason_tags(S, price, as_of, *, frame_prices=None, near_pct=None)`: the market frame lists Jason's SPY/QQQ setups; it says above/below only for symbols whose price the caller passes. | Scout has no SPY/QQQ price; Radar does. The extra arguments are keyword-only and optional, so SS 10.3's signature holds. |
| D11 | Fixtures that the spec says are "saved from live reads" start as **synthetic** files written from the shapes in SS 4.1, 6.3 and 7.3, each with a `_SYNTHETIC` marker (a comment line or a JSON key). The lead replaces them with live captures at build step 2 (owner-run, outside tests), except nowflation's: its terms forbid redistributing its raw files, so its fixtures stay synthetic in its live layout. | Units must not touch the network. |

## 1. Repository layout

```
E:\Scout_dev\
  pyproject.toml            one install: packages "scout" and "scout_reader"
  config.example.toml       every key, documented (section 6)
  CLAUDE.md  README.md  .gitignore
  .github\workflows\ci.yml
  Start Scout.bat  Scout Brief.bat  Add Levels.bat  Update Scout.bat
  docs\SCOUT_SPEC.md  docs\DESIGN.md  docs\TRACEABILITY.md  docs\INBOX.md (the inbox contract)
  tools\make_calendar_fixture.py   run once against Guardian's checkout (read-only)
  scout\                    the bot (flat package, no subpackages)
  scout_reader\             separate stdlib-only distribution
    pyproject.toml          name "scout-reader"; Radar/Pilot: pip install "git+...@<tag>#subdirectory=scout_reader"
    README.md
    scout_reader\           the importable package
  tests\                    all tests (scout and scout_reader); fixtures under tests\fixtures\
  state\ logs\ reports\ inbox\jason\ inbox\discord\   runtime, git-ignored, created on first run
```

`pyproject.toml` (top level): `name = "scout"`, `version = "0.2.0"`, `requires-python = ">=3.11,<3.14"`,
`dependencies = ["anthropic==2.3.0", "truststore==0.10.5"]` (`anthropic` imported lazily, only by
`scout.llm_client`; an exact pin, because the request shape uses `output_config.effort/format` and
the `web_*_20260209` tools, which the 1.x SDK lacks; `tests/test_integration.py` checks the shape
against the installed SDK offline; `truststore` only by `scout.http`, see 2.2), dev extra
`["pytest", "hypothesis", "tzdata"]` (`tzdata` only for the `_et` agreement test).
`[tool.setuptools] packages = ["scout", "scout_reader"]`,
`package-dir = {"scout" = "scout", "scout_reader" = "scout_reader/scout_reader"}`.
`[tool.pytest.ini_options] pythonpath = [".", "scout_reader", "tests"]`, `testpaths = ["tests"]`.
Nothing named `webull*` or `trade-guardian` may ever appear (test `test_boundaries.py`).

`scout_reader/pyproject.toml`: `name = "scout-reader"`, `version = "0.2.0"`, `dependencies = []`,
`requires-python = ">=3.11"`, `packages = ["scout_reader"]`.

## 2. Package `scout`: modules and public interfaces

Each entry: purpose, then the public names other modules may use. Anything not listed is private to
its module. The owning unit is in brackets (section 12).

### 2.1 Core (unit `scaffold`)

**`scout/__init__.py`**: `__version__ = "0.2.0"`; `FORMAT = 1` (the record format all files carry).

**`scout/__main__.py`**: `raise SystemExit(cli.main())`.

**`scout/cli.py`**: argparse, every command and argument declared here (section 8); handlers import
their implementing module lazily so the CLI loads before every unit exists.
- `main(argv: Sequence[str] | None = None) -> int`
- `build_parser() -> argparse.ArgumentParser`
- Dispatch table (fixed names; each `def cli_<x>(ctx: AppContext, args: argparse.Namespace) -> int`):
  `run` -> `scout.app.cli_run`; `brief` -> `scout.brief.cli_brief`; `levels add` ->
  `scout.levels_add.cli_levels_add`; `levels score` -> `scout.levels_score.cli_levels_score`;
  `check` -> `scout.verify_check.cli_check`; `verify` -> `scout.verify.cli_verify`; `answers` ->
  `scout.answers_cli.cli_answers`; `discord check` -> `scout.discord_ingest.cli_discord_check`
  (2026-11-01, section 2.7). A missing module prints "not built yet: <module>" and returns 3.
- Exit codes: 0 ok, 1 runtime failure, 2 bad input or config, 3 not built.

**`scout/config.py`** and **`scout/config_load.py`**: strict TOML loader in Radar's style (an unknown
key, a wrong type, or a table where a value belongs is a `ConfigError` naming the key; the contact
email is never echoed; `repr` hides it).
- `class ConfigError(ValueError)`
- `@dataclass(frozen=True) class SourceConfig`: `name, enabled, kind, url, paths, min_spacing_s,
  idle_spacing_s, allowed_hosts, terms_note, max_items, gate6` (section 6.3)
- `@dataclass(frozen=True) class LlmJobConfig`: `enabled, model, effort, max_tokens`
- `@dataclass(frozen=True) class LlmPrice`: `input_per_mtok, output_per_mtok, cache_write_per_mtok,
  cache_read_per_mtok`
- `@dataclass(frozen=True) class ScoutConfig`: every key of section 6, plus `scout_dir: Path`;
  derived: `source(name) -> SourceConfig`, `enabled_sources(kind: str | None = None) ->
  tuple[SourceConfig, ...]`, `llm_job(job: str) -> LlmJobConfig`, `price(model: str) -> LlmPrice |
  None`, `user_agent() -> str` (empty when `contact_email` is empty), `http_allowed() -> bool`,
  `reader_params() -> dict` (section 5.10).
- `load_config(path: Path | str) -> ScoutConfig`; `config_from_dict(data: Mapping, scout_dir: Path)
  -> ScoutConfig`; `CONFIG_KEYS: frozenset[str]`.
- Validation highlights: `scout_webull_enabled` must be false in v1; `class_keywords` keys must be
  exactly the SS 6.4 classes (`bankruptcy`, added 2026-11-04, may be left out and keeps its default
  list, so an install table written before then still loads); `radar_handoff_subdir` relative,
  without a drive or ".."; `hold_catalyst_classes` only from known classes; times "HH:MM";
  `min_spacing_s` for `nasdaq_halts` >= 60 and for wires >= 30; `levels_dir`, `inbox_dir` inside
  Scout's folder.

**`scout/clock.py`**: the clock abstraction (section 9).
- `class Clock(Protocol): def now(self) -> datetime` (aware UTC)
- `class SystemClock`; `class ManualClock(start)` with `now, set, advance(seconds), sleep(seconds),
  monotonic()` (Guardian's semantics: a clock never runs backwards)
- `iso(dt) -> str` ("...Z", seconds precision unless sub-second given); `parse_iso(value) ->
  datetime | None`; `to_et(dt) -> datetime` (aware, fixed-offset tz from `_et`); `et_date(dt) -> date`;
  `et_datetime(day, t) -> datetime` (aware UTC); `to_brisbane(dt) -> datetime` (UTC+10, no DST);
  `parse_hhmm(text) -> time`; `fmt_et_bne(dt) -> str` (report format "HH:MM ET / HH:MM AEST").

**`scout/calendar.py`**: re-exports `scout_reader.calendar` and adds
`class Session(StrEnum): PRE, CORE, POST, NIGHT, CLOSED` (Guardian's names), `session_at(dt) ->
Session`, `trading_day_of(dt) -> date` (20:00 ET rollover, as Guardian), `in_active_hours(dt, cfg)
-> bool` (`active_hours_et`).

**`scout/paths.py`**: Scout's folder and the write gate.
- `class OwnFolderError(RuntimeError)`
- `@dataclass(frozen=True) class ScoutPaths(root: Path)`: properties `state, logs, reports, inbox,
  levels, facts, health, answers, labels, llm, macro, raw, beats, edgar_due, jason, verify,
  heartbeat_file, params_file, lock_file, env_file`; `day_file(folder: Path, day: date, ext=".jsonl")
  -> Path`; `writable(p: Path) -> Path` (resolves, refuses anything outside `root`, and anything
  inside `radar_dir`/`pilot_dir`); `ensure() -> None` (creates the runtime folders).
- `is_within(child, parent) -> bool` (case-insensitive on Windows).

**`scout/store.py`**: atomic and append-only file IO (Guardian's heartbeat pattern).
- `atomic_write_text(paths, path, text) -> None` (temp file in the same folder, `os.replace`, retried
  `REPLACE_TRIES = 20` times, `REPLACE_PAUSE_S = 0.05`, on `PermissionError`; temp removed on failure)
- `atomic_write_json(paths, path, obj) -> None` (`sort_keys=True`, `ensure_ascii=False`)
- `append_jsonl(paths, path, record: Mapping) -> None` (one `write` of one line ending `\n`; opens with
  `"a"`; requires `record["format"]`)
- `read_jsonl(path) -> list[dict]` (delegates to `scout_reader.records.read_jsonl`)
- `sha256_bytes(b) -> str`; `sha256_text(s) -> str`; `canonical_json(obj) -> str`

**`scout/models.py`**: frozen dataclasses with `to_record() -> dict` (adds `format`) and
`from_record(d) -> Self` (raises `ValueError` on a bad record). Field lists are section 5.
`Fact, HealthRecord, Beat, AnswerRecord, DueWindowRecord, Label, LlmCall, MacroRecord, LevelMeta,
JasonSnapshot, HeartbeatBody, Filing, PeriodicInfo, FetchResult, SourceState`; `Tri =
Literal["true", "false", "unknown"]`; constants `FACT_KINDS`, `WIRE_CLASSES` (SS 6.4 order),
`FILING_CLASSES`, `SOURCE_STATUSES = ("ok", "stale", "unavailable", "disabled", "refused",
"starting")`.

**`scout/interfaces.py`**: the only cross-unit contracts (Protocols). Units may import each other only
through these names and through the record shapes of section 5.
```python
class Fetcher(Protocol):
    def fetch(self, source: str, url: str, *, conditional: bool = True,
              max_bytes: int | None = None) -> FetchResult: ...
class RawSink(Protocol):
    def save(self, source: str, name: str, body: bytes, at: datetime) -> str: ...  # returns sha256
class FactSink(Protocol):
    def add(self, fact: Fact) -> bool: ...          # False when fact_id already stored
    def seen(self, fact_id: str) -> bool: ...
class HealthSink(Protocol):
    def record(self, rec: HealthRecord) -> None: ...
class StatusBoard(Protocol):
    def update(self, source: str, state: SourceState) -> None: ...
    def set_llm(self, enabled: bool, spent_today_usd: float, cap_reached: bool) -> None: ...
    def set_levels(self, current_files: list[str], expires: str | None) -> None: ...
class TickerMap(Protocol):
    def ticker_for_cik(self, cik: int) -> tuple[str, str | None] | None: ...   # (ticker, exchange)
    def cik_for_ticker(self, ticker: str) -> int | None: ...
class SubmissionsSource(Protocol):
    def periodic_info(self, ticker: str, now: datetime) -> PeriodicInfo: ...   # never raises
class LlmTransport(Protocol):
    def create(self, **params: Any) -> Any: ...      # an anthropic Message-like object
```

**`scout/heartbeat.py`**: SS 10.1, every `heartbeat_interval_s` (30).
- `class Heartbeat(paths, cfg, board: SourceStatusBoard, clock)`: `beat(now) -> bool` (writes
  `scout.heartbeat` atomically and appends one `Beat` to `beats\<ET date>.jsonl`; never raises),
  `starting(now)` (writes `stopped: true`, `sources: {}` so no reader accepts a crashed run's beat),
  `stopped(now)`. Writes are serialised by a lock.
- `class Pulse(heartbeat, clock)`: a daemon thread (real clock only) that writes a live beat when one
  is due while the main loop is busy in a long job (a host that does not answer, a slow Claude
  call), once the loop has written its first live beat. Without it one slow job could leave no beat
  for longer than `scout_stale_s`, and every reader would see Scout as down.
- The answers jobs wait for the loop's first live beat (`ctx.live_beat_at`), so a start after 19:30
  ET never answers against the "starting" beat (every answer stale) and marks the day done.
- `class SourceStatusBoard` (implements `StatusBoard`): in-memory state the heartbeat publishes.

**`scout/scheduler.py`**: the job model (section 8).
- `class Job(Protocol): name: str; def due(self, now: datetime) -> bool; def run(self, now: datetime)
  -> None`
- `class EveryJob(name, fn, interval_s, idle_interval_s=None, cfg=None)` (idle interval outside
  `active_hours_et`); `class DailyJob(name, fn, at_et: time, trading_days_only=True,
  early_close_shift_h: float = 0.0)`; `class OnceAtStartJob(name, fn)`.
- `run_due(jobs, now, log) -> list[str]` (runs each due job inside `try/except Exception`; logs the
  job name and the exception type plus a redacted message; one failing job never stops the others).

**`scout/app.py`**: context and main loop.
- `@dataclass class AppContext`: `cfg, paths, clock, board, log`; lazily built services, each importing
  its unit's module on first use and cached: `fetcher() -> Fetcher` (`scout.http.build_fetcher`),
  `raw() -> RawSink` (`scout.raw.RawStore`), `facts() -> FactSink` (`scout.facts.FactWriter`),
  `health() -> HealthSink` (`scout.health.HealthWriter`), `ticker_map() -> TickerMap`
  (`scout.edgar_submissions.build_ticker_map`), `submissions() -> SubmissionsSource`
  (`scout.edgar_submissions.build_submissions`), `llm_transport() -> LlmTransport | None`
  (`scout.llm_client.build_transport`).
- `build_context(config_path, clock=None) -> AppContext`
- `JOB_MODULES = ("scout.jobs_wire", "scout.jobs_macro", "scout.jobs_levels", "scout.jobs_answers",
  "scout.jobs_llm", "scout.jobs_brief")`; each exposes `build_jobs(ctx: AppContext) -> list[Job]`.
- `load_jobs(ctx, modules=JOB_MODULES, strict=False) -> list[Job]` (a missing module logs "not built"
  unless `strict`).
- `write_params(ctx) -> None` (appends to `params.jsonl` when `cfg.reader_params()` differs from the
  last line).
- `run(ctx, *, max_ticks: int | None = None) -> int`: lock (`scout.running`), `ensure()`, startup
  warnings (calendar coverage, empty `contact_email`, missing inputs), `write_params`, heartbeat
  `starting`, then loop: `run_due`, heartbeat, `clock.sleep(loop_interval_s)` (real sleep with
  `SystemClock`); Ctrl+C and a `STOP` (or `STOP.txt`) file in Scout's folder stop it, and Scout
  deletes that file (one left before the start is deleted with a warning); heartbeat `stopped` on
  exit. Returns 0, or `EXIT_ALREADY_RUNNING` (4) when another Scout holds the lock.
- `cli_run(ctx, args) -> int`.

**`scout/inputs.py`**: read-only views of the other bots' files and the owner's lists (SS 2.4).
- `radar_finds(cfg, day: date) -> list[RadarFind]` (owner, 2026-11-04: Radar's real hand-off file
  `radar_dir\<radar_handoff_subdir>\<trading day>.jsonl`, default `state\handoff`, Radar's format 1
  with `price` as a string; a line whose `format` is not 1 or whose `trading_day` names another day is
  skipped; `RadarFind(candidate_id, symbol, found_at, price)`; missing folder or file -> `[]`)
- `pilot_positions(cfg, now) -> list[str]` (open positions from Pilot's plan folder; the exact Pilot
  file is not yet built, so this reads `pilot_dir\state\plans\*.json` with keys `symbol`, `status`
  and treats an unreadable file as absent; refined when Pilot ships)
- `covered_symbols(cfg, now) -> list[str]` (Pilot positions, Radar's finds of today's ET date and of
  the trading day `now` belongs to (after 20:00 ET Radar writes the next day's file), `watch_symbols`;
  upper-cased, sorted, unique)
- Every read is open-read-close, never a lock, never a write.

**`scout/secrets.py`**: `load_env(paths) -> None` (reads `.env` in Scout's folder only; sets only
names in `ALLOWED_ENV = ("ANTHROPIC_API_KEY",)` that are not already set; never logs a value);
`redact(text, cfg=None) -> str` (masks `sk-ant-[A-Za-z0-9_\-]+`, any `x-api-key` value and the
contact email).

**`scout/logs.py`**: `setup_logging(paths, level) -> logging.Logger` (file `logs\scout-<ET date>.log`
plus console; a `RedactingFilter` applies `secrets.redact` to every record; `ANTHROPIC_LOG` is
removed from the environment at startup).

**`scout/running.py`**: `class ScoutLock(paths)` (exclusive lock file, `msvcrt.locking` on Windows,
`fcntl.flock` elsewhere); `main() -> int` for `python -m scout.running` (exit 3 while Scout runs; used
by **Update Scout**).

### 2.2 Layer 1: HTTP, EDGAR and wires (unit `wire`)

**`scout/http.py`**: the single fetch path (section 10).
- `class HttpFetcher(cfg, clock, *, transport: Transport | None = None, log=None, state=None)`
  implementing `Fetcher`; `build_fetcher(ctx) -> Fetcher`. With `state` (`scout/http_state.py`,
  `state\http_hosts.json`) each host's request count for the ET day, last request time and backoff,
  and each source's last request time, survive a restart. A hard daily cap in code
  (`HARD_MAX_REQUESTS_PER_DAY`: nowflation.com 10, covering every host under it) applies whatever
  the config says; the config may only lower it.
- Restart-safe schedules (`scout/jobs_macro.py`): BLS reads when a week has passed since the last
  read in the macro history (checked hourly); the nowflation and Cboe start reads are skipped when
  today's read (Cboe: the latest scheduled read) is already in the history, and the daily jobs are
  seeded from the history too (a restart after 16:30 or 08:20 ET does not read again); the halts
  poller waits for the fetcher's own last send time (no immediate poll after a restart, no sleep in
  the loop), and its job is checked every second so the 60 s holds from the real send. Only a read
  the host answered (`ok` or `stale`) counts for these spacings (`macro_seed.last_read`): a latest
  history line that is `unavailable` does not use up the week or the slot, so a restart reads again
  once at start. At start the board is seeded for BLS, Cboe and nowflation from the same history
  (`scout/macro_seed.py`, section 16), so they do not sit at "starting" until their next read.
- A stored time in the future (the wall clock stepped back) never makes a fetch sleep for hours: a
  negative elapsed time counts as the spacing passed, the wait is at most the spacing, and times in
  `state\http_hosts.json` later than now are clamped on load (a backoff to its longest length).
- `Transport = Callable[[str, Mapping[str, str], float, int], RawResponse]`;
  `RawResponse(status: int, headers: Mapping[str, str], body: bytes)`; `urllib_transport` is the
  only real one.
- **TLS trust (2026-11-01).** `urllib_transport` verifies certificates with the operating system's
  trust store through `truststore` (`truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)`, pip's own
  approach), with CERT_REQUIRED and hostname checking. On the owner's PC (first run, 2026-10-31)
  Python's OpenSSL with the Windows store built an expired path for the new Let's Encrypt chains of
  www.globenewswire.com and cdn.cboe.com (leaf, YE1, Root YE, ISRG Root X2, ISRG Root X1, all in
  date) and refused them, while Windows itself accepted them. (The VIX file has since moved:
  `cboe_vix` now reads it from cdn-api.cboe.com, as cdn.cboe.com answers 307 there and Scout follows
  no redirect.) If `truststore` cannot be imported,
  `ssl.create_default_context()` is used; `scout.http.TLS_TRUST` ("os_trust_store" or
  "python_default") says which, and `build_fetcher` logs it. Verification is never turned off (a
  test greps the package for CERT_NONE, `check_hostname = False` and unverified contexts).
- `class HostBlocked(Exception)` is internal; `fetch` never raises: every failure is a `FetchResult`.

**`scout/raw.py`**: `class RawStore(paths)` implementing `RawSink`
(`raw\<source>\<ET date>\<HHMMSS>-<name>` written once; the sha256 returned and stored on facts).
PR Newswire and Stock Titan store a reduced body (title, link, guid, pubDate only) until
`terms_ok = true` in their source table (SS 4.1 #4).

**`scout/edgar_parse.py`**: pure parsers. Radar's `parse_ticker_map`, `parse_submissions`,
`flags_from_filings` copied with a provenance header (Radar commit `6c971c8`, file sha256) and a
`RadarConfigAdapter` (the fields `flags_from_filings` reads). Additions: `parse_periodic(body) ->
tuple[list[Filing], str | None]` (adds `reportDate` to each filing and returns `fiscalYearEnd`
"MMDD"), `parse_getcurrent_atom(body: bytes) -> AtomPage` (`AtomEntry(guid, form, title, cik,
company, accepted_at, filed_on, items: tuple[str, ...], link)`; raises `SchemaError` on drift),
`filing_class(form, items, description) -> str | None` (SS 6.4 table; an 8-K/A keeps only the
blocking classes bankruptcy, offering, reverse_split and listing_notice: it re-files an earlier 8-K,
so it is never `earnings_result` or `contract_or_other` and never makes gate 6 true on stale news;
5.03 with 3.03 is `reverse_split`. Owner, 2026-11-04: 1.03 is `bankruptcy` and wins over every
other item; 424B2 has no class (`NOT_OFFERING_424B`); 424B8 stays `offering`; the
`contract_or_other` rows are unchanged),
`refuse_dtd(data)` (expat's own doctype and entity handlers, so a DTD is refused in any encoding;
used by `safe_xml_root` and the halts parser),
`parse_ticker_rows(body)` (every row of the ticker file, duplicates kept; Radar's
`parse_ticker_map` keeps the first row of a ticker and is built from it).

**`scout/edgar_atom.py`**: `class EdgarAtomPoller(cfg, fetcher, raw, facts, health, board,
ticker_map, clock)`; `poll(now) -> int` (new facts). One request per form in `edgar_atom_forms`, each
a feed; facts `kind = "filing"`, one per ticker listed under the filer's CIK (GOOGL and GOOG,
a common and its warrants), with the raw body kept once.

**`scout/edgar_submissions.py`**: `class TickerMapClient` (implements `TickerMap`; refreshed once per
ET day from `edgar_tickers`, held in memory and in `raw\`), `class SubmissionsClient` (implements
`SubmissionsSource`; cached per CIK per ET day; `periodic_info` returns `PeriodicInfo.status` in
`ok | no_cik | ambiguous_ticker | fetch_failed | schema | no_http`); `build_ticker_map(ctx)`,
`build_submissions(ctx)`. `TickerMapClient.tickers_for_cik(cik)` lists every ticker under a CIK;
`ambiguous(ticker)` is true when a ticker is listed under more than one CIK, and then no due window
is built from the first row's CIK (status `ambiguous_ticker`, so gate 5 answers `unknown`).

**`scout/wire_rss.py`**: `parse_rss(body: bytes) -> list[RssItem]` (`RssItem(guid, title, link,
published_at, raw_categories)`; strips a BOM; raises `SchemaError` on drift); `class WirePoller(cfg,
source_name, fetcher, raw, facts, health, board, clock)`; `poll(now) -> int`. Used for every source of kind
`wire` (`prnewswire`, `stocktitan`; GlobeNewswire was removed on 2026-11-03, see 6.3). Tickers come from
exchange tags (`tickers_in`), then from a title that ends "| XXX Stock News" (`title_suffix_ticker`,
`wire_title_ticker_suffixes`; Stock Titan's form, no exchange) when XXX is not already named. A wire that answers
`refused_after_403s` (3) consecutive 403s is marked `refused` and not polled again that day.

**`scout/tickers.py`**: `tickers_in(text, prefixes) -> list[tuple[str, str]]` (exchange, ticker;
regex over "(EXCHANGE: XXX)" or "(EXCHANGE - XXX)" with `ticker_exchange_prefixes`; segments split on
",", ";", "and", "&" and a "/" before another prefix; other text in the tag is skipped; several per
title); `normalise(sym) ->
str` (upper, `.`/`-` class separators as Radar's `normalise_ticker`).

**`scout/classify.py`**: `wire_class(title, keywords: Mapping[str, list[str]]) -> str` (first match
in `WIRE_MATCH_ORDER`, `other` last; case-insensitive word match); re-exports `filing_class`. The
order is SS 6.4's with `bankruptcy` first (owner, 2026-11-04: a Chapter 11 headline also names a
restructuring "agreement"), then `earnings_date`. A results-date notice ("to
Announce Financial Results for ... on November 3", "to Report Q3 Results and Phase 2 Topline Data on
November 12") also matches "financial results for", often "agreement" or "order", and for a biotech
"Phase 2" or "PDUFA"; classed as those it made gate 6 answer true for a notice that only names a
date, and gate 5 (which reads dates from `earnings_date` facts) missed it. `earnings_date` is in the
reader's `NEVER_PASS`, whatever `hold_catalyst_classes` says.
Because it goes first, the `earnings_date` keywords are narrow, as SS 6.4 words the class ("to
report / announce / release ... results"): ` ... ` inside a keyword matches any text, and each
default pairs a scheduling phrase with "results" or "earnings" after it ("will host ... results",
"timing of ... results", "date for ... results", "schedules ... earnings"). A bare "to release" or
"conference call to discuss" also caught contract and results headlines ("Agreement to Release Its
Products", "Reports Q3 Results and Will Host Conference Call to Discuss"): gate 6 lost the catalyst
and the dateless `earnings_date` fact made gate 5 `unknown` for `earnings_lookback_days`.
This needs the spec owner's note in SS 6.4.

**`scout/facts.py`**: `class FactWriter(paths, clock)` implementing `FactSink` (`facts\<ET date of
first_seen_at>.jsonl`; dedup on `fact_id` over the last `fact_dedupe_days` files, loaded at start);
`make_fact_id(source, guid, ticker) -> str`. The dedup reaches back `fact_dedupe_days` (7) ET days, so
a restart never stores again an entry a quiet EDGAR feed still lists from last week.

**`scout/health.py`**: `class HealthWriter(paths)` implementing `HealthSink` (`health\<ET
date>.jsonl`); `class CoverageTracker(cfg, seeds=None)` (per source and feed: the last good poll and
its spacing, the failed polls since, seen guids (bounded `seen_guid_cap`, 5000), computes `overlap`
and `gap_before` for the record it builds with `scout_reader.coverage.allowance_s`, exposes
`gap_open(source) -> bool` for the heartbeat).

**`scout/coverage_seed.py`** (0.2.1; owner, 2026-11-04): `load_seeds(paths, now, *, sources, cap, days)
-> {(source, feed): FeedSeed}`, read once by `jobs_wire.build_jobs` at start, so the first poll after a
restart is judged like any other (SCOUT_SPEC 6.2). A feed's seed is its last good health record
(start, `spacing_s`, an unreadable spacing read as 0 as the reader does), the `spacing_s` of each
failed poll recorded after it (a feed with only failed polls gets those with no anchor), and the
guids of the facts stored by good
polls of that feed: a fact belongs to a good poll when its (source, feed, `first_seen_at`) equals
the (source, feed, `end`) of a good health record. Every new item of a good EDGAR or wire page
becomes at least one fact (one without a ticker when none is found), so these guids are a subset
of the ones the tracker had seen, up to `seen_guid_cap` (an item already stored under the same
fact id from another feed is not stored again under this feed; fewer ids only means fewer
overlaps). Health records carry no feed URL, so a wire `url` changed by the owner is not detected
(SCOUT_SPEC 6.2). Facts written by a poll that then failed have no good
health record and are not used. Only records up to `now` are read (no look-ahead). Never raises; a
missing or corrupt file gives fewer guids or no seed, and the tracker then claims no overlap it
cannot show (the first poll is judged with no seen ids). No new file and no new config key: the seed reads `fact_dedupe_days` ET days of the
files Scout already writes.

**`scout/jobs_wire.py`**: `build_jobs(ctx) -> list[Job]`: `edgar_tickers` (daily 03:50 ET and at
start), `edgar_atom` (`EveryJob` with the source's spacings), and one job per source of kind `wire` (`prnewswire`, `stocktitan`).
Since 0.2.4 every one of them runs at the polling pace (`ctx.pace()`, section 21): the wires'
`interval_fn` and the EDGAR job's (`EdgarAtomPoller.interval_at`, its fastest form) ask
`health.spacing_in_force(src, now, cfg, pace, feed)`, and a cycle polls only the forms that are due.

**`scout/pace.py`**, **`scout/jobs_pace.py`**, **`scout/config_pace.py`**, **`scout/brief_pace.py`**
(owner, 2026-11-10; 0.2.4): the polling pace, section 21.

### 2.3 Layer 1: halts, macro and regime (unit `macro`)

**`scout/halts.py`**: `parse_halts(body: bytes) -> list[HaltRow]` and `parse_halts_rows(body) ->
(rows, skipped reasons)` (BOM stripped; `ndaq` namespace; Market stored as received, any non-empty
text, or None when missing (owner, 2026-11-04); HaltTime ET with optional fractional seconds;
`pubDate` ignored; no order assumed; empty `ResumptionTradeTime` = still halted; a bad row is skipped
and counted, the poll logs a warning per row and writes "skipped N bad row(s): ..." in its health
record's `error` while staying `ok`; raises `SchemaError` on feed drift or when no row is readable);
`class HaltPoller(...)`, `poll(now) -> int` (facts `kind = "halt"`, guid = symbol|HaltDate|HaltTime;
a later row with a resumption writes a new fact with `resumed_at`; the after-20:00 ET
`&haltdate=MMDDYYYY` read once per day). Hard minimum 60 s spacing in code. A halt or resumption is
marked seen only after its fact is stored; a failed write raises, and the next read is unconditional
(no 304) so the row is tried again.

**`scout/nowflation.py`**: `parse_release_calendar(body) -> NowCalendar`, `parse_realtime(body) ->
NowRealtime`, `parse_forecasts(body) -> NowForecasts`, `parse_ics(body) -> list[IcsEvent]`
(fallback); pinned schemas (`SchemaError` refuses the source, never skips rows);
`freshness(cal, rt, now, cfg) -> tuple[str, list[str]]` (SS 7.3 fail-closed list); `class
NowflationReader(...)`, `read(now) -> MacroRecord` (at most `[host_max_requests_per_day]` reads).

**nowflation's live layout (read 2026-10-31).** The first build pinned synthetic
shapes and the install refused both files ("realtime: missing 'last_good_held'", "forecasts:
missing 'forecasts'"); the schemas now follow the live files. Extra keys are allowed; a missing
required key or a wrong type refuses the source.
- `realtime.json` required: `generated_at` (ISO UTC), `run.ok`, `run.checks_ok` (booleans),
  `today.record_type` (string), `today.last_good_held` (boolean), `today.realtime_yoy_pct` (finite
  number), `freshness.realtime.live_share_pct` (finite number). Optional:
  `freshness.realtime.stale_weight_pct`; when it is missing, the documented fallback is the sum of
  `freshness.realtime.stale[].weight_pct` (an empty list is 0); with neither, the weight is None and
  `freshness` gives `stale_weight_missing` (stale). Live share and stale weight are not complements:
  held components are neither (on 2026-10-31 the two together were well under half the weight), so
  the old `100 - live share` fallback is gone.
- **The stale-weight gate (SS 7.3) on the live layout.** The first build's threshold (70) was set
  for `100 - live share`. nowflation's `stale_weight_pct` counts only market-sourced components whose
  every source is older than 2x its cadence (`freshness.definitions.stale`); BLS-held components
  count in neither figure. In the 2026-10-31 read the market-sourced (live plus stale) weight was
  well under half the basket, so `stale_weight_pct` could never reach 70 and a threshold of 70 could
  never fire: every market source stopping would still read "ok".
  The gate is now the stale share of the market-sourced weight, `stale / (live + stale) * 100`,
  against `nowflation_max_stale_weight_pct`, recalibrated from 70 to **20** (same key, so existing
  config files still load; an install that sets 70 keeps a looser gate that still fires when every
  market source stops). On 2026-10-31 it read well under 20 % (ok); 20 % takes several
  market-sourced components, or one large one, going stale at once. `live + stale = 0` (every
  component held from BLS) gives `no_live_inputs` (stale). What it adds: no new gate, it repairs the existing SS 7.3 one; what it removes: the unreachable absolute
  70. `live_share_pct` itself is not floored: a monthly source stepping aside for a newer BLS print
  lowers it legitimately (that weight counts as held, not stale). Also read when present: `freshness.realtime.avg_input_age_days`,
  `today.cpi_gauge_yoy_pct`, `today.official_cpi_yoy_pct`, `today.official_cpi_month`.
- The 1/5/20-day changes (SS 7.3): 1 day = `today.realtime_dod_pp`; 5 days = `today.realtime_wow_pp`
  (nowflation's week: 7 calendar days, i.e. 5 trading days; both are like-for-like under the
  current method); 20 days is not published, so Scout computes it from `history`: today's history
  point (else `today.realtime_yoy_pct`) minus the point nearest 28 calendar days back (26 to 30 days
  back will do). It is absent (and the regime `unknown` at the 20-day window) when no such point
  exists, when that point is not `record_type` "live", or when its `method_version` differs from the
  file's (a change across a method switch in nowflation's `history` is not like for like).
  `history` is optional.
- `forecasts/index.json` required: `generated_at`, `n_graded` (int), `receipts` (list of objects
  with a `print_month` string). Each receipt keeps `status`, `record_type`, `pre_registered`,
  `release_datetime_utc`, `official_forecast`, `graded`, `actual`, `signed_error`, and from
  `headline_yoy`: forecast (`nowflation_forecast_ensemble`), nowcast, cleveland (`cleveland_fed`),
  kalshi (`kalshi_implied`), street (`street_consensus`). `latest` is optional; `horizons` (T-14,
  T-7, T-1 locks) is not read.
- Forecast lean (SS 7.3, informational): per upcoming calendar event with a forecast, forecast
  minus prior and minus street (consensus), plus minus Cleveland Fed when known, with `n_graded` as
  the graded count. A CPI event is matched to the receipt for its `data_month`; the receipt fills a
  forecast, street or Cleveland value the calendar lacks (the calendar wins when both have one) and
  adds `receipt_status` ("provisional" until locked the evening before the release). A provisional
  receipt may have `street_consensus` null; the lean then reads "n/a" vs street.
- Thresholds: over the 2019-2026 history in the capture (reconstructed twice a month before
  2026-09-15, daily since), 28-day changes have a median size of 0.27 pp (0.19 in calm years, 0.65
  in 2021-22); at 0.25 / 0.25 about a third read hot, a fifth cooling and half steady. The defaults
  stay (CLAUDE.md: no change without a journal effect).

**`scout/bls_ics.py`**: `parse_bls_ics(body) -> list[IcsEvent]` (lenient: no DTSTAMP, folded lines,
`TZID=US-Eastern` or `America/New_York` converted once via `_et`); `class BlsReader`, `read(now)`.

**`scout/fomc.py`**: `fomc_events(cfg) -> list[MacroEvent]` (`fomc_dates` at `fomc_decision_time_et`).

**`scout/cboe_vix.py`**: `parse_vix_csv(body) -> list[VixRow]` (header must be
`DATE,OPEN,HIGH,LOW,CLOSE`); `class VixReader`, `read(now) -> MacroRecord`.

**`scout/bls_data.py`** (SS 7.7): `parse_series(body, series_id) -> list[MonthValue] | None` (None
when BLS refused); `results_from(series, cfg, expect)`; `expected(history, kind, month, before)`;
`jobs_trend(changes, cfg)`; `class BlsDataReader`, `read(now) -> MacroRecord` (three requests, one per
series); `summary_lines(results)` for the brief.

**`scout/macro.py`**: `merge_events(now_events, bls_events, fomc_events, cfg) -> list[MacroEvent]`
(CPI and NFP cross-checked with BLS within `macro_agree_minutes`; FOMC against the table by date; PCE
`sources_agree = None`; earlier time wins on disagreement); `inflation_regime(rt, cfg) -> str`;
`forecast_lean(cal) -> list[dict]` ("informational, untested"); `class MacroWriter(paths, facts)`:
`write(record)` (appends `macro\history.jsonl`, rewrites `macro\<ET date>.json`, adds `kind =
"macro_event"` facts for new events).

**`scout/jobs_macro.py`**: `build_jobs(ctx)`: `nasdaq_halts` (60 s, 04:00-20:00 ET, plus the
after-20:00 read), `nowflation` (at start, 08:20 ET, 08:50 ET on release days), `bls` (weekly),
`cboe_vix` (daily after 16:30 ET), `bls_data` (at start, 08:40 ET, 09:15 ET on release days),
`macro_merge` (after each of those). At build time it calls
`macro_seed.seed_board` (below).

**`scout/macro_seed.py`**: `last_read(writer, source)` (the last read that counts for the spacing:
the latest history line when its status is `ok` or `stale`, else None); `recheck(cfg, rec, now)`
(why an `ok` line is no longer fresh); `seeded_state(cfg, rec, last_ok, now) -> SourceState`;
`seed_board(board, cfg, writer, now, readers)`. `MacroWriter.last_ok` keeps each source's latest
`ok` line time. Rules: section 16.

### 2.4 Jason's levels (unit `levels`)

**`scout/levels_parse.py`**: the price grammar ported from `config/jason_levels.py`
(`_one_price`, `_prices_in`, `_level_readings`, `_parse_levels`, `_parse_conditionals`), public
names without the underscore. Provenance header: `ai-trading-platform` commit
`d62a282659f4c70fba7450471a2add98eef5af82`, file sha256.

**`scout/levels.py`**: the loader and queries.
- `class JasonPlanError(ValueError)`
- `load_plan(path) -> dict` (raises only for an unreadable file or no `session_date`/`setups`;
  everything else to `warnings`; honours `expires_default`; ignores unknown top-level keys; reads
  `source_kind`, default `"tuesday"` with a warning)
- `load_folder(levels_dir, today: date) -> dict` (merges every current file, newest `session_date`
  first per symbol; each setup keeps `source_kind`, `session_date`, `file`)
- `setup_is_current, current_setups, plan_is_current, setup_for, levels_of, conflict_for,
  invalidation_reference, symbols, describe_levels` (same contracts as the source; records only,
  never a veto). Removed: `_platform_view`, the `research.levels` import, `PLAN_PATH`.

**`scout/levels_add.py`**: `add_file(paths, src: Path, now, *, confirm: Callable[[str], bool]) ->
AddResult` (validate with `load_plan`; print warnings; ask once; copy to
`levels\<session_date>.<source_kind>.json` plus `.meta.json`; refuse when the target exists with a
different sha256, never overwrite or edit); `cli_levels_add(ctx, args) -> int`. Since 2026-11-01
(section 2.7) `add_file(..., auto=True)` is the scheduled tasks' `--yes` and `--inbox`: it also
refuses a missing or unknown `source_kind`, a file with no setup left and a `<date>.<kind>.json` name
that disagrees with the contents (`--inbox` refuses any other name); a refusal prints a last line
"REFUSED: ...", exits 2 and writes `<file>.rejected.txt` beside a file inside Scout's folder. The
file is read once (`_plan_from_bytes`): the bytes validated are the bytes hashed, noted and copied;
a read error (`OSError`: missing, locked, mid-write) refuses with no note, so it is retried.
`add_inbox(paths, now)` adds every `inbox\jason\*.json` whose sha256 is neither added (sidecars) nor
refused in that form (the note's sha256 line). The level file appears by a hard link of a temp file
(a Windows rename where links are missing), which fails on an existing name, so a concurrent add
never replaces a file and a reader never sees half of one; an add that finds its file but no
sidecar writes the sidecar.

**`scout/levels_bars.py`**: `class DailyBar(day, open, high, low, close)`; `class BarsLoader(Protocol):
def daily(self, symbol, start: date, end: date) -> list[DailyBar]`; `RadarBars(radar_dir)` (Radar's
SQLite opened `mode=ro` by URI; daily bars only if Radar stores them, else it reports "no daily bars");
`CsvBars(folder)` (owner-supplied `<SYMBOL>.csv`, header `date,open,high,low,close`).

**`scout/levels_score.py`**: the fixed scorer (SS 5.4): `score_level(price_readings, side, forward,
tol) -> str`, `tolerance_for(bars, price, cfg) -> Decimal`, `null_for(...)` seeded by
`hashlib.sha256(f"{seed}|{symbol}|{session_date}|{kind}|{price}")`, `score_folder(levels_dir,
loader, today, cfg) -> Scorecard`, `render(scorecard) -> str` (n everywhere; INDICATIVE under
`levels_score_min_scored`); `cli_levels_score(ctx, args) -> int` (writes
`reports\levels-score-<ET date>.md`).

**`scout/levels_snapshot.py`**: `build_snapshot(levels_dir, today, now) -> JasonSnapshot`;
`write_if_changed(paths, snapshot) -> bool` (appends to `jason\snapshots.jsonl`).

**`scout/jobs_levels.py`**: `build_jobs(ctx)`: `jason_snapshot` (at start, at 00:05 ET daily, and
every 5 min when the `levels\` folder's file set changed); updates `board.set_levels`.

### 2.5 Answers (unit `answers`)

**`scout/due_window.py`**: pure SS 7.2 item 2.
- `next_period_end(report_date: date) -> date` (+3 calendar months, end-of-month clamped)
- `is_fiscal_year_end(period_end, fiscal_year_end_mmdd) -> bool` (same month, day within 7)
- `due_window(info: PeriodicInfo, cfg) -> DueWindowRecord` (status `ok | no_periodic_200d |
  no_cik | fetch_failed | schema | no_http`; `nt_recent` when an NT 10-Q/NT 10-K was accepted within
  `nt_recent_days` (45))
- `replay(filings, fiscal_year_end, cfg, start, end) -> list[ReplayRow]` (for each trading day:
  would the rule have placed the day outside the window while a periodic report was filed before the
  next open; used by tests now and by the lead's history replay at build step 4)

**`scout/answers.py`**: `class AnswersJob(cfg, paths, submissions, clock)`; `answer_symbols(symbols,
as_of, now) -> list[AnswerRecord]` (refreshes each symbol's due record via `submissions` then calls
`scout_reader` on Scout's own folder with `now`); `write(records)`; `run_pilot(now)` (19:30 and 19:45
ET, Pilot positions and `watch_symbols`), `run_radar(now)` (after 20:00 ET, today's Radar finds with
`as_of` 19:45 ET). Early-close days: every time moves 3 hours earlier.

**`scout/answers_cli.py`**: `cli_answers(ctx, args) -> int` (prints the six reader answers for the
given symbols and `--as-of`; writes nothing).

**`scout/jobs_answers.py`**: `build_jobs(ctx)`: `answers_pilot` (`answer_times_et`), `answers_radar`
(`radar_answers_after_et`).

### 2.6 Claude jobs, brief and verification (unit `llm`)

**`scout/llm_client.py`**: `build_transport(ctx) -> LlmTransport | None` (None unless `llm_enabled`
and `ANTHROPIC_API_KEY` is set; imports `anthropic` lazily; `max_retries=0` (Scout retries at most
once itself), timeout `llm_timeout_s`); `call(transport, request: LlmRequest) -> LlmResponse`
(normalises the SDK object: `content blocks, stop_reason, usage, request_id, model`); never logs the
request headers or key.

**`scout/llm_prompts.py`**: `SYSTEM_PROMPTS: dict[str, str]` per job (fixed, versioned);
`prompt_sha256(job) -> str`; `item_block(item_id, text, nonce, limit) -> str` (escapes `<`, `>`, `&`,
truncates to `llm_item_chars`); `new_nonce() -> str` (`secrets.token_hex(8)`); `build_j1(facts, cfg)
-> LlmRequest`, `build_j3(symbol, company, facts, cfg) -> LlmRequest`, `build_j4(brief_facts, cfg)
-> LlmRequest`. System prompt first with `cache_control: {"type": "ephemeral"}`, nothing variable
before the breakpoint. Only public tickers and company names; no account data, sizes, P&L, paths.

**`scout/llm_schemas.py`**: `LABEL_SCHEMA_V1` (SS 8.3; `additionalProperties: false`; enums),
`J1_OUTPUT`, `J3_OUTPUT`, `J4_OUTPUT`, `SCHEMA_VERSION = 1`; `validate(obj, schema) -> list[str]`
(a small stdlib validator for the subset used: object, array, string enums, bool, null, required,
additionalProperties).

**`scout/llm_checks.py`**: SS 8.4. `check_label(label, input_tickers, evidence_ids) -> list[str]`;
`check_j3(label, response_blocks, allowed_domains) -> list[str]` (every cited URL appears in a
`web_search_tool_result` or `web_fetch_tool_result` block of the same response, its host is allowed,
the fetched text contains the quote, the ticker or company name appears); `check_date(date, quote,
source_text, today) -> list[str]` (-7..+200 days, verbatim quote). A label is `verified` only when the
list is empty.

**`scout/llm_ledger.py`**: SS 8.6. `class Ledger(paths, cfg, clock)`: `spent_today() -> float`,
`spent_month() -> float` (sums `llm\*.jsonl` by ET date and month), `calls_today(job) -> int`,
`searches_today() -> int`, `estimate(request) -> float`, `allow(job, request) -> tuple[bool, str]`,
`price(usage, model) -> float`, `cap_reached() -> bool`. The estimate of a call with server tools is
the tool loop: with n = searches + fetches, base = input characters / 3.5 and r = the larger of
`web_fetch_max_content_tokens` and `SEARCH_RESULT_TOKENS` (10000), input = (n + 1) x base +
r x n(n + 1)/2 tokens (each iteration re-reads the prompt and every result so far), plus
`max_tokens` output and the searches' fee; a call with no tools is base + `max_tokens`. A day file
that exists but cannot be read refuses every call (`ledger_unreadable`) and counts as cap reached.

**`scout/llm_records.py`**: `write_call(paths, LlmCall, reason=None, extra=None)`, `write_label(paths,
Label)`, `calls_on(paths, day)` (a settled reservation replaced by its final record), `settled`.
`llm_client.execute` writes a `reserved` record at the estimate (with the call's searches) before
each call and does not send the call when that write fails (`ledger_unwritable`); the final record
carries `settles: <reservation>`. When the final record cannot be written the reservation stands at
the worst case, so a full disk can never send calls the caps do not see. A timeout or connection
error is charged the estimate and the call's searches.

**`scout/llm_j1.py`**, **`scout/llm_j3.py`**, **`scout/llm_j4.py`**: one job each:
`class J1Job(cfg, paths, transport, ledger, clock)`: `run(now) -> int`; `class J3Job`: `run(now)`
(19:30 ET for each open Pilot position; and Radar finds with no Layer-1 fact within
`j3_no_fact_after_s`); `class J4Job`: `paragraph(brief_inputs, now) -> str | None`. Shared flow:
ledger check, build, call, on refusal / `max_tokens` / schema failure one retry then `unlabelled`,
post-checks, write `llm` and `labels` records. No job writes outside `state\llm\`, `state\labels\`
and `reports\`; no job changes an answer.

**`scout/brief.py`**: `build_brief(ctx, day: date, now) -> str` (the deterministic skeleton: macro
events credited "Nowflation" with a link, inflation regime and VIX, overnight catalysts, open halts,
earnings due windows among recent finds, Jason's current setups and frame or "no current levels
file", source health, LLM spend or "LLM cap reached", the week's verification sample; times ET and
Brisbane; all third-party text escaped); `write_brief(ctx, day, now) -> Path`
(`reports\scout-<ET date>.md`, J4 paragraph appended when on); `cli_brief(ctx, args) -> int`.
**`scout/brief_sections.py`**: one function per section, each `(inputs) -> list[str]`, reading files
through `scout_reader` and `scout.store` only.

**`scout/verify.py`**: SS 11. `weekly_sample(paths, week, seed) -> list[str]` (10 answered symbols,
seed stored in `verify\samples.jsonl`; a short sample, from a thin Monday, is topped up from later
days of the week on each verify run, keeping what was picked); `auto_check(paths, day) -> list[Miss]`
(each gate-5 `false` against filings and wires that appear before the next session's close: an
earnings report there is a miss, including a 10-Q or 10-K, which `edgar_atom_forms` polls for this
check; the 19:30, 19:45 and Radar rows of one symbol on one evening are one check, a miss in any of
them a miss); `report(paths, start, end) -> str` (`reports\scout-verify-<date>.md`: per source and answer,
agreement, false positives, misses, unknown share, coverage gaps, pass status per SS 11);
`cli_verify(ctx, args) -> int`.
**`scout/verify_check.py`**: `cli_check(ctx, args) -> int` (lists the week's sample; owner marks
agree or disagree with a note; appends to `verify\checks.jsonl`).

**`scout/jobs_llm.py`**: `build_jobs(ctx)`: `llm_j1` (every `j1_interval_s` in PRE/CORE/POST),
`llm_j3` (`j3_time_et`, plus the 10-minute no-fact check). Returns `[]` unless `llm_enabled`.
**`scout/jobs_brief.py`**: `build_jobs(ctx)`: `brief` (`brief_time_et`, trading days), `verify`
(daily 20:30 ET report), `ledger_status` (updates `board.set_llm` every minute).

### 2.7 The inbox and the Paper Gains Discord (owner decisions 2026-11-01)

SCOUT_SPEC 18 and `docs/INBOX.md` (the file format the scheduled Claude tasks write). Record and brief
first; the only gate input is gate 5, in the blocking direction.

**`scout/discord_parse.py`** (pure): `Rules` / `rules_from(cfg)`; `parse_capture(body, *, channel,
name, now, rules) -> Capture(posts, rejects, lines)`; `validate_line(obj, ...) -> Post | Reject`
(exact keys, `format` 1, channel = folder and configured, snowflake `message_id`, permalink ending in
it, `posted_at` within `discord_posted_at_tolerance_s` of the snowflake's own time, not future, not
older than `discord_max_post_age_days`, `captured_at` sane, `kind` known, `earnings` only for
`earnings_schedule` (required) and `levels_card`, only in `discord_schedule_channels`, dates from the
post's ET date - 1 to + `discord_schedule_max_ahead_days`); `post_tickers(text, channel, rules)`
(cashtags, exchange tags, and "TICKER - headline" in `discord_prefix_ticker_channels`, all through
`tickers.normalise`); `snowflake_time`, `capture_time`, `split_capture_name`. Format limits kept in
code: 40 entries, 400 tickers each, 100-character author.

**`scout/discord_ingest.py`**: `class DiscordInbox(cfg, paths, clock, facts, raw, health, board)`;
`poll(now) -> int` (new posts) reads every `inbox\discord\<channel>\*.jsonl` not yet read in its
current form (`state\discord\files.jsonl`: file, size, mtime_ns, sha256, capture_at, counts), skips
`*.tmp`, hidden files and names older than `discord_max_post_age_days` + 1 day, keeps the bytes in
`state\raw\discord\`, writes each refused line to `state\discord\rejects\<ET date>.jsonl` and one
health record per file (source `discord_inbox`, `gate6` false, result `ok` or `schema`, `error` the
reasons). New posts (deduped by (channel, message_id) over the facts of the last
`discord_max_post_age_days` + 2 days, so across restarts) become facts: `earnings_schedule` facts
first (one per date, session, ticker; `event_kind` "earnings", `extra.earnings_date`,
`extra.session`), then `discord` facts (one per ticker, or one with ticker null; class from
`classify.wire_class` on the text; `extra` holds post kind, author, text, image text, captured_at,
the earnings list and every ticker). Scout never edits, moves or deletes an inbox file (decided here:
bookkeeping in `state` instead of a `done` folder, so the inbox stays the tasks' folder and a
re-run that rewrites a file is read again). `publish(now)` sets the heartbeat's `discord_inbox`
(`cadence_status`: `ok` while the trading-day time inside `discord_capture_hours_et` since the newest
capture is at most `discord_expected_every_min`; `stale` with a reason; `disabled`). An empty capture
file counts as a capture; a file with lines of which none was accepted (no new post, no repeat) does
not (`capture_at` null in the bookkeeping), the reader's rule that a file whose every line is
unreadable counts as missing. A file that raises while it is read is booked as refused
(`ingest_error:<type>`, no capture) so it neither stops the poll nor repeats; an `OSError` leaves it
for the next poll. `validate_line` refuses strings that do not encode as UTF-8 (`text_not_utf8`);
reasons never carry capture text (unknown keys named only when plain, at most three; a refused
line's `message_id` kept only when a snowflake); the brief names at most `MAX_REASONS` (6) distinct
reasons. `cli_discord_check` checks one file (also `.jsonl.tmp`) and writes nothing.

**`scout/jobs_discord.py`**: `build_jobs(ctx)`: `discord_inbox` (`EveryJob`, `inbox_poll_s`), placed
before the answers jobs in `JOB_MODULES`; at build it publishes the status from the bookkeeping.

**`scout/brief_discord.py`**: `sec_discord(inp)`, after Jason's levels: today's daily card from the
snapshot's `daily` setups (or a note when a card post exists but no file was added); earnings lists
for today and the next trading day by session; small-cap catalysts (`discord_catalyst_channels`,
Radar-priced tickers at or under `scan_max_price` flagged); market headlines
(`discord_headline_channels`); reactions (`discord_reaction_channels`) naming a Radar find or a watch
symbol; capture health (heartbeat status, each channel's newest capture, today's refused lines by
reason). Lists newest first, at most `discord_brief_max_items`; every string through `md_escape`.

**Paper Gains scanners** (SCOUT_SPEC 18.7, INBOX 3.3; record and brief only). `discord_parse`:
kind `scanner_alert` with a required `scanner` object (refused for every other kind), allowed only in
the folders of `SCANNER_TIMEFRAMES` (`15m-scanner` -> `15m`, `daily-scanner` -> `daily`,
`weekly-scanner` -> `weekly`; a format fact, not a tunable) with the timeframe agreeing; pattern 1-80
and state 1-40 characters or null, as printed (whitespace collapsed); 1-200 tickers through
`tickers.normalise` and the ticker pattern. `Post.scanner` holds a `ScannerAlert`. `discord_ingest`
writes one fact of kind `scanner` per ticker **instead of** `discord` facts (class `scanner_alert`,
title "<T>: <pattern> - <timeframe> (<state>)", `extra` post kind, timeframe, pattern, state,
tickers, author, text, captured_at, capture file); the (channel, message_id) dedupe reads both kinds
(`POST_KINDS`). **`scout/brief_scanner.py`**: `sec_scanners(inp)`, inside the Discord section before
capture health: the scanner facts since the prior close grouped by timeframe, then (pattern, state),
newest group first, at most `discord_brief_max_items` groups per timeframe and
`discord_scanner_brief_max_tickers` tickers per group (Radar finds and watch symbols flagged and
first, "+N more"). **`scout_reader/scout_reader/scanner.py`**: `scanner_alerts_from(facts, beat,
symbol, as_of, start)`; `Reader.scanner_alerts(S, as_of, *, days=7)` (at most 14) for Radar's later
tags (SCOUT_SPEC 9.1): `true` with `alerts` (timeframe, pattern, state, posted_at, channel,
fact_id; newest first, at most 50), `false`, or `unknown` while `discord_inbox` is not `ok` in the
governing beat (rule 6 for a descriptive answer; no gate reads it). Gates 5 and 6 never read kind
`scanner` (`CATALYST_KINDS`, `MACRO_KINDS`, `discord_schedule.KIND` exclude it).

**`scout_reader/scout_reader/discord_schedule.py`** (stdlib): `schedule_hits(facts, symbol, as_of,
start, end)` returns `discord:<fact_id>` for visible `earnings_schedule` facts naming S whose session
interval overlaps gate 5's window; `session_interval(day, session)`: `pre` = [00:00 ET, the 09:30
open), `post` = [the close, 20:00 ET), `unknown` = the ET day (a non-trading day: the ET day for
every session). Malformed records are skipped.

**Gate 5** (3.6) adds the hits only when `params.discord_gate5_enabled is True` (absent in older
params: nothing changes), with the extra reason `discord_earnings_schedule`. The source never takes
part in `false`. Gate 5's announced-date source reads `earnings_date` facts of kind wire or filing
only (`CATALYST_KINDS`): a `discord` post whose text the classifier calls `earnings_date` is record
and brief only and never reaches gate 5, whatever `discord_gate5_enabled` says. **Gate 6** never
reads `discord` facts (`CATALYST_KINDS` is wire and filing), nor
J1, J3 or verify. `scout_reader.coverage.NOT_COVERAGE` keeps `discord_inbox` out of the beat-based
fallback list, so no gate reads its status.

## 3. Package `scout_reader` (stdlib only, never raises)

```
scout_reader/scout_reader/
  __init__.py   [scaffold] __all__ and PEP 562 lazy __getattr__ forwarding to api.py
  _et.py        [scaffold] UTC <-> ET by the US DST rule (D2)
  calendar.py   [scaffold] the Guardian calendar copy (D1)
  records.py    [scaffold] tolerant file reading, format check, as-of filters, Result
  coverage.py   [wire]     the SS 6.2 gap rule over health records
  cache.py                 the per-Reader file cache for entry_blocks (3.9; 2026-11-04)
  blocks.py                entry_blocks: Radar's buy-signal blocks (3.9; 2026-11-04)
  halts.py      [macro]    halt_open_from(...)
  macro.py      [macro]    market_context_from(...)
  jason.py      [levels]   jason_tags_from(...)
  datetext.py   [answers]  announced-date parser
  discord_schedule.py      gate 5's Discord earnings-list source (2.7; block only)
  scanner.py               Paper Gains scanner alerts, descriptive (2.7; no gate)
  gates.py      [answers]  gate 6, gate 5, news_headline
  api.py        [answers]  the six public functions, scanner_alerts, entry_blocks and Reader
```

### 3.1 Public API (`scout_reader/api.py`, re-exported by `__init__`)

```python
Result = dict  # {"value": "true"|"false"|"unknown", "evidence": list[str], "stale": bool,
               #  "reasons": list[str]} plus optional extra keys per function

class Reader:
    def __init__(self, scout_dir: str | os.PathLike, *, now: Callable[[], datetime] | None = None,
                 stale_s: float | None = None, cache: bool = True): ...
    def news_headline(self, symbol: str, as_of: datetime) -> Result: ...          # + "classes"
    def fresh_catalyst(self, symbol: str, as_of: datetime) -> Result: ...
    def event_before_next_session(self, symbol: str, as_of: datetime) -> Result: ...
    def halt_open(self, symbol: str, as_of: datetime) -> Result: ...
    def jason_tags(self, symbol: str, price: float | None, as_of: datetime, *,
                   frame_prices: Mapping[str, float] | None = None,
                   near_pct: float | None = None) -> dict: ...   # one Result per tag
    def market_context(self, as_of: datetime) -> dict: ...       # Results per field
    def scanner_alerts(self, symbol: str, as_of: datetime, *,
                       days: float = 7) -> Result: ...          # + "alerts"; descriptive (2.7)
    def entry_blocks(self, symbol: str, as_of: datetime, *, lookback_days: int = 3,
                     macro_before_min: float = 5, macro_after_min: float = 15) -> Result: ...  # + "blocks", "window" (3.9)

# module-level conveniences: same names, first argument scout_dir
def news_headline(scout_dir, symbol, as_of) -> Result: ...
def fresh_catalyst(scout_dir, symbol, as_of) -> Result: ...
def event_before_next_session(scout_dir, symbol, as_of) -> Result: ...
def halt_open(scout_dir, symbol, as_of) -> Result: ...
def jason_tags(scout_dir, symbol, price, as_of, **kw) -> dict: ...
def market_context(scout_dir, as_of) -> dict: ...
def scanner_alerts(scout_dir, symbol, as_of, **kw) -> Result: ...
def entry_blocks(scout_dir_or_reader, symbol, as_of, **kw) -> Result: ...  # a Reader keeps its cache
```
Every public function catches `BaseException` except `KeyboardInterrupt`/`SystemExit` and returns
`unknown` with reason `"error:<ExceptionType>"`. A naive `as_of` is refused (`unknown`,
`"naive_as_of"`). `jason_tags` returns `{"on_jason_list", "near_jason_level", "jason_conflict",
"jason_market_frame"}`; `market_context` returns `{"macro_event_next_session", "inflation_regime",
"vix", "next_macro_event"}`.

### 3.2 Rules every function applies
1. **As-of** (SS 2 rule 9): a record counts only with `first_seen_at <= as_of` and, where present,
   `published_at <= as_of` (labels: `labelled_at <= as_of`). `scout_reader.records.visible(rec,
   as_of)`.
2. **Freshness**: `stale` is true unless a `beats` record exists with `at` in `(as_of - stale_s,
   as_of]` and `stopped == false`; `stale_s` is the reader argument, else `params.scout_stale_s`,
   else 120. Stale makes every value `unknown` (reason `"scout_stale"`).
3. **Params**: the last `params.jsonl` line with `first_seen_at <= as_of`; none means `unknown`
   (`"no_params"`).
4. **Format**: a record with a missing or unknown `format` is ignored and noted (`"unknown_format"`);
   a file whose every line is unreadable counts as missing.
5. **Files read**: the ET-date files covering the window only (`facts`, `health`, `beats`, `answers`,
   `edgar\due`), plus `params.jsonl`, `jason\snapshots.jsonl`, `macro\history.jsonl`.
6. **Source status**: a source whose status in the governing beat is not `ok` makes every answer that
   depends on it `unknown` (`"source_not_ok:<name>"`). `Reader.market_context` passes the beat to
   the macro module, so this holds for nowflation, BLS and Cboe through the public API too. Every
   beat ages the polled feeds (kind wire, filing, halt; halts inside active hours only): one whose
   `last_ok` is older than its gap limit is published with `gap_open: true`, so a beat written while
   the main loop sits in a long job never claims coverage nobody is polling.

### 3.3 `records.py` (scaffold)
`read_jsonl(path) -> list[dict]` (ignores a last line without `\n` and lines that fail to parse,
including one nested too deeply to parse (`RecursionError`)); `read_json(path) -> dict | None`; `KNOWN_FORMATS = {1}`; `visible(rec, as_of) -> bool`;
`day_files(folder, start, end) -> list[Path]` (ET dates via `_et`); `last_params(scout_dir, as_of)`;
`governing_beat(scout_dir, as_of, stale_s) -> dict | None`; `result(value, evidence=(), stale=False,
reasons=()) -> dict`; `UNKNOWN(reason) -> dict`; file-name constants matching section 5.

### 3.4 `calendar.py` (scaffold)
Provenance header: copied from Trade Guardian `trade_guardian/clock.py`, commit
`72dccdc2e4f865ba9c50848fa49e6717a80712ed`, sha256
`7c3cf51168d48c4bc1a5bda13d947aae60196dde53365527f448b723e4f9206d`. Copies `HOLIDAYS`,
`EARLY_CLOSES`, `CALENDAR_YEARS`, `PRE_OPEN`, `CORE_OPEN`, `CORE_CLOSE`, `POST_CLOSE`,
`EARLY_CORE_CLOSE`, `EARLY_POST_CLOSE`, `calendar_covers`, `is_holiday`, `is_early_close`,
`is_trading_day`, `next_trading_day`, `previous_trading_day`, `et_datetime`, `session_times`,
`core_open`, `core_close`, `post_close`; ET via `_et`. Adds `prior_close_before(as_of) -> datetime`
(the close of the last trading day whose close is <= `as_of`, 13:00 on early closes) and
`next_open_after(as_of) -> datetime | None` (next trading day's 09:30 ET strictly after `as_of`;
`None` when the calendar does not cover it).

### 3.5 Gate 6 (`gates.fresh_catalyst_from`, unit `answers`)
Window `[close of previous_trading_day(trading_day_of(as_of)), as_of]`: the prior trading day's close
taken from the trading day `as_of` belongs to (Guardian's 20:00 ET rollover), so the 19:40 check on
a Monday looks back to Friday's close (SS 7.1), not to Monday's own 16:00. `true` when a visible fact names S, `kind in
("wire", "filing")`, `published_at` and `first_seen_at` in the window, `class in
params.hold_catalyst_classes`. `false` when none, every enabled source with `gate6 = true` has no gap
over the window (`coverage.gaps(...) == []`) and Scout is fresh. Otherwise `unknown`.
`news_headline` is the same search with any class (`classes` lists what matched).

### 3.6 Gate 5 (`gates.event_before_next_session_from`, unit `answers`)
Window `[as_of, next_open_after(as_of))`. Sources (SS 7.2): (1) visible `earnings_date` wire or filing facts (never `discord`)
naming S with `published_at >= as_of - earnings_lookback_days` (`first_seen_at` when `published_at`
is null), date parsed by `datetext.announced_date(title, published_at)`: a date in the window means `true`, an unparseable matching title means
`unknown`; (2) the latest visible `edgar\due` record for S with `computed_at` on as_of's ET trading day:
status not `ok`, `nt_recent`, today or the next trading day inside `[due_start, due_end]`, or overdue
means `unknown`; (3) enabled calendars (none in v1); (4) since 2026-11-01, with
`params.discord_gate5_enabled`, Paper Gains Discord earnings lists (2.7), which add `true` only.
`false` only when (1) found nothing, (2) places S outside its window, and `next_open_after` is not
`None`; (4) never takes part in `false`. If `params.hold_block_macro_events` is true a
macro event in the window also means `true` (default false).

### 3.7 `coverage.py` (unit `wire`)
`gaps(health_records, source, start, end, *, max_gap_s) -> list[dict]` (each `{"from", "to",
"why"}`; why in `failed_poll | no_overlap | interval | no_poll_at_start | no_poll_at_end`); interval
rule D7 using each record's `spacing_s`; `sources_for_gate6(beat) -> list[str]`. Owner, 2026-11-04:
a failed poll is a gap only when the next good poll does not overlap or the good-to-good interval is
over the limit, or no good poll follows it yet (one timed-out EDGAR poll followed by an overlapping
poll is not a gap). 0.2.1: `allowance_s(max_gap_s, spacing_before, spacing_after, failed, overlap)`
is the limit between two good polls: max(`max_gap_s`, 2 x the larger spacing), plus each failed
poll's own spacing (`failed_spacings`) when 1 to `MAX_FORGIVEN` (3) failed between them and the
later poll overlaps; four or more failures in a row stay gaps, up to the next good poll. `gaps`
checks only the feeds passed in `feeds` (params `gate6_feeds`; all feeds when none or an empty
list) and uses only polls that ended by the window's end. `scout.health` uses the same function, and a test drives the
writer and the reader over the same random sequences (with and without restarts) and checks that
they agree. The rule runs on `Poll` tuples (`chain_gaps`), walked from the last good poll
before the window, which gives the same gaps as a walk over every poll (a test), so `entry_blocks`
can keep a 3-day chain parsed in its cache. 0.2.2 (owner, 2026-11-04): `gaps(..., overlap_max_gap_s=)`
and `chain_gaps(..., overlap_max_gap_s)` take params `source_overlap_max_gap_s` (missing or not a
positive number: the 0.2.1 rule alone); `Poll` is (start, good, overlap, spacing_s, seen);
`seen_overlap(rec)` reads `seen_items` > 0, or for records before 0.2.2 `new_items` < `items`;
0.2.2 review: `Poll` is (start, good, overlap, spacing_s, seen, not_modified, lost) (`POLL_LEN` 7;
`blocks` keeps its own fields after it), `page_lost(result, http_status)` marks a failed poll that
received a page it did not read, a 304 after one has no overlap, and `overlap_cap` accepts only a
finite cap in (0, 86400];
`interval_gap(...)` is the one rule for the interval between two good polls, used by the reader and
by `scout.health` (section 20).

### 3.8 `halts.py`, `macro.py`, `jason.py`
- `halt_open_from(facts, beat, symbol, as_of) -> Result` (`true` with a visible halt fact and no
  visible resumption at or before `as_of`; `false` only when `nasdaq_halts` is `ok`; else `unknown`).
- `market_context_from(history, facts, params, as_of) -> dict` (`inflation_regime` from the latest
  visible nowflation record with `status == "ok"`, else `unknown`; `vix` latest close with its date;
  `next_macro_event` and `macro_event_next_session` over gate 5's window; `unknown` when neither
  nowflation nor BLS/FOMC data is fresh, SS 7.3).
- `jason_tags_from(snapshot, symbol, price, as_of, near_pct, frame_prices) -> dict` (the setups
  current on as_of's ET date; `near_jason_level` counts every reading of an ambiguous level and says
  so; `jason_conflict` carries reason codes from the ported `conflict_for` rules, recomputed in pure
  form; `unknown` when no snapshot is visible). Damage or a future format in `jason\snapshots.jsonl`
  turns a `false` tag into `unknown` (rule 4), as for every other answer.

### 3.9 `blocks.py` and `cache.py`: Radar's buy-signal blocks (owner, 2026-11-04)
SCOUT_SPEC 7.8; the squad-split proposal in Radar's `docs/SQUAD_SPLIT_PROPOSAL.md` (decisions A, B, C, D).
- `entry_blocks_in(...)`: the blocks for S at as_of: class facts (`offering`, `reverse_split`,
  `listing_notice`, `bankruptcy`; wire and filing kinds only) first seen on or after
  `lookback_start(as_of, 3)` (20:00 ET of the trading day before the first of the 3 trading days);
  `halt_open` from `halts.open_halts` for every halt in the facts read with no resumption by as_of,
  also one that began on an earlier ET date (reason `halt_unresolved_since:<date>`); `macro_window`
  for `cpi`, `fomc`, `nfp` events of the latest visible nowflation and BLS history records and the
  params' FOMC table with `event_at` in [as_of - 15 min, as_of + 5 min].
- Unlike every other answer, rules 2 and 6 of 3.2 do not hide evidence: blocks on file give `true`
  with Scout stale (`stale: true`, reasons `block_on_file`, `scout_stale`). `false` needs a fresh beat,
  params, every gate-6 source `ok` and gap-free over [lookback start, as_of], `nasdaq_halts` `ok`
  with no gap open, no `nasdaq_halts` poll in [lookback start, as_of] that skipped a bad row
  (`halt_rows_skipped`, read from the health `error` that `scout.halts` starts with "skipped "), a
  fresh macro calendar (as 3.8's `covered`), and no damaged line (rule 4).
- A 424B2 fact stored as `offering` before the class fix never blocks; a filing fact whose `items`
  hold 1.03 is a `bankruptcy` block whatever class it was stored with (`blocks._block_class`).
- The halt and fact search reads the facts files of the lookback window only (not the 30 days
  `halt_open` reads); an older halt still open is outside it and simply not seen.
- `cache.FileCache`: one per Reader. A view of a file (facts: only the records a block can come
  from, indexed by ticker; health: one `Poll` per record with its end, source and feed; beats;
  `macro\history.jsonl`) is keyed on (path, size, mtime_ns). On growth only the appended bytes are
  parsed, after checking that the 4 KiB before the old end are unchanged; otherwise, or when the file
  shrank, it is parsed again from the start. Damage notes follow 3.2 rule 4. A lock makes one Reader
  safe across threads; at most 96 views are kept. The other answers do not use the cache: they read
  afresh as before. Measured on the live install's files of 1-3 Nov 2026 (10.8 MB facts, 4.4 MB
  health a day): first call 0.35 s, repeated calls about 0.02 s.

## 4. Folder and write rules
`ScoutPaths(root)`; runtime folders under `root`: `state\{facts,health,answers,labels,llm,macro,
raw,beats,edgar\due,jason,levels,verify,discord}`, `logs`, `reports`, `inbox\jason`, `inbox\discord`. `levels_dir` and
`inbox_dir` are config, relative to `root`. Every writer takes a `ScoutPaths` and calls
`writable()`. Reads of `radar_dir` and `pilot_dir` are open-read-close in `scout.inputs` and
`scout.levels_bars` only. Nothing reads any `.env` but Scout's own, any `conf\`, or any token file.

## 5. Records (exact JSON keys)

Every record has `format: 1`. Times are `"YYYY-MM-DDTHH:MM:SSZ"`. Daily files are named by the ET
trading date of the record's main time (`first_seen_at`, `start`, `at`, `as_of`). `null` is written
for an absent value; keys are never omitted.

### 5.1 Fact (`state\facts\<date>.jsonl`)
`format, fact_id, kind ("wire"|"filing"|"halt"|"scheduled_event"|"macro_event"|"discord"|"earnings_schedule"|"scanner"), ticker (str|null),
cik (int|null), class (str|null), source, feed, guid, url, title, published_at, first_seen_at,
raw_sha256, exchange (str|null), form (str|null), items (list[str]), accession (str|null),
halt_code, halt_time, resumed_at, halt_market, event_at, event_kind, event_source, sources_agree
(bool|null), extra (dict)`. Wire `class` from SS 6.4; filing `class` from the filing table (`null`
when no row applies). PR Newswire titles are stored; bodies never. `discord` and
`earnings_schedule` and `scanner` facts (2.7): source `discord:<channel>`, feed the channel, guid the message id
(`<message_id>|<date>|<session>` for a schedule fact), url the permalink, published_at the post's
time; `extra` as in 2.7.

### 5.2 Health (`state\health\<date>.jsonl`)
`format, source, feed, kind, enabled, gate6, start, end, result ("ok"|"not_modified"|"http_error"|
"timeout"|"network"|"schema"|"too_large"|"refused"|"backoff"|"no_http"|"host_cap"), http_status,
items, new_items, overlap (bool|null), spacing_s, gap_before (bool), error (str|null), seen_items
(int|null; 0.2.2: distinct ids on a good page already seen in the feed and below every new id on
the page, 0 for a 304; null for a failed poll; absent in records written before 0.2.2)`. A 304
after a lost page (section 20) has `overlap` false.

### 5.3 Beat (`state\beats\<date>.jsonl`) and heartbeat (`state\scout.heartbeat`)
Beat: `format, at, stopped, sources ({name: status}), gap_open ({name: bool})`. `gap_open` is aged
at each beat (`SourceStatusBoard.states_at`, rule 6 in 3.2); the heartbeat's `sources` too.
Heartbeat (SS 10.1): `format, at, version, stopped, sources ({name: {enabled, status, last_ok,
last_error, gap_open}}), llm ({enabled, spent_today_usd, cap_reached}), levels ({current_files,
expires}), calendar_covers_next_45d`.

### 5.4 Params (`state\params.jsonl`)
`format, first_seen_at, config_sha256, hold_catalyst_classes, earnings_lookback_days,
earnings_due_start_days, earnings_due_end_days_quarter, earnings_due_end_days_annual,
hold_block_macro_events, scout_stale_s, source_max_gap_s, jason_near_level_pct, gate6_sources,
gate6_feeds, fomc_dates, fomc_decision_time_et, discord_gate5_enabled` (the last since 2026-11-01:
`discord_enabled and discord_gate5_enabled`). `gate6_feeds` is {source: [feed, ...]} (each EDGAR
form; "rss" for a wire): a listed feed with no health record is a `missing_feed` coverage gap.

### 5.5 Answer (`state\answers\<date>.jsonl`)
`format, symbol, as_of, written_at, origin ("pilot"|"radar"|"watch"|"manual"), fresh_catalyst,
event_before_next_session, macro_event_next_session, news_headline, halt_open, reasons ({gate:
list[str]}), evidence ({gate: list[str]}), stale`.

### 5.6 Due window (`state\edgar\due\<date>.jsonl`)
`format, ticker, cik, computed_at, status, last_periodic_form, last_periodic_accession, report_date,
fiscal_year_end, next_period_end, next_is_fiscal_year_end, due_start, due_end, overdue, nt_recent,
nt_accession, submissions_fetched_at`.

### 5.7 Label (`state\labels\<date>.jsonl`) and LLM call (`state\llm\<date>.jsonl`)
Label: SS 8.3 fields (`ticker, catalyst_type, direction, dilution_risk, is_new_information,
scheduled_event, confidence, evidence_ids, summary`) plus `format, job, fact_id, status
("labelled"|"unlabelled"), verified, check_failures, model_id, prompt_sha256, schema_version,
request_id, usage, cost_usd, labelled_at, urls, quotes, published_at, trust ("layer2")`.
LLM call: `format, job, at, model_id, effort, prompt_sha256, schema_version, request_id, usage
({input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens,
web_search_requests, web_fetch_requests}), cost_usd, estimate_usd, stop_reason, validation
("ok"|"schema_fail"|"refusal"|"max_tokens"|"api_error"|"refused_by_cap"|"no_key"|"reserved"), retry,
fact_ids, symbols`, plus `reason`, and `reservation` (on a `reserved` record) or `settles` (on the
record that replaces it).

### 5.8 Macro (`state\macro\<date>.json`, `state\macro\history.jsonl`)
History record (one per source read): `format, source ("nowflation"|"bls"|"fomc"|"cboe_vix"|"bls_data"),
first_seen_at, fetched_at, status ("ok"|"stale"|"unavailable"), reasons, source_generated_at, sha256,
events (list of {kind, event_at, prior, street, nowflation_forecast, cleveland, kalshi, extra}),
realtime ({realtime_yoy_pct, change_1d, change_5d, change_20d, cpi_gauge_yoy_pct,
official_cpi_yoy_pct, official_cpi_month, live_share_pct, avg_input_age_days} | null),
inflation_regime, forecast_lean, vix ({date, close} | null), attribution, results` (bls_data only, else
null: payrolls, unemployment, cpi, jobs_trend, jobs_trend_avg_k; SS 7.7). The daily file adds `results`.
Daily file: `format, at, inflation_regime, regime_status, vix, events (merged, with sources and
sources_agree), forecast_lean (from the fresh nowflation record; the brief shows it as
"informational, untested", SS 7.3), sources ({name: {status, reasons, fetched_at}})`.
nowflation's `source_generated_at` is the older of the calendar's and `realtime.json`'s
`generated_at`, so `stale_after` follows SS 7.3 for both files.

### 5.9 Levels (`state\levels\<session>.<kind>.json` + `.meta.json`) and Jason snapshot
Level file: the owner's file, byte-for-byte. Meta: `format, added_at, sha256, original_filename,
session_date, source_kind, warnings`.
Snapshot (`state\jason\snapshots.jsonl`): `format, first_seen_at, today_et, files, setups (list of
{symbol, direction, attribution, source_kind, session_date, expires, horizon, levels: [{kind,
readings: [str], ambiguous, note}]}), warnings`. Prices are decimal strings.

### 5.10 Verify (`state\verify\samples.jsonl`, `checks.jsonl`)
Sample: `format, week, seed, picked_at, symbols`. Check: `format, week, symbol, as_of, gate, scout,
owner ("agree"|"disagree"), note, checked_at`.

### 5.11 Discord bookkeeping (`state\discord\files.jsonl`, `state\discord\rejects\<date>.jsonl`)
Files: `format, file ("<channel>/<name>"), channel, size, mtime_ns, sha256, capture_at, ingested_at,
lines, posts_new, posts_repeat, rejects`. Rejects: `format, at, file, channel, line, reason,
message_id`. The capture files themselves are `docs/INBOX.md` section 3.

### 5.12 Pace plan (`state\pace\<ET date>.jsonl`; 0.2.4, section 21)
`format, first_seen_at, lookback_from, factor, lookback_days, min_hours, page_fill, feeds` (a list of
{`source, feed, max_items, wanted_s, polled_h, polls, hits, max_new`}, each list 25 values: ET hours
0-23 of a trading day, then closed days; `wanted_s` null for an hour polled less than `min_hours`).
Scout's own file: no reader reads it (`scout.paths.PACE`, not in `scout_reader.records`).

## 6. Configuration (`config.toml`; `config.example.toml` documents every key)

Top-level keys must come before tables. Types: str, int, float, bool, list[str], time "HH:MM",
date "YYYY-MM-DD". Values marked * are this design's additions beyond SS 13.

### 6.1 Paths and identity
| Key | Type | Default |
|---|---|---|
| radar_dir | str | "" |
| radar_handoff_subdir | str | "state/handoff" (Radar's hand-off file, `<trading day>.jsonl`; owner, 2026-11-04) |
| pilot_dir | str | "" |
| levels_dir | str | "state/levels" |
| inbox_dir | str | "inbox" |
| watch_symbols | list[str] | [] |
| contact_email | str | "" (no HTTP at all) |
| scout_user_agent | str | "Scout/{version} ({contact_email})" |

### 6.2 Sources and HTTP (global)
| Key | Type | Default |
|---|---|---|
| source_max_gap_s | int | 180 |
| source_overlap_max_gap_s | int | 1800 (0.2.2: how long a repeated seen id covers; 0 = off; in params) |
| hour_turn_sources | list[str] | ["prnewswire"] (0.2.2: wires polled faster around the hour) |
| hour_turn_start_min, hour_turn_end_min | int | 58, 5 (ET minutes; end not included; the window wraps the hour) |
| hour_turn_spacing_s | float | 30.0 (never below the host's hard minimum in `scout/http.py`; a larger configured `host_min_spacing_s` for the host bounds it at run time) |
| source_backoff_max_min | int | 30 |
| fetch_max_bytes | int | 2000000 |
| fetch_timeout_s * | float | 20.0 |
| http_default_host_spacing_s * | float | 1.0 |
| active_hours_et * | list[time] | ["04:00", "20:00"] |
| refused_after_403s * | int | 3 |
| seen_guid_cap * | int | 5000 |
| fact_dedupe_days | int | 7 |
| ticker_map_retry_s * | int | 600 (a failed read of SEC's ticker list is retried after this; added at integration) |
| edgar_atom_forms * | list[str] | ["8-K", "6-K", "S-1", "S-3", "F-1", "F-3", "424B", "EFFECT", "NT 10-Q", "NT 10-K", "10-Q", "10-K"] (10-Q and 10-K for the gate-5 check only; periodic forms are left out of `gate6_feeds`) |
| ticker_exchange_prefixes | list[str] | ["NASDAQ", "Nasdaq", "NYSE", "NYSE American", "NYSE MKT", "NYSE Arca", "OTCQB", "OTCQX", "OTC", "Cboe BZX"] |
| wire_title_ticker_suffixes | list[str] | ["Stock News"] (a wire title ending "\| XXX Stock News" names XXX; Stock Titan) |
| [host_min_spacing_s] * | table host -> float | "www.sec.gov" = 0.5, "data.sec.gov" = 0.5, "www.nasdaqtrader.com" = 60.0, "www.stocktitan.net" = 30.0 |
| [host_max_requests_per_day] * | table host -> int | "nowflation.com" = 10 (also a hard cap in code: never above 10, for nowflation.com and every host under it) |
| [class_keywords] | table class -> list[str] | SS 6.4 lists: bankruptcy (2026-11-04; may be left out), offering, reverse_split, listing_notice, fda, trial, contract, earnings_result, earnings_date (exactly these keys; `other` is implicit; ` ... ` inside a keyword matches any text; the earnings_date defaults are "<scheduling phrase> ... results/earnings" pairs, see `scout/classify.py`) |

### 6.3 `[sources.<name>]` (one table per source; every key present in the example)
Keys: `enabled` (bool), `kind` (str: wire, filing, reference, halt, macro, vix), `url` (str), `paths`*
(list[str], default []), `min_spacing_s` (float), `idle_spacing_s`* (float; 0 = same), `allowed_hosts`
(list[str]), `terms_note` (str), `max_items`* (int; feed maximum for the overlap rule, 0 = unknown),
`gate6`* (bool), `terms_ok`* (bool; false keeps titles/links/times only).

| name | enabled | kind | url | min / idle spacing | allowed_hosts | max_items | gate6 |
|---|---|---|---|---|---|---|---|
| edgar_atom | true | filing | https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type={form}&company=&dateb=&owner=include&start=0&count=100&output=atom | 60 / 600 | www.sec.gov | 100 | true |
| edgar_submissions | true | reference | https://data.sec.gov/submissions/CIK{cik:010d}.json | 0 / 0 (on demand) | data.sec.gov | 0 | false |
| edgar_tickers | true | reference | https://www.sec.gov/files/company_tickers_exchange.json | 86400 / 86400 | www.sec.gov | 0 | false |
| prnewswire | true | wire | https://www.prnewswire.com/rss/news-releases-list.rss | 60 / 60 | www.prnewswire.com | 20 | true |
| stocktitan | true | wire | https://www.stocktitan.net/rss | 60 / 600 | www.stocktitan.net | 100 | true |
| nasdaq_halts | true | halt | https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts | 60 / 0 (not polled 20:00-04:00 except the one dated read) | www.nasdaqtrader.com | 0 | false |
| nowflation | true | macro | https://nowflation.com, paths ["/data/release_calendar.json", "/data/realtime.json", "/forecasts/index.json", "/nowflation.ics"] | schedule-driven | nowflation.com | 0 | false |
| bls | true | macro | https://www.bls.gov/schedule/news_release/bls.ics (UNVERIFIED path; confirm at build step 2) | 604800 | www.bls.gov | 0 | false |
| cboe_vix | true | vix | https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv | 86400 | cdn-api.cboe.com | 0 | false |
| bls_data | true | macro | https://api.bls.gov/publicAPI/v1/timeseries/data/ (+ series id) | schedule-driven | api.bls.gov | 0 | false |

`terms_ok` is false for `prnewswire` until the owner reads its terms, and for `stocktitan` (its items
carry only a title, link, guid and time, so nothing is lost).

**Stock Titan was added (2026-11-04, owner).** It reposts releases from GlobeNewswire, Business Wire,
PR Newswire and the smaller wires within minutes, each title ending "| XXX Stock News". Its terms ban
bots in general but name "our provided RSS feeds" as an authorised way to collect data, for personal
non-commercial use; robots.txt allows `/rss`. The feed allows 30 requests in 5 minutes
(`RateLimit-Policy: 30;w=300`) and keeps the newest 100 items (about 100 in 20 minutes at a busy
open), so a 60 s poll overlaps. Only the feed is read: `stocktitan.net` is not in
`llm_allowed_domains`.

**GlobeNewswire was removed (2026-11-03, owner).** Its feed answered 403 to Scout's declared
User-Agent on 2026-11-01 and again on 2026-11-03 (Akamai bot protection), and reading it would need
GlobeNewswire's permission. The owner's rule: Scout uses only sources that allow automated reading
without asking. Gate 6 rests on `edgar_atom`, `prnewswire` and (from 2026-11-04) `stocktitan`. Business Wire (feed deactivated;
robots.txt disallows `/rss/`) and ACCESS Newswire (robots.txt disallows `/rss`) were checked the same
day and are out for the same reason. Older beats and health records may still name `globenewswire`;
the reader ignores any source the params do not name.

### 6.4 Answers
| Key | Type | Default |
|---|---|---|
| hold_catalyst_classes | list[str] | ["fda", "trial", "contract", "earnings_result"] (contract_or_other left out, owner, 2026-11-04) |
| earnings_lookback_days | int | 45 |
| earnings_due_start_days | int | 10 |
| earnings_due_end_days_quarter | int | 50 |
| earnings_due_end_days_annual | int | 95 |
| periodic_max_age_days * | int | 200 |
| nt_recent_days * | int | 45 |
| hold_block_macro_events | bool | false |
| fomc_dates | list[date] | 2026 decision days: 2026-01-28, 2026-03-18, 2026-04-29, 2026-06-17, 2026-07-29, 2026-09-16, 2026-10-28, 2026-12-09 (2027 to be added from federalreserve.gov by hand; tentative) |
| fomc_decision_time_et * | time | "14:00" |
| scan_max_price | float | 20.0 |
| answer_times_et * | list[time] | ["19:30", "19:45"] |
| radar_answers_after_et * | time | "20:00" |
| radar_answers_as_of_et * | time | "19:45" |

### 6.5 Macro
| Key | Type | Default |
|---|---|---|
| nowflation_max_age_h | float | 30 |
| nowflation_max_stale_weight_pct | float | 20 (% of the market-sourced weight; 2.3) |
| nowflation_fresh_after_et * | time | "09:15" |
| nowflation_read_times_et * | list[time] | ["08:20"] |
| nowflation_release_day_time_et * | time | "08:50" |
| macro_agree_minutes * | int | 5 |
| inflation_hot_pp | float | 0.25 (provisional; set at build from history) |
| inflation_cooling_pp | float | 0.25 (provisional; set at build from history) |
| inflation_regime_window_days * | int | 20 |
| vix_read_after_et * | time | "16:30" |

### 6.6 Readers and Jason
| Key | Type | Default |
|---|---|---|
| scout_stale_s | int | 120 |
| jason_near_level_pct | float | 0.01 |
| jason_market_symbols * | list[str] | ["SPY", "QQQ"] (also in `params.jsonl`, so the reader uses the value in force at t) |
| levels_long_horizon_sessions * | int | 10 (the scorer's minimum window for a weeks/months/quarter horizon; added at integration) |
| levels_score_min_scored * | int | 20 |
| levels_tolerance_atr_mult * | float | 0.5 |
| levels_tolerance_floor_pct * | float | 0.0075 |
| levels_null_draws * | int | 60 |
| levels_null_window_bars * | int | 126 |
| levels_null_seed * | str | "scout-levels-v1" |

### 6.7 LLM
| Key | Type | Default |
|---|---|---|
| llm_enabled | bool | false |
| llm_daily_cost_cap_usd | float | 1.00 |
| llm_monthly_cost_cap_usd | float | 20.0 |
| j1_max_calls_per_day | int | 400 |
| j3_max_calls_per_day | int | 15 |
| web_search_max_per_day | int | 30 |
| llm_allowed_domains | list[str] | ["sec.gov", "prnewswire.com", "businesswire.com", "accesswire.com", "accessnewswire.com", "newsfilecorp.com", "thenewswire.com", "nasdaq.com", "fda.gov", "clinicaltrials.gov"] |
| llm_item_chars | int | 2000 |
| web_fetch_max_content_tokens | int | 8000 |
| web_search_max_uses * | int | 3 |
| j1_batch_size * | int | 20 |
| j1_interval_s * | int | 120 |
| j3_time_et * | time | "19:30" |
| j3_no_fact_after_s * | int | 600 |
| llm_timeout_s * | float | 120.0 |
| [llm.jobs.J1] | enabled true, model "claude-haiku-5-5", effort "low", max_tokens 4000 |
| [llm.jobs.J3] | enabled true, model "claude-sonnet-5-5", effort "medium", max_tokens 4000 |
| [llm.jobs.J4] | enabled true, model "claude-sonnet-5-5", effort "medium", max_tokens 3000 |
| [llm.prices] | `web_search_usd_per_1000` 10.0; one subtable per model id with input_per_mtok, output_per_mtok, cache_write_per_mtok, cache_read_per_mtok: haiku-5-5 0.10 / 0.50 / 0.125 / 0.01; sonnet-5-5 2.00 / 10.00 / 2.50 / 0.20; opus-5-5 4.00 / 20.00 / 5.00 / 0.20. Checked against the Models API and price list at build step 5 (SS 8.1 item 5). A model with no price row: the ledger refuses the call. |

### 6.8 Schedule, verification, loop, Webull
| Key | Type | Default |
|---|---|---|
| brief_time_et | time | "08:20" |
| verify_days | int | 20 |
| verify_time_et * | time | "20:30" (3 hours earlier on early closes; added at integration) |
| verify_sample_size * | int | 10 |
| verify_sample_weekday * | str | "Mon" |
| heartbeat_interval_s * | int | 30 |
| loop_interval_s * | float | 1.0 |
| log_level * | str | "INFO" |
| scout_webull_enabled | bool | false (true is a ConfigError in v1) |
| scout_webull_budget_per_min | int | 2 |

### 6.9 The inbox and the Paper Gains Discord (2026-11-01; section 2.7)
| Key | Type | Default |
|---|---|---|
| inbox_poll_s | int | 60 |
| discord_enabled | bool | true |
| discord_channels | list[str] | daily-watchlist, weekly-trade-plan, free-analysis, brianna-news, stockmktnewz, major-headlines, earnings-whispers, market-movers, open-outcrier, 15m-scanner, daily-scanner, weekly-scanner |
| discord_schedule_channels | list[str] | daily-watchlist, open-outcrier, market-movers |
| discord_prefix_ticker_channels | list[str] | brianna-news |
| discord_headline_channels | list[str] | major-headlines, stockmktnewz |
| discord_catalyst_channels | list[str] | brianna-news |
| discord_reaction_channels | list[str] | earnings-whispers, open-outcrier |
| discord_gate5_enabled | bool | true (block only; in the params) |
| discord_text_max_chars | int | 2000 |
| discord_image_text_max_chars | int | 4000 |
| discord_max_post_age_days | int | 14 |
| discord_posted_at_tolerance_s | int | 120 |
| discord_schedule_max_ahead_days | int | 14 |
| discord_max_file_bytes | int | 2000000 |
| discord_expected_every_min | int | 40 |
| discord_cadence_channels | list[str] | the six MARKET NEWS channels ([] = every channel) |
| discord_capture_hours_et | list[time] | ["04:00", "20:00"] (trading days) |
| discord_brief_max_items | int | 10 |
| discord_scanner_brief_max_tickers | int | 12 (tickers per scanner pattern line in the brief) |

Every channel list must be a subset of `discord_channels`; channel names are lower case.

### 6.10 The polling pace (`[pace]`; owner, 2026-11-10; section 21)
| Key | Type | Default |
|---|---|---|
| pace.enabled | bool | true (false: every source at its fixed spacing, as before 0.2.4) |
| pace.factor | float | 0.5 (0.05 to 1.0) |
| pace.lookback_days | int | 7 (2 to 60) |
| pace.recalc_time_et | time | "03:30" |
| pace.min_hours | float | 2.0 (0.25 to 1000) |
| pace.page_fill | float | 0.5 (0.05 to 1.0) |
| [pace.slowest_active_s] | table "source" or "source:FORM" -> float | "edgar_atom" 300; "edgar_atom:NT 10-Q", ":NT 10-K", ":10-Q", ":10-K" 1800; "prnewswire" 120; "stocktitan" 120 |
| [pace.slowest_idle_s] | table, as above | "edgar_atom", "prnewswire", "stocktitan" 1800 |

Each value 0 (not paced) or 30 to 3600; only wire and filing sources; a form must be one of
`edgar_atom_forms` (a default line for a form not polled is unused). A table given in config.toml
replaces the default table.

## 7. Facts, health and coverage flow (one poll)
1. The job's `due(now)` uses the source's spacing for `now` (`health.spacing_in_force`:
   `active_hours_et`, and for `hour_turn_sources` the hour turn). `run_due` asks each job at the
   clock's time when its turn comes. The wire jobs also run between EDGAR's form requests.
2. `fetcher.fetch(source, url)` -> `FetchResult`.
3. Body parsed; `SchemaError` -> health `result = "schema"`, source status `unavailable`, no facts.
4. `CoverageTracker` computes `overlap` (a guid already seen, or fewer items than `max_items`; a 304
   counts as overlap) and `gap_before` (D7), writes the health record, updates the board. After a
   restart its seen guids and last good poll come from the seed (`scout/coverage_seed.py`).
5. New items -> raw store -> normalise tickers, class -> one `Fact` per ticker -> `FactSink.add`.

## 8. Main loop and commands
`python -m scout run`: `build_context`, `load_env`, `setup_logging`, lock, startup warnings
(calendar coverage of today + 45 days, empty `contact_email`, empty `radar_dir`/`pilot_dir`, no current
levels file), `write_params`, then the loop of section 2.1. Jobs (owner unit):

| Job | When | Unit |
|---|---|---|
| edgar_tickers | start, daily 03:50 ET | wire |
| pace_start, pace | at start when the saved plan is stale; daily at `pace.recalc_time_et` (03:30 ET), closed days too (0.2.4, section 21) | pace |
| edgar_atom | 60 s 04:00-20:00 ET, 600 s otherwise; the due wire jobs run between its form requests (0.2.2); since 0.2.4 each form at its pace (section 21) | wire |
| prnewswire, stocktitan (every source of kind wire) | 60 s (stocktitan 600 s 20:00-04:00 ET); prnewswire 30 s from :58 to :05 ET inside 04:00-20:00 ET (0.2.2); since 0.2.4 at their pace (section 21) | wire |
| nasdaq_halts | 60 s 04:00-20:00 ET (the job is checked every 1 s; the poller keeps the 60 s from the real send); once after 20:00 ET with `haltdate` | macro |
| nowflation | start; 08:20 ET; 08:50 ET on release days (at most 10 requests a day) | macro |
| bls | start, then every `[sources.bls] min_spacing_s` | macro |
| fomc | start, then daily (the table in config) | macro |
| cboe_vix | trading days after 16:30 ET | macro |
| bls_data | start unless read today; 08:40 ET daily; 09:15 ET on CPI, NFP or PCE days (at most 12 requests a day) | macro |
| jason_snapshot | start; 00:05 ET; on a change in `levels\` | levels |
| discord_inbox | every `inbox_poll_s` (60 s); status at start (2.7) | inbox |
| answers_pilot | 19:30 and 19:45 ET (16:30, 16:45 on early closes) | answers |
| answers_radar | after 20:00 ET (17:00) with as_of 19:45 ET (16:45) | answers |
| llm_j1, llm_j3 | only with `llm_enabled` | llm |
| brief | `brief_time_et` (08:20 ET) trading days, as of the moment it is written | llm |
| verify | `verify_time_et` (20:30 ET) trading days | llm |
| ledger_status | every 60 s (LLM spend and cap on the heartbeat) | llm |
| heartbeat | every 30 s (inside the loop, not a job) | scaffold |

Commands (`scout/cli.py`; global `--config PATH` default `config.toml` in Scout's folder, `--version`):
- `run`
- `brief [--date YYYY-MM-DD]` (writes the deterministic brief now; J4 only when the LLM is on)
- `levels add <file> [--yes]`, `levels add --inbox` (2.4, 2.7)
- `discord check <file>` (one capture file checked as the job reads it; writes nothing)
- `levels score [--bars radar|csv] [--csv-dir PATH] [--since YYYY-MM-DD]`
- `check [--week YYYY-Www]`
- `verify [--from YYYY-MM-DD] [--to YYYY-MM-DD]`
- `answers <SYMBOL>... [--as-of ISO-UTC]`

Launchers (each `cd /d "%~dp0"`, runs `.venv\Scripts\python.exe -m scout ...`, pauses with a
plain-English message): **Start Scout** (`run`; exit code 4 says another Scout is running in
another window), **Scout Brief** (`brief`, then opens the report in
Notepad), **Add Levels** (`levels add "%~1"`, or the newest file in `inbox\jason\` when no file is
dropped on it), **Update Scout** (`python -m scout.running` stops when Scout runs; then one
parenthesised block ending in `exit /b`, read whole by cmd before `git pull` can replace the file:
`git pull`; `pip install -q -e ".[dev]"`; `python -m scout --version`).

## 9. Clock and time
- `Clock` protocol, `SystemClock`, `ManualClock` (section 2.1). Every module takes a clock; nothing
  calls `datetime.now()` except `SystemClock`.
- ET only through `scout_reader._et` (`utc_to_et(dt)`, `et_to_utc(naive_et_dt)`, `et_offset(dt)`);
  an ambiguous or missing local time (the DST hour) resolves as `zoneinfo` does with `fold=0`.
- Brisbane is fixed UTC+10 (`scout.clock.to_brisbane`).
- Daily files: the ET calendar date of the record's main time (not the 20:00 trading-day rollover),
  so a reader can find a record from its time alone.
- Calendar coverage: startup and the 08:20 brief warn while `calendar_covers(today + 45 days)` is
  false; gate 5 answers `unknown` when `next_open_after(as_of)` is `None`.

## 10. HTTP layer (`scout/http.py`)
`HttpFetcher.fetch(source, url, *, conditional=True, max_bytes=None) -> FetchResult`:
1. `cfg.http_allowed()` false (empty `contact_email`) -> `result = "no_http"`, no socket opened.
2. Scheme must be `https`; host must be in the source's `allowed_hosts`; otherwise `refused`.
3. Per-host daily cap (`host_max_requests_per_day`) -> `host_cap`.
4. Per-host spacing: waits (via the clock's `sleep`) until `max(host_min_spacing_s[host] or
   http_default_host_spacing_s, hard minimum, backoff)` has passed since the host's last request.
   Hard minimums in code: `www.nasdaqtrader.com` 60 s; `www.sec.gov`/`data.sec.gov` 0.5 s;
   `www.prnewswire.com` and `www.stocktitan.net` 30 s (SS 12; the validator also refuses a wire
   `idle_spacing_s` other than 0 or >= 30). A stored last request in the future counts as the
   spacing passed, and no wait is longer than the spacing.
5. Headers: `User-Agent: cfg.user_agent()`, `Accept-Encoding: gzip, deflate`, and when `conditional`
   the stored `If-None-Match`/`If-Modified-Since` for that URL (memory only).
6. `transport(url, headers, fetch_timeout_s, max_bytes + 1)`; a body over the cap -> `too_large`;
   gzip/deflate decoded (Radar's `_decompress`).
7. 200 -> `ok` with body; 304 -> `not_modified`; 403/429 -> `http_error` and the host's backoff doubles
   (from its spacing) up to `source_backoff_max_min` minutes, reset by the next 200/304; other
   statuses -> `http_error`; timeout -> `timeout`; other `OSError` -> `network`. No automatic retry.
8. Never logs headers, the User-Agent or the contact email; logs source, host, status, bytes, ms.
`FetchResult(source, url, result, status, body, headers, started_at, ended_at, bytes, not_modified,
error)`. Tests inject a `transport` (a dict of URL -> `RawResponse`) and a `ManualClock`.

## 11. Traceability: spec -> module -> tests

Moved to `docs/TRACEABILITY.md` (spec section -> code -> test), kept up to date as built.

## 12. Implementation units

Unit `scaffold` runs first and alone. The five others run in parallel after it and own disjoint files.
Nobody edits a file another unit owns; a unit that needs something missing stubs it in its own tests
(fakes in its own test helpers, or `tests/helpers_fakes.py` from the scaffold) and reports the need.
No unit edits `pyproject.toml`, `config.py`, `config_load.py`, `config.example.toml`, `models.py`,
`interfaces.py` or `app.py`; if one finds a missing key or field, it says so in its report instead.

Shared test helpers (scaffold): `tests/conftest.py` (`no_network` autouse, `clean_env` autouse
removing `ANTHROPIC_*`, `clock` ManualClock at 2026-11-02 14:00Z, `et(day, "HH:MM")`, `scout_dir`
tmp folder with `ScoutPaths.ensure()`, `cfg` from `config_from_dict({"contact_email":
"test@example.invalid"}, scout_dir)`, `paths`, `write_audit`), `tests/helpers_fakes.py`
(`FakeTransport`, `FakeFetcher`, `FakeRawSink`, `FakeFactSink`, `FakeHealthSink`, `FakeBoard`,
`FakeTickerMap`, `FakeSubmissions`, and `write_records(scout_dir, kind, records)` to lay out state
files for reader tests).

## 13. As built (integration, 2026-11-01)

The six units were joined and run end to end. What the integration changed or settled:

- **Main loop.** `python -m scout run` loads every job module strictly at integration test time
  (`tests/test_wiring.py` builds all jobs from a real `AppContext`). Each job runs inside its own
  try/except (`scheduler.run_due`), so one failing source never stops the others or the heartbeat.
  `run()` also takes `until=` (a clock time) for tests; the launcher never passes it.
- **End-to-end test** (`tests/test_e2e_day.py`). Monday 2026-11-02, 03:55 to 20:40 ET on a
  ManualClock, Scout built from a `config.toml` on disk, the real `HttpFetcher` over a fake
  transport serving the offline fixtures, a scripted Claude transport (`llm_enabled = true`), and
  stand-in Radar and Pilot folders. It checks facts of every kind, health, beats, macro files, the
  Jason snapshot, params, answers for Pilot, watch and Radar symbols (gate 6 `true` for a release
  after the close, `false` with clean coverage otherwise), the brief with J4, the verify report, LLM
  call records, nothing written outside Scout's folder, and the `answers` and `brief` commands on
  the day's files. A second short run at the real 10 s loop has one feed raising: the others keep
  polling and the heartbeat keeps its 30 s beat. The quiet core session (09:30-15:30) is skipped to
  keep the test near 30 s, because each append costs about 4 ms on Windows.
- **Brief as of its own time.** The brief at 08:20 runs in the same tick as the 08:20 nowflation
  read, and `macro\<date>.json` is rewritten through the day, so the brief said "No macro data yet".
  Now the brief job writes as of the moment it runs, and `brief_sections` rebuilds the macro view
  from `macro\history.jsonl` lines fetched at or before its as-of (`scout.macro.daily_as_of`); the
  daily file is only a fallback. A past `--date` brief therefore shows the macro view of that day.
- **One open per append.** `store.append_jsonl` opens the file once ("a+b") to check the last byte
  and append, instead of twice.
- **Config.** Validation moved to `scout/config_validate.py` (config.py was at the size limit).
  Three keys replaced unit constants: `ticker_map_retry_s`, `levels_long_horizon_sessions`,
  `verify_time_et`; `jason_market_symbols` joined the reader params.
- **`scout_reader`** re-exports `hold_allowed`.
- **Constants kept in code on purpose** (rules from the spec or market facts, not tunables):
  the hard per-host spacing minimums (`http.HARD_MIN_SPACING_S`), the early-close shift of 3 hours,
  the J1/J3 post-check bounds (quote at least 10 characters, dates -7..+200 days, SS 8.4), the
  7-day window for pairing one macro release across sources, the brief's list lengths (40
  catalysts, 30 setups), the J3 gap check every 60 s, the Jason snapshot at 00:05 ET and its 300 s
  folder check, and the halt reader's 7-day look-back.

Unit departures from this design that stand (each unit's report has the detail):
`HttpFetcher` never follows a redirect (3xx is `http_error`) and does not sleep through a backoff
(it returns `backoff`); raw items are stored one file per item; wire bodies are cut to title, link,
guid and time while `terms_ok` is false; EDGAR polling waits for the ticker map; `first_seen_at` is
when the page arrived; macro history lines carry `stale_after`; a corrupt or partial line in any file
an answer read turns a `false` into `unknown` (a `true` stays); `due_window` takes `now`;
`Reader(now=...)` refuses an as-of in the future; LLM call records carry `reason`; J1 and J3 replies
wrap label schema v1 and use short item ids mapped back to fact ids.

## 14. Second review (2026-11-01)

A second review found 23 defects; each fix has a regression test that fails on the code before it
(`docs/TRACEABILITY.md`, "Second review"). Behaviour that changed:

- **Wire classes.** The `earnings_date` keywords are narrow pairs with a ` ... ` gap (2.2,
  `scout/classify.py`): date notices worded "Will Host Conference Call on ... to Discuss Financial
  Results", "Announces Timing of", "Schedules Conference Call and Webcast for" and "Sets Date for
  Release of" are `earnings_date`, and contract or results headlines with "to release", "will
  announce" or "conference call to discuss" keep their class.
- **Filings.** An 8-K/A is never `earnings_result` or `contract_or_other` (blocking classes kept).
  A filing is one fact per ticker under its CIK. A ticker under two CIKs gets no due window
  (`ambiguous_ticker`). 10-Q and 10-K are polled for the gate-5 check and are not gate-6 coverage.
- **Halts.** The job is checked every second (it polled every 120 s); a failed fact write is tried
  again; a clock stepped back does not stop the polls.
- **Heartbeat.** Polled feeds are aged at every beat, so the Pulse never publishes coverage the
  loop is not polling.
- **HTTP.** No sleep longer than the spacing after a clock step; future times clamped on load; wire
  hosts have a 30 s hard minimum and the validator checks wire `idle_spacing_s`.
- **Macro jobs.** The daily jobs are seeded from the macro history, so a restart after a daily read
  time does not fetch Cboe or nowflation again. nowflation's staleness uses the older of its two
  files' `generated_at`. The daily file and the brief show the forecast lean.
- **Reader.** `market_context` applies rule 6 through the public API; the jason tags apply rule 4;
  `read_jsonl` and `read_json` skip a line nested too deeply instead of raising.
- **Verification.** One check per symbol-day; the weekly sample is topped up through the week.
- **LLM.** J3's web tools are `allowed_callers: ["direct"]` (no code-execution sandbox, SS 8.1
  rule 1); the estimate is the tool loop's worst case; every call is reserved in the ledger before
  it is sent; an unreadable ledger refuses calls; a timed-out call counts its searches.

Still open (owner or later steps): live captures to replace the `_SYNTHETIC` fixtures (SS 15 step
2) except nowflation's, which stay synthetic in its live layout because its terms forbid
redistributing its raw files (live captures stay only in the install's `state\raw`, see
`tests/fixtures/macro/README.md`),
and whether `type=424B` returns every 424B* form (D6); the LLM model ids and prices (step 5); the
first real Claude call against `anthropic==2.3.0` (step 5).

## 15. Third review (2026-11-01)

A third review found 14 defects; each fix has a regression test that fails on the code before it
(`docs/TRACEABILITY.md`, "Third review"). Behaviour that changed:

- **Wire classes.** `earnings_date` is tried first of all (section 14 put it ahead of contract and
  earnings_result only, so "to Report Q3 Results and Phase 2 Topline Data on November 12" was
  `trial` and "... Third Quarter Results and PDUFA Update" was `fda`), and the reader never counts
  it for gate 6 (`NEVER_PASS`). Cost, fail closed: a call notice for trial results with no date in
  it ("to Host Call to Discuss Phase 3 Topline Results") is `earnings_date`, so gate 6 does not
  count it and gate 5 is `unknown` for the lookback. New `earnings_date` keywords: "announces
  conference call ... results", "announces ... results conference call", "sets ... earnings call
  for", "to webcast ... results", "notice of ... earnings call", "conference call date", "results
  release date" and their earnings forms. New `offering` keywords for dilution deals that
  "agreement" (contract) caught: "at the market" (unhyphenated), ATM program and facility, equity
  distribution, equity purchase, standby equity, equity line, securities purchase agreement,
  "warrant" (singular), inducement agreement, convertible notes, debentures, preferred and bonds,
  debt exchange, exchange or settlement of notes. Fail-closed misses also classed: Phase 1b, 2a,
  2b and the roman forms (trial), breakthrough device designation (fda), "Reports Q1-Q4" and
  "Reports Fiscal" (earnings_result). A 6-K still has no class (unchanged: no catalyst, fail closed).
- **Filings.** The getcurrent description carries only item captions, so a reverse split never
  matched "reverse": 5.03 with 3.03 is now `reverse_split`, and 7.01 or 8.01 beside 5.03 or 3.03 no
  longer give `contract_or_other` (a 5.03 + 8.01 reverse split made gate 6 true).
- **Gate 5.** An `earnings_date` fact with `published_at` null uses `first_seen_at` for the
  lookback and the year; it was skipped, so gate 5 could answer `false` on a matching title.
- **Tickers.** Default prefixes add Nasdaq CM/GM/GS (with and without the space), NASDAQ Capital
  Market, Nasdaq Global Market, Nasdaq Global Select Market and OTC Pink. A "/" before another
  prefix splits ("(CSE: ACM / OTCQB: ACMEF)"), other text in a tag is skipped instead of ending it
  ('(the "Company", NASDAQ: ACME)'), and " - " works as the separator ("(Nasdaq - ACME)").
- **Brief.** The brief job waits for the first live beat (`AfterLiveBeat`) and is seeded from
  today's brief file when that was written at or after `brief_time_et` (a restart after 08:20 ET
  rewrote a good brief from the "starting" heartbeat, STALE with no source lines, and paid for J4
  again; a brief made by hand earlier is still replaced at 08:20). Overnight catalysts are those
  published and first seen since the prior close, newest first, with `offering` filings folded to
  one line per issuer ("+N more offering filings by this issuer"); the oldest 40 were shown, so an
  after-close flood of 2.02 8-Ks and bank 424B2 notes hid the pre-market items. J4 gets the same list.
- **STOP.** `STOP` or `STOP.txt` stops Scout, which deletes the file; one left before a start is
  deleted with a warning. The README says how to make it.
- **Launchers.** `run` returns 4 when another Scout holds the lock (2 is the CLI's bad-input code);
  Start Scout says "already running in another window" for it. Update Scout runs everything after
  the running check as one block ending in `exit /b`, so `git pull` replacing the file cannot make
  cmd read the new text at the old offset.
- **Logs.** Each line's time is ET with "ET" written (`logs.EtFormatter`), like the file name.
- **XML.** The DTD and entity guard asks expat itself (`edgar_parse.refuse_dtd`), so a UTF-16 body
  is refused too; an encoding expat cannot read is `SchemaError`, not a bare ValueError.

## 16. Restart: the macro sources on the board (2026-11-01)

Seen live after a restart on Sunday 2026-11-01 02:12 ET: the heartbeat showed `bls` and `cboe_vix`
as "starting". Their jobs were seeded done from `macro\history.jsonl` (sections 2.2, 15), so they
were not read again, but the board was not seeded, so "starting" lasted until the next read: up to
a week for BLS, until Monday 16:30 ET for Cboe. SS 10.1 makes every answer that depends on a source
not `ok` unknown, so after any restart the macro fallback (BLS) and the VIX context stayed unknown
for days. Cboe's own start read on Saturday 23:18 ET had failed (network), and that failed line
counted as the read for Friday's slot, so the restart did not read it again either.

- **Seed** (`macro_seed.seed_board`, called by `jobs_macro.build_jobs`): for each enabled source
  in `SEEDED` (bls, cboe_vix, nowflation; fomc has no board entry) with a history line, the board
  gets that line's status, first reason and the time of the source's latest `ok` line as
  `last_ok`; each reader's own `last_ok` is set to it too. An `ok` line stays `ok` only while it is
  still fresh by the source's own rules, re-checked against now (`recheck`): `stale_after` not
  passed (all); a CPI and an NFP release still after now (BLS); the close not older than the
  previous trading day, with the calendar covering today (Cboe); `source_generated_at` not older
  than `nowflation_max_age_h` and, after `nowflation_fresh_after_et`, today's (nowflation). Else it
  is `stale` with the failing reason. A seed never turns a line that was not `ok` into `ok`.
- **Never `ok` from a line it cannot trust** (review, 2026-11-01): a line read (`first_seen_at`,
  `fetched_at`) or generated (`source_generated_at`) more than `FUTURE_SLACK` (1 h, as nowflation's
  own `generated_in_future`) after now, as after the clock went back or with a copied history, is
  `stale` (`read_after_now`, `generated_in_future`); an `ok` line without `stale_after` is `stale`
  (`no_stale_after`); a line with a field of the wrong type is `stale` (`history_unreadable`) and
  never stops `build_jobs`, so a damaged history cannot keep Scout from starting.
- **Not seeded:** the polled feeds (EDGAR Atom, the wires, the halt feed, the ticker map,
  EDGAR submissions). They poll within a minute or two of the start, and their gap rules (D7) must
  start from a real poll.
- **Retry rule** (`macro_seed.last_read`). SS 12 says "BLS weekly; Cboe daily"; the spacing limits
  how often the host is asked for data it answered. A read the host did not answer (`unavailable`:
  network, timeout, HTTP error, refused, schema drift) gives nothing to space from, so it does not
  use up the week (BLS), the trading-day slot (Cboe) or the day (nowflation's start read), and a
  failed latest line never marks a daily job done (`_seed`). A restart therefore reads such a
  source again once, at start; that costs one request per start, and the fetcher's backoff after a
  403 or 429 and nowflation's daily cap still apply. Within one run nothing changes: a failed read
  is not retried before its next scheduled time (BLS a week after the attempt, Cboe the next
  trading day's slot, nowflation its next read time). A `stale` line (the host answered with old
  data) counts as a read.

## 17. Owner decisions 2026-11-01: the inbox and the Paper Gains Discord (as built)

SCOUT_SPEC 18. Built in section 2.7, format in `docs/INBOX.md`, keys in 6.9.

- **Gathering stays outside Scout.** Scheduled Claude tasks write `inbox\jason\` and
  `inbox\discord\`; Scout reads them as untrusted data and has no network path to Discord, YouTube,
  X or the newsletter host (SCOUT_SPEC 2 rule 10; `tests/test_boundaries.py` unchanged).
- **Auto-add** (`levels add --yes`, `--inbox`) is the 2.4 command with stricter checks, a one-line
  refusal, a non-zero exit and a `.rejected.txt` note; safe beside a running Scout (no lock; the level
  file appears by a link that refuses an existing name).
- **Discord** posts are facts of kind `discord` (record and brief) and `earnings_schedule` (gate 5,
  block only). What the gate-5 source adds: a way to remove holds into an earnings date that only a
  Discord list named; what it removes: nothing else. It cannot add a hold. Measured later over the
  journal and the verification period like every gate-5 source (SCOUT_SPEC 11).
- **Not a gate:** the `discord_inbox` heartbeat status (honest capture coverage for the owner), every
  other Discord fact, and image transcriptions.

## 18. Owner decisions 2026-11-04: Scout blocks Radar's buy signals (as built, 0.2.0)

The owner approved Radar's squad-split proposal (`E:\Radar_dev\docs\SQUAD_SPLIT_PROPOSAL.md`) with every
recommended answer. Scout's part is its section 5, items 1-5; SCOUT_SPEC 1, 2, 6.2-6.4, 7.1, 7.5,
7.8, 9.1, 10.3, 13 and 17 are amended. `scout_reader` is 0.2.0, Scout 0.2.0.

1. **Radar's finds** come from `radar_dir\state\handoff\<trading day>.jsonl` (`radar_handoff_subdir`),
   the file Radar really writes (2.1). The planned `state\finds\` export was never built.
2. **Halts**: any Market text is stored, a missing one is None, and one bad row is skipped and
   counted instead of refusing the feed (2.3). The feed was down all day on 2 and 3 Nov.
3. **Classes**: `bankruptcy` (8-K 1.03 and wire keywords, tried first); 424B2 has no class;
   `contract_or_other` left the default `hold_catalyst_classes` (2.2, 6.4). Facts already stored
   keep the class they were stored with (append-only); only `entry_blocks` re-checks them: a 424B2
   never blocks, an 8-K with 1.03 blocks as `bankruptcy`.
4. **Coverage**: a failed poll followed in time by an overlapping good poll is not a gap (3.7). This
   applies to every gate-6 source, not EDGAR only: the rule is the same for all of them.
5. **`entry_blocks`** (3.9), with its cache.

Choices made in the build (the proposal left them open):
- `bankruptcy` may be missing from an install's `[class_keywords]` table (its default list
  stands), so an install config written before 0.2.0 still loads; every other class must still be
  listed.
- A 424B2 is `None`, not a new `debt_note` class: nothing reads such a class, and `None`
  keeps it out of `news_headline` classes too.
- The cache serves `entry_blocks` only; gates 5 and 6 still read afresh, so a cache fault can never
  change a hold answer.
- `halt_open` in `blocks` uses the halt code as `class`, `macro_window` the release kind.

Review fixes (same 0.2.0, before release; owner, 2026-11-04 approval of the proposal, applied the safe way):
- An unresolved halt from an earlier ET date now blocks (it gave `unknown`, so a halt at 17:30 ET
  with no resumption seen did not stop a 04:05 ET signal the next day). Proposal 3.4: "bad news on
  file always blocks". A lost resumption row can block a stock for at most the 3-trading-day window;
  `halt_unresolved_since:<date>` in the reasons shows it. Gate questions (`halt_open`) are unchanged.
- 424B8 stays an `offering`, and 8-K 7.01 or 8.01 alone stays `contract_or_other`: the proposal
  asked only that banks' 424B2 notes stop being offerings and that `contract_or_other` stop counting
  for holds (done through `hold_catalyst_classes`). The first build went further without the owner's
  request; reverted.
- A filing fact stored before the fix with item 1.03 blocks as `bankruptcy` (the Mercer case).
- A halt poll that skipped a bad row turns `false` into `unknown` (`halt_rows_skipped`) for the
  lookback window: the skipped row may have been the stock's halt.
- `radar_handoff_subdir` also refuses a drive ("C:state") or a root.

## 19. Coverage fixes (owner, 2026-11-04; 0.2.1)

Seen live (a replay of `state\health` from 1 to 4 Nov): `entry_blocks` and gate 6 answered
`unknown` for almost every stock because every gate-6 source had gaps over the 3-trading-day window.
Three causes, each fixed in Scout's `gap_before` and in `scout_reader.coverage` alike (SCOUT_SPEC 6.2):

1. **Restarts.** The tracker kept seen guids and the last good poll in memory only, so the first poll
   of every feed after each restart had no overlap: a `no_overlap` gap on every feed for 3 trading
   days, though Scout was down 20 s to 2 min. Fix: the seed (`scout/coverage_seed.py`, 2.2). The
   interval rule still runs from the last good poll before the stop, so a long outage is a gap.
2. **The 04:00 ET switch.** EDGAR polls every 600 s at night and every 60 s by day. The limit was
   taken from the later poll (180 s), though the earlier poll was scheduled at 600 s, so every
   trading day had an `interval` gap near 03:50-04:00 ET. Fix: the larger spacing of the two polls.
   It is never tighter than before and loosens only at a switch, to what the schedule allowed.
3. **One failed poll.** Owner-approved (proposal 5.4): one timed-out poll is not a gap when the next
   overlaps. The first build still compared the good-to-good interval with the plain limit, so a
   timeout between two 60 s polls (185 s apart) stayed a gap, dozens of times a day. Fix: plus each
   failed poll's own spacing, at most 3 in a row (review: using the larger good-poll spacing would
   have forgiven a 50-minute hole right after 04:00 ET).

Review fixes (same 0.2.1, owner 2026-11-04):
- Only the feeds in `gate6_feeds` count for coverage, in the reader and in the heartbeat's
  `gap_open`: EDGAR's 10-Q and 10-K are polled for the gate-5 check only and the SS 6.4 table never
  classes them, so their slow-cycle gaps hid nothing gate 6 or `entry_blocks` reads.
- `gaps` uses only polls that ended by the window's end (as `entry_blocks` already did): no
  look-ahead in "as of t" gate-6 queries.
- More than 3 failures in a row are one gap up to the next good poll, so a window that starts
  between the last failure and that poll still sees it; Scout's `gap_before` is true after failed
  polls even with no good poll before them. Writer and reader agree on both (tests).
- The seed reads an unreadable spacing as 0 (as the reader does) and keeps failure-only feeds.
- A restart is still a gap when the first poll comes later than the limit after the last good
  poll before the stop: about 180 s by day.

Not changed: past health records stay as written (append-only), so the restart gaps recorded before
0.2.1 turn `false` into `unknown` until they leave the 3-trading-day window. The `no_poll_at_start`
gaps (Scout's first records on 1 Nov 03:17 UTC; Stock Titan from 3 Nov 19:46 UTC) are true gaps and
age out the same way. Other gaps the replay still shows, which these fixes do not touch: a slow or
timing-out EDGAR cycle (12 forms one after another) delays every later poll in the single-threaded
loop past 180 s, and PR Newswire's 20-item feed passes more than 20 releases within a minute at the
top of some hours. They need the owner's decision. In the live replay every remaining `interval`
gap ends on a poll that overlaps; ways out (owner to choose): run the wires on their own schedule
so EDGAR cannot delay them, a shorter EDGAR timeout, measuring each interval from the time the poll
was due, or not counting the interval when the later poll is a full page that repeats a seen id.
The owner chose two of them the same day (0.2.2, section 20).

## 20. Overlap rule and wire schedule (owner, 2026-11-04; 0.2.2)

A replay of `state\health` (1-4 Nov) through 0.2.1 still showed gaps every day from two causes.
(a) Scout runs one job at a time; with the SEC slow (10 s replies, 20 s timeouts) one EDGAR cycle of
12 forms took about 140 s and pushed every later poll, the wires too, past 180 s. All 64 remaining
interval gaps ended on a poll that overlapped (31 full pages repeating a seen id, 33 short pages):
nothing was missed. (b) PR Newswire's RSS holds 20 items, and at the top of some hours more than 20
releases appear within a minute: 7 true no-overlap gaps. The owner chose both fixes:

1. **The overlap rule** (`scout_reader.coverage.interval_gap`, used by the reader and by
   `health.CoverageTracker`). A good poll that overlaps the previous good poll of its feed by a seen
   id (at least one item Scout had already seen in that feed, or a 304) shows that its page reached
   back past that poll, so the interval between them is covered however long, with any number of
   failed polls in it, up to `source_overlap_max_gap_s`. Past the cap the 0.2.1 rule applies (so the
   new rule never adds a gap; a test checks this on random runs). An overlap from a short page only
   (fewer items than `max_items`, no seen id) keeps the 0.2.1 rule: a short page proves nothing
   about items that were never on it. No poll at the start or the end, no overlap and a missing feed
   are unchanged.
   - Records: the health record gains `seen_items` (distinct ids on the page already seen in the
     feed). Records written before 0.2.2 have no `seen_items`; for them a good page with `items` > 0
     and `new_items` < `items` counts (the owner's rule). One limit: before 0.2.2 `items` counted a
     guid repeated on one page twice while `new_items` counted it once, so such a page with no seen
     id would read as seen; `seen_items` removes that for new records, and the old records leave
     the 3-trading-day window within 3 trading days of the install. A `seen_items` key that is present but unreadable claims nothing.
   - The cap is passed to the reader in params `source_overlap_max_gap_s`. Params without it (Scout
     before 0.2.2), or a value that is not a positive number, mean the 0.2.1 rule (fail closed).
   - Default 1800 s. It covers every interval the replay showed (the longest one ending on a seen
     id was 1294 s, an EDGAR feed at the 600 s night spacing) and the longest backoff after a 403 or 429 (`source_backoff_max_min`, 30 min),
     so a backoff whose next page still repeats a seen id is not a gap. It is a cap and not
     unlimited because one repeated id is weaker evidence the longer the pause: a feed may insert an
     item out of order below one already seen and push it off the page, or serve a cached page.
     Half an hour keeps that exposure small; past it the outage is a gap as before.
2. **The wire schedule.**
   - EDGAR yields: `EdgarAtomPoller.poll` calls `between()` before each form request after the first;
     `jobs_wire` passes one that runs `run_due` over the wire jobs (the same `EveryJob` objects the
     main loop holds, so a wire run there is not run again). No thread: the fetcher's host spacing,
     the fact, raw and health writers, the coverage tracker and the board are used exactly as before,
     one call at a time, and the heartbeat is unchanged (the `Pulse` thread already beats while any
     job runs long). A wire now waits at most one EDGAR request (up to `fetch_timeout_s`, 20 s). A test
     with a fake slow SEC (10 s replies, a 20 s timeout every third request) shows every wire
     interval within its spacing + 22 s, against more than 150 s without the yield.
   - `run_due` takes the clock and asks each job at the clock's time when its turn comes; before,
     every job of a tick was asked at the tick's start, so after a 140 s EDGAR cycle a wire that had
     just run inside it looked due again and would have been polled twice. `EveryJob.due` also
     treats a last run later than `now` by at most `CLOCK_STEP_BACK_S` (5 s) as not due; a wall
     clock that stepped back further makes the job due at once, as before 0.2.2 (review fix: a
     limit of one interval made a daily job wait out the step).
   - PR Newswire around the hour: `hour_turn_sources` (["prnewswire"]) are polled every
     `hour_turn_spacing_s` (30 s) from minute `hour_turn_start_min` (58) to `hour_turn_end_min` (5,
     not included) ET around each hour, inside `active_hours_et` only (`health.spacing_in_force`, so
     the health record stores the spacing the poll ran at). 30 s is the fastest allowed: the hard
     minimum for www.prnewswire.com in `scout/http.py` is 30 s, and validation refuses a lower value
     or a host whose hard minimum is higher. PR Newswire's `terms_note` (titles, links and times
     only until the owner reads the terms) sets no rate. Extra requests: 16 hour turns a day inside
     04:00-20:00 ET (15 full windows of 7 minutes, 04:00-04:05 and 19:58-20:00), 112 minutes at 30 s
     instead of 60 s: about 112 more a day, 1552 instead of 1440 (a test counts it). No new source or
     URL. A burst of more than 20 releases within 30 s is still recorded as a gap, never hidden.
     The window runs on trading days only since 0.2.3 (owner, 2026-11-09: `in_active_hours` is
     false on weekends and market holidays, which run at idle spacing as overnight does). `spacing_in_force` never goes below the spacing the fetcher keeps
     for the host (`http.host_spacing_s`: a configured `host_min_spacing_s` above 30 s sets it), so
     the job never polls faster than the fetcher sends and the record stores the real spacing.
3. **Review fixes** (0.2.2 review; the owner's rule kept, only made safe):
   - A lost page and a 304. The fetcher stores a 200's ETag before the poller parses the page. If
     the page then failed (`schema`), or storing it raised, a 304 on the next request would have
     meant "unchanged since a page Scout never read", and with the cap it would have covered up to
     1800 s. Now: (i) `WirePoller.refetch_full` and `EdgarAtomPoller.refetch_full` (per form) make
     the next request after such a page unconditional, as `HaltPoller.refetch_full` already did;
     (ii) `coverage.page_lost` marks a failed record with result "schema" or HTTP 200, and a 304
     after one (since the last good poll) has no overlap, in the reader, the writer and the seed
     (`FeedSeed.lost_page`). The live records of 31 Oct-4 Nov hold no such 304 on a gate-6 feed.
   - `seen_items` counts only seen ids below every new id on the page, so an old item re-listed
     at the top (an updated release keeping its guid), or a late repost slotted below new items,
     is not taken as proof. `overlap` keeps its 0.2.1 meaning. Records before 0.2.2 keep the
     owner's rule.
   - The reader refuses a cap that is not finite or is above one day (config allows 0-86400).
   - Records before 0.2.2 with a guid repeated on one page (`items` counted it twice) could still
     read as seen with no truly seen id. Not fixable from those records; they leave the window
     within 3 trading days of the install.

Not changed: an EDGAR form whose page is empty or short (NT 10-Q at quiet times) cannot show a seen
id, so a slow EDGAR cycle with a timeout on it can still be an `interval` gap under the 0.2.1 rule
(twice in the 1-4 Nov replay). Other jobs (macro, Claude calls) still run one at a time and do not
yield to the wires. The halt feed (`nasdaq_halts`) is not run between EDGAR's forms either: on 1-4
Nov 31 of its intervals were over 180 s (the longest 307 s), so an open halt can reach Scout's facts
a few minutes late; passing the halts job to `between()` is the same one-thread mechanism and
needs the owner's decision. PR Newswire bursts at half-hour release times (3 Nov 13:31 UTC) are
outside the :58-:05 window.

## 21. Polling pace (owner, 2026-11-10; 0.2.4)

The owner: "Slow Scout down by a factor based on how frequent each source has updated data - and
continue to monitor and adjust timings as time goes on." SCOUT_SPEC 6.5 is the contract.

**Measured first** (the live install's health records, 2-9 Nov 2026; each poll records `new_items`):
- EDGAR releases nothing new from about 18:00 to 06:00 ET (filings after 17:30 are disseminated at
  06:00, where every form has a burst), yet every form was polled every 60 s until 20:00 and every
  10 minutes overnight. F-1, F-3, NT 10-Q, NT 10-K, EFFECT, S-1 and 10-K found something new in under
  3 % of polls; 8-K in 24 % before the open, 8 % in regular hours, 28 % after the close; 424B (banks'
  notes) in 61 % of regular-hours polls.
- PR Newswire found something new in 35-38 % of polls by day, 14 % at night, 3 % on closed days, and
  overflowed its 20-item page at the top of busy hours on most days, even at 30 s (8:00, 9:00, 10:00
  ET), and at 21:22 and 21:29 ET on 5 Nov. Stock Titan's 100-item page overflowed at 16:06 and 16:07
  ET on 4 and 5 Nov.
- Halts cluster in regular hours: polls finding a new halt about 13 times a day in each of the 09:00
  and 10:00 ET hours, about twice a day before 09:00 and twice after 16:00. Radar's 210 signals of 4-9 Nov all had Scout `unknown`; the only bad-news facts near any of
  them were LULD pauses (MF paused 9 minutes after its signal; CRIS and AEHL within 2-4 hours).
- A wire item reaches Scout a median 154 s (PR Newswire) and 116 s (Stock Titan) after it is
  published, an EDGAR filing about 60 s after acceptance, at today's 60 s polls.

**Modules.**
- `scout/pace.py`: `slot_of(at)` (ET hour 0-23 on a trading day, `CLOSED` = 24 otherwise);
  `measure(records, start, end, sources) -> {(source, feed): FeedPolls}` (good polls in time order,
  per slot the polled time, polls, polls with new items and the largest one; `busiest(slot, s)`, the
  most new items within s seconds ending at a poll of the slot); `wanted_spacing` (`factor` x the
  polled time per poll with new items, rounded down; the page guard binary-searches `GRID_S` for the
  largest spacing whose `busiest` fits in `page_fill` x `max_items`); `build_plan` -> `PacePlan` of
  `FeedPlan`s (record 5.12); `Pace`: `spacing(src, feed, now, fixed)` clamps the wanted spacing
  between `fixed` and the feed's slowest (`PaceConfig.slowest`), `source_spacing` (the fastest
  feed), `stale(now)`, `recalc(now)` (reads the lookback's health day files, appends the plan);
  `load_latest(paths, as_of=None)` (newest readable plan, as-of rule for the brief; never raises).
- `scout/jobs_pace.py`: `pace_start` (at start, when the plan is missing, more than 26 hours old, from
  the future, or made with other `[pace]` settings; it steps aside when the daily job is due on the
  same tick) and `pace` (`DailyJob` at `recalc_time_et`, closed days too; seeded from the saved
  plan's time so a restart after 03:30 does not read again). First in `scout.app.JOB_MODULES`.
- `scout/config_pace.py`: `PaceConfig`, `pace_from_table`, `validate_pace` (6.10).
- `scout/brief_pace.py`: `sec_pace`, after "Source health": per feed the spacing by hour (listed from
  the start of active hours), closed days, the fixed spacing and the hit rate; the requests a trading
  day now and at the fixed spacing; what is not paced. "No pace worked out yet" before the first plan.
- `health.spacing_in_force(src, now, cfg, pace=None, feed=None)`: the fixed spacing, then the pace,
  then the hour turn (unchanged). `health.recorded_spacing`: the larger of the spacing now and the
  one when the next poll falls due (below). `CoverageTracker(cfg, seeds, pace)` records it, and
  `gap_open` allows the larger of a feed's recorded spacing and its spacing now.
- `EdgarAtomPoller(..., pace)`: `interval_at(now)` (the fastest form, the job's `interval_fn`), a cycle
  polls the forms whose own spacing has passed since the cycle they were last polled in (a clock
  step back of more than `CLOCK_STEP_BACK_S` makes them due), and the status and `last_ok` come from
  every form's latest result. Without a pace every cycle polls every form, as before.
- `AppContext.pace()` builds the one `Pace` and sets `board.pace`; `heartbeat._aged` ages a source by
  the larger of its spacing now and the recorded spacing at its `last_ok`.

**Choices.**
- **By ET hour, not by session:** EDGAR's dead hours (04:00-06:00 and 18:00-20:00 inside active
  hours) and its 06:00 burst fall inside sessions. A switch to a faster hour takes effect at once
  (the job asks its interval at every tick), so the 06:00 burst is read at 06:00.
- **`factor` 0.5:** when new items arrive at random, polling every 0.5 x the measured gap settles
  where about half the polls find something new (at about 0.7 x the mean time between items), from
  any start, within a day or two; a factor of 1 or more would drift every quiet hour to the slowest
  allowed. A busy hour (most polls find something) stays at the fixed spacing.
- **The page guard counts items within a window, not a rate:** one new item in a 60 s poll is not a
  rate of one a minute; counting the items seen within a window of the candidate spacing keeps an
  hour with a real burst fast and lets an hour with a release now and then slow down.
- **Never faster than before** (`min_spacing_s` / `idle_spacing_s` stay the floor); the hour turn
  stays. **The slowest allowed** and why: SCOUT_SPEC 6.5 (halts never; wires 2 minutes and EDGAR
  forms 5 minutes in trading hours, the gate-5-only forms 30 minutes; 30 minutes otherwise).
- **Recorded spacing (also without pacing):** the gap rule allows 2 x the larger spacing of two
  polls and 2 x the last poll's spacing at the end of a window. A poll just before a slow-down stored
  the faster spacing, so the reader saw a `no_poll_at_end` gap until the next poll: about 7 minutes
  after 20:00 ET every trading day before 0.2.4, and after every slower hour with the pace. One look
  ahead suffices (the pace changes on the hour, no spacing exceeds an hour). Writer and reader use the
  stored value, so they still agree.
- **Old restart records:** polls after restarts before the 0.2.1 seed (2-4 Nov) show up to 100 new
  items at once; they keep those hours at the fixed spacing until they leave the 7-day window (the
  safe direction).

**Measured effect** (the plan from the live records of 3-10 Nov): about 5,100 requests a trading day to
the paced feeds instead of 14,500, and about 700 instead of 3,300 on a closed day. Recalculating
reads about 35 MB of health records and takes about 2 seconds, once a day at 03:30 ET.

**Not changed:** the halt feed (60 s in active hours), the macro and reference schedules (already at
publication times or on demand), the inbox check (`inbox_poll_s`, local files only), and the Claude
jobs (not sources; capped by cost). Seen while measuring and left for the owner (no change here): the
after-20:00 ET dated halt read (`&haltdate=`) fails on every trading day ("no readable row ...
unreadable time '09:30:17            '": that feed pads HaltTime with spaces, which `_TIME` refuses)
and is tried again every 60 s until midnight ET, about 235 requests a night; its rows never reach the
facts.
