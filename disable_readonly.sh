#!/bin/bash
# disable_readonly_permanent.sh
# Navigates Gateway Configure → Settings → API and unchecks Read-Only API.
# One-time operation. Persists to Gateway session files permanently.
# Run ONCE after Gateway is logged in. Never needs to run again.

export DISPLAY=:99
LOG=/var/log/ibgateway/disable_readonly.log
echo "[$(date)] permanent disable starting" >> "$LOG"

# Wait for login
for i in $(seq 1 180); do
    grep -q "Login has completed" /var/log/ibgateway/ibgateway.log 2>/dev/null && break
    sleep 1
done
echo "[$(date)] login ready" >> "$LOG"
sleep 3

# Find Gateway main window
WID=$(xdotool search --name "IB Gateway" 2>/dev/null | head -1)
if [ -z "$WID" ]; then
    WID=$(xdotool search --class "ibgateway" 2>/dev/null | head -1)
fi
echo "[$(date)] Gateway window: $WID" >> "$LOG"

# Screenshot current state
scrot /tmp/gw_step1_main.png
echo "[$(date)] step1: main window" >> "$LOG"

# Get window position
X=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f1)
Y=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f2)
W=$(xdotool getwindowgeometry "$WID" 2>/dev/null | grep Geometry | awk '{print $2}' | cut -d'x' -f1)
echo "[$(date)] window at ($X,$Y) width=$W" >> "$LOG"

# Raise and focus Gateway window
xdotool windowraise "$WID"
xdotool windowfocus --sync "$WID"
sleep 1

# Click Configure menu — typically ~4th item in menu bar, ~350px from left
# Gateway width=700, menu items roughly at 60,150,250,350
MENU_X=$(( X + 350 ))
MENU_Y=$(( Y + 25 ))
echo "[$(date)] clicking Configure at ($MENU_X,$MENU_Y)" >> "$LOG"
xdotool mousemove "$MENU_X" "$MENU_Y"
sleep 0.3
xdotool click 1
sleep 1
scrot /tmp/gw_step2_configure_menu.png
echo "[$(date)] step2: Configure menu" >> "$LOG"

# Click Settings — last item in Configure dropdown (~bottom of menu)
# Dropdown items are ~20px tall, Settings is typically ~5th item down
SETTINGS_X=$(( MENU_X + 20 ))
SETTINGS_Y=$(( MENU_Y + 100 ))
echo "[$(date)] clicking Settings at ($SETTINGS_X,$SETTINGS_Y)" >> "$LOG"
xdotool mousemove "$SETTINGS_X" "$SETTINGS_Y"
sleep 0.3
xdotool click 1
sleep 1.5
scrot /tmp/gw_step3_settings.png
echo "[$(date)] step3: Settings dialog" >> "$LOG"

# In Settings dialog, find and click the API tab
# API tab is usually the last tab — click at right side of tab bar
DIAG_WID=$(xdotool search --name "Settings" 2>/dev/null | head -1)
echo "[$(date)] Settings dialog window: $DIAG_WID" >> "$LOG"
if [ -n "$DIAG_WID" ]; then
    DX=$(xdotool getwindowgeometry "$DIAG_WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f1)
    DY=$(xdotool getwindowgeometry "$DIAG_WID" 2>/dev/null | grep Position | awk '{print $2}' | cut -d',' -f2)
    DW=$(xdotool getwindowgeometry "$DIAG_WID" 2>/dev/null | grep Geometry | awk '{print $2}' | cut -d'x' -f1)
    echo "[$(date)] Settings at ($DX,$DY) width=$DW" >> "$LOG"

    # Click API tab — typically rightmost tab, ~80% across tab bar at ~35px down
    API_TAB_X=$(( DX + DW * 8 / 10 ))
    API_TAB_Y=$(( DY + 35 ))
    echo "[$(date)] clicking API tab at ($API_TAB_X,$API_TAB_Y)" >> "$LOG"
    xdotool mousemove "$API_TAB_X" "$API_TAB_Y"
    sleep 0.3
    xdotool click 1
    sleep 1
    scrot /tmp/gw_step4_api_tab.png
    echo "[$(date)] step4: API tab" >> "$LOG"

    # The Read-Only API checkbox is typically in the top section of API tab
    # Try clicking ~130px down from dialog top, ~25px from left edge
    CHECKBOX_X=$(( DX + 25 ))
    CHECKBOX_Y=$(( DY + 130 ))
    echo "[$(date)] clicking Read-Only checkbox at ($CHECKBOX_X,$CHECKBOX_Y)" >> "$LOG"
    xdotool mousemove "$CHECKBOX_X" "$CHECKBOX_Y"
    sleep 0.3
    xdotool click 1
    sleep 0.5
    scrot /tmp/gw_step5_unchecked.png
    echo "[$(date)] step5: checkbox clicked" >> "$LOG"

    # Click OK/Apply to save
    OK_X=$(( DX + DW - 60 ))
    OK_Y=$(( DY + 400 ))
    echo "[$(date)] clicking OK at ($OK_X,$OK_Y)" >> "$LOG"
    xdotool mousemove "$OK_X" "$OK_Y"
    sleep 0.3
    xdotool click 1
    sleep 1
    scrot /tmp/gw_step6_saved.png
    echo "[$(date)] step6: saved" >> "$LOG"
else
    echo "[$(date)] Settings dialog not found — Configure menu click may have missed" >> "$LOG"
fi

echo "[$(date)] permanent disable done" >> "$LOG"
ls -lh /tmp/gw_step*.png >> "$LOG" 2>/dev/null
