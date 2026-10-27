"""research.common — shared, deterministic building blocks for all research lanes.

* hashing   — canonical JSON digests (reuses M19 provenance), frame digests
* rng       — named, independent, reproducible random streams
* bars      — OHLCV validation and session-aware resampling
* synthetic — synthetic markets with KNOWN ground truth (regimes, sectors,
              planted effects, intrabar paths) for testing the machinery
"""
