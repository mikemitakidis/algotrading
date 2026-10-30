#!/bin/bash
# OP1 — one read-only VPS session (gate G2). Prepared 29 Oct 2026; NOT run on the VPS.
#
# Usage (from the unpacked docs tarball, see the runbook OP1.1):
#   bash docs/runbooks/op1/op1_readonly.sh
# Environment (defaults in brackets):
#   OP1_ROOT [/opt/algo-trader]   OP1_OUT [/tmp/m21_op1]   OP1_SRC [/tmp/m21_op1/src] (code tarball, for OP1.3)
#   OP1_PY   [$OP1_ROOT/venv/bin/python3]                  OP1_CALENDAR [/tmp/m21_op1/calendar.json]
#
# What it does: copies the three SQLite databases through read-only connections (.backup from a
# mode=ro URI), runs the read-only SQL reports on the COPIES, summarises the parquet store, runs the
# WP11 readiness report when a calendar file is present, records .env permission metadata and RISK_*
# NAMES (never values), and collects service/process evidence (op1_runtime_status.sh).
# What it never does: write under OP1_ROOT, read .env values, start/stop/restart anything, connect to
# a broker or data vendor, or print unit files, process arguments or environments.
# Copies: a rollback-journal database (signals.db, written by the running bot) is read with mode=ro, which
# takes a shared lock, so the copy is a consistent snapshot. A WAL-format database (data/historical.db) with no
# -wal file has no unflushed content and is read with immutable=1 so SQLite creates no side files; with a -wal
# file the side files already exist and mode=ro reads through them. Every copy gets PRAGMA quick_check.
# The database files themselves are never written.
# Stop rules (exit codes): 2 refused before anything was copied (OUT inside ROOT, or OUT already holds *_copy.db
# from an earlier run); 3 a database copy failed; 4 a copy's PRAGMA quick_check was not exactly "ok". A stop ends
# the session at once: no report, summary or later step runs, and only copies made AND checked in this run are
# ever read (COPIED).
set -u
ROOT="${OP1_ROOT:-/opt/algo-trader}"
OUT="${OP1_OUT:-/tmp/m21_op1}"
SRC="${OP1_SRC:-$OUT/src}"
PY="${OP1_PY:-$ROOT/venv/bin/python3}"
CAL="${OP1_CALENDAR:-$OUT/calendar.json}"
HERE="$(cd "$(dirname "$0")" && pwd)"
SQL="$HERE/../sql"
case "$(realpath -m "$OUT")/" in "$(realpath -m "$ROOT")"/*) echo "refused: OUT inside ROOT"; exit 2;; esac
mkdir -p "$OUT" || exit 2
LOG="$OUT/op1.log"
log() { echo "[$(date -u +%FT%TZ)] $*" | tee -a "$LOG"; }

log "OP1 start; root=$ROOT out=$OUT user=$(id -un) groups=$(id -Gn | tr ' ' ',')"
SQLITE="$(command -v sqlite3 || true)"
[ -n "${OP1_FORCE_PY_SQL:-}" ] && SQLITE=""
log "python=$("$PY" -c 'import sys;print(sys.version.split()[0])' 2>&1) sqlite3=${SQLITE:-absent (python fallback op1_sql.py)}"

# ── 1. read-only copies of the databases ────────────────────────────────────
shopt -s nullglob
OLD=("$OUT"/*_copy.db)
shopt -u nullglob
if [ ${#OLD[@]} -gt 0 ]; then
  log "STOP: $OUT already holds ${#OLD[@]} *_copy.db file(s) from an earlier run; use a new OP1_OUT directory"
  exit 2
fi
COPIED=" "
for db in signals paper_lifecycles historical; do
  src="$ROOT/data/$db.db"
  if [ -f "$src" ]; then
    uri="file:$src?mode=ro"
    fmt=$(od -An -tu1 -j18 -N1 "$src" 2>/dev/null | tr -d ' ')          # SQLite header byte 18: 2 = WAL
    [ "$fmt" = "2" ] && [ ! -e "$src-wal" ] && uri="$uri&immutable=1"
    if [ -n "$SQLITE" ]; then sqlite3 "$uri" ".backup '$OUT/${db}_copy.db'"
    else "$PY" -I "$HERE/op1_sql.py" backup "$src" "$OUT/${db}_copy.db"; fi 2>>"$LOG"
    rc=$?
    if [ $rc -ne 0 ] || [ ! -f "$OUT/${db}_copy.db" ]; then
      log "STOP: COPY FAILED for $db.db (rc=$rc). Nothing else runs; do not use or share ${db}_copy.db; report this."
      exit 3
    fi
    if [ -n "$SQLITE" ]; then qc=$(sqlite3 -readonly "$OUT/${db}_copy.db" 'PRAGMA quick_check;' 2>&1 | head -1)
    else qc=$("$PY" -I "$HERE/op1_sql.py" check "$OUT/${db}_copy.db" 2>&1 | head -1); fi
    if [ "$qc" != "ok" ]; then
      log "STOP: quick_check ${db}_copy.db: ${qc:-no result}. Nothing else runs; do not use or share it; report this."
      exit 4
    fi
    COPIED="$COPIED${db}_copy.db "
    log "copied $db.db -> ${db}_copy.db sha256=$(sha256sum "$OUT/${db}_copy.db" | cut -c1-16) format=${fmt:-?} quick_check=ok"
  else
    log "absent: $src"
  fi
done

# ── 2. SQL reports on the copies (sqlite3 -readonly) ────────────────────────
run_sql() {   # <copy> <sql> <out>   only a copy made and checked in this run (COPIED) is ever read
  case "$COPIED" in *" $(basename "$1") "*) ;; *) log "skip $(basename "$2"): no verified $(basename "$1")"; return;; esac
  if [ -n "$SQLITE" ]; then sqlite3 -readonly -header -column "$1" < "$2" > "$3" 2>&1
  else "$PY" -I "$HERE/op1_sql.py" run "$1" "$2" > "$3" 2>&1; fi
  log "$(basename "$2") -> $(basename "$3") (rc=$?, $(grep -ci 'error' "$3") error lines; 'no such table' is expected for absent features)"
}
run_sql "$OUT/historical_copy.db"       "$SQL/op1_m16_inventory.sql" "$OUT/m16_inventory.txt"
run_sql "$OUT/signals_copy.db"          "$SQL/old_paper_signals.sql" "$OUT/old_paper_signals.txt"
run_sql "$OUT/paper_lifecycles_copy.db" "$SQL/old_paper_journal.sql" "$OUT/old_paper_journal.txt"

# ── 3. parquet store: symbols, timeframes, windows, adjustment basis ────────
if [ -d "$ROOT/data/historical" ]; then
  PYTHONDONTWRITEBYTECODE=1 "$PY" -I "$HERE/op1_parquet_basis.py" "$ROOT/data/historical" "$OUT/parquet_basis.csv" \
    > "$OUT/parquet_basis_summary.txt" 2>&1
  log "parquet basis -> parquet_basis.csv ($(($(wc -l < "$OUT/parquet_basis.csv") - 1)) files)"
else
  log "absent: $ROOT/data/historical"
fi

# ── 4. WP11 readiness (needs the calendar rows from OP1.2 and the code tarball) ──
if [ -f "$CAL" ] && [ -d "$SRC/research" ] && [ -d "$ROOT/data/historical/yfinance/1D" ]; then
  SYMS="$(ls "$ROOT/data/historical/yfinance/1D" | sed 's/\.parquet$//' | head -50 | paste -sd,)"
  ( cd "$SRC" && PYTHONDONTWRITEBYTECODE=1 "$PY" -I -c "import sys; sys.path.insert(0, '.'); \
from research.dataqa.readiness import main; raise SystemExit(main(sys.argv[1:]))" \
      --store "$ROOT/data/historical" --provider yfinance --calendar "$CAL" --symbols "$SYMS" \
      --timeframes 1D,4H,1H,15m --convention start --out "$OUT/readiness" ) > "$OUT/readiness.log" 2>&1
  log "readiness rc=$? -> readiness/ (report.json, report.md)"
else
  log "readiness skipped: needs $CAL, $SRC/research and the yfinance 1D store"
fi

# ── 5. .env metadata and RISK_* names (never values) ────────────────────────
{
  echo "== permissions (E07)"
  stat -c '%a %U %G %s %y %n' "$ROOT"/.env* 2>/dev/null
  echo "== RISK_* variable NAMES present in .env"
  grep -oE '^RISK_[A-Z0-9_]+' "$ROOT/.env" 2>/dev/null | sort -u
  echo "== broker-selection variable NAMES present (values not read)"
  grep -oE '^(BROKER|IBKR_[A-Z_]+|ETORO_[A-Z_]+|PAPER_[A-Z_]+|LIVE_[A-Z_]+)=' "$ROOT/.env" 2>/dev/null | tr -d '=' | sort -u
} > "$OUT/env_metadata.txt" 2>&1
log "env metadata -> env_metadata.txt"

# ── 6. was the old system running: services, timers, cron, processes, journal ──
OP1_ROOT="$ROOT" bash "$HERE/op1_runtime_status.sh" "$OUT" > /dev/null 2>&1
log "runtime status -> runtime_status.txt, journal_*.txt"

# ── 6b. deployment discovery: would a merge to main reach this server, and what would a restart do? ──
OP1_ROOT="$ROOT" bash "$HERE/op1_deploy_discovery.sh" "$OUT" > /dev/null 2>&1
log "deployment discovery -> deploy_discovery.txt ($(grep -m1 '^sync_sh_running=' "$OUT/deploy_discovery.txt"))"

# ── 7. secret screen of everything produced, then a manifest ────────────────
HITS=$(grep -rIEl '[0-9]{8,10}:[A-Za-z0-9_-]{30,}|(PASSWORD|SECRET|TOKEN|API_KEY)[A-Z_]*=[^<]|\b(DU|U)[0-9]{6,}\b' \
       "$OUT" --exclude='*.db' --exclude='*.db-wal' --exclude='*.db-shm' --exclude='*.tgz' --exclude=op1.log \
       --exclude-dir=src --exclude-dir=docs 2>/dev/null | wc -l)       # src/ and docs/ are the unpacked tarballs
log "secret screen: $HITS output file(s) match a secret-like pattern (0 expected; review any before sharing)"
( cd "$OUT" && find . -maxdepth 2 -type f ! -name '*.db' ! -name 'src.tgz' ! -path './src/*' -printf '%P\n' | sort \
  | xargs -r sha256sum ) > "$OUT/MANIFEST.sha256"
log "OP1 done; share MANIFEST.sha256 and the .txt/.csv/readiness outputs, not the *_copy.db files"
