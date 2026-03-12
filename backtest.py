#!/usr/bin/env python3
"""
Algo Trader v2 — Event-Driven Backtest
Tests 4-timeframe categorical scoring logic on 3 years of Alpaca historical data.
Run: python3 /opt/algo-trader/backtest.py
Results saved to: /opt/algo-trader/data/backtest_results.json
"""
import sys, os, json, time, logging
sys.path.insert(0, '/opt/algo-trader')
sys.path.insert(0, '/opt/algo-trader/config')

from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import yaml, sqlite3

os.makedirs('/opt/algo-trader/data', exist_ok=True)

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s',
    handlers=[logging.FileHandler('/opt/algo-trader/logs/backtest.log'), logging.StreamHandler()])
log = logging.getLogger(__name__)

# ── Config ────────────────────────────────────────────────────────────────────
INITIAL_CAPITAL   = 10000      # $10,000 starting capital
POSITION_SIZE_PCT = 0.02       # 2% of capital per trade
ATR_SL_MULTIPLIER = 2.0        # Stop loss = 2x ATR
ATR_TP_MULTIPLIER = 3.0        # Take profit = 3x ATR
BACKTEST_YEARS    = 3
MIN_BARS_DAILY    = 30
TEST_SYMBOLS      = [          # Representative sample for backtest speed
    'AAPL','MSFT','NVDA','GOOGL','AMZN','META','TSLA','AMD','AVGO','ORCL',
    'CRM','ADBE','INTC','QCOM','TXN','AMAT','PANW','CRWD','SNOW','DDOG',
    'JPM','GS','MS','BAC','V','MA','PYPL','BLK','WFC','C',
    'JNJ','UNH','PFE','ABBV','LLY','MRK','BMY','AMGN','GILD','CVS',
    'XOM','CVX','COP','EOG','SLB','PSX','VLO','MPC','OXY','HAL',
    'SPY','QQQ','IWM','DIA',
]

def get_client():
    with open('/opt/algo-trader/config/settings.yaml') as f:
        cfg = yaml.safe_load(f)
    from alpaca.data.historical import StockHistoricalDataClient
    return StockHistoricalDataClient(
        api_key=cfg['alpaca']['api_key'],
        secret_key=cfg['alpaca']['secret_key']
    )

def fetch_daily_bars(client, symbols, years=3):
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
    end   = datetime.utcnow() - timedelta(days=1)
    start = end - timedelta(days=365 * years)
    all_bars = {}
    for i in range(0, len(symbols), 20):
        batch = symbols[i:i+20]
        try:
            req = StockBarsRequest(
                symbol_or_symbols=batch,
                timeframe=TimeFrame(1, TimeFrameUnit.Day),
                start=start, end=end,
                feed='iex', adjustment='split'
            )
            df = client.get_stock_bars(req).df
            if df is None or df.empty:
                continue
            if isinstance(df.index, pd.MultiIndex):
                for sym in batch:
                    try:
                        s = df.loc[sym].copy()
                        if len(s) >= MIN_BARS_DAILY:
                            all_bars[sym] = s
                    except KeyError:
                        pass
        except Exception as e:
            log.warning(f"Fetch error batch {i//20}: {e}")
        time.sleep(0.5)
    log.info(f"Fetched daily data for {len(all_bars)} symbols")
    return all_bars

