"""M21.U4 Europe source-audit package (read-only).

Audits candidate constituent sources for the supported EU venues (DAX, SMI,
AEX, CAC, IBEX), attempts machine downloads from documented endpoints, inspects
each saved file, and emits a single markdown report. Performs NO curation, NO
writes to global_expanded.json / source_registry.json, NO runtime changes.
"""
