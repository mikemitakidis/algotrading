"""Entrypoint for `python -m bot.ml`."""
from __future__ import annotations

import sys

from bot.ml.cli import main

if __name__ == "__main__":
    sys.exit(main())
