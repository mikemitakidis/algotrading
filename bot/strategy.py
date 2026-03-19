"""
bot/strategy.py
Single source of truth for the live trading strategy.

All signal thresholds, routing rules, and risk parameters live here.
The bot reads from data/strategy.json on every cycle.
The dashboard writes to data/strategy.json when settings are saved.
If data/strategy.json is absent, DEFAULTS are used automatically.

Audit trail: every save appends one line to data/strategy_audit.jsonl
"""
import copy
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR       = Path(__file__).resolve().parent.parent
STRATEGY_PATH  = BASE_DIR / 'data' / 'strategy.json'
AUDIT_PATH     = BASE_DIR / 'data' / 'strategy_audit.jsonl'

# ─────────────────────────────────────────────────────────────────────────────
# DEFAULTS — canonical reference, never mutated at runtime
# ─────────────────────────────────────────────────────────────────────────────
DEFAULTS = {
    "version":    1,
    "updated_at": None,
    "updated_by": "defaults",

    # ── Timeframes ────────────────────────────────────────────────────────────
    # Each can be independently enabled/disabled.
    # period/interval are yfinance fetch params — do not change without testing.
    "timeframes": {
        "tf_1d":  {"enabled": True,  "label": "Daily (1D)",  "period": "3mo", "interval": "1d",  "resample": False},
        "tf_4h":  {"enabled": True,  "label": "4-Hour (4H)", "period": "1mo", "interval": "1h",  "resample": True},
        "tf_1h":  {"enabled": True,  "label": "1-Hour (1H)", "period": "15d", "interval": "1h",  "resample": False},
        "tf_15m": {"enabled": True,  "label": "15-Min (15m)","period": "5d",  "interval": "15m", "resample": False},
    },

    # ── Confluence ────────────────────────────────────────────────────────────
    # How many timeframes must independently agree for a signal to fire.
    "confluence": {
        "min_valid_tfs": 3,
    },

    # ── Long signal rules ─────────────────────────────────────────────────────
    # ALL three conditions (momentum, trend, volume) must pass on a timeframe
    # for that timeframe to count as a valid long agreement.
    "long": {
        "rsi_min":       30,     # RSI must be above this (not oversold extreme)
        "rsi_max":       75,     # RSI must be below this (not overbought)
        "macd_hist_gt":  0.0,    # MACD histogram must be positive (bullish momentum)
        "ema_tolerance": 0.005,  # EMA20 > EMA50 * (1 - tolerance) = uptrend
        "vwap_dev_min": -0.015,  # Price not more than 1.5% below VWAP
        "vol_ratio_min": 0.6,    # Volume at least 60% of 20-bar average
    },

    # ── Short signal rules ────────────────────────────────────────────────────
    "short": {
        "rsi_min":       50,    # RSI must be above this (not already oversold)
        "macd_hist_lt":  0.0,   # MACD histogram must be negative (bearish momentum)
        "ema_tolerance": 0.005, # EMA20 < EMA50 * (1 + tolerance) = downtrend
        "vwap_dev_max":  0.015, # Price not more than 1.5% above VWAP
        "vol_ratio_min": 0.6,   # Volume at least 60% of 20-bar average
    },

    # ── Risk parameters ───────────────────────────────────────────────────────
    # Stop and target are computed from entry price and ATR(14).
    # These values are logged with every signal for ML/backtesting use.
    # No broker execution in V1 — this is informational only.
    "risk": {
        "atr_stop_mult":   2.0,  # Stop loss   = entry - (ATR * stop_mult)
        "atr_target_mult": 3.0,  # Take profit = entry + (ATR * target_mult)
    },

    # ── Routing rules ─────────────────────────────────────────────────────────
    # Route is a label only in shadow mode. No real execution.
    "routing": {
        "etoro_min_tfs": 4,  # All 4 TFs agree → label ETORO (highest confidence)
        "ibkr_min_tfs":  2,  # 2–3 TFs agree  → label IBKR  (medium confidence)
        # Below ibkr_min_tfs → WATCH (logged only, not stored in DB as signal)
    },
}

# ─────────────────────────────────────────────────────────────────────────────
# Validation rules: field → (min, max, type)
# ─────────────────────────────────────────────────────────────────────────────
_VALIDATORS = {
    "confluence.min_valid_tfs":   (1,   4,    int),
    "long.rsi_min":               (1,   99,   float),
    "long.rsi_max":               (2,   100,  float),
    "long.macd_hist_gt":          (-10, 10,   float),
    "long.ema_tolerance":         (0,   0.1,  float),
    "long.vwap_dev_min":          (-0.5, 0.5, float),
    "long.vol_ratio_min":         (0,   5,    float),
    "short.rsi_min":              (1,   99,   float),
    "short.macd_hist_lt":         (-10, 10,   float),
    "short.ema_tolerance":        (0,   0.1,  float),
    "short.vwap_dev_max":         (-0.5, 0.5, float),
    "short.vol_ratio_min":        (0,   5,    float),
    "risk.atr_stop_mult":         (0.1, 20,   float),
    "risk.atr_target_mult":       (0.1, 50,   float),
    "routing.etoro_min_tfs":      (1,   4,    int),
    "routing.ibkr_min_tfs":       (1,   4,    int),
}