def compute_indicators_backtest(df, i):
    """Compute indicators at bar index i (no lookahead)."""
    window = df.iloc[max(0, i-90):i+1]
    if len(window) < 20:
        return None
    try:
        c = window['close'].astype(float)
        v = window['volume'].astype(float)
        h = window['high'].astype(float)
        l = window['low'].astype(float)

        delta = c.diff()
        up    = delta.clip(lower=0).rolling(14).mean()
        dn    = (-delta.clip(upper=0)).rolling(14).mean()
        rsi   = 100 - (100 / (1 + up / (dn + 1e-9)))

        ema12     = c.ewm(span=12, adjust=False).mean()
        ema26     = c.ewm(span=26, adjust=False).mean()
        macd_hist = (ema12 - ema26) - (ema12 - ema26).ewm(span=9, adjust=False).mean()

        ema20 = c.ewm(span=20, adjust=False).mean()
        ema50 = c.ewm(span=50, adjust=False).mean()

        n     = min(20, len(c))
        sma   = c.rolling(n).mean()
        std   = c.rolling(n).std()
        bb_up = sma + 2 * std
        bb_lo = sma - 2 * std
        bb_rng = float(bb_up.iloc[-1] - bb_lo.iloc[-1])
        bb_pos = float((c.iloc[-1] - bb_lo.iloc[-1]) / (bb_rng + 1e-9)) if bb_rng > 0 else 0.5

        vwap     = (c * v).cumsum() / (v.cumsum() + 1e-9)
        vwap_dev = float((c.iloc[-1] - vwap.iloc[-1]) / (vwap.iloc[-1] + 1e-9))

        obv       = (np.sign(c.diff()) * v).cumsum()
        obv_slope = float((obv.iloc[-1] - obv.iloc[-5]) / (abs(obv.iloc[-5]) + 1e-9)) if len(obv) > 5 else 0.0

        tr  = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        atr = float(tr.rolling(14).mean().iloc[-1])

        vol_ratio = float(v.iloc[-1] / (v.rolling(min(20, len(v))).mean().iloc[-1] + 1e-9))

        ind = {
            'rsi': float(rsi.iloc[-1]), 'macd_hist': float(macd_hist.iloc[-1]),
            'ema20': float(ema20.iloc[-1]), 'ema50': float(ema50.iloc[-1]),
            'bb_pos': bb_pos, 'vwap_dev': vwap_dev, 'obv_slope': obv_slope,
            'atr': atr, 'vol_ratio': vol_ratio, 'price': float(c.iloc[-1])
        }
        if any(not np.isfinite(x) for x in ind.values()):
            return None
        return ind
    except:
        return None

def score_signal(ind, direction):
    if ind is None:
        return 0
    if direction == 'long':
        m = 1 if (30 < ind['rsi'] < 75 and ind['macd_hist'] > 0)        else 0
        t = 1 if (ind['ema20'] > ind['ema50'] * 0.995)                   else 0
        vol = 1 if (ind['vwap_dev'] > -0.01 and ind['vol_ratio'] > 0.7)  else 0
    else:
        m = 1 if (ind['rsi'] > 52 and ind['macd_hist'] < 0)              else 0
        t = 1 if (ind['ema20'] < ind['ema50'] * 1.005)                   else 0
        vol = 1 if (ind['vwap_dev'] < 0.01 and ind['vol_ratio'] > 0.7)   else 0
    return m + t + vol

