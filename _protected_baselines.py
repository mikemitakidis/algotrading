"""Approved post-baseline changes to protected production files (M21 C5b).

TEST-ONLY helper. Imports no project module, writes nothing, opens no
network connection. It runs ``git rev-parse`` (read-only) and reads the
protected files to hash them.

Why this exists
---------------
Several milestone test modules freeze protected production files against
the commit that was HEAD when that milestone was written (e.g. M15.3.A
freezes ``bot/scanner.py`` at ``648682c``; the M20 suites freeze
``bot/brokers`` and ``bot/signal_scoring`` at M19 head ``e823fe6``). Later,
separately reviewed and approved milestones legitimately changed some of
those files (three at C5b; dashboard/app.py added with the D3 wiring), so
the guards went red for a reason that is not a defect.

Rather than moving each baseline forward (which would silently re-bless
everything that changed in between) or removing files from the protected
lists (which is what the P0-4 precedent did, and which stops guarding the
file at all), each guard keeps its original baseline and its full protected
list. A changed file is excused ONLY if the change is exactly one of the
transitions below:

  * the file's content AT THE GUARD'S BASELINE is the recorded ``from_blob``,
    AND
  * the file's content in the working tree is the recorded ``to_blob``.

Any other edit to these files, or any change to any other protected file,
still fails the guard. A future approved change to one of these files must
update ``to_blob`` (and the approving commit) here, in review.

Chained transitions (M21.1 A01): when a file is approved a second time, guards
whose baseline predates the first approval hold ``from_blob`` while guards
whose baseline sits between the two approvals hold the first approval's
``to_blob``. ``also_from`` lists those recorded, previously approved
intermediate blobs explicitly. A baseline must still hold exactly one listed
blob and the file must still be exactly ``to_blob``; nothing else is excused.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, NamedTuple, Optional, Tuple

REPO = Path(__file__).resolve().parent


class ApprovedChange(NamedTuple):
    from_blob: str      # git blob id of the file at the old milestone baselines
    to_blob: str        # git blob id of the approved current content
    commit: str         # last approved commit that produced ``to_blob``
    approval: str       # what approved it
    also_from: Tuple[str, ...] = ()   # earlier approved blobs (chained approvals)


APPROVED_CHANGES: Dict[str, ApprovedChange] = {
    "bot/scanner.py": ApprovedChange(
        from_blob="63a71b91ac88a34905fe29135b5d857f318ba129",
        to_blob="581f2a7cb40b1b372b443cd416826d16b85a58b7",
        commit="a49375a0aa17ca0350ac00bdacf107517b3a583d",
        approval="pre-M19 Group D: scanner partial-data hard-block "
                 "(ISSUE-009) + WATCH drop (ISSUE-010) [85c5092, blob 4317e57]; "
                 "then M21.1 A01: metadata-only bar as-of record in scan_cycle "
                 "meta (no signal/decision change) [6242bde, blob 08dc751]; "
                 "then M21 WP6 (C01 §11 / D-4): opt-in completed-bar basis seam "
                 "(default unchanged; operator instruction 28 Oct 2026 09:15) "
                 "— PENDING independent review",
        # Group D's approved content: what the M19/M20 guard baseline holds.
        also_from=("4317e57e72f9d8d862607a8e3f155cc53f4ecd3d",),
    ),
    "bot/signal_scoring/gates.py": ApprovedChange(
        from_blob="35675d747b9e81bfd3e03ef85a1e268a95ab4dfa",
        to_blob="7c94181976d45236a9fed96a75cbac9c612dfe87",
        commit="f2184fbc65ccb016f869a536fc2b8ad8af431ce1",
        approval="M21.1: scoring bridge (read-only, research-grade ranking)",
    ),
    "bot/brokers/ibkr_broker.py": ApprovedChange(
        from_blob="b0b3cb0fa8cb903c8d3089906d9b202621fa2183",
        to_blob="08640cd09acace7c04b4f55ce30ac5e00b98c58a",
        commit="5d6a2300fa3cc9aa4847bb7a58fbf97ce49de4a5",
        approval="M21.1extra-B2flat flatten primitive (3a5d74f..802491b) + "
                 "D2/R2-3 paper-mode submit() port==4002 guard (5d6a230)",
    ),
    "bot/providers/alpaca_provider.py": ApprovedChange(
        from_blob="944f3918c0b10a60edca5e68dc3c4d153a1e9a93",
        to_blob="0d756dd3dbe899bc7aa42a2a317c4385fc562a4e",
        commit="a49375a0aa17ca0350ac00bdacf107517b3a583d",
        approval="M21 WP6: explicit adjustment=raw on both Alpaca bar requests "
                 "(the documented API default; makes the price basis provable) "
                 "— PENDING independent review",
    ),
    "dashboard/app.py": ApprovedChange(
        from_blob="1bd8712c660348d0471ac8a56759c5880e868908",
        to_blob="564fc812ef15e2cc088ab84f1e377021a18b8e99",
        commit="7f65141329bb1a3af4e5a84203b3a1cf06037ca5",
        approval="M21.1extra-D3b: one nav link + registration of the read-only, "
                 "GET-only, auth-required paper-metrics blueprint (reviewed "
                 "patch a4986d2; sha256 97a3d678... pinned in test_m17)",
    ),
}


def worktree_blob(rel: str, repo: Path = REPO) -> Optional[str]:
    """Git blob id of the working-tree file (no filters: the repo has no
    .gitattributes), or None if it does not exist."""
    p = repo / rel
    if not p.is_file():
        return None
    data = p.read_bytes()
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def rev_blob(baseline: str, rel: str, repo: Path = REPO) -> Optional[str]:
    """Git blob id of ``rel`` at revision ``baseline``, or None if absent."""
    r = subprocess.run(["git", "rev-parse", "--verify", "--quiet",
                        f"{baseline}:{rel}"],
                       cwd=str(repo), capture_output=True, text=True, timeout=20)
    out = r.stdout.strip()
    return out if r.returncode == 0 and out else None


def is_approved_change(baseline: str, rel: str, repo: Path = REPO,
                       table: Optional[Dict[str, ApprovedChange]] = None,
                       target: Optional[str] = None) -> bool:
    """True only if ``rel`` differs from ``baseline`` by exactly one approved
    transition: baseline content == from_blob (or one of the recorded
    ``also_from`` blobs) AND target content == to_blob.

    ``target`` must match what the calling guard diffs against: None for the
    working tree (``git diff <baseline> -- f``), or a revision such as
    ``"HEAD"`` (``git diff <baseline> HEAD -- f``)."""
    entry = (APPROVED_CHANGES if table is None else table).get(rel)
    if entry is None:
        return False
    current = (worktree_blob(rel, repo) if target is None
               else rev_blob(target, rel, repo))
    return (rev_blob(baseline, rel, repo) in (entry.from_blob,) + tuple(entry.also_from)
            and current == entry.to_blob)


def unapproved(baseline: str, changed: Iterable[str], repo: Path = REPO,
               table: Optional[Dict[str, ApprovedChange]] = None,
               target: Optional[str] = None) -> List[str]:
    """Filter a list of changed repo-relative paths down to the ones that are
    NOT exactly an approved transition from ``baseline`` to ``target``."""
    return [p for p in changed
            if not is_approved_change(baseline, p, repo, table, target)]
