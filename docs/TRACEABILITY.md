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
| SS 12 | No Webull; per-host limits (EDGAR 2 a second, feeds 60 s, wires at least 30 s in code, nowflation 10 a day) | `http` (hard minimums), `config_validate`, `config.example.toml` | `test_http.py`, `test_halts.py::test_sixty_second_hard_minimum`, `test_jobs_macro.py` |
| SS 13 | Every tunable in `config.toml`; unknown keys are errors; the example documents every key | `config`, `config_load`, `config_validate`, `config.example.toml` | `test_config.py` (`test_example_documents_every_key`), `test_integration.py` (keys added at integration) |

## SS 14-15. Tests, acceptance, build steps 1-5

| Spec | Rule | Code | Tests |
|---|---|---|---|
| SS 14 fail closed | Any mix of stale, missing, corrupt, partial or unknown-format files gives `unknown`; only (gate 6 true, gate 5 false, fresh) allows a hold | `scout_reader` | `test_reader_api.py` (hypothesis) |
| SS 14 no look-ahead | Replay of a stored day gives identical answers | `scout_reader` | `test_reader_asof.py` |
| SS 15 step 1 | Scaffold: main loop, scheduler, CLI, lock, wiring | `app`, `scheduler`, `cli`, `running` | `test_app.py`, `test_scheduler.py`, `test_cli.py`, `test_wiring.py` (strict: every job module and handler), `test_integration.py::test_run_until_stops_at_the_clock_time` |
| SS 15 steps 2-5 end to end | A simulated trading day produces facts, health, beats, macro, the Jason snapshot, answers, the brief (with J4) and the verify report; one failing source never stops the loop | all of the above | `test_e2e_day.py` |

## Second review (2026-11-01)

Each row is a defect the second review found; its test fails on the code before the fix.

