"""M19.G optional JSONL output — the ONLY signal_scoring module permitted to
open/write files, and only to an explicit, validated, system-temp path.

Hard rules (per approved M19.G contract):
  * No default path, no env fallback, no module-level path constant.
  * is_write_safe_path() accepts ONLY paths resolving under the system temp
    directory (tempfile.gettempdir()); everything else is rejected — including
    signals.db, data/ml, data/m19, the repo working tree, repo-root files,
    bot/, configs/, docs/, test files, tracked repo locations, and any path
    whose parent directory does not already exist.
  * The writer validates the path BEFORE opening anything (raises ValueError).
  * Atomic write: serialize all, write to a temp file in the same safe dir,
    os.replace() only on success, cleanup temp on exception, never claim
    success on error.
  * No reader/loader. No runtime integration. No auto-call from scoring.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Iterable, Tuple

from bot.signal_scoring import provenance
from bot.signal_scoring.schema import ScoredSignalCandidate

# Repo root = <repo>/bot/signal_scoring/io.py -> parents[2]. Pure, no git/subprocess.
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _resolved(p) -> Path:
    return Path(p).resolve()


def is_write_safe_path(output_path: "str | os.PathLike") -> Tuple[bool, str]:
    """Return (ok, reason). Only a file path resolving under the system temp
    directory is accepted; everything else is rejected. The parent directory
    must already exist (no implicit mkdir)."""
    try:
        target = _resolved(output_path)
    except (TypeError, ValueError) as e:
        return False, f"unresolvable path: {e}"

    temp_root = Path(tempfile.gettempdir()).resolve()

    # Must resolve UNDER the system temp directory (realpath + commonpath, not
    # raw startswith, so /tmp symlinks and /tmpfoo edge cases are handled).
    try:
        under_temp = os.path.commonpath([str(target), str(temp_root)]) == \
            str(temp_root)
    except ValueError:
        # different drives / no common path
        under_temp = False
    if not under_temp:
        # Explicit, deterministic rejection reasons for the forbidden locations.
        reason = _classify_reject(target)
        return False, reason

    # Even under temp: parent must already exist (no implicit mkdir).
    if not target.parent.is_dir():
        return False, f"parent directory does not exist: {target.parent}"
    # Refuse to treat an existing directory as a file target.
    if target.is_dir():
        return False, f"path is a directory: {target}"
    return True, "ok"


def _classify_reject(target: Path) -> str:
    """Deterministic reason string for a non-temp path."""
    name = target.name
    s = str(target)
    if name == "signals.db" or s.endswith(os.sep + "signals.db"):
        return "rejected: signals.db is never writable"
    repo = str(_REPO_ROOT)
    try:
        in_repo = os.path.commonpath([s, repo]) == repo
    except ValueError:
        in_repo = False
    if in_repo:
        rel = os.path.relpath(s, repo)
        first = rel.split(os.sep)[0]
        if rel.startswith(os.path.join("data", "ml")):
            return "rejected: data/ml is never writable"
        if rel.startswith(os.path.join("data", "m19")):
            return "rejected: data/m19 is never writable in M19.G"
        if first in ("bot", "configs", "docs"):
            return f"rejected: {first}/ is a tracked repo location"
        return "rejected: path is inside the repo working tree"
    return "rejected: only system-temp paths are allowed"


def scored_candidate_to_jsonl_line(candidate: ScoredSignalCandidate) -> str:
    """Pure: one ScoredSignalCandidate -> a single-line canonical-JSON string
    (sorted keys, compact, UTF-8 safe, no NaN/inf). No newline, no I/O."""
    return provenance.canonical_json(candidate.to_dict())


def write_scored_candidates_jsonl(
    candidates: Iterable[ScoredSignalCandidate],
    output_path: "str | os.PathLike",
    *,
    allow_existing: bool = False,
) -> int:
    """Write one ScoredSignalCandidate per line as canonical JSON to an explicit,
    validated, system-temp path. Atomic (temp file + os.replace). Returns the
    number of records written. Raises ValueError on an unsafe path or on an
    existing target when allow_existing is False — before opening anything."""
    ok, reason = is_write_safe_path(output_path)
    if not ok:
        raise ValueError(f"unsafe output_path: {reason}")

    target = _resolved(output_path)
    if target.exists() and not allow_existing:
        raise ValueError(
            f"output_path already exists (pass allow_existing=True to "
            f"overwrite): {target}")

    # Serialize everything first (so a serialization error never leaves a
    # partial file). Newline-terminate every record.
    lines = [scored_candidate_to_jsonl_line(c) for c in candidates]
    payload = "".join(line + "\n" for line in lines)

    # Atomic write: temp file in the SAME directory, then os.replace.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(target.parent), prefix=".m19g_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(target))
    except BaseException:
        # cleanup temp file on any error; never claim success.
        try:
            if os.path.exists(tmp_name):
                os.remove(tmp_name)
        except OSError:
            pass
        raise
    return len(lines)
