# Halt fixtures (unit macro)

All files here are **synthetic** (DESIGN D11), hand-built on 2026-11-01 from the shapes in
SCOUT_SPEC 4.1 #6 and 6.3; no network was used. The lead replaces them with live captures of
`https://www.nasdaqtrader.com/rss.aspx?feed=tradehalts` at build step 2.

- `tradehalts_SYNTHETIC.xml`: UTF-8 BOM; `ndaq` namespace; items deliberately out of time order;
  Market `NASDAQ` and the letters `N`, `Q`, `P`; a 2019 row (OLDC, T12, never resumed); LUDP and M
  codes; empty resumptions (WXYZ, OLDC); a resumption scheduled after the test clock (NEWS, 10:30 ET);
  fractional HaltTime seconds (ABCD `.123`, ARCA `.1`); a resumption with no ResumptionDate (ARCA).
- `tradehalts_empty_SYNTHETIC.xml`: a feed with no items.

Drift variants (a missing field, an unknown Market form, a bad time) are built in the tests from
the first file.
