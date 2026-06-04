#!/usr/bin/env python3
"""tools/set_dashboard_password.py — M15.3.A operator helper.

Interactive tool to set DASHBOARD_PASSWORD_HASH in .env using bcrypt.
Per user correction #4:
  * Never prints the password
  * Backs up .env before writing (.env.bak.<timestamp>)
  * Preserves unrelated .env lines (key=value, comments, blanks)
  * Sets safe permissions on .env (0600) if it edits the file
  * Prefers DASHBOARD_PASSWORD_HASH while keeping DASHBOARD_PASSWORD
    fallback (operator opts out of fallback manually if desired)
  * Generates a stable DASHBOARD_SECRET_KEY on first run

Usage:
  $ python tools/set_dashboard_password.py
  (prompts twice, no echo)

Flags:
  --remove-plaintext     Also remove DASHBOARD_PASSWORD from .env after
                         writing the hash (hard-cutover). Default off
                         (transitional).
  --no-secret-key        Don't generate/update DASHBOARD_SECRET_KEY
                         (keep existing or fall back to password-derived).
                         Default: generates DASHBOARD_SECRET_KEY if missing.
  --env-path PATH        Override the .env file path. Default: .env in
                         repo root.

Exit codes:
  0  success
  1  user input error (mismatched confirmation, empty password)
  2  bcrypt not installed
  3  .env file I/O error
"""
from __future__ import annotations

import argparse
import getpass
import os
import secrets
import shutil
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# M15.3.A.fix — sys.path bootstrap for script-mode invocation.
# When this file is run directly (e.g. `python3 tools/set_dashboard_password.py`
# from any cwd), Python only puts the script's directory (tools/) on
# sys.path — not the repo root. That makes `from dashboard.auth.passwords
# import hash_password` fail with ModuleNotFoundError. Identical issue to
# the one fixed in dashboard/app.py for systemd script-mode invocation.
# Adding the repo root here makes the tool work from any cwd without
# requiring the operator to set PYTHONPATH manually.
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _err(msg: str, code: int = 1) -> "noreturn":  # type: ignore
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def _read_env_file(p: Path) -> list[str]:
    """Return list of lines. Empty list if file does not exist."""
    if not p.exists():
        return []
    try:
        return p.read_text(encoding="utf-8").splitlines()
    except OSError as e:
        _err(f"could not read {p}: {e}", code=3)


