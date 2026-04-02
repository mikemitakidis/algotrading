#!/bin/bash
# disable_readonly.sh v3 — click-based dialog dismissal for Java/Swing Gateway popup
export DISPLAY=:99
LOG=/var/log/ibgateway/disable_readonly.log
DIALOG_TITLE="API client needs write access action confirmation"

echo "[$(date)] disable_readonly v3 starting (pid=$$)" >> "$LOG"

# Wait for login
for i in $(seq 1 180); do
    grep -q "Login has completed" /var/log/ibgateway/ibgateway.log 2>/dev/null && break
    sleep 1
done
echo "[$(date)] login ready" >> "$LOG"

# Probe connection to trigger the write-access dialog
/opt/algo-trader/venv/bin/python3 - >> "$LOG" 2>&1 << 'PYEOF'
try:
    from ib_insync import IB
    ib = IB()
    ib.connect('127.0.0.1', 4002, clientId=99, timeout=10, readonly=False)
    ib.sleep(3)
    ib.managedAccounts()
    ib.sleep(1)
    ib.disconnect()
    print("probe done")
except Exception as e:
    print("probe error:", e)
PYEOF

# Poll for dialog and dismiss via mouse click (Java Swing ignores synthetic keypresses)
for i in $(seq 1 60); do
    WID=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
    if [ -n "$WID" ]; then
        echo "[$(date)] dialog found window=$WID poll=$i" >> "$LOG"
        scrot /tmp/ibgw_dialog_before.png 2>/dev/null

        # Get window geometry
        GEOM=$(xdotool getwindowgeometry "$WID" 2>/dev/null)
        echo "[$(date)] geometry: $GEOM" >> "$LOG"

        # Get absolute window position
        X=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f1)
        Y=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f2)
        W=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Geometry | awk '{print $2}' | cut -d'x' -f1)
        H=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Geometry | awk '{print $2}' | cut -d'x' -f2)
        echo "[$(date)] pos=($X,$Y) size=${W}x${H}" >> "$LOG"

        # Raise and focus window properly for Java
        xdotool windowraise "$WID" 2>/dev/null
        xdotool windowfocus --sync "$WID" 2>/dev/null
        sleep 0.8

        # Click the confirm button — Java dialogs put the default button
        # (Yes/Allow/OK) at bottom-right. For 602x210 dialog that's ~x=490,y=175
        # Use absolute screen coordinates = window_origin + button_offset
        BTN_X=$(( X + W - 112 ))
        BTN_Y=$(( Y + H - 35 ))
        echo "[$(date)] clicking button at abs=($BTN_X,$BTN_Y)" >> "$LOG"
        xdotool mousemove "$BTN_X" "$BTN_Y"
        sleep 0.3
        xdotool click 1
        sleep 0.8

        scrot /tmp/ibgw_dialog_after.png 2>/dev/null

        # Verify dismissed
        STILL=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
        if [ -z "$STILL" ]; then
            echo "[$(date)] SUCCESS — write access dialog dismissed" >> "$LOG"
        else
            # Fallback: try clicking at fixed offsets for 602x210 known size
            echo "[$(date)] first click missed — trying fixed offset for 602x210" >> "$LOG"
            ABS_X=$(( X + 490 ))
            ABS_Y=$(( Y + 175 ))
            xdotool mousemove "$ABS_X" "$ABS_Y"
            sleep 0.3
            xdotool click 1
            sleep 0.5
            STILL2=$(xdotool search --name "$DIALOG_TITLE" 2>/dev/null | head -1)
            [ -z "$STILL2" ] && echo "[$(date)] SUCCESS via fallback click" >> "$LOG" \
                             || echo "[$(date)] FAIL — dialog still present" >> "$LOG"
        fi
        break
    fi
    sleep 2
done

echo "[$(date)] v3 done" >> "$LOG"
