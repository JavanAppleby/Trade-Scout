# Wire-unit fixtures

Written 2026-11-01. Every file except `radar_shared_submissions.json`'s `expected` block is
**synthetic** (DESIGN D11) and carries a `_SYNTHETIC` marker (an XML comment or a JSON key). They were
written by hand from the shapes in SCOUT_SPEC 4.1, 6.3 and SEC's documented formats. The company
names, tickers, CIKs and text are invented. Replace them with trimmed live captures at build step 2
(owner-run, outside tests).

| File | Shape | Notes |
|---|---|---|
| `edgar_getcurrent_8k.atom` | SEC `browse-edgar?action=getcurrent&type=8-K&count=100&output=atom` | 7 entries: items 2.02/9.01, 1.01+3.02, 3.01, 8.01 (CIK not in the map), a "(Subject)" and a "(Reporting)" entry with the same accession, an 8-K/A with 5.03. Not fetched: SEC fair access needs the owner's contact email. |
| `edgar_getcurrent_424b.atom` | as above, `type=424B` | 424B5, 424B3, 424B4. Whether `type=424B` returns every 424B* form is UNVERIFIED (D6). |
| `edgar_getcurrent_empty.atom` | as above, `type=NT 10-K` | No entries. |
| `submissions_calendar_filer.json` | `data.sec.gov/submissions/CIK##########.json` | fiscalYearEnd 1231; 10-Q, 10-K, 8-K 2.02, S-3. |
| `submissions_june_fy_filer.json` | as above | fiscalYearEnd 0630; 10-K, NT 10-K, 10-Q, NT 10-Q, and one row with an unreadable acceptance time. |
| `company_tickers_exchange_excerpt.json` | `www.sec.gov/files/company_tickers_exchange.json` | A CIK with two tickers (first wins), a null exchange, a broken row. |
| `prnewswire.rss` | PR Newswire `news-releases-list.rss` | 20 items, starts with a BOM. Shape checked against one live read on 2026-11-01 (User-Agent "Trade-Scout fixture capture"): RSS 2.0, 20 items, guid = link, pubDate "+0000", exchange tags such as "(NASDAQ: GOOGL, GOOG)" in the description, not the title. No live text is kept (the RSS terms are unread, SS 4.1 #4). |
| `category_wire.rss` | an invented second wire (`TEST_WIRE` in `helpers_wire.py`) | 3 items: tickers in `<category domain=".../rss/stock">Nasdaq:XXX</category>`, pubDate without seconds. First written as a GlobeNewswire guess; that source was removed on 2026-11-03 (it refuses automated readers), and the file now tests category tickers and a second gate-6 wire. |
| `radar_shared_submissions.json` | inputs + Radar's outputs | Inputs shaped like Radar's `tests/test_edgar.py` helpers (Radar has no EDGAR fixture files). `expected` was produced once, on 2026-11-01, by Radar's own `radar/catalysts_edgar.py` (commit 6c971c8, sha256 379d46e1...b03c3) run from `E:\Radar_dev\.venv` with `python -B` (read-only). `tests/test_edgar_parse.py` checks Scout's copy gives the same results. |
