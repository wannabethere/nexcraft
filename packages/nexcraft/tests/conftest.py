"""Ensure sibling test helpers (``cross_csod_*.py``) import when pytest rootdir is the monorepo."""

from __future__ import annotations

import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parent
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