def _get_nested(d, dotpath):
    parts = dotpath.split('.')
    for p in parts:
        d = d.get(p, {})
    return d


def validate(cfg: dict) -> list:
    """Return list of error strings. Empty list = valid."""
    errors = []
    for path, (lo, hi, typ) in _VALIDATORS.items():
        section, key = path.split('.', 1)
        val = cfg.get(section, {}).get(key)
        if val is None:
            errors.append(f"Missing field: {path}")
            continue
        try:
            v = typ(val)
        except (ValueError, TypeError):
            errors.append(f"{path}: must be {typ.__name__}")
            continue
        if not (lo <= v <= hi):
            errors.append(f"{path}: must be between {lo} and {hi} (got {v})")

    # Cross-field checks
    long_cfg = cfg.get('long', {})
    if long_cfg.get('rsi_min', 0) >= long_cfg.get('rsi_max', 100):
        errors.append("long.rsi_min must be less than long.rsi_max")

    risk = cfg.get('risk', {})
    if float(risk.get('atr_target_mult', 3)) <= float(risk.get('atr_stop_mult', 2)):
        errors.append("risk.atr_target_mult must be greater than atr_stop_mult")

    routing = cfg.get('routing', {})
    if int(routing.get('ibkr_min_tfs', 2)) > int(routing.get('etoro_min_tfs', 4)):
        errors.append("routing.ibkr_min_tfs must be <= etoro_min_tfs")

    return errors


def load() -> dict:
    """
    Load strategy from data/strategy.json.
    Falls back to DEFAULTS if file is missing or invalid.
    Always returns a complete dict with all keys.
    """
    base = copy.deepcopy(DEFAULTS)
    if not STRATEGY_PATH.exists():
        return base

    try:
        saved = json.loads(STRATEGY_PATH.read_text())
    except Exception as e:
        log.warning('[STRATEGY] Could not load strategy.json (%s) — using defaults', e)
        return base

    # Deep-merge saved values into defaults (so new default keys always present)
    for section, val in saved.items():
        if isinstance(val, dict) and section in base and isinstance(base[section], dict):
            base[section].update(val)
        else:
            base[section] = val

    errs = validate(base)
    if errs:
        log.warning('[STRATEGY] Loaded strategy has validation issues (using anyway): %s', errs)

    return base


def save(cfg: dict, updated_by: str = 'dashboard') -> list:
    """
    Validate and save strategy to data/strategy.json.
    Returns list of errors (empty = success).
    Appends an audit entry on success.
    """
    errors = validate(cfg)
    if errors:
        return errors

    cfg = copy.deepcopy(cfg)
    cfg['updated_at'] = datetime.now(timezone.utc).isoformat()
    cfg['updated_by'] = updated_by
    cfg['version']    = int(cfg.get('version', 1)) + 1

    # Preserve timeframe period/interval (not user-editable, just toggles)
    defaults = copy.deepcopy(DEFAULTS)
    for tf_key, tf_val in defaults['timeframes'].items():
        if tf_key in cfg.get('timeframes', {}):
            cfg['timeframes'][tf_key]['period']   = tf_val['period']
            cfg['timeframes'][tf_key]['interval'] = tf_val['interval']
            cfg['timeframes'][tf_key]['resample'] = tf_val['resample']
            cfg['timeframes'][tf_key]['label']    = tf_val['label']

    try:
        STRATEGY_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STRATEGY_PATH.with_suffix('.tmp')
        tmp.write_text(json.dumps(cfg, indent=2, default=str))
        tmp.replace(STRATEGY_PATH)
        log.info('[STRATEGY] Saved v%d by %s', cfg['version'], updated_by)
    except Exception as e:
        return [f'Save failed: {e}']

    # Audit trail
    try:
        audit_entry = {
            'ts':         cfg['updated_at'],
            'by':         updated_by,
            'version':    cfg['version'],
            'confluence': cfg.get('confluence', {}),
            'long':       cfg.get('long', {}),
            'short':      cfg.get('short', {}),
            'risk':       cfg.get('risk', {}),
            'routing':    cfg.get('routing', {}),
        }
        with open(AUDIT_PATH, 'a') as f:
            f.write(json.dumps(audit_entry, default=str) + '\n')
    except Exception as e:
        log.debug('[STRATEGY] Audit write failed (non-fatal): %s', e)

    return []


def reset() -> dict:
    """Delete strategy.json so load() returns fresh defaults on next call."""
    try:
        if STRATEGY_PATH.exists():
            STRATEGY_PATH.unlink()
        log.info('[STRATEGY] Reset to defaults.')
    except Exception as e:
        log.warning('[STRATEGY] Reset failed: %s', e)
    return copy.deepcopy(DEFAULTS)


def get_audit(limit: int = 20) -> list:
    """Return last N audit entries as list of dicts (newest first)."""
    try:
        if not AUDIT_PATH.exists():
            return []
        lines = AUDIT_PATH.read_text().strip().splitlines()
        entries = []
        for line in reversed(lines[-limit:]):
            try:
                entries.append(json.loads(line))
            except Exception:
                pass
        return entries
    except Exception:
        return []
