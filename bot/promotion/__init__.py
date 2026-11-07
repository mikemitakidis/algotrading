"""M21 WP14 (canonical B13, B23 auto-revert) — Promotion & Approval Model for the paper lane.

tiers       proposals and the GREEN / AMBER / RED classifier
evaluation  shadow comparison on live paper signals; the decay / auto-revert check
approvals   operator approval tokens (mock local service; bound to one proposal)
machine     the persisted state machine: shadow-first, change budget, approval,
            RED cooling-off, apply through the WP9 controls, auto-revert with an alert

Nothing here places, cancels or modifies an order, or changes live routing.
"""
