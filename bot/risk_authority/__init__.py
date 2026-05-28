"""bot/risk_authority/ — M14 Risk Intelligence Layer.

M14.B scope: schema/migration helpers only. The decision engine, governor,
ingestion, exposure logic, and dashboard are deferred to M14.C–G per the
approved M14.A design (docs/M14_A_design.md).

This package must NEVER be imported from bot.scanner, bot.strategy,
bot.risk, or main.py. M14.E will introduce the pure decide() core; until
then this module contains read-only helpers for Risk-Authority-internal
use only.
"""
