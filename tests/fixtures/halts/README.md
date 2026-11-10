# Halt fixtures (unit macro)

All files here are **synthetic** (DESIGN D11), hand-built on 2026-11-01 from the shapes in
SCOUT_SPEC 4.1 #6 and 6.3; no network was used. The lead replaces them with live captures of
`https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts` at build step 2.

- `tradehalts_SYNTHETIC.xml`: UTF-8 BOM; `ndaq` namespace; items deliberately out of time order;
  Market `NASDAQ` and the letters `N`, `Q`, `P`; a 2019 row (OLDC, T12, never resumed); LUDP and M
  codes; empty resumptions (WXYZ, OLDC); a resumption scheduled after the test clock (NEWS, 10:30 ET);
  fractional HaltTime seconds (ABCD `.123`, ARCA `.1`); a resumption with no ResumptionDate (ARCA).
- `tradehalts_empty_SYNTHETIC.xml`: a feed with no items.
- `tradehalts_dated_SYNTHETIC.xml` (0.3.1): hand-built on 2026-11-10 in the shape of the dated feed
  (`&haltdate=MMDDYYYY`) as read live that day, with made-up rows: its element order; `Mkt` (a letter)
  where the live feed has Market; HaltTime padded with 22 spaces between the seconds and the fraction
  (`09:31:05` + 22 spaces + `.123`), or padded with no fraction (NEWS, WXYZ); empty fields
  self-closing; CRLF line endings; a description table that is not read. NEWS, ABCD, WXYZ and ARCA are
  the 2 Nov halts of `tradehalts_SYNTHETIC.xml` (the same keys once the padding is left out); PADS has
  whitespace around every field; MISS and PADS ended before the evening read; LATE (19:50 ET, T1) is
  still open.

Drift variants (a missing field, an unknown Market form, a bad time) are built in the tests from
the first file.
