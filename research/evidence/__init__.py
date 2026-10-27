"""research.evidence — lane A: counterfactual evidence engine (canonical B03, B04; supports A02, A17).

* candidates     point-in-time candidate events for every decision stage
* costs          explicit cost scenarios (1x / 2x stress)
* labeler        path labels with path_observed / ambiguous_intrabar /
                 unfillable / simulated_execution flags and data quality
* effective_n    uniqueness and cluster-robust effective sample sizes
* gate_economics observational avoided / forfeited R per gate reason
* parity         exact agreement with the M17 simulator on unambiguous paths
* replay         scanner-replica stage replay + an auditable gate layer
* snapshots      read-only adapter for production candidate_snapshots exports
* evidence       reproducible evidence run (CLI)
"""
