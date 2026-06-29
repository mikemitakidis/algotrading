#!/usr/bin/env python3
"""Runtime registry FOCUS_SIZE shadow-run (read-only, fixture-backed, no network).

Validates the REAL registry -> FOCUS_SIZE cap -> bot.scanner.scan_cycle path,
using the existing US default scan-ready universe (536) capped to FOCUS_SIZE
(default 150). A deterministic fixture data provider is monkeypatched in place
of the live provider, so Yahoo/yfinance is never called.

Read-only / side-effect-free:
  - focus = get_scan_ready_symbols()[:FOCUS_SIZE]  (US default registry, capped)
  - conn = None  -> no DB writes, no flywheel log_candidate path
  - fixture provider replaces bot.providers.get_provider (the callable
    bot.data.fetch_bars resolves at call time), serving deterministic bars for
    ARBITRARY requested US symbols
  - no Telegram (scan_cycle only logs + returns; main()'s notifier/insert path
    is never invoked here)
  - no broker / live / paper

Does NOT load the global 193 set, does NOT load the UK pilot (unless explicitly
passed via --source uk_pilot), no HK / Europe. Committed report is SIMULATED;
live output goes to /tmp. This module is a tool; nothing in bot/ imports it.
"""
import argparse
import datetime
import json
import time
from pathlib import Path
from unittest import mock

import pandas as pd

from bot.providers.base import DataProvider
from bot.universe.active_selection import get_scan_ready_symbols

_PATCH_TARGET = "bot.providers.get_provider"
_DEFAULT_FOCUS_SIZE = 150


def _make_bars(n_days: int, end_date: str, seed: float) -> pd.DataFrame:
    """Deterministic UTC-indexed daily OHLCV (lowercase cols), seeded per
    symbol so every requested symbol gets its own reproducible series."""
    end = datetime.datetime.fromisoformat(end_date).replace(
        tzinfo=datetime.timezone.utc)
    idx = pd.date_range(end=end, periods=n_days, freq="D", tz="UTC")
    rows = []
    price = 100.0 + (seed % 50)
    for i in range(n_days):
        drift = 0.05 * i
        osc = 2.0 * ((i % 7) - 3) / 3.0
        close = price + drift + osc
        rows.append((close - 0.5 * osc, close + 1.0, close - 1.0, close,
                     float(1_000_000 + (i % 5) * 25_000 + int(seed * 97))))
    return pd.DataFrame(
        rows, index=idx,
        columns=["open", "high", "low", "close", "volume"])


def _seed_for(symbol: str) -> float:
    """Stable per-symbol seed derived from the ticker (deterministic)."""
    return float(sum(ord(c) for c in symbol) % 500)


class ArbitrarySymbolFixtureProvider(DataProvider):
    """Offline DataProvider serving deterministic bars for ANY requested
    symbol (so a 150/300/536-symbol US shadow run is fully exercised).
    Records requested symbols so the harness can assert scope."""

    def __init__(self, end_date="2026-06-26", n_days=120):
        self.end_date = end_date
        self.n_days = n_days
        self.requested_symbols = []
        self.fetch_calls = 0

    @property
    def name(self) -> str:
        return "fixture-arbitrary (focus-cap shadow-run, offline)"

    def fetch_bars(self, symbols, period, interval):
        self.fetch_calls += 1
        out = {}
        for s in symbols:
            self.requested_symbols.append(s)
            out[s] = _make_bars(self.n_days, self.end_date, _seed_for(s))
        return out

    def fetch_bars_range(self, sym, interval, start, end):
        return None, "not_implemented_in_fixture"


def select_focus(focus_size=_DEFAULT_FOCUS_SIZE, source="us_default"):
    """Select the focus list. Default: US scan-ready registry capped to
    focus_size. source='uk_pilot' is explicit opt-in only."""
    if source == "uk_pilot":
        from bot.universe.uk_pilot import get_uk_pilot_symbols
        base = get_uk_pilot_symbols()
    elif source == "us_default":
        base = get_scan_ready_symbols()
    else:
        raise ValueError("unknown source: %s" % source)
    return base[:focus_size]


def _min_config():
    return {
        "strategy": "default",
        "routing": {"etoro_min_tfs": 4, "ibkr_min_tfs": 2, "min_valid_tfs": 1},
    }


