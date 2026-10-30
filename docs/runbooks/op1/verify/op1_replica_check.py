"""OFFLINE ONLY (never on the VPS). usage: python3 -I op1_replica_check.py <code checkout> <docs checkout> <scratch dir>
(OP1_STRACE=<file> runs the session under strace -f; then op1_trace_audit.py <file> <scratch>/tmp/m21_op1 <scratch>/opt/algo-trader)

Verify the OP1 runbook scripts against a synthetic replica of the VPS layout built with the real code.

The replica tree is hashed (content of every file) before and after; the scripts must leave it unchanged,
write only under OUT, and print none of the planted fake secrets.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys

REPO, DOCS, BASE = sys.argv[1], sys.argv[2], sys.argv[3]       # code checkout, docs checkout, scratch dir
sys.path.insert(0, REPO)
import pandas as pd                                               # noqa: E402

from bot import flywheel as FW                                   # noqa: E402
from bot.database import init_db                                 # noqa: E402
from bot.historical import schema as HS                          # noqa: E402
from bot.historical import store as HSTORE                       # noqa: E402
from bot.paper_loop_d2.journal import LoopJournal                # noqa: E402

shutil.rmtree(BASE, ignore_errors=True)
ROOT = os.path.join(BASE, "opt", "algo-trader")
OUT = os.path.join(BASE, "tmp", "m21_op1")
DATA = os.path.join(ROOT, "data")
os.makedirs(DATA)
FAKE = {"tg": "123456789:AAFakeFakeFakeFakeFakeFakeFakeFake12", "pw": "hunter2-fake-password", "acct": "DU1234567",
        "gh": "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE1234"}

# signals.db with the real schema and a few claims
sig = os.path.join(DATA, "signals.db")
c = init_db(sig)
FW.init_flywheel_tables(c)
for f in ("ensure_execution_intents_migrations", "ensure_broker_positions_migration",
          "ensure_daily_state_per_broker_migrations"):
    if hasattr(FW, f):
        getattr(FW, f)(c)
for ddl in ("RISK_DECISIONS_SCHEMA", "RISK_SNAPSHOTS_SCHEMA"):
    if hasattr(FW, ddl):
        try:
            c.execute(getattr(FW, ddl))
        except sqlite3.OperationalError:
            pass
c.execute("INSERT INTO execution_intents (timestamp, symbol, direction, broker, status) VALUES "
          "('2026-07-01T14:00:00+00:00','AAA','long','paper','paper_logged')")
c.execute("INSERT INTO execution_intents (timestamp, symbol, direction, broker, status, fill_price, fill_qty, filled_at, "
          "broker_order_id, lifecycle_json) VALUES ('2026-07-02T14:00:00+00:00','BBB','long','ibkr_paper','filled',10,5,"
          "'2026-07-02T14:01:00+00:00','17','{\"note\": \"account %s\"}')" % FAKE["acct"])
c.commit()
c.close()
# loop journal
LoopJournal(os.path.join(DATA, "paper_lifecycles.db"))
# M16 metadata + one parquet file
hc = HS.open_db(os.path.join(DATA, "historical.db"))
HS.apply_schema(hc)
hc.execute("INSERT INTO historical_symbols VALUES ('AAA','equity',1,'2026-01-01','2026-10-01')")
hc.execute("INSERT INTO historical_coverage (symbol,timeframe,provider,first_ts_utc,last_ts_utc,bar_count,missing_count) VALUES ('AAA','1D','yfinance','2026-01-02','2026-01-06',5,1)")
hc.commit()
hc.close()
store = os.path.join(DATA, "historical")
idx = pd.date_range("2026-01-02", periods=5, freq="D", tz="UTC")
df = pd.DataFrame({"ts_utc": idx, "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 10.0,
                   "adj_close": 1.0, "adjustment_ratio": [0.5, 0.5, 1.0, 1.0, 1.0], "is_adjusted": True,
                   "provider": "yfinance", "ingested_at_utc": idx, "quality_flags": ""})
p = HSTORE._parquet_path("yfinance", "1D", "AAA", root=store)
os.makedirs(os.path.dirname(p), exist_ok=True)
HSTORE._write_parquet_atomic(p, df)
with open(os.path.join(DATA, "bot_state.json"), "w") as fh:
    json.dump({"phase": "scanning", "last_cycle_at": "2026-10-28T14:00:00+00:00", "telegram_token": FAKE["tg"],
               "focus": ["AAA"]}, fh)
with open(os.path.join(ROOT, ".env"), "w") as fh:
    fh.write("RISK_MAX_DAILY_LOSS=%s\nDASHBOARD_PASSWORD=%s\nTELEGRAM_TOKEN=%s\nIBKR_ACCOUNT=%s\nBROKER=ibkr_paper\n"
             % ("123", FAKE["pw"], FAKE["tg"], FAKE["acct"]))
os.chmod(os.path.join(ROOT, ".env"), 0o600)
os.makedirs(os.path.join(ROOT, "logs"))
with open(os.path.join(ROOT, "logs", "sync.log"), "w") as fh:
    fh.write("Mon: Sync daemon started (branch: main)\nMon: New commit detected (a -> b)\nMon: Updated to b\n"
             "Mon: ERROR posting to https://api.telegram.org/bot%s/sendMessage\nMon: Restarted via systemd.\n" % FAKE["tg"])
for f in os.listdir(DATA):
    if f.endswith(("-wal", "-shm")):
        os.remove(os.path.join(DATA, f))
import shutil as _sh
_sh.copy(os.path.join(REPO, "sync.sh"), os.path.join(ROOT, "sync.sh"))
_sh.copy(os.path.join(REPO, "deploy.sh"), os.path.join(ROOT, "deploy.sh"))
with open(os.path.join(DATA, "kill_switch.json"), "w") as fh:
    json.dump({"active": False, "reason": "Initialised at startup", "updated_at": "2026-10-28T10:00:00+00:00"}, fh)
os.makedirs(os.path.join(BASE, "daemon"))
with open(os.path.join(BASE, "daemon", "sync.sh"), "w") as fh:
    fh.write("sleep 120\n")                      # stands in for a running sync daemon
FAKE_DAEMON = subprocess.Popen(["bash", os.path.join(BASE, "daemon", "sync.sh")], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
DECOY = subprocess.Popen(["bash", "-c", "sleep 120 # grep sync.sh password=%s" % FAKE["pw"]],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
subprocess.run(["git", "init", "-q", ROOT], check=True)
subprocess.run(["git", "-C", ROOT, "remote", "add", "origin",
                "https://x-access-token:ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKE1234@github.com/o/r.git"], check=True)
subprocess.run(["git", "-C", ROOT, "-c", "user.name=t", "-c", "user.email=t@t", "add", "-A"], check=True)
subprocess.run(["git", "-C", ROOT, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "replica"], check=True)
# side files of WAL databases are created by any reader; remove them so the hash covers content files
for f in os.listdir(DATA):
    if f.endswith(("-wal", "-shm")):
        os.remove(os.path.join(DATA, f))


def tree_hash(root):
    h = hashlib.sha256()
    for d, _, files in sorted(os.walk(root)):
        for f in sorted(files):
            if f.endswith(("-wal", "-shm")):
                continue                      # SQLite side files (documented); the databases themselves are hashed
            fp = os.path.join(d, f)
            h.update(fp.encode())
            with open(fp, "rb") as fh:
                h.update(fh.read())
    return h.hexdigest()


# the code tarball (OP1.2) and calendar rows (OP1.3), so the WP11 readiness step runs too
os.makedirs(os.path.join(OUT, "src"))
subprocess.run("git -C %s archive HEAD | tar -x -C %s" % (REPO, os.path.join(OUT, "src")), shell=True, check=True)
with open(os.path.join(OUT, "calendar.json"), "w") as fh:
    json.dump([{"date": d, "open": "09:30", "close": "16:00"} for d in ("2026-01-02", "2026-01-05", "2026-01-06")], fh)
before = tree_hash(ROOT)
env = dict(os.environ, OP1_ROOT=ROOT, OP1_OUT=OUT, OP1_PY=sys.executable, OP1_SRC=os.path.join(OUT, "src"))
TRACE = os.environ.get("OP1_STRACE")              # path: run the whole session under strace -f
cmd = ["bash", os.path.join(DOCS, "docs/runbooks/op1/op1_readonly.sh")]
if TRACE:
    cmd = ["strace", "-f", "-qq", "-s", "400", "-o", TRACE, "-e",
           "trace=execve,connect,sendto,open,openat,creat,unlink,unlinkat,rename,renameat,renameat2,mkdir,mkdirat,"
           "rmdir,truncate,link,linkat,symlink,symlinkat,chmod,fchmodat,chown,fchownat,lchown,kill,tkill,tgkill,"
           "ptrace,mount,setsid"] + cmd
r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=900)
after = tree_hash(ROOT)
import signal as _sig
for _p in (FAKE_DAEMON, DECOY):
    os.killpg(_p.pid, _sig.SIGKILL)
print("rc", r.returncode)
print(r.stdout[-4000:])
print("STDERR", r.stderr[-2000:])
print("replica unchanged:", before == after)
side = [f for f in os.listdir(DATA) if f.endswith(("-wal", "-shm"))]
print("side files created by read-only opens:", side)
leaks = []
for d, _, files in os.walk(OUT):
    for f in files:
        if f.endswith(".db"):
            continue
        txt = open(os.path.join(d, f), errors="replace").read()
        for k, v in FAKE.items():
            if v in txt:
                leaks.append((f, k))
print("fake secrets found in outputs:", leaks)
for f in sorted(os.listdir(OUT)):
    print("OUT", f)
for f in ("deploy_discovery.txt", "old_paper_signals.txt", "old_paper_journal.txt", "m16_inventory.txt", "env_metadata.txt",
          "parquet_basis.csv", "runtime_status.txt"):
    fp = os.path.join(OUT, f)
    if os.path.exists(fp):
        t = open(fp).read()
        print("----", f, "error lines:", sum(1 for l in t.splitlines() if "Error" in l or "error:" in l.lower()))
        print(t[:1500])
