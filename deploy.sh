#!/bin/bash
# deploy.sh — First-time setup and start
#
# Two modes, auto-detected at runtime:
#
#   M15.0 mode  (canonical systemd units present, run as root):
#       deploy.sh is a venv/.env/dependency bootstrapper.
#       It does NOT pkill or nohup main.py / dashboard/app.py;
#       it does NOT install an @reboot cron;
#       systemd owns the bot + dashboard lifecycle.
#       The sync.sh daemon is still launched via nohup (intentional;
#       sync.sh is not a systemd unit).
#
#   Legacy mode (M15.0 units absent — pre-install or post-rollback):
#       Original behaviour preserved verbatim:
#       pkill + nohup main.py + nohup dashboard + @reboot crontab.
#       Use this only when M15.0 systemd install has not been applied.
#
# To install the canonical M15.0 systemd units (one-time, operator-only):
#   sudo bash /opt/algo-trader/infra/systemd/install.sh
# See docs/M15_0_systemd_canonical.md for the full canonical service map.
#
# Safe to re-run. Creates minimal .env if missing.

set -e

BASE=/opt/algo-trader
VENV=$BASE/venv
LOG=$BASE/logs/boot.log

mkdir -p $BASE/logs $BASE/data
echo "$(date): === deploy.sh ===" | tee -a $LOG

# ── 1. Create .env if missing (bot will warn but still start) ─────────────────
if [ ! -f "$BASE/.env" ]; then
    echo "$(date): .env not found — creating minimal .env with defaults" | tee -a $LOG
    cat > $BASE/.env << 'ENVEOF'
# Auto-created by deploy.sh
# Edit this file to set your real values
DASHBOARD_PASSWORD=changeme
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=
ENVEOF
    echo "$(date): .env created at $BASE/.env — edit it to set your password and Telegram keys" | tee -a $LOG
fi

# ── 2. Create venv if missing ─────────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
    echo "$(date): Creating Python venv..." | tee -a $LOG
    python3 -m venv $VENV
fi

# ── 3. Install dependencies ───────────────────────────────────────────────────
echo "$(date): Installing dependencies..." | tee -a $LOG
$VENV/bin/pip install --upgrade pip --quiet
$VENV/bin/pip install -r $BASE/requirements.txt --quiet
echo "$(date): Dependencies installed" | tee -a $LOG

# ── 4. Verify imports ─────────────────────────────────────────────────────────
echo "$(date): Verifying imports..." | tee -a $LOG
$VENV/bin/python3 -c "
import yfinance, pandas, numpy, flask, dotenv, requests
print('  yfinance:', yfinance.__version__)
print('  pandas:  ', pandas.__version__)
print('  flask:   ', flask.__version__)
print('  All imports OK')
" 2>&1 | tee -a $LOG

# ── 5. Detect M15.0 systemd-managed services ────────────────────────────────
# If both canonical M15.0 systemd units exist AND the script can use
# systemctl, we treat systemd as the source of truth and DO NOT pkill /
# nohup / install @reboot cron. The systemd units have their own auto-start
# at boot via WantedBy=multi-user.target; an @reboot cron that runs nohup
# would race with systemd and re-introduce the M15.0 audit problem.
#
# When the units are absent (pre-install, or post-rollback), the legacy
# nohup + @reboot path runs verbatim, preserving deploy.sh's original
# behaviour. This is the same detection logic sync.sh uses.
BOT_UNIT=algo-trader.service
DASH_UNIT=algo-trader-dashboard.service
USE_SYSTEMD=0
if [ "$(id -u)" = "0" ] && command -v systemctl >/dev/null 2>&1; then
    if systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^$BOT_UNIT" && \
       systemctl list-unit-files --no-legend 2>/dev/null | grep -q "^$DASH_UNIT"; then
        USE_SYSTEMD=1
    fi
fi