| # | Spec | Rule | Code | Tests |
|---|---|---|---|---|
| 1 | SS 6.4, 7.1 | A date notice ("Will Host Conference Call on ... to Discuss Financial Results", "Timing of", "Schedules ... Webcast for", "Sets Date for Release of") is `earnings_date`, never a catalyst | `classify` (` ... ` gap keywords), `config` `DEFAULT_CLASS_KEYWORDS`, `config.example.toml` | `test_classify.py::test_more_date_notice_wordings_are_earnings_date` |
| 2 | SS 6.4, 7.1 | An 8-K/A is not fresh news: never `earnings_result` or `contract_or_other` | `edgar_parse.filing_class` | `test_edgar_atom.py::test_an_8k_amendment_is_not_a_fresh_catalyst`, `test_classify.py::test_filing_class_table` |
| 3 | SS 1, 7.2 | A ticker under two CIKs gets no due window (`ambiguous_ticker`, gate 5 `unknown`) | `edgar_parse.parse_ticker_rows`, `edgar_submissions` | `test_edgar_submissions.py::test_a_ticker_under_two_ciks_is_ambiguous_and_gets_no_due_window` |
| 4 | SS 6.4 | Contract and results headlines with "to release", "will announce", "conference call to discuss" keep their class | `classify`, `config` | `test_classify.py::test_contract_and_results_titles_are_not_date_notices`, `test_a_gap_keyword_needs_both_parts_in_order` |
| 5 | SS 4.1 #6, 12 | Halts polled every 60 s from the real send, not every 120 s | `jobs_macro` (`HALTS_CHECK_S`) | `test_jobs_macro.py::test_halts_are_polled_every_minute_when_earlier_jobs_delay_the_send` |
| 6 | SS 8.6 | A timed-out J3 call counts its searches against `web_search_max_per_day` | `llm_client` | `test_llm_client.py::test_a_timed_out_j3_call_counts_its_searches` |
| 7 | SS 10.2 | A failed halt fact write is tried again | `halts` | `test_halts.py::test_a_failed_fact_write_is_tried_again_on_the_next_poll` |
| 8 | DESIGN 3.2 rule 6 | `market_context` checks the beat's macro source status through the public API | `scout_reader.api` | `test_reader_api.py::test_market_context_passes_the_beat_for_rule_6`, `test_reader_macro.py::test_source_in_error_in_the_beat_through_the_reader_api` |
| 9 | DESIGN 3.2 rule 4 | Damage or a future format in the snapshot file turns a false jason tag into unknown | `scout_reader.api` | `test_reader_api.py::test_jason_tags_note_damage_in_the_snapshot_file` |
| 10 | SS 10 | A line nested too deeply is skipped, never raised | `scout_reader.records` | `test_reader_records.py::test_a_deeply_nested_line_is_skipped_not_raised` |
| 11 | SS 8.6 | Every call is reserved before it is sent; no reservation, no call; an unreadable ledger refuses | `llm_client`, `llm_ledger`, `llm_records` | `test_llm_client.py::test_a_call_whose_record_cannot_be_written_still_counts`, `test_no_reservation_no_call`, `test_an_unreadable_ledger_refuses_every_call` |
| 12 | SS 7.5, 10.1 | Every beat ages the polled feeds; a busy loop never publishes "ok, no gap" | `heartbeat` (`SourceStatusBoard.states_at`) | `test_heartbeat.py::test_a_beat_while_the_loop_is_busy_shows_the_gap`, `test_the_halt_feed_is_not_aged_outside_active_hours` |
| 13 | SS 4.1, 12 | A clock stepped back never makes a fetch sleep for hours; future times clamped on load | `http`, `halts` | `test_http.py::test_a_clock_stepped_back_never_sleeps_for_hours`, `test_a_future_time_in_the_state_file_is_clamped_on_load`, `test_halts.py::test_a_clock_stepped_back_does_not_stop_the_halt_polls` |
| 14 | SS 4.1 | A restart after a daily read time does not fetch Cboe or nowflation again | `jobs_macro` (`_seed`) | `test_jobs_macro.py::test_a_restart_after_the_daily_read_times_does_not_fetch_again` |
| 15 | SS 11 | One gate-5 check per (symbol, ET day); a miss in any row is a miss | `verify.gate5_checks` | `test_verify.py::test_one_symbol_day_is_one_gate5_check`, `test_a_miss_in_any_row_of_the_symbol_day_is_a_miss` |
| 16 | SS 11 | A 10-Q or 10-K the next morning is a gate-5 miss (polled; not gate-6 coverage) | `config` (`edgar_atom_forms`, `PERIODIC_FORMS`) | `test_verify.py::test_a_10q_filed_the_next_morning_is_a_miss` |
| 17 | SS 11 | The weekly sample is topped up from later days | `verify.weekly_sample` | `test_verify.py::test_the_weekly_sample_is_topped_up_from_later_days` |
| 18 | SS 7.3 | The forecast lean is shown as "informational, untested" | `macro.daily_body`, `brief_sections.sec_macro` | `test_brief.py::test_the_nowflation_forecast_lean_is_shown` |
| 19 | SS 12 | Wire hosts at least 30 s apart in code; wire `idle_spacing_s` validated | `http.HARD_MIN_SPACING_S`, `config_validate` | `test_http.py::test_wire_hosts_have_a_hard_minimum_in_code`, `test_config.py::test_wire_idle_spacing_must_be_zero_or_at_least_30` |
| 20 | SS 7.3 | nowflation is stale after 09:15 ET when either file is not today's | `nowflation` (older `generated_at`), `macro.stale_after` | `test_nowflation.py::test_an_old_realtime_file_makes_the_record_stale_at_0915` |
| 21 | SS 6.3, 11 | A filing is one fact per ticker under its CIK | `edgar_atom`, `edgar_submissions.tickers_for_cik` | `test_edgar_atom.py::test_a_filing_becomes_one_fact_per_ticker_under_its_cik`, `test_edgar_submissions.py::test_every_ticker_under_a_cik_is_kept_in_file_order` |
| 22 | SS 8.1 rule 1, 8.4 | J3's web tools are called directly only (no code-execution sandbox) | `llm_prompts.j3_tools` | `test_llm_prompts.py::test_j3_tools_only_server_web_tools` |
| 23 | SS 8.6 | The estimate is the tool loop's worst case | `llm_ledger.estimate` | `test_llm_ledger.py::test_the_j3_estimate_covers_a_long_tool_loop`, `test_estimate_is_worst_case` |

## Third review (2026-11-01)

Each row is a defect the third review found; its test fails on the code before the fix.

