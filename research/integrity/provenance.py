"""research.integrity.provenance — verifiable identities for code and outcomes.

* ``code_identity(paths)`` — a reproducible digest of the source files a trial
  ran: relative path plus SHA-256 of the bytes of every ``.py`` file under
  the given package directories or files, sorted. Any edit to any of those
  files changes it; identical checkouts give identical identities on any
  machine. Replaces literal placeholders such as ``code_digest='research.mllab'``
  (PR #7 / #10 review).
* ``outcome_digest(values)`` — the digest of a float64 outcome series. A trial
  records the digests of the series it produced in ``TrialRecord.artifacts``;
  the report card recomputes them from its inputs and refuses outcome
  provenance on any mismatch (PR #5 review, P1).

What this proves and does not: equality with the ledgered digest proves the
card evaluated exactly the series committed in that trial record. It does not
prove the record itself came from the claimed computation; that needs an
independent recomputation from the snapshot and code identity (or an
anchored ledger tip held outside the research process).
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Iterable, Optional, Union

import numpy as np

from research.common.hashing import array_digest, digest

REPO_ROOT = Path(__file__).resolve().parents[2]


def outcome_digest(values) -> str:
    return array_digest(np.ascontiguousarray(np.asarray(values, dtype=np.float64).ravel()))


def _files(paths: Iterable[Union[str, Path]], root: Path):
    out = set()
    for p in paths:
        full = (root / p) if not Path(p).is_absolute() else Path(p)
        if full.is_dir():
            out.update(f for f in full.rglob("*.py") if "__pycache__" not in f.parts)
        elif full.is_file():
            out.add(full)
        else:
            raise FileNotFoundError("code identity path not found: %s" % full)
    return sorted(out)


def code_identity(paths: Iterable[Union[str, Path]], root: Optional[Union[str, Path]] = None) -> str:
    root = Path(root) if root is not None else REPO_ROOT
    items = [{"path": f.relative_to(root).as_posix(), "sha256": hashlib.sha256(f.read_bytes()).hexdigest()}
             for f in _files(paths, root)]
    if not items:
        raise ValueError("no source files for code identity")
    return "src-" + digest(items)


__all__ = ["REPO_ROOT", "outcome_digest", "code_identity"]