if [ "$USE_SYSTEMD" = "1" ]; then
    # ── M15.0 path: systemd owns main.py + dashboard/app.py ─────────────────
    # deploy.sh becomes a venv/.env/deps bootstrapper only; it does NOT
    # touch the bot/dashboard processes (systemd does), does NOT install
    # an @reboot cron (systemd auto-starts via WantedBy=multi-user.target),
    # and does NOT pkill anything systemd owns.
    echo "$(date): M15.0 detected — both canonical units present; skipping pkill/nohup/@reboot" | tee -a $LOG

    # Make sure systemd actually has the latest unit definitions in case
    # the operator just synced an updated repo. This is a no-op when
    # the units haven't changed.
    systemctl daemon-reload >> $LOG 2>&1 || true

    # Ensure both units are running. We do NOT enable here — that's the
    # explicit operator step in infra/systemd/install.sh. If the operator
    # disabled the units intentionally, deploy.sh must respect that.
    for u in "$BOT_UNIT" "$DASH_UNIT"; do
        if systemctl is-enabled "$u" >/dev/null 2>&1; then
            current=$(systemctl is-active "$u" 2>/dev/null || echo unknown)
            if [ "$current" != "active" ]; then
                echo "$(date): starting $u (was $current)" | tee -a $LOG
                systemctl start "$u" >> $LOG 2>&1 || true
            else
                echo "$(date): $u already active" | tee -a $LOG
            fi
        else
            echo "$(date): $u not enabled — operator disabled it; not starting" | tee -a $LOG
        fi
    done

    # Remove any pre-M15.0 @reboot crontab entry so it doesn't compete
    # with systemd's auto-start. Idempotent — safe if the entry is absent.
    if crontab -l 2>/dev/null | grep -q "deploy.sh"; then
        echo "$(date): removing pre-M15.0 @reboot crontab entry (systemd handles auto-start)" | tee -a $LOG
        ( crontab -l 2>/dev/null | grep -v "deploy.sh" ) | crontab -
    fi

else
    # ── Legacy path: no M15.0 units present (pre-install or rollback) ───────
    echo "$(date): M15.0 units NOT detected — using legacy nohup path" | tee -a $LOG

    # ── 5a. Kill existing processes ─────────────────────────────────────────
    pkill -f "python3.*main.py"   2>/dev/null || true
    pkill -f "python3.*app.py"    2>/dev/null || true
    sleep 1

    # ── 6. Start bot ────────────────────────────────────────────────────────
    nohup $VENV/bin/python3 $BASE/main.py > /dev/null 2>&1 &
    echo "$(date): Bot started PID=$!" | tee -a $LOG

    # ── 7. Start dashboard ──────────────────────────────────────────────────
    nohup $VENV/bin/python3 $BASE/dashboard/app.py >> $BASE/logs/dashboard.log 2>&1 &
    echo "$(date): Dashboard started PID=$!" | tee -a $LOG

    # ── 8. Crontab for reboot recovery ──────────────────────────────────────
    CRON="@reboot sleep 15 && bash $BASE/deploy.sh >> $BASE/logs/boot.log 2>&1"
    ( crontab -l 2>/dev/null | grep -v "deploy.sh" ; echo "$CRON" ) | crontab -
    echo "$(date): Crontab set" | tee -a $LOG
fi

# ── 9. Start sync daemon ──────────────────────────────────────────────────────
# The sync daemon is NOT a systemd unit (deliberately — it polls GitHub
# every 60s and remains a small nohup background daemon). It is
# systemd-aware: when canonical M15.0 units exist, it uses
# `systemctl restart` instead of pkill+nohup. So sync.sh is always
# relaunched the same way regardless of USE_SYSTEMD; the difference is
# how IT restarts the bot/dashboard on new commits.
pkill -f "sync.sh" 2>/dev/null || true
sleep 1
nohup bash $BASE/sync.sh >> $BASE/logs/sync.log 2>&1 &
echo "$(date): Sync daemon started PID=$!" | tee -a $LOG

echo "" | tee -a $LOG
echo "$(date): === DONE ===" | tee -a $LOG
echo "  Dashboard: http://$(hostname -I | awk '{print $1}'):8080  (password: changeme unless you edited .env)" | tee -a $LOG
echo "  Bot log:   tail -f $BASE/logs/bot.log" | tee -a $LOG
