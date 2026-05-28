"""tools/ — operator-only CLIs.

Modules here are NEVER imported by main.py, the scanner, the strategy,
the risk manager, or any non-operator runtime path. Importing any
module here at scanner-load time is a violation of the M13.5.A §1.4
scanner-isolation invariant and is asserted against by the
test_m13_5_scanner_isolation.py test suite.
"""
