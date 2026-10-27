"""research.common.rng — named, independent, reproducible random streams.

``stream(seed, "synthetic", "returns")`` always yields the same generator for
the same (seed, names) and statistically independent generators for different
names. Components never share a generator, so adding draws in one component
never changes another component's numbers (a common source of irreproducible
research results).
"""
from __future__ import annotations

import hashlib
from typing import Tuple

import numpy as np


def _name_words(names: Tuple[str, ...]) -> Tuple[int, ...]:
    h = hashlib.sha256("\x1f".join(names).encode("utf-8")).digest()
    return tuple(int.from_bytes(h[i:i + 4], "little") for i in range(0, 16, 4))


def seed_sequence(seed: int, *names: str) -> np.random.SeedSequence:
    if not isinstance(seed, (int, np.integer)) or isinstance(seed, bool) or seed < 0:
        raise ValueError("seed must be a non-negative int, got %r" % (seed,))
    return np.random.SeedSequence(entropy=int(seed), spawn_key=_name_words(tuple(names)))


def stream(seed: int, *names: str) -> np.random.Generator:
    """A PCG64 generator keyed by (seed, *names)."""
    return np.random.Generator(np.random.PCG64(seed_sequence(seed, *names)))


def child_seed(seed: int, *names: str) -> int:
    """A derived 31-bit integer seed (safe for sklearn / xgboost / lightgbm)."""
    return int(seed_sequence(seed, *names).generate_state(1, dtype=np.uint32)[0] >> np.uint32(1))


__all__ = ["seed_sequence", "stream", "child_seed"]
