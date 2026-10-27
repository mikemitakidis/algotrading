# Historical data: vendor and licensing memo (B01, B02)

*Lane F · 27 Oct 2026 · Research memo. **Nothing has been purchased, subscribed to or connected.***

## 1. What we need, and why the current data is not enough

The research lanes need data that a backtest can trust:

1. **Survivorship-free US equities.** Delisted symbols must keep their full history, and the universe must be the one known at each date (point-in-time index membership).
2. **Split and dividend adjustment factors.** These should be stored separately from raw prices, so either view can be rebuilt.
3. **Daily bars at minimum.** 15m and 1h bars are needed for the M17 multi-timeframe strategies and the lane A finer-data resolution.
4. **Licensing that allows private research and backtesting** on our own infrastructure. That means a Linux VPS, not only a desktop app.

The repository holds **no historical bars** (lane F report §1). The production M16 store is filled from yfinance. yfinance is not survivorship-free and has no licence for systematic use, so it cannot support evidence-grade research on its own.

## 2. Vendors checked (facts verified on 27 Oct 2026 from the pages cited)

| vendor | delisted / point-in-time universe | history | delivery and platform | price (as published) | licence notes |
|---|---|---|---|---|---|
| **Norgate Data** (US Stocks) | **Platinum** and **Diamond** include delisted securities and historical index constituents. Silver and Gold have current securities only. | Platinum: daily from 1990. Diamond: daily from 1950. | Local database maintained by the **Norgate Data Updater, which Norgate describes as "a Windows application"**. Linux would need a Windows VM. | Not shown on the subscribe page. USD-priced, with a 10 % discount for 12 months versus 6. | "the data can only be licensed for personal use by individuals" |
| **Massive** (formerly Polygon.io) | "delisted tickers stay in the universe with their full history" | Starter 5 y, Developer 10 y, Advanced 20+ y. Tick data from 2003. | REST, WebSocket and flat files; Linux-friendly | Basic free (EOD, 2 y); Starter $29/mo; Developer $79/mo; Advanced $199/mo; Business $2,499/mo | Individual plans are for personal, non-professional use. Redistribution and display need Business. |
| **Sharadar** (via Nasdaq Data Link or QuantRocket) | "No survivorship bias: includes active and delisted tickers". Also S&P 500 constituents from 1957. | EOD prices from 1998; fundamentals from 1990 | API or bulk; Linux-friendly | Shown only after login | Through QuantRocket, use is limited to QuantRocket and redistribution is prohibited. **Publishing evaluations needs Sharadar's written approval.** Data must be deleted within 30 days of termination. Professional users must license through Nasdaq Data Link. |
| **EODHD** | Delisted US symbols available: EOD for all; fundamentals, dividends and splits for delistings after 2018; intraday for delistings after 2021 | Varies by ticker | API; Linux-friendly | Not shown on the delisted-data page | Personal and commercial plans are separate; terms were not reviewed |
| **Alpaca Market Data** | No statement on delisted symbols found | Since 2016 | API; Linux-friendly | Basic free (IEX; historical bars limited to older than the latest 15 minutes); **Algo Trader Plus $99/mo** (CTA and UTP feeds, "100% of market volume") | Not stated on the page |
| **Databento** | Not stated on the datasets index | Not stated on the index | API; Linux-friendly. Equities venues include Nasdaq TotalView-ITCH, NYSE Integrated and consolidated EQUS Mini/Summary, plus adjustment-factor, corporate-action and security-master reference sets. | Not stated on the index | Not reviewed |

**Not re-verified today:** the CRSP/WRDS academic route (institutional licence only), FirstRate Data, Tiingo and IBKR historical data. IBKR is excluded anyway, because research must not connect to the broker.

## 3. Assessment

| need | Norgate Platinum | Massive Developer/Advanced | Sharadar | EODHD |
|---|---|---|---|---|
| survivorship-free daily US equities | ✅ | ✅ (delisted kept) | ✅ | ✅ (partial before 2018) |
| point-in-time index membership | ✅ | ✗ not stated | S&P 500 only | ✗ not stated |
| intraday (15m/1h) | ✗ (daily) | ✅ | ✗ | ✅ for later delistings |
| runs on the Linux VPS | ⚠ Windows VM needed | ✅ | ✅ | ✅ |
| can publish research findings internally | personal use only | personal use only (individual plans) | ⚠ evaluations need approval | not reviewed |

**Recommendation for the operator's decision. This is not a purchase request.**

1. **Daily evidence-grade work:** Norgate Platinum is the strongest single source, because it covers delisted symbols, historical constituents and history back to 1990. It costs a Windows VM on the VPS or a separate machine, and it is personal-use only.
2. **Intraday work and a Linux-native API:** Massive at Developer ($79/mo, 10 y) or Advanced ($199/mo, 20+ y). Check that our use (private research by one individual) fits "personal, non-professional use" before subscribing.
3. **Avoid for now:** Sharadar through QuantRocket, because publishing evaluations needs approval. That conflicts with the review workflow on GitHub.
4. Whatever is chosen, data enters research only through **snapshot manifests** (`research.dataqa.snapshot`) and the **quality audit** (`research.dataqa.quality`). The manifest digest becomes the ledger's `data_digest`.

Prices and terms change. Re-check every figure on the vendor's own page before any decision. **Purchasing requires the operator's explicit approval.**

## 4. Licensing guard-rails for the research lanes

- Vendor data must never be committed to GitHub. Only manifests, digests and aggregate statistics may be committed.
- Personal-use licences: research outputs stay private to the operator; no redistribution.
- Every external dataset needs a manifest entry recording its vendor, licence tier and retrieval date.

## 5. Runbook: read-only inventory of the production store (requires approval)

These steps are written for the operator and **must not be run without explicit approval**. They read the M16 parquet root, write nothing into it and export no bars.

```bash
# on the VPS, as the bot user, from the repository checkout at the reviewed SHA
python - <<'PY'
import json
from bot.historical.schema import default_parquet_root
from research.dataqa.inventory import inventory_store
from research.dataqa.snapshot import build
root = default_parquet_root()
inv = inventory_store(root)                    # read-only: rows, spans, spacing, quality grades
man = build(root, name="m16-store")            # sha256 per file: the snapshot identity
print(json.dumps({"files": inv["files"], "groups": inv["groups"],
                  "grades": {k: v["grades"] for k, v in inv["quality"].items()},
                  "snapshot_digest": man["snapshot_digest"]}, indent=1))
PY
```

Only the printed aggregate summary, with no bars, should be pasted back into the review thread.

## Sources

- [Norgate Data: subscription levels](https://norgatedata.com/subscribe)
- [Norgate Data Updater overview](https://norgatedata.com/ndu-overview.php)
- [WealthLab: Norgate extension](https://www.wealth-lab.com/extension/detail/Norgate)
- [Massive (formerly Polygon.io): plans](https://massive.com/landing/earnings-historical-data)
- [QuantRocket: Sharadar data pricing and terms](https://quantrocket.com/pricing/data/sharadar)
- [EODHD: delisted stock data](https://eodhd.com/financial-apis/delisted-stock-companies-data-2)
- [Alpaca: About Market Data API](https://docs.alpaca.markets/docs/about-market-data-api)
- [Databento: datasets](https://databento.com/docs/datasets)
