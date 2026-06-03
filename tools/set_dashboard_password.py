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


def _prompt_password_twice() -> str:
    """getpass twice, confirm match. Never echoes or returns to stdout."""
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


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(
        "Set DASHBOARD_PASSWORD_HASH in .env via bcrypt. Never prints "
        "the password. Backs up .env. Preserves unrelated lines."
    ))
    ap.add_argument("--remove-plaintext", action="store_true",
                     help="Remove DASHBOARD_PASSWORD from .env "
                          "(hard cutover). Default: leave for transition.")
    ap.add_argument("--no-secret-key", action="store_true",
                     help="Don't generate DASHBOARD_SECRET_KEY. Default: "
                          "generate one if missing.")
    ap.add_argument("--env-path", default=str(REPO_ROOT / ".env"),
                     help="Path to .env file (default: <repo>/.env)")
    args = ap.parse_args(argv)

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

    plaintext = _prompt_password_twice()

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
