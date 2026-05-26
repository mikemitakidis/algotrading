"""
bot/etoro/
==========

eToro Public API client package.

M13.2 contract: READ-ONLY. This package contains no code path capable
of issuing a non-GET HTTP request. It is NOT a `BrokerAdapter`
subclass and is NOT registered in the broker factory. No runtime path
in main.py calls it in M13.2.

See also:
  - docs/M13_1_design.md
  - docs/M13_1_order_schema_mapping.md
  - docs/M13_2_read_adapter.md
"""
