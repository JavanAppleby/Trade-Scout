# Jason level fixtures (unit `levels`)

Copied byte-for-byte on 2026-11-01 from the old platform, `ai-trading-platform` (local repo
`E:\AI Trading Platform`), commit `d62a282659f4c70fba7450471a2add98eef5af82`. The copies were taken
from a scratch copy of that commit, never from the repo folder itself.

| File here | Source path at that commit | sha256 |
|---|---|---|
| `jason_levels_20260915.json` | `tests/fixtures/jason_levels_20260915.json` | `a4eb24d92a88afc992a11ccb0aaa39f672f314cc58444195a1591c5e96cba7cb` |
| `jason_levels_live_20260915.json` | `config/jason_levels.json` | `da92ee49f9a0eab89fa0c051a9c49b1181121bbf21d879dc08edba73a4e8e1a7` |

- `jason_levels_20260915.json` is the old platform's test fixture: 6 setups hand-transcribed from
  the 2026-09-15 Technical Tuesday digest.
- `jason_levels_live_20260915.json` is the 2026-09-15 read itself, the reference SCOUT_SPEC 5.1
  names (76 setups, 206 declared levels). It carries real ASR artefacts (for example ETH's
  `"2K, 21, $2,85.84"` and TSLA's `33874` alternative), which is why the loader tests read it.
  Sentences in `notes` about the owner's own positions and brokers were removed (2026-11-01), so
  the hash differs from the source file; nothing the loader reads changed.

Neither file has `source_kind` (the key Scout adds, SCOUT_SPEC 5.1), so both load as `"tuesday"`
with one warning. Do not edit them: `tests/test_levels.py` checks the sha256 values above.
Private test data; not for redistribution.
