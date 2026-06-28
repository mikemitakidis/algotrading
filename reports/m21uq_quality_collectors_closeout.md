# M21.UQ — Global Quality Collectors / Gates — Closeout / Status

**Read-only status.** The M21.UQ quality collectors/gates framework is complete
as a read-only foundation. It evaluates EXISTING global candidates only; it adds
no symbols, sets no scan_ready, and activates no runtime path.

- main HEAD at closeout: `5d893ea5fa06a72997567d027cde283d595ace5e`
- milestone: **M21.UQ** (naming preserved; not renamed)

## Scope — existing candidates only

- total global candidates: **193**
- UK: **100**
- HK: **93**
- EU: **0**

No Europe/Japan/China/ADR source work was done here.

## Default offline structural run

- data_source: **structural_only** (network disabled)
- attempted: **193**
- passed: **193**
- failed: **0**
- `liquidity_unknown` is a **non-fatal** warning at this stage (inactive
  candidates carry null liquidity by design)

## Provider-backed yfinance mode

- **explicit-only**: CLI default is `--provider none` (structural-only); the
  yfinance path runs only when `--provider yfinance` is passed.
- **no default network calls**.
- **no runtime path imports it** — nothing in `bot/` or `main.py` imports the
  quality tool or the yfinance adapter.
- **reports only**: provider-backed mode writes report artifacts, never configs;
  no live reports are committed in the repo.

## Live VPS smoke result

- UK sample: **passed 5/5**.
- HK sample: initially **failed 5/5** because Yahoo/yfinance returned
  `YFRateLimitError` ("Too Many Requests. Rate limited.").
- This was correctly diagnosed as **provider availability**, NOT a
  symbol-quality failure.
- Deterministic confirmation now maps the HK rate-limit to
  **`provider_rate_limited`**, not `ohlcv_empty` or `volume_missing_or_zero`:
  - `HKEX:0001 0001.HK ['provider_rate_limited']`
  - `HKEX:0002 0002.HK ['provider_rate_limited']`
  - `HKEX:0003 0003.HK ['provider_rate_limited']`
  - `HKEX:0005 0005.HK ['provider_rate_limited']`
  - `HKEX:0006 0006.HK ['provider_rate_limited']`

## Provider availability semantics

- `provider_rate_limited` — could not evaluate due to a provider throttle /
  rate limit (a live check could NOT be completed; not a data-quality verdict).
- `provider_fetch_error` — provider/network failure (also "could not
  evaluate").
- `ohlcv_empty` — a true empty dataset with NO provider exception.

A rate-limited or errored symbol is reported with the provider code and the
OHLCV/volume checks are skipped, so it is never double-labelled `ohlcv_empty` /
`volume_missing_or_zero`.

## Safety confirmations

- no `configs/universe/global_expanded.json` change
- no `configs/universe/source_registry.json` change
- no scan_ready change (still 536 US scan-ready symbols)
- no runtime activation; `global_in_default_paths=False`
- no scanner / main / dashboard / risk / broker / live / paper changes
- no symbols added (global_symbols still 193)
- no Europe / Japan / China / ADR source work
- no live reports committed

## Status

**M21.UQ is closed as a read-only quality foundation.**

NOT started (require explicit operator approval before any work):
- **M21.UR** — Regional universe activation
- **Runtime Registry Activation** — separate config task
- any **scan_ready activation** of the global universe

These remain gated; nothing in M21.UQ activates or schedules them.
