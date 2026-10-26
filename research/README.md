# `research/` — offline M21 research package

Offline research code for the M21 research lanes (Issues #2 and #3). Nothing here trades, contacts a broker, sends messages, reads secrets or writes production data. Production code never imports it, and `test_m21_research_foundation.py` enforces both directions of that boundary.

| Package | Lane | Canonical tasks | Purpose |
|---|---|---|---|
| `research.common` | foundation | — | Canonical hashing, named RNG streams, OHLCV conventions, synthetic markets with known ground truth |
| `research.evidence` | A | B03, B04 | Counterfactual candidate/outcome labels with observability flags, effective N, gate economics |
| `research.integrity` | B | B05–B08, B20 | Trial ledger, pre-registration, purged validation, multiple-testing corrections, controls, report cards |
| `research.mllab` | C | B14–B17, B19, D07 | Model laboratory: baselines vs boosted trees, ensembles, meta-labeling, regimes, uncertainty, abstention |
| `research.genome` | D | B09–B11, A15 | Strategy-as-data, the four templates, validator, multi-symbol backtester |
| `research.agents` | E | B21, B22, D06 | Offline research-agent foundations: permissions, sandbox, knowledge library, mock flow |
| `research.data` | F | B01, B02, E05, E09 | Data inventory, OHLCV quality audit, snapshot manifests, vendor memo |

## Rules

- **Import boundary** (`research/boundary.py`): `bot.*` imports must come from the read-only allow-list (M16 store readers, M17 simulator and indicators, M18 ML, M19 scoring). Network, broker SDK, messaging, secret-loading and process-spawning modules are forbidden.
- **Writes** go only to caller-supplied paths. Research code never hard-codes `data/` or `.env`.
- **Determinism:** every random draw comes from `research.common.rng.stream(seed, *names)`.
- **Bar conventions** (`research/common/bars.py`): `ts_utc` is tz-aware UTC and marks the bar **close**. Synthetic US sessions run 14:30–21:00 UTC; daylight saving and exchange holidays are ignored.
- **Honesty:** synthetic results describe the machinery, never the market. Counterfactual R is never P&L. No result here is a validated edge.

## Tests

Each lane has a root test module named `test_m21_research_<lane>.py`, so `_test_isolation.py --all` picks it up:

```bash
python _test_isolation.py test_m21_research_foundation
```