def run_shadow(focus_size=_DEFAULT_FOCUS_SIZE, source="us_default",
               config=None, end_date="2026-06-26"):
    """Run the REAL scan_cycle against the fixture for the capped focus set."""
    from bot.scanner import scan_cycle
    focus = select_focus(focus_size=focus_size, source=source)
    config = config or _min_config()
    fixture = ArbitrarySymbolFixtureProvider(end_date=end_date)

    t0 = time.monotonic()
    with mock.patch(_PATCH_TARGET, return_value=fixture):
        signals, meta = scan_cycle(focus, config, conn=None, cycle_id=0)
    elapsed = time.monotonic() - t0

    requested_unique = sorted(set(fixture.requested_symbols))
    return {
        "source": source,
        "focus_size": focus_size,
        "n_focus": len(focus),
        "focus_sample": focus[:10],
        "fixture_fetch_calls": fixture.fetch_calls,
        "n_requested_unique": len(requested_unique),
        "requested_unique_sample": requested_unique[:10],
        "n_signals": len(signals),
        "symbols_scanned": (meta or {}).get("symbols_scanned"),
        "elapsed_seconds": round(elapsed, 4),
    }


def render(result, data_source="simulated_fixture"):
    L = []
    L.append("# Runtime Registry — FOCUS_SIZE Shadow-Run")
    L.append("")
    L.append("- report_type: **real scan_cycle FOCUS_SIZE shadow-run "
             "(fixture-backed)**")
    L.append("- universe_source: **%s** (US default scan-ready registry, "
             "capped)" % result["source"])
    L.append("- focus_size: **%d**" % result["focus_size"])
    L.append("- data_source: **%s**" % data_source)
    L.append("- network: **disabled**")
    L.append("- provider: **fixture-arbitrary (monkeypatched; yfinance NOT "
             "called)**")
    L.append("- not_live_yfinance: **true**")
    L.append("- symbols_selected: **%d**" % result["n_focus"])
    L.append("- focus_sample: %s"
             % ", ".join("`%s`" % s for s in result["focus_sample"]))
    L.append("- fixture_fetch_calls: **%d**" % result["fixture_fetch_calls"])
    L.append("- unique_symbols_requested: **%d**"
             % result["n_requested_unique"])
    L.append("- meta.symbols_scanned: **%s**" % result["symbols_scanned"])
    L.append("- signals_returned: **%d**" % result["n_signals"])
    L.append("- elapsed_seconds: **%s**" % result["elapsed_seconds"])
    L.append("")
    L.append("> Read-only shadow run of the REAL bot.scanner.scan_cycle with "
             "focus sourced from get_scan_ready_symbols()[:FOCUS_SIZE] (US "
             "default registry) and a monkeypatched fixture provider. No "
             "Yahoo/yfinance, no network, no DB writes (conn=None), no "
             "Telegram, no broker / live / paper. The global 193 set and the "
             "UK pilot are NOT loaded (unless --source uk_pilot is explicit).")
    L.append("")
    L.append("## Safety confirmation")
    L.append("")
    L.append("- real scan_cycle invoked; focus = US scan-ready capped at "
             "FOCUS_SIZE=%d" % result["focus_size"])
    L.append("- fixture provider used; yfinance not called; no network")
    L.append("- conn=None -> no DB insert / no flywheel log_candidate")
    L.append("- no Telegram; no broker / live / paper; no orders")
    L.append("- no global 193 load; no UK pilot (default); no HK; no Europe")
    L.append("- no main.py / bot/ edit; no default-path change")
    L.append("")
    return "\n".join(L)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--focus-size", type=int, default=_DEFAULT_FOCUS_SIZE)
    ap.add_argument("--source", choices=("us_default", "uk_pilot"),
                    default="us_default")
    ap.add_argument("--report",
                    default="reports/runtime_registry_focus_cap_shadow_run.md")
    ap.add_argument("--json-out", default="")
    ap.add_argument("--end-date", default="2026-06-26")
    args = ap.parse_args()
    result = run_shadow(focus_size=args.focus_size, source=args.source,
                        end_date=args.end_date)
    rp = Path(args.report)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(render(result), encoding="utf-8")
    print("wrote %s" % rp)
    if args.json_out:
        Path(args.json_out).write_text(json.dumps(result, indent=2))
        print("wrote %s" % args.json_out)
    print("source=%s focus_size=%d selected=%d requested_unique=%d "
          "signals=%d elapsed=%ss"
          % (result["source"], result["focus_size"], result["n_focus"],
             result["n_requested_unique"], result["n_signals"],
             result["elapsed_seconds"]))


if __name__ == "__main__":
    main()
