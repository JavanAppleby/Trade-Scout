# Scout v1: traceability (spec -> code -> test)

Every rule of `docs/SCOUT_SPEC.md` that build steps 1-5 cover, the module that meets it, and the
test that proves it. "SS x" means SCOUT_SPEC section x. Modules are under `scout\` unless they say
`scout_reader`; tests are under `tests\`. Build steps 6-11 and phases 2-4 are out of scope and listed
at the end. Run every test with `.venv\Scripts\python -m pytest -q` from Scout's folder.

## SS 2. Boundaries

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 2.1 | Never trades: no `webull*` or `trade-guardian` declared, installed or importable | `pyproject.toml`, `.github/workflows/ci.yml` (boundary step) | `test_boundaries.py`: `test_no_forbidden_dependency_declared`, `test_no_forbidden_package_installed_or_importable`, `test_no_forbidden_imports_anywhere` |
| SS 2.2 | No Webull requests in v1 | `config_validate.validate` (`scout_webull_enabled` must be false) | `test_boundaries.py::test_webull_cannot_be_enabled`, `test_config.py` |
| SS 2.3 | Own folder and repo | repo layout, launchers `*.bat` | (layout; `test_paths.py::test_layout`) |
| SS 2.4 | Writes only inside Scout's folder; reads others read-only | `paths.ScoutPaths.writable`, `store`, `inputs` (open-read-close) | `test_paths.py`, `test_store.py`, `test_boundaries.py::test_a_run_writes_only_inside_scouts_folder` (audit hook), `test_e2e_day.py::test_nothing_written_outside_scouts_folder` |
| SS 2.5 | Missing, stale or corrupt file means `unknown`; `unknown` never allows a hold | `scout_reader.api`, `scout_reader.records`, `scout_reader.gates.hold_allowed` | `test_reader_api.py` (hypothesis property tests), `test_reader_records.py` |
| SS 2.6 | Web text and LLM output are untrusted data | `llm_prompts` (escaped, nonce-fenced items), `llm_checks` | `test_llm_prompts.py`, `test_llm_hostile.py` |
| SS 2.7 | Facts gate; labels are evidence only | `scout_reader.gates` reads facts only; labels live in `labels\` | `test_llm_j1.py::test_answers_identical_with_and_without_labels` and its static check that no `scout_reader` module but `records.py` names the labels folder |
| SS 2.8 | Secrets: key from the environment or `.env` only, never logged, `ANTHROPIC_LOG` unset | `secrets` (`load_env`, `redact`), `logs` (`RedactingFilter`), `llm_client` | `test_secrets.py`, `test_llm_client.py`, `test_integration.py::test_request_params_match_the_pinned_sdk` (no key in any request) |
| SS 2.9 | No look-ahead: `first_seen_at <= t` and `published_at <= t` | `scout_reader.records`, `scout_reader.gates`, `scout_reader.api` | `test_reader_asof.py`, `test_reader_gate6.py::test_fact_first_seen_after_as_of_is_invisible` |
| SS 2.10 | Jason's material is gathered by hand | `levels_add` (local files only) | `test_levels_add.py` |
| SS 2.11 | Tests never touch the network | `tests/conftest.py` `no_network` (autouse), fakes, `ManualClock` | every test; `test_e2e_day.py` adds its own socket guard for the module-scoped day |

## SS 3. Where it lives and how it keeps time

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 3 time | UTC `Z` stored; ET and Brisbane in reports only; ET converted once | `clock`, `scout_reader._et` | `test_clock.py`, `test_et.py` (every hour of 2026-2027 against `zoneinfo`) |
| SS 3 calendar | Copy of Guardian's calendar with provenance; agrees every day of 2026-2027 | `scout_reader.calendar`, `calendar` (Session enum), `tools/make_calendar_fixture.py` | `test_calendar.py::test_every_day_of_2026_2027_agrees_with_guardian`, `test_provenance_matches_the_fixture` |
| SS 3 coverage | Warn while the calendar does not cover today + 45 days; gate 5 `unknown` past it | `app.startup_warnings`, `heartbeat` (`calendar_covers_next_45d`), `brief_sections.sec_calendar`, `scout_reader.gates` | `test_app.py::test_startup_warnings`, `test_heartbeat.py::test_calendar_coverage_flag`, `test_reader_gate5.py` |
| SS 3 reader | `scout_reader` stdlib only, never raises | `scout_reader/` | `test_boundaries.py::test_scout_reader_is_stdlib_only`, `test_reader_api.py` |

## SS 4 and 6. Layer 1: sources, fetchers, facts, coverage

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 4.1, 6.1 | Allowed hosts, declared User-Agent, per-host spacing and daily caps, size cap, timeout, conditional GET, backoff after 403/429, no redirects followed, empty `contact_email` means no HTTP | `http` | `test_http.py`, `test_boundaries.py::test_empty_contact_email_means_no_http` |
| SS 6.1 | Raw bodies kept per item; wire bodies cut to title, link, guid and time while `terms_ok` is false | `raw`, `wire_rss` | `test_raw.py`, `test_wire_rss.py` |
| SS 4.1 #1-3 | EDGAR getcurrent Atom (per form), submissions, ticker list | `edgar_parse`, `edgar_atom`, `edgar_submissions` | `test_edgar_parse.py` (also checks Radar's own results), `test_edgar_atom.py`, `test_edgar_submissions.py` |
| SS 4.1 #4-5 | PR Newswire, GlobeNewswire (403 stops it for the day) | `wire_rss` | `test_wire_rss.py` |
| SS 4.1 #6, 7.5 | Nasdaq halts (BOM, letters, 2019 row, empty resumption, LUDP, M; 60 s minimum) | `halts`, `scout_reader.halts` | `test_halts.py`, `test_reader_halts.py` |
| SS 4.1 #7-10, 7.3 | nowflation (fail closed on every SS 7.3 condition, schema pinned), BLS `.ics`, FOMC table, Cboe VIX | `nowflation`, `bls_ics`, `fomc`, `cboe_vix`, `macro`, `jobs_macro` | `test_nowflation.py`, `test_bls_ics.py`, `test_fomc.py`, `test_cboe_vix.py`, `test_macro.py`, `test_jobs_macro.py` |
| SS 6.2 | Coverage: a gap or a poll without overlap turns `false` into `unknown` | `health` (`CoverageTracker`), `scout_reader.coverage` | `test_health.py`, `test_reader_coverage.py`, `test_reader_gate6.py::test_a_coverage_gap_turns_false_into_unknown` |
| SS 6.3 | Tickers: exchange tags, one fact per ticker, CIK map | `tickers`, `edgar_parse`, `facts` | `test_tickers.py`, `test_facts.py` |
| SS 6.4 | Class from title and form only (keywords in config) | `classify` | `test_classify.py` |
| SS 10.2 | facts, health, raw files written per poll | `facts`, `health`, `raw`, `jobs_wire` | `test_jobs_wire.py`, `test_e2e_day.py::test_facts_of_every_kind`, `test_only_known_urls_and_no_failed_source` |

## SS 5. Jason's level files

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 5.1-5.3 | Format, archive, the ported loader, `expires` default | `levels_parse`, `levels` | `test_levels_parse.py`, `test_levels.py` (ported AO1-AO9, AO17, AO36-AO38) |
| SS 5.2 | **Add Levels** validates, refuses a raising file, never edits one already added | `levels_add` | `test_levels_add.py` |
| SS 5.4 | The scorer: readings expanded, each setup's horizon, ATR tolerance, a null identical across processes | `levels_score`, `levels_bars` | `test_levels_score.py`, `test_integration.py::test_long_horizon_sessions_follow_the_config` |
| SS 5.5, 9.1 | Jason tags for Radar and Pilot | `levels_snapshot`, `jobs_levels`, `scout_reader.jason` | `test_levels_snapshot.py`, `test_reader_jason.py`, `test_integration.py::test_reader_params_carry_the_market_frame` |

## SS 7. The hold answers and other context

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 7.1 | Gate 6 `fresh_catalyst`: window from the prior close; just inside true, just outside false; offering, reverse split, listing notice and other never pass | `scout_reader.gates`, `scout_reader.coverage` | `test_reader_gate6.py`, `test_e2e_day.py::test_answers_for_pilot_watch_and_radar` |
| SS 7.2 | Gate 5 `event_before_next_session`: announced date, EDGAR due window per fiscal quarter (10-K season), NT and overdue unknown, window to the next open across Friday and holidays | `due_window`, `answers`, `jobs_answers`, `scout_reader.gates`, `scout_reader.datetext` | `test_due_window.py` (replay over 2025-2026), `test_reader_gate5.py`, `test_reader_datetext.py`, `test_answers.py` |
| SS 7.3 | Macro events recorded and reported only (never block a hold in v1) | `macro`, `scout_reader.macro`, `brief_sections.sec_macro` | `test_macro.py`, `test_reader_macro.py`, `test_integration.py::test_macro_view_is_rebuilt_as_of` |
| SS 7.5 | Halts recorded; a stale feed gives `unknown`, never "not halted" | `halts`, `scout_reader.halts` | `test_reader_halts.py` |
| SS 7.6 | Market regime (record only) | `macro.inflation_regime`, `cboe_vix`, `scout_reader.macro` | `test_macro.py`, `test_reader_macro.py` |
| SS 9.2 | Answers at 19:30 and 19:45 ET for Pilot's positions; Radar's finds after 20:00 as of 19:45 | `jobs_answers`, `inputs` | `test_answers.py`, `test_e2e_day.py::test_answers_for_pilot_watch_and_radar` |

## SS 8. Layer 2: the Claude API (off unless `llm_enabled`)

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 8.1 | Every call: system prompt first and cached, schema output, no fallbacks, recorded with usage and cost, max_retries 0, one retry on schema failure | `llm_client`, `llm_prompts`, `llm_records`, `llm_schemas` | `test_llm_client.py`, `test_llm_prompts.py`, `test_llm_schemas.py`, `test_integration.py::test_request_params_match_the_pinned_sdk` |
| SS 8.2 | Jobs J1 (headlines), J3 (evening research with web tools limited to allowed domains), J4 (brief paragraph) | `llm_j1`, `llm_j3`, `llm_j4`, `jobs_llm`, `brief` | `test_llm_j1.py`, `test_llm_j3.py`, `test_llm_j4.py`, `test_e2e_day.py::test_claude_calls_recorded_and_labels_kept_apart` |
| SS 8.3 | Label schema v1 | `llm_schemas` | `test_llm_schemas.py` |
| SS 8.4 | Post-checks: cited URL in results, allowed host, verbatim quote, ticker named, date bounds; hostile items | `llm_checks` | `test_llm_checks.py`, `test_llm_hostile.py` |
| SS 8.5 | No LLM output changes an answer | (gates read no labels) | `test_llm_j1.py::test_answers_identical_with_and_without_labels` |
| SS 8.6 | Spend ledger: daily, monthly and per-job caps; at the cap Layer 1 keeps running | `llm_ledger`, `jobs_brief.ledger_status` | `test_llm_ledger.py::test_at_the_cap_layer1_and_answers_carry_on` |

## SS 10. Output files

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 10 | Atomic single files (rename retries), append-only JSONL, `format` on every record, partial last line ignored | `store`, `scout_reader.records` | `test_store.py`, `test_reader_records.py` |
| SS 10.1 | Heartbeat every 30 s with sources, LLM, levels, calendar flag; stale after 120 s | `heartbeat`, `app.run` | `test_heartbeat.py`, `test_e2e_day.py::test_heartbeat_all_day_with_every_source`, `test_one_failing_source_never_stops_the_others` |
| SS 10.2 | Files: facts, health, answers, labels, llm, macro, levels, raw (plus DESIGN D4: beats, params, due, snapshots) | `models`, writers per unit | `test_models.py`, `test_e2e_day.py` (every file present after the day) |
| SS 10.2 brief | Brief sections, ET and Brisbane times, Nowflation credit, escaped third-party text, J4 only when on | `brief`, `brief_sections`, `jobs_brief` | `test_brief.py`, `test_integration.py::test_the_brief_sees_a_read_made_in_its_own_tick`, `test_e2e_day.py::test_brief_with_every_section_and_j4` |
| SS 10.3 | Reader API: six functions, `{value, evidence, stale, reasons}`, never raises | `scout_reader.api` | `test_reader_api.py` |

## SS 11-13. Verification, budget, configuration

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 11 | Weekly seeded sample, owner hand checks, gate-5 automatic check, report PASS / NOT YET | `verify`, `verify_check`, `jobs_brief` (`verify_time_et`) | `test_verify.py`, `test_verify_check.py`, `test_integration.py::test_verify_runs_at_verify_time_et`, `test_e2e_day.py::test_verify_report_and_sample` |
| SS 12 | No Webull; per-host limits (EDGAR 2 a second, feeds 60 s, nowflation 10 a day) | `http` (hard minimums), `config.example.toml` | `test_http.py`, `test_halts.py::test_sixty_second_hard_minimum`, `test_jobs_macro.py` |
| SS 13 | Every tunable in `config.toml`; unknown keys are errors; the example documents every key | `config`, `config_load`, `config_validate`, `config.example.toml` | `test_config.py` (`test_example_documents_every_key`), `test_integration.py` (keys added at integration) |

## SS 14-15. Tests, acceptance, build steps 1-5

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 14 fail closed | Any mix of stale, missing, corrupt, partial or unknown-format files gives `unknown`; only (gate 6 true, gate 5 false, fresh) allows a hold | `scout_reader` | `test_reader_api.py` (hypothesis) |
| SS 14 no look-ahead | Replay of a stored day gives identical answers | `scout_reader` | `test_reader_asof.py` |
| SS 15 step 1 | Scaffold: main loop, scheduler, CLI, lock, wiring | `app`, `scheduler`, `cli`, `running` | `test_app.py`, `test_scheduler.py`, `test_cli.py`, `test_wiring.py` (strict: every job module and handler), `test_integration.py::test_run_until_stops_at_the_clock_time` |
| SS 15 steps 2-5 end to end | A simulated trading day produces facts, health, beats, macro, the Jason snapshot, answers, the brief (with J4) and the verify report; one failing source never stops the loop | all of the above | `test_e2e_day.py` |

## Out of scope for this build (SS 15 steps 6-11, SS 4.2-4.4)
Install at `E:\Scout` and the five-day operational acceptance (step 6); Radar, Pilot and Guardian
changes (steps 7 and 9); the verification period and holds (steps 8 and 10); the Webull probe
(step 11, SS 7.4); phase 2-4 sources. Live captures that replace the `_SYNTHETIC` fixtures are part of
step 2's owner tasks and are not yet done.
