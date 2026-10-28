"""M21 WP8 — strategy-id helpers with no heavy imports (safe for dashboards).

Strategy ids: ``tpl:<template>@v<version>#<params digest>`` for templates
run through ``bot.templates``; ``scanner_v<n>`` for the scanner (Template 1).
"""
from __future__ import annotations

from typing import Optional

TEMPLATE_1 = "scanner"


def template_of(strategy_id: Optional[str]) -> str:
    """Template id of a strategy id; unrecognised ids are returned unchanged
    (never guessed)."""
    s = (strategy_id or "").strip()
    if s.startswith("tpl:") and "@v" in s:
        return s[4:s.index("@v")]
    if s.startswith("scanner_v"):
        return TEMPLATE_1
    return s


__all__ = ["TEMPLATE_1", "template_of"]
