"""research — offline M21 research and self-development package (Issues #2 / #3).

Everything under ``research/`` is OFFLINE research code. It exists to produce
trustworthy learning evidence and to build and benchmark AI/ML models on
synthetic, repository or separately approved data. It never trades.

Hard boundaries (enforced by ``test_m21_research_foundation.py``):

* No production module imports ``research`` (``bot/``, ``dashboard/``,
  ``main.py``, ``tools/`` stay unaware of it).
* ``research`` imports only the read-only libraries listed in
  ``research.boundary.ALLOWED_BOT_MODULES`` (M16 store readers, M17
  simulator/indicators, M18 ML library, M19 scoring) plus the scientific
  Python stack. Broker, order, notifier, kill-switch, runtime, dashboard,
  network and secret-loading modules are forbidden.
* Research code writes only to caller-supplied paths (never ``data/``).
* No result produced here is a realised profit or a validated edge. Synthetic
  results describe the machinery, not the market.

Lanes (each on its own ``research/*`` branch and draft PR):
  A  research.evidence   counterfactual candidate/outcome evidence
  B  research.integrity  trial ledger, validation, controls, report cards
  C  research.mllab      advanced model laboratory
  D  research.genome     strategy-as-data and multi-symbol backtesting
  E  research.agents     offline research-agent foundations
  F  research.dataqa       data inventory, quality and manifests
Shared: research.common (hashing, RNG streams, synthetic markets, bars).
"""

__all__ = ["RESEARCH_PACKAGE_VERSION"]

RESEARCH_PACKAGE_VERSION = "m21-research-0.1"
