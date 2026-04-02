#!/bin/bash
# disable_readonly.sh — Auto-dismiss IBKR write-access dialog at Gateway startup
#
# Root cause analysis:
#   - Gateway starts in Read-Only API mode by default
#   - "API client needs write access action confirmation" dialog appears on
#     FIRST write attempt by any connecting client
#   - Previous fix (ENABLEAPI via IBC command server) is TWS-only, not Gateway
#   - ReadOnlyApi=no in config.ini is overridden by persisted session settings
#   - The dialog appears AFTER the order is rejected (Error 321), too late
#
# This fix: watch for the dialog from startup, dismiss it immediately so the
# next order attempt succeeds. Runs as background process from start_ibgateway.sh.

export DISPLAY=:99
LOG=/var/log/ibgateway/disable_readonly.log
DIALOG_TITLE="API client needs write access action confirmation"
MAX_LOGIN_WAIT=180
WATCH_WINDOW=120
POLL=2

echo "[$(date)] disable_readonly v2 starting (pid=$$)" >> "$LOG"

# Phase 1: Wait for login
for i in $(seq 1 $MAX_LOGIN_WAIT); do
    if grep -q "Login has completed" /var/log/ibgateway/ibgateway.log 2>/dev/null; then
        echo "[$(date)] login completed after ${i}s" >> "$LOG"
        break
    fi
    sleep 1
done

# Phase 2: Also send a dummy read-only safe API call to TRIGGER the dialog
# Connect via python/ib_insync with a benign request — this provokes the
# write-access dialog without actually placing an order
echo "[$(date)] triggering write-access dialog via probe connection..." >> "$LOG"
/opt/algo-trader/venv/bin/python3 -c "
import sys, time
try:
    from ib_insync import IB
    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=99, timeout=10, readonly=False)
    ib.sleep(2)
    # Request managed accounts — this triggers the write-access dialog
    accts = ib.managedAccounts()
    ib.sleep(1)
    ib.disconnect()
    print('probe connected, accounts:', accts)
except Exception as e:
    print('probe error:', e)
" >> "$LOG" 2>&1 &

# Phase 3: Poll for dialog and dismiss it
echo "[$(date)] polling for write-access dialog (${WATCH_WINDOW}s window)..." >> "$LOG"
DISMISSED=0
POLLS=$((WATCH_WINDOW / POLL))

for i in $(seq 1 $POLLS); do
    WID=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
    if [ -n "$WID" ]; then
        echo "[$(date)] FOUND dialog window=$WID on poll $i — dismissing" >> "$LOG"

        xdotool windowactivate --sync "$WID" 2>/dev/null
        sleep 0.5

        # Press Return (confirms/grants write access)
        xdotool key --window "$WID" Return 2>/dev/null
        sleep 0.5

        # Verify dismissed
        STILL=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
        if [ -z "$STILL" ]; then
            echo "[$(date)] dialog dismissed — write access granted for this session" >> "$LOG"
            DISMISSED=1
        else
            # Try space bar as fallback
            xdotool key --window "$WID" space 2>/dev/null
            sleep 0.5
            STILL2=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
            if [ -z "$STILL2" ]; then
                echo "[$(date)] dialog dismissed via space — write access granted" >> "$LOG"
                DISMISSED=1
            else
                echo "[$(date)] WARNING: dialog still present after two attempts" >> "$LOG"
            fi
        fi
        break
    fi
    sleep $POLL
done

if [ "$DISMISSED" -eq 0 ]; then
    echo "[$(date)] dialog never appeared — Gateway may already have write access or dialog was not raised" >> "$LOG"
fi

echo "[$(date)] disable_readonly v2 done (dismissed=$DISMISSED)" >> "$LOG"