| # | Spec | Rule | Code | Tests |
|---|---|---|---|---|
| 1 | SS 6.4, 7.1, 7.2 | A date notice naming trial data or a PDUFA update is `earnings_date` (tried first), never a gate-6 catalyst; gate 5 reads its date | `classify.WIRE_MATCH_ORDER`, `scout_reader.gates.NEVER_PASS` | `test_classify.py::test_a_biotech_date_notice_is_earnings_date_not_fda_or_trial`, `test_reader_gate5.py::test_a_biotech_date_notice_blocks_the_hold_end_to_end`, `test_reader_gate6.py::test_never_pass_classes` |
| 2 | SS 7.2 | An `earnings_date` fact with `published_at` null is read (first_seen_at for lookback and year), never skipped | `scout_reader.gates._announced` | `test_reader_gate5.py::test_an_announcement_without_published_at_is_not_skipped`, `test_an_old_announcement_without_published_at_is_outside_the_lookback` |
| 3 | SS 6.4, 7.1 | A reverse-split 8-K (5.03 with 3.03, or 5.03 with 8.01/7.01) is never `contract_or_other` | `edgar_parse.filing_class` | `test_classify.py::test_a_reverse_split_8k_from_live_captions_is_never_a_catalyst` |
| 4 | SS 6.4, 14 | ATM sales agreements, equity lines, warrant inducements, convertible notes and debt exchanges are `offering`, not `contract` | `config.DEFAULT_CLASS_KEYWORDS`, `config.example.toml` | `test_classify.py::test_dilution_agreements_are_offering_not_contract` |
| 5 | SS 6.4, 7.1 | "Announces Conference Call to Discuss Financial Results for ... on <date>" is `earnings_date` | `config` | `test_classify.py::test_common_date_notice_wordings_are_earnings_date` |
| 6 | SS 6.3 | Nasdaq CM, Capital Market, Global Market, OTC Pink, "/" between tags, prose in a tag and "(Nasdaq - ACME)" give the ticker | `tickers`, `config.ticker_exchange_prefixes` | `test_tickers.py::test_common_exchange_tag_spellings` |
| 7 | SS 6.4, 7.2 | "Sets ... Earnings Call for", "Conference Call Date", "Results Release Date", "to Webcast", "Notice of ... Earnings Call" are `earnings_date`; Phase 2b/IIb, breakthrough device designation and "Reports Q3" are classed | `config` | `test_classify.py::test_common_date_notice_wordings_are_earnings_date`, `test_fail_closed_misses_are_classed` |
| 8 | SS 10.2 brief | A restart after 08:20 ET keeps today's brief; the brief waits for a live beat | `jobs_brief` (`AfterLiveBeat`, `_seed_brief`) | `test_brief.py::test_a_restart_after_the_brief_time_keeps_todays_brief`, `test_a_start_after_the_brief_time_writes_the_brief_after_a_live_beat`, `test_a_brief_written_by_hand_before_the_brief_time_is_replaced` |
| 9 | SS 10.2 brief | Overnight catalysts: published since the prior close, newest first, offering filings folded per issuer | `brief_sections.catalysts`, `fold_offering_filings`, `brief.j4_items` | `test_brief.py::test_overnight_catalysts_are_newest_first_and_bank_notes_are_folded` |
| 10 | DESIGN 8 | `STOP` or `STOP.txt` stops Scout and is deleted; a leftover one is deleted at start | `app.run`, `paths.stop_files`, `README.md` | `test_app.py::test_stop_file_stops_the_loop_and_is_deleted`, `test_a_stop_file_left_before_the_start_is_deleted_and_scout_runs` |
| 11 | DESIGN 8 | Update Scout runs `git pull` inside one block ending in `exit /b` | `Update Scout.bat` | `test_bat_files.py::test_update_runs_git_pull_inside_one_block_that_exits` |
| 12 | DESIGN 8 | A refused second start says another Scout is running (exit code 4) | `app.EXIT_ALREADY_RUNNING`, `Start Scout.bat` | `test_bat_files.py::test_start_scout_says_when_another_scout_is_running`, `test_app.py::test_second_scout_refused` |
| 13 | DESIGN 2.1 | Log lines show ET with the zone | `logs.EtFormatter` | `test_app.py::test_log_lines_show_et_with_the_zone` |
| 14 | SS 2.8, DESIGN 2.2 | A DTD or entity declaration is refused in any encoding (UTF-16 included) | `edgar_parse.refuse_dtd`, `safe_xml_root`, `halts.parse_halts` | `test_edgar_parse.py::test_a_dtd_in_a_utf16_body_is_refused`, `test_an_encoding_expat_cannot_read_is_schema_drift`, `test_halts.py::test_a_dtd_in_a_utf16_halt_feed_is_refused` |

## Out of scope for this build (SS 15 steps 6-11, SS 4.2-4.4)
Install at `E:\Scout` and the five-day operational acceptance (step 6); Radar, Pilot and Guardian
changes (steps 7 and 9); the verification period and holds (steps 8 and 10); the Webull probe
(step 11, SS 7.4); phase 2-4 sources. Live captures that replace the `_SYNTHETIC` fixtures are part of
step 2's owner tasks and are not yet done.
