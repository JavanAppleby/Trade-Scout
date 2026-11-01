# Inbox fixtures (synthetic)

Invented files in the inbox format of `docs/INBOX.md`, written for the tests on 2026-11-01. No line
is real Paper Gains Discord text: tickers (ACME, BETA, GAMA, DELT, EPSI, ZETA, THETA, OMEG, KAPA, LAMB, MUON,
NUON, PIXL), prices,
headlines, server and channel ids are made up. Message ids are valid Discord snowflakes whose embedded
time matches each line's `posted_at` (the check Scout makes).

| File | What it shows |
|---|---|
| `jason/2026-11-02.daily.json` | a daily card mapped per SCOUT_SPEC 16 Q12: long trigger -> `trigger`, long targets -> `target`, short trigger -> `invalidation`, short targets -> `support`, expiring on the session date |
| `jason/2026-11-02.weekly.json` | a weekly plan: `direction` kept, expiring on the week's last day (`expires_default`) |
| `discord/daily-watchlist/20261102T131500Z.jsonl` | a `levels_card` with its AM ER / PM ER lists as `earnings` |
| `discord/open-outcrier/20261102T133000Z.jsonl` | an `earnings_schedule` line, an earnings-reaction `post`, and one bad line (permalink does not end in the message id) |
| `discord/brianna-news/20261102T132900Z.jsonl` | a "TICKER - headline" catalyst |
| `discord/major-headlines/20261102T131000Z.jsonl` | a market-wide headline with no ticker |
| `scanners/15m-scanner/20261102T135000Z.jsonl` | three `scanner_alert` cards ("Outside Bar - 15m (In Force)", "Inside Bar - 15m" with a `$` ticker and `BRK.B`, "Failed 2D - 15m"). Kept outside `discord/` (copied by `helpers_discord.copy_scanner_fixtures`) |
| `scanners/daily-scanner/20261102T133000Z.jsonl` | a "Outside Bar - Daily (Current Candle)" card |
| `scanners/weekly-scanner/20261102T133000Z.jsonl` | a weekly card, and one bad line (timeframe `daily` in the weekly folder: `scanner:timeframe_not_channel`) |