def run_backtest(client):
    log.info("=" * 60)
    log.info("STARTING BACKTEST — 3 Years, Daily TF, Event-Driven")
    log.info(f"Symbols: {len(TEST_SYMBOLS)} | Capital: ${INITIAL_CAPITAL:,}")
    log.info("=" * 60)

    all_bars = fetch_daily_bars(client, TEST_SYMBOLS, BACKTEST_YEARS)

    capital   = INITIAL_CAPITAL
    trades    = []
    equity_curve = [{'date': str(datetime.utcnow().date()), 'equity': capital}]

    for sym, df in all_bars.items():
        df = df.reset_index()
        in_trade   = False
        entry_price = 0
        sl = tp = 0
        direction  = 'long'
        entry_date = None

        for i in range(60, len(df)):
            bar  = df.iloc[i]
            date = str(bar.get('timestamp', bar.name))[:10]
            price = float(bar['close'])
            high  = float(bar['high'])
            low   = float(bar['low'])

            if in_trade:
                pnl_pct = 0
                closed  = False
                if direction == 'long':
                    if low <= sl:
                        pnl_pct = (sl - entry_price) / entry_price
                        closed  = True
                    elif high >= tp:
                        pnl_pct = (tp - entry_price) / entry_price
                        closed  = True
                else:
                    if high >= sl:
                        pnl_pct = (entry_price - sl) / entry_price
                        closed  = True
                    elif low <= tp:
                        pnl_pct = (entry_price - tp) / entry_price
                        closed  = True

                if not closed and i == len(df) - 1:
                    pnl_pct = (price - entry_price) / entry_price if direction == 'long' else (entry_price - price) / entry_price
                    closed  = True

                if closed:
                    pos_size = capital * POSITION_SIZE_PCT
                    pnl_usd  = pos_size * pnl_pct
                    capital += pnl_usd
                    trades.append({
                        'sym': sym, 'direction': direction,
                        'entry': entry_price, 'exit': price,
                        'entry_date': entry_date, 'exit_date': date,
                        'pnl_pct': round(pnl_pct * 100, 2),
                        'pnl_usd': round(pnl_usd, 2),
                        'win': pnl_usd > 0
                    })
                    equity_curve.append({'date': date, 'equity': round(capital, 2)})
                    in_trade = False
                continue

            # Look for entry signal (daily score >= 2 for backtest sensitivity)
            ind = compute_indicators_backtest(df, i)
            if ind is None:
                continue

            for d in ['long', 'short']:
                sc = score_signal(ind, d)
                if sc >= 2:
                    atr = ind['atr']
                    if d == 'long':
                        sl = price - ATR_SL_MULTIPLIER * atr
                        tp = price + ATR_TP_MULTIPLIER * atr
                    else:
                        sl = price + ATR_SL_MULTIPLIER * atr
                        tp = price - ATR_TP_MULTIPLIER * atr
                    in_trade    = True
                    entry_price = price
                    entry_date  = date
                    direction   = d
                    break

    # ── Stats ─────────────────────────────────────────────────────────────────
    total     = len(trades)
    wins      = sum(1 for t in trades if t['win'])
    losses    = total - wins
    win_rate  = round(wins / total * 100, 1) if total else 0
    total_pnl = round(sum(t['pnl_usd'] for t in trades), 2)
    avg_win   = round(np.mean([t['pnl_usd'] for t in trades if t['win']]), 2) if wins else 0
    avg_loss  = round(np.mean([t['pnl_usd'] for t in trades if not t['win']]), 2) if losses else 0
    profit_factor = round(abs(sum(t['pnl_usd'] for t in trades if t['win']) /
                    (sum(t['pnl_usd'] for t in trades if not t['win']) + 1e-9)), 2)

    # Max drawdown
    eq = [e['equity'] for e in equity_curve]
    peak = INITIAL_CAPITAL
    max_dd = 0
    for e in eq:
        peak = max(peak, e)
        dd   = (peak - e) / peak
        max_dd = max(max_dd, dd)

    results = {
        'run_date':       datetime.utcnow().isoformat(),
        'symbols_tested': len(all_bars),
        'years':          BACKTEST_YEARS,
        'initial_capital': INITIAL_CAPITAL,
        'final_capital':  round(capital, 2),
        'total_return_pct': round((capital - INITIAL_CAPITAL) / INITIAL_CAPITAL * 100, 1),
        'total_trades':   total,
        'wins':           wins,
        'losses':         losses,
        'win_rate_pct':   win_rate,
        'profit_factor':  profit_factor,
        'avg_win_usd':    avg_win,
        'avg_loss_usd':   avg_loss,
        'total_pnl_usd':  total_pnl,
        'max_drawdown_pct': round(max_dd * 100, 1),
        'top_trades':     sorted(trades, key=lambda x: x['pnl_usd'], reverse=True)[:10],
        'worst_trades':   sorted(trades, key=lambda x: x['pnl_usd'])[:10],
        'equity_curve':   equity_curve[-50:],  # last 50 equity points
    }

    out = '/opt/algo-trader/data/backtest_results.json'
    with open(out, 'w') as f:
        json.dump(results, f, indent=2)

    log.info("=" * 60)
    log.info("BACKTEST COMPLETE")
    log.info(f"  Symbols tested : {results['symbols_tested']}")
    log.info(f"  Total trades   : {total}")
    log.info(f"  Win rate       : {win_rate}%")
    log.info(f"  Profit factor  : {profit_factor}")
    log.info(f"  Total return   : {results['total_return_pct']}%")
    log.info(f"  Max drawdown   : {results['max_drawdown_pct']}%")
    log.info(f"  Final capital  : ${capital:,.2f}")
    log.info(f"  Results saved  : {out}")
    log.info("=" * 60)
    return results

if __name__ == '__main__':
    client = get_client()
    run_backtest(client)
