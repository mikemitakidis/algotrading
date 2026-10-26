"""research.integrity.splits — leakage-resistant evaluation splits.

Observations are EVENTS with an information interval [start, end]: `start` is
when the decision is made (the anchor) and `end` is when its label is known
(the label resolution). Two events overlap when their intervals intersect.
Any training event whose interval overlaps a test block is PURGED, and events
starting within `embargo` units after a test block are EMBARGOED (López de
Prado, Advances in Financial Machine Learning, ch. 7). Units are whatever the
caller uses consistently (bar index, or int64 nanoseconds).

Provides:
* purged_kfold        — K contiguous test folds (for OOF predictions)
* walk_forward        — chronological folds, train strictly before test
* cpcv                — combinatorial purged CV with backtest-path assembly
* leakage_report      — independent verifier used by tests and other lanes
"""
from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations
from math import comb
from typing import Dict, List, Sequence, Tuple

import numpy as np


@dataclass(frozen=True)
class Fold:
    train: np.ndarray
    test: np.ndarray
    test_groups: Tuple[int, ...] = ()
    purged: int = 0
    embargoed: int = 0


def _events(starts, ends) -> Tuple[np.ndarray, np.ndarray]:
    s = np.asarray(starts)
    e = np.asarray(ends)
    if s.shape != e.shape or s.ndim != 1:
        raise ValueError("starts and ends must be 1-D and equal length")
    if s.dtype.kind == "M":
        s = s.astype("datetime64[ns]").astype(np.int64)
        e = e.astype("datetime64[ns]").astype(np.int64)
    s = s.astype(np.int64)
    e = e.astype(np.int64)
    if len(s) and (np.diff(s) < 0).any():
        raise ValueError("events must be sorted by start")
    if (e < s).any():
        raise ValueError("an event ends before it starts")
    return s, e


def _purge(train_idx: np.ndarray, s: np.ndarray, e: np.ndarray,
           blocks: Sequence[Tuple[int, int]], embargo: int) -> Tuple[np.ndarray, int, int]:
    """Remove train events overlapping any [lo, hi] test block, then embargo."""
    keep = np.ones(len(train_idx), dtype=bool)
    purged = embargoed = 0
    ts, te = s[train_idx], e[train_idx]
    for lo, hi in blocks:
        ov = keep & (ts <= hi) & (te >= lo)
        purged += int(ov.sum())
        keep &= ~ov
        if embargo > 0:
            em = keep & (ts > hi) & (ts <= hi + embargo)
            embargoed += int(em.sum())
            keep &= ~em
    return train_idx[keep], purged, embargoed


def _groups(n: int, n_groups: int) -> List[np.ndarray]:
    if n_groups < 2 or n < n_groups:
        raise ValueError("need at least n_groups (>= 2) events")
    return np.array_split(np.arange(n), n_groups)


def _block(s, e, idx) -> Tuple[int, int]:
    return int(s[idx].min()), int(e[idx].max())


def purged_kfold(starts, ends, n_splits: int = 5, embargo: int = 0) -> List[Fold]:
    s, e = _events(starts, ends)
    folds = []
    groups = _groups(len(s), n_splits)
    for g, test in enumerate(groups):
        train = np.setdiff1d(np.arange(len(s)), test)
        train, p, em = _purge(train, s, e, [_block(s, e, test)], embargo)
        folds.append(Fold(train=train, test=test, test_groups=(g,), purged=p, embargoed=em))
    return folds


def walk_forward(starts, ends, n_folds: int = 5, embargo: int = 0, expanding: bool = True,
                 min_train_groups: int = 1) -> List[Fold]:
    """Split events into n_folds + min_train_groups chronological groups; fold
    j tests group (min_train_groups + j) and trains only on earlier groups
    (all of them if expanding, else the most recent min_train_groups)."""
    s, e = _events(starts, ends)
    groups = _groups(len(s), n_folds + min_train_groups)
    folds = []
    for j in range(n_folds):
        g = min_train_groups + j
        test = groups[g]
        lo_g = 0 if expanding else g - min_train_groups
        train = np.concatenate(groups[lo_g:g])
        # Purge: a train event must also have RESOLVED before the test block starts.
        train, p, em = _purge(train, s, e, [_block(s, e, test)], embargo)
        folds.append(Fold(train=train, test=test, test_groups=(g,), purged=p, embargoed=em))
    return folds


@dataclass(frozen=True)
class CPCV:
    folds: List[Fold]
    n_groups: int
    n_test_groups: int
    paths: List[Dict[int, int]]    # path -> {group: fold index supplying its OOS predictions}

    @property
    def n_paths(self) -> int:
        return len(self.paths)


def cpcv(starts, ends, n_groups: int = 6, n_test_groups: int = 2, embargo: int = 0) -> CPCV:
    """Combinatorial purged CV: every choice of `n_test_groups` of `n_groups`
    groups is a test set (C(N, k) folds); test predictions combine into
    k * C(N, k) / N complete backtest paths."""
    if not 1 <= n_test_groups < n_groups:
        raise ValueError("need 1 <= n_test_groups < n_groups")
    s, e = _events(starts, ends)
    groups = _groups(len(s), n_groups)
    folds = []
    for combo in combinations(range(n_groups), n_test_groups):
        test = np.concatenate([groups[g] for g in combo])
        train = np.setdiff1d(np.arange(len(s)), test)
        blocks = [_block(s, e, groups[g]) for g in combo]
        train, p, em = _purge(train, s, e, blocks, embargo)
        folds.append(Fold(train=train, test=test, test_groups=tuple(combo), purged=p, embargoed=em))
    n_paths = n_test_groups * comb(n_groups, n_test_groups) // n_groups
    occurrences: Dict[int, List[int]] = {g: [] for g in range(n_groups)}
    for fi, f in enumerate(folds):
        for g in f.test_groups:
            occurrences[g].append(fi)
    paths = [{g: occurrences[g][p] for g in range(n_groups)} for p in range(n_paths)]
    return CPCV(folds=folds, n_groups=n_groups, n_test_groups=n_test_groups, paths=paths)


@dataclass(frozen=True)
class LeakageReport:
    overlapping_pairs: int
    embargo_violations: int
    shared_indices: int

    @property
    def clean(self) -> bool:
        return self.overlapping_pairs == 0 and self.embargo_violations == 0 and self.shared_indices == 0


def leakage_report(train_idx, test_idx, starts, ends, embargo: int = 0) -> LeakageReport:
    """Independent brute-force check of a split (O(n_train * n_test))."""
    s, e = _events(starts, ends)
    tr = np.asarray(train_idx, dtype=np.int64)
    te = np.asarray(test_idx, dtype=np.int64)
    shared = int(np.intersect1d(tr, te).size)
    ov = (s[tr][:, None] <= e[te][None, :]) & (e[tr][:, None] >= s[te][None, :])
    # Embargo: train events starting within `embargo` after the end of a
    # contiguous test run are violations.
    viol = 0
    if embargo > 0 and len(te):
        te_sorted = np.sort(te)
        runs = np.split(te_sorted, np.flatnonzero(np.diff(te_sorted) > 1) + 1)
        for r in runs:
            hi = e[r].max()
            viol += int(((s[tr] > hi) & (s[tr] <= hi + embargo)).sum())
    return LeakageReport(overlapping_pairs=int(ov.sum()), embargo_violations=viol, shared_indices=shared)


__all__ = ["Fold", "purged_kfold", "walk_forward", "CPCV", "cpcv", "LeakageReport", "leakage_report"]