def _replace_or_append(lines: list[str], key: str, value: str) -> list[str]:
    """Return new list with KEY=VALUE either replacing the first
    occurrence of KEY=... or appended at the end. Preserves all
    unrelated lines, comments, and blanks exactly as-is."""
    target = f"{key}="
    out: list[str] = []
    replaced = False
    for line in lines:
        stripped = line.lstrip()
        if not replaced and stripped.startswith(target):
            # Preserve any leading whitespace on the line.
            ws = line[:len(line) - len(stripped)]
            out.append(f"{ws}{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        # Append. If last line is non-empty, add a separator newline.
        if out and out[-1].strip() != "":
            out.append("")
        out.append(f"{key}={value}")
    return out


def _remove_key(lines: list[str], key: str) -> list[str]:
    target = f"{key}="
    return [l for l in lines if not l.lstrip().startswith(target)]


def _has_key(lines: list[str], key: str) -> bool:
    target = f"{key}="
    return any(l.lstrip().startswith(target) for l in lines)


def _backup_env(p: Path) -> Path:
    """Copy .env to .env.bak.<unix_ts> preserving permissions."""
    if not p.exists():
        return p  # Nothing to back up.
    ts = int(time.time())
    bak = p.with_suffix(p.suffix + f".bak.{ts}") if p.suffix else \
          Path(f"{p}.bak.{ts}")
    shutil.copy2(str(p), str(bak))
    return bak


def _write_env_safely(p: Path, lines: list[str]) -> None:
    """Write atomically + set 0600 perms. Never prints content."""
    tmp = p.with_suffix(p.suffix + ".tmp") if p.suffix else \
          Path(f"{p}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))
            if lines and lines[-1] != "":
                f.write("\n")
        os.replace(str(tmp), str(p))
        try:
            os.chmod(str(p), 0o600)
        except OSError:
            pass  # Best-effort on non-POSIX FS.
    except OSError as e:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        _err(f"could not write {p}: {e}", code=3)


def _prompt_password_twice(*, use_stdin: bool = False) -> str:
    """Prompt for password twice, confirm match. Never echoes or
    returns secret material to stdout.

    Two modes:
      * Default (use_stdin=False): use getpass.getpass(), which reads
        from the controlling TTY (`/dev/tty` on POSIX) — the normal
        interactive operator path. Secure: nothing is echoed and
        piped stdin is ignored.
      * use_stdin=True: read two lines from sys.stdin (no echo
        suppression — caller is responsible for not displaying them).
        This is needed for non-interactive automation paths (CI,
        subprocess tests) where the controlling TTY is unavailable
        AND the piped stdin must be honoured. Default behaviour
        for an unattended operator is NOT this mode — it's only
        triggered by an explicit --stdin flag.
    """
    if use_stdin:
        # Non-interactive: read from sys.stdin. The caller pipes
        # password\npassword\n on the subprocess stdin.
        try:
            a = sys.stdin.readline().rstrip("\n")
            b = sys.stdin.readline().rstrip("\n")
        except (KeyboardInterrupt, EOFError):
            _err("aborted", code=1)
        if not a or len(a) < 8:
            _err("password must be at least 8 characters", code=1)
        if a != b:
            _err("passwords did not match", code=1)
        return a
    # Interactive (default): getpass reads from /dev/tty.
    try:
        a = getpass.getpass("New dashboard password: ")
    except (KeyboardInterrupt, EOFError):
        _err("aborted", code=1)
    if not a or len(a) < 8:
        _err("password must be at least 8 characters", code=1)
    try:
        b = getpass.getpass("Confirm password:        ")
    except (KeyboardInterrupt, EOFError):
        _err("aborted", code=1)
    if a != b:
        _err("passwords did not match", code=1)
    return a


def _prompt_totp_code_once(*, use_stdin: bool = False) -> str:
    """Prompt for ONE 6-digit TOTP code. Echoed (unlike password) —
    the code is short-lived (30 sec window) and seeing what you typed
    helps when fat-fingering. Validates format before returning."""
    prompt = "Enter the 6-digit code from your authenticator: "
    if use_stdin:
        try:
            code = sys.stdin.readline().rstrip("\n").strip()
        except (KeyboardInterrupt, EOFError):
            _err("aborted", code=1)
    else:
        try:
            code = input(prompt).strip()
        except (KeyboardInterrupt, EOFError):
            _err("aborted", code=1)
    if not code.isdigit() or len(code) != 6:
        _err("code must be exactly 6 digits", code=1)
    return code


def _load_dotenv_for_totp(env_path: Path) -> None:
    """Read .env and inject DASHBOARD_PASSWORD / DASHBOARD_PASSWORD_HASH
    into os.environ so we can sanity-check that a password exists.
    Only loads keys we need — does not overwrite already-set env vars."""
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        if k in ("DASHBOARD_PASSWORD", "DASHBOARD_PASSWORD_HASH",
                  "DASHBOARD_SECRET_KEY") and k not in os.environ:
            os.environ[k] = v.strip()


def _enable_totp_flow(args) -> int:
    """M15.3.A.2 — interactive TOTP enable flow.

    Sequence:
      1. Sanity-check that a password is configured (TOTP without a
         password is nonsensical — there's nothing for TOTP to be the
         SECOND factor OF).
      2. Refuse to overwrite an existing DASHBOARD_TOTP_SECRET unless
         the operator explicitly removes it first via --disable-totp
         (prevents lockout from a copy-paste accident).
      3. Generate a fresh base32 secret.
      4. Render a Unicode-block QR to stdout (operator's terminal).
      5. Prompt for the first code from the authenticator app.
      6. Verify the code against the new secret with pyotp.
      7. Only on verify-success: backup .env, write
         DASHBOARD_TOTP_SECRET=<secret>, restore 0600, write audit row.
      8. On any failure (wrong code, Ctrl-C, etc.): .env is untouched.
    """
    try:
        from dashboard.auth.totp import (
            generate_secret, build_otpauth_uri, render_qr_terminal,
            verify_code, ReplayCache,
        )
    except ImportError as e:
        _err(f"could not import TOTP helper: {e}", code=2)

    env_path = Path(args.env_path).resolve()
    _load_dotenv_for_totp(env_path)

    # 1. Sanity check.
    has_pw = bool(os.getenv("DASHBOARD_PASSWORD_HASH", "").strip()) or \
             (os.getenv("DASHBOARD_PASSWORD", "") not in ("", "changeme"))
    if not has_pw:
        _err("no password configured — run set_dashboard_password.py "
             "WITHOUT --enable-totp first to set a password",
             code=1)

    # 2. Refuse to overwrite an existing TOTP secret.
    existing_lines = _read_env_file(env_path)
    if _has_key(existing_lines, "DASHBOARD_TOTP_SECRET"):
        # Check if it's actually populated (not just present but empty).
        for line in existing_lines:
            if line.lstrip().startswith("DASHBOARD_TOTP_SECRET="):
                value = line.split("=", 1)[1].strip()
                if value:
                    _err("DASHBOARD_TOTP_SECRET is already set in .env. "
                         "Run --disable-totp first if you want to rotate "
                         "to a new authenticator app.", code=1)

    # 3. Generate secret.
    secret = generate_secret()

    # 4. Render QR. ONLY printed to stdout — never logged, never returned.
    uri = build_otpauth_uri(secret, account_name="operator",
                              issuer="Algo Trader")
    print()
    print("Scan this QR with your authenticator app (Google Authenticator,")
    print("Authy, 1Password, Bitwarden, etc.):")
    print()
    print(render_qr_terminal(uri))
    print()
    print("If the QR doesn't render properly in your terminal, you can")
    print("enter the secret manually into the app. The secret is shown")
    print("ONCE below — do NOT screenshot or save it anywhere:")
    print()
    print(f"  Secret: {secret}")
    print()

    # 5. Prompt for first code.
    print("Now enter the 6-digit code your authenticator app is showing")
    print("RIGHT NOW. The .env file will NOT be modified unless this code")
    print("verifies successfully.")
    code = _prompt_totp_code_once(use_stdin=args.stdin)

    # 6. Verify. Use a fresh ReplayCache so we don't poison the dashboard's
    # cache (and so a verify-success in setup doesn't block the operator's
    # very first login).
    setup_cache = ReplayCache()
    ok, info = verify_code(code, secret=secret, replay_cache=setup_cache)
    # Wipe local code variable as defence-in-depth (memory hygiene).
    code = "\0" * 8  # noqa: F841
    del code
    if not ok:
        _err(f"code did not verify (reason: {info.get('reason')}). "
             ".env was not modified. Re-run --enable-totp to try again.",
             code=1)

    # 7. Write to .env.
    backup = _backup_env(env_path)
    if backup != env_path:
        print(f"Backup:  {backup}")
    lines = _read_env_file(env_path)
    lines = _replace_or_append(lines, "DASHBOARD_TOTP_SECRET", secret)
    _write_env_safely(env_path, lines)
    print(f"Wrote DASHBOARD_TOTP_SECRET to {env_path} (permissions 0o600).")

    # Wipe local secret reference.
    secret = "\0" * 32  # noqa: F841
    del secret

    # 8. Audit (best-effort — same resilience pattern as --disable-totp).
    _try_audit("totp_setup", success=True,
                extras={"via": "tool", "tool": "set_dashboard_password.py"})

    print()
    print("TOTP enabled. Restart the dashboard service to pick up:")
    print("  sudo systemctl restart algo-trader-dashboard.service")
    print()
    print("Your NEXT login will require both the password AND a 6-digit")
    print("code from the authenticator app.")
    return 0


def _disable_totp_flow(args) -> int:
    """M15.3.A.2 — disable TOTP. Recovery path: must work even if DB
    is broken (audit write is best-effort)."""
    env_path = Path(args.env_path).resolve()
    lines = _read_env_file(env_path)
    if not _has_key(lines, "DASHBOARD_TOTP_SECRET"):
        print("DASHBOARD_TOTP_SECRET is not set in .env — nothing to disable.")
        return 0

    backup = _backup_env(env_path)
    if backup != env_path:
        print(f"Backup:  {backup}")
    lines = _remove_key(lines, "DASHBOARD_TOTP_SECRET")
    _write_env_safely(env_path, lines)
    print(f"Removed DASHBOARD_TOTP_SECRET from {env_path}. "
          "TOTP is now disabled; the next login is password-only.")

    _try_audit("totp_disabled", success=True,
                extras={"via": "tool", "tool": "set_dashboard_password.py"})

    print()
    print("Restart the dashboard service to pick up the change:")
    print("  sudo systemctl restart algo-trader-dashboard.service")
    return 0


def _try_audit(kind: str, *, success: bool, extras: dict | None = None) -> None:
    """Best-effort auth_events row. If anything fails (no DB, locked,
    schema mismatch, etc.) — warn to stderr but do NOT block the
    operator action. The disable flow especially MUST NOT be blocked
    by a broken DB; it's the recovery path."""
    try:
        import sqlite3
        from dashboard.auth.audit import (
            record_auth_event, ensure_auth_events_schema,
        )
        db_path = REPO_ROOT / "data" / "signals.db"
        if not db_path.parent.exists():
            print(f"  (audit skipped — {db_path.parent} not present)",
                   file=sys.stderr)
            return
        conn = sqlite3.connect(str(db_path))
        try:
            ensure_auth_events_schema(conn)
            record_auth_event(
                conn,
                kind=kind,
                client_ip="local-tool",
                user_agent="set_dashboard_password.py",
                session_id="",  # no session — it's a CLI invocation
                success=success,
                extras=extras,
            )
        finally:
            conn.close()
    except Exception as e:
        # NEVER include the value of any secret in the warning.
        print(f"  (audit write failed for kind={kind}: "
               f"{type(e).__name__}; tool action completed anyway)",
               file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(
        "Set DASHBOARD_PASSWORD_HASH in .env via bcrypt. Never prints "
        "the password. Backs up .env. Preserves unrelated lines. "
        "Also manages DASHBOARD_TOTP_SECRET via --enable-totp / "
        "--disable-totp."
    ))
    ap.add_argument("--remove-plaintext", action="store_true",
                     help="Remove DASHBOARD_PASSWORD from .env "
                          "(hard cutover). Default: leave for transition.")
    ap.add_argument("--no-secret-key", action="store_true",
                     help="Don't generate DASHBOARD_SECRET_KEY. Default: "
                          "generate one if missing.")
    ap.add_argument("--env-path", default=str(REPO_ROOT / ".env"),
                     help="Path to .env file (default: <repo>/.env)")
    ap.add_argument("--stdin", action="store_true",
                     help="Read password and confirmation from sys.stdin "
                          "instead of the controlling TTY. For tests + "
                          "automation only. Default uses getpass (TTY-only) "
                          "so an interactive operator session NEVER reads "
                          "a piped password by accident.")
    ap.add_argument("--enable-totp", action="store_true",
                     help="M15.3.A.2 — Enable TOTP / Google Authenticator "
                          "2FA. Generates a new TOTP secret, displays a "
                          "QR code in the terminal, prompts for the first "
                          "code to verify the operator has scanned it, "
                          "and writes DASHBOARD_TOTP_SECRET to .env only "
                          "if verification succeeds. NEVER writes the "
                          "secret to disk before verification passes. "
                          "Aborts cleanly on Ctrl-C without modifying .env. "
                          "Requires DASHBOARD_PASSWORD or DASHBOARD_PASSWORD_HASH "
                          "to already be set.")
    ap.add_argument("--disable-totp", action="store_true",
                     help="M15.3.A.2 — Disable TOTP. Removes "
                          "DASHBOARD_TOTP_SECRET from .env. Preserves the "
                          "password hash and secret key. Writes a "
                          "totp_disabled audit event if possible. Continues "
                          "(with warning) if audit-DB is unavailable — "
                          "this is the recovery path and must not be "
                          "blocked by a broken DB.")
    args = ap.parse_args(argv)

    # M15.3.A.2 — dispatch to TOTP sub-flows BEFORE the password prompt.
    # --enable-totp and --disable-totp are exclusive of each other AND of
    # the default password-setting flow (no point combining a password
    # change with a TOTP enable in one command; they are independent
    # operator actions).
    if args.enable_totp and args.disable_totp:
        _err("--enable-totp and --disable-totp are mutually exclusive", code=1)
    if args.enable_totp:
        return _enable_totp_flow(args)
    if args.disable_totp:
        return _disable_totp_flow(args)

    # Import bcrypt early so we fail before prompting if missing.
    try:
        from dashboard.auth.passwords import hash_password
    except ImportError as e:
        _err(f"could not import bcrypt helper: {e}", code=2)

    env_path = Path(args.env_path).resolve()
    print(f"Editing: {env_path}")
    if env_path.exists():
        backup = _backup_env(env_path)
        print(f"Backup:  {backup}")
    else:
        print("(.env does not exist yet — will be created)")

    plaintext = _prompt_password_twice(use_stdin=args.stdin)

    print("Hashing with bcrypt cost factor 12 (this takes ~250ms)...")
    try:
        hashed = hash_password(plaintext)
    except Exception as e:
        # Never include the password in error output.
        _err(f"bcrypt hash failed: {type(e).__name__}", code=2)
    finally:
        # Best-effort overwrite of the local plaintext variable.
        plaintext = "\0" * 64  # noqa: F841 — local-scope hygiene
        del plaintext

    # Edit .env: set DASHBOARD_PASSWORD_HASH, optionally remove
    # DASHBOARD_PASSWORD, optionally generate DASHBOARD_SECRET_KEY.
    lines = _read_env_file(env_path)
    lines = _replace_or_append(lines, "DASHBOARD_PASSWORD_HASH", hashed)
    if args.remove_plaintext:
        lines = _remove_key(lines, "DASHBOARD_PASSWORD")
        print("Removed DASHBOARD_PASSWORD (hard cutover).")
    else:
        print(
            "Kept DASHBOARD_PASSWORD as transitional fallback. "
            "When you're ready for hard cutover, re-run with "
            "--remove-plaintext."
        )
    if not args.no_secret_key and not _has_key(lines, "DASHBOARD_SECRET_KEY"):
        new_secret = secrets.token_urlsafe(48)
        lines = _replace_or_append(lines, "DASHBOARD_SECRET_KEY", new_secret)
        print("Generated DASHBOARD_SECRET_KEY (stable across password rotations).")
    elif _has_key(lines, "DASHBOARD_SECRET_KEY"):
        print("DASHBOARD_SECRET_KEY already set — left as-is.")

    _write_env_safely(env_path, lines)
    try:
        mode = oct(os.stat(env_path).st_mode & 0o777)
        print(f"Wrote .env with permissions {mode} (expected 0o600).")
    except OSError:
        pass

    # Final guidance — no secret material in this message.
    print()
    print("Done. Restart the dashboard service to pick up the new hash:")
    print("  sudo systemctl restart algo-trader-dashboard.service")
    print()
    print("To verify the new password works without printing it:")
    print("  curl -s -X POST http://127.0.0.1:8080/api/login \\")
    print("       -H 'Content-Type: application/json' \\")
    print("       -d @<(echo '{\"password\":\"'$NEW_PW'\"}')")
    print("  (set NEW_PW in a one-shot shell, never paste inline)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
