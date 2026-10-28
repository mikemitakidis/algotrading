"""M21 WP15 (canonical B18, M21.6) — market context, point-in-time, shadow only.

store     point-in-time observation store (publication time, vintages, licence)
sources   source registry; parsers for FRED CSV and the operator's event calendar
features  point-in-time features with explicit OK / STALE / UNAVAILABLE states
throttle  reduce-only shadow decision (size multiplier in [0, 1], abstain)

No network access, no data purchase, no broker. Nothing is enforced yet.
"""
