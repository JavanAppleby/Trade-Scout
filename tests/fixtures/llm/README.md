# LLM fixtures (synthetic)

`_SYNTHETIC`: every file here was written by hand from the documented Messages API response shape
(content blocks `server_tool_use`, `web_search_tool_result`, `web_fetch_tool_result`, `text`;
`stop_reason`; `usage` with `server_tool_use`). None is a captured API response: tests never call
the Claude API (SCOUT_SPEC 2.11, 14). Written 2026-11-01 by the llm unit.

- `j3_response.json`: a J3 research reply whose citation passes every SS 8.4 check, plus a second
  citation whose URL appears in no result block (must be rejected).
