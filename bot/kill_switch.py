"""
bot/kill_switch.py
Persistent kill switch for live trading.

State stored in data/kill_switch.json — persists across restarts.
Checked before every broker submission in ibkr_broker.py submit().

States:
  {"active": false}  — trading enabled (default)
  {"active": true}   — ALL broker submissions blocked immediately

Design decisions:
  - Kill switch blocks NEW submissions only
  - Does NOT auto-cancel open orders (risk of worse outcomes from
    mis-sequenced bracket cancellation — operator cancels manually via Gateway)
  - State persists across bot restart so kill switch stays active
    if bot crashes and restarts while operator investigates
  - Fail-safe: if state file is unreadable, kill switch is treated as ACTIVE
"""
import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

BASE_DIR        = Path(__file__).resolve().parent.parent
KILL_SWITCH_PATH = BASE_DIR / 'data' / 'kill_switch.json'


def _read_state() -> dict:
    try:
        if KILL_SWITCH_PATH.exists():
            return json.loads(KILL_SWITCH_PATH.read_text())
        return {'active': False}
    except Exception as e:
        log.error('[KILL_SWITCH] Failed to read state — defaulting to ACTIVE: %s', e)
        return {'active': True}   # fail-safe: if unreadable, treat as active


def _write_state(active: bool, reason: str = '') -> None:
    try:
        KILL_SWITCH_PATH.parent.mkdir(parents=True, exist_ok=True)
        state = {
            'active':     active,
            'reason':     reason,
            'updated_at': datetime.now(timezone.utc).isoformat(),
        }
        KILL_SWITCH_PATH.write_text(json.dumps(state, indent=2))
    except Exception as e:
        log.error('[KILL_SWITCH] Failed to write state: %s', e)


def is_kill_switch_active() -> bool:
    """Returns True if kill switch is active (submissions blocked)."""
    return bool(_read_state().get('active', True))


def get_kill_switch_state() -> dict:
    """Returns full state dict for dashboard display."""
    state = _read_state()
    state.setdefault('active', True)
    state.setdefault('reason', '')
    state.setdefault('updated_at', None)
    return state


def activate_kill_switch(reason: str = 'Manual activation') -> dict:
    """Activate kill switch — blocks all new submissions."""
    log.warning('[KILL_SWITCH] ACTIVATED: %s', reason)
    _write_state(True, reason)
    return get_kill_switch_state()


def deactivate_kill_switch(reason: str = 'Manual deactivation') -> dict:
    """Deactivate kill switch — re-enables submissions."""
    log.warning('[KILL_SWITCH] DEACTIVATED: %s', reason)
    _write_state(False, reason)
    return get_kill_switch_state()


def ensure_default_state() -> None:
    """Called at startup — creates kill_switch.json if missing."""
    if not KILL_SWITCH_PATH.exists():
        _write_state(False, 'Initialised at startup')
        log.info('[KILL_SWITCH] Initialised: inactive')
    else:
        state = _read_state()
        log.info('[KILL_SWITCH] State on startup: active=%s reason=%s',
                 state.get('active'), state.get('reason', ''))
