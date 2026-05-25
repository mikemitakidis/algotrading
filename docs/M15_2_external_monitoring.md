# M15.2 — External Monitoring of `/api/health`

The bot exposes `GET /api/health` on the dashboard port (default 8080).
This endpoint is the contract; the monitor is your choice — UptimeRobot,
Healthchecks.io, BetterStack, Uptime Kuma, cron + curl, anything that can
read an HTTP status code.

## Status semantics

| status     | HTTP | meaning                                          | monitor action |
|------------|------|--------------------------------------------------|----------------|
| `ok`       | 200  | bot alive, scan loop healthy, gateway healthy    | no action      |
| `degraded` | 200  | gateway needs attention OR kill switch active    | **no page**, watch Telegram |
| `critical` | 503  | bot dead/wedged, heartbeat stale, or DB unreadable | **page operator** |

`degraded` deliberately returns HTTP 200 so external monitors do not
fire pages every morning when IB Gateway needs 2FA after its 23:59
restart. Gateway-level alerts are already handled by the M15.1 watchdog
via Telegram.

`reason_code` field on the response tells you why:
- `heartbeat_missing` — no heartbeat file (process never started)
- `heartbeat_stale` — process died or wedged
- `db_unwritable` — disk full, permissions, etc.
- `scan_wedged` — scan loop started but never completed (broker hang)
- `scan_stale` — scan loop has not completed in 3× scan_interval
- `gateway_degraded` — see watchdog Telegram alerts
- `kill_switch_active` — operator stopped trading

## Auth

Set `HEALTH_ENDPOINT_AUTH_TOKEN` in `.env` to a long random secret:

```
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

| token set? | header sent?           | result                    |
|------------|------------------------|---------------------------|
| no         | —                      | minimal payload + status code (good for public monitors) |
| yes        | none                   | minimal payload + status code |
| yes        | `Bearer <correct>`     | full payload + status code |
| yes        | `Bearer <wrong>`       | `401`                     |

Comparison is constant-time (`hmac.compare_digest`).

## Curl examples

### Unauthenticated minimal check (any monitor)

```
curl -sS -o /dev/null -w '%{http_code}\n' \
  http://YOUR_HOST:8080/api/health
```

Exit code: 0 if reachable. HTTP 200 = ok or degraded; HTTP 503 = critical.

### Authenticated full payload (for dashboards, manual ops)

```
curl -sS \
  -H "Authorization: Bearer $HEALTH_ENDPOINT_AUTH_TOKEN" \
  http://YOUR_HOST:8080/api/health | jq .
```

Full payload includes: heartbeat age, scan ages, db_writable, full gateway
state, kill switch, pid, process_started_at, warnings.

## Cron + Telegram (provider-free)

If you don't want a third-party monitor, this 12-line script pings the
endpoint every minute and sends a Telegram alert on `503`:

```bash
#!/bin/bash
# /usr/local/bin/algo-health-check.sh
URL="http://127.0.0.1:8080/api/health"
TG_TOKEN="..."     # your Telegram bot token
TG_CHAT="..."      # your Telegram chat id
STATE_FILE=/var/tmp/algo-health-last-state

code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 5 "$URL" || echo "000")
last=$(cat "$STATE_FILE" 2>/dev/null || echo "ok")
if [ "$code" != "200" ] && [ "$last" = "ok" ]; then
    curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT}" \
        -d "text=🚨 algo-trader /api/health = ${code}" >/dev/null
    echo "alerted" > "$STATE_FILE"
elif [ "$code" = "200" ] && [ "$last" = "alerted" ]; then
    curl -sS -X POST "https://api.telegram.org/bot${TG_TOKEN}/sendMessage" \
        -d "chat_id=${TG_CHAT}" \
        -d "text=✅ algo-trader recovered" >/dev/null
    echo "ok" > "$STATE_FILE"
fi
```

Crontab line:

```
* * * * * /usr/local/bin/algo-health-check.sh
```

State file gives one-shot alerts (no spam every minute while down,
one recovery message when it comes back).

## UptimeRobot-style provider notes

Any monitor that supports custom HTTP headers can be pointed at the
authenticated endpoint. Configure:

- **URL:** `http://YOUR_HOST:8080/api/health`
- **Method:** GET
- **Custom header:** `Authorization: Bearer <token>` (optional; unauth gives
  enough for liveness checks)
- **Interval:** 5 minutes (matches 90s heartbeat staleness threshold)
- **Alert condition:** HTTP status ≠ 200

Use the monitor's own alerting (email, Slack, SMS, Telegram, PagerDuty);
the bot does not need to know who the monitor is.
