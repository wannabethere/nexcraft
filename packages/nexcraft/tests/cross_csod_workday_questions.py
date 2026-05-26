"""Parse natural-language questions from ``tests/fixtures/crosscsod_workdayquestions.md``."""

from __future__ import annotations

import re
from pathlib import Path


def load_questions(markdown_path: Path) -> list[str]:
    """Collect questions: double-quoted lines and plain lines ending in ``?`` (heuristic)."""
    text = markdown_path.read_text(encoding="utf-8")
    out: list[str] = []

    for m in re.finditer(r'"([^"]{12,})"', text):
        q = m.group(1).strip()
        if q:
            out.append(q)

    skip_prefixes = (
        "🔗",
        "🔄",
        "➡️",
        "📊",
        "Corner",
        "CSOD Training",
        "Training Effectiveness",
        "Employee Learning",
        "ROI Analysis",
        "Manager Team",
        "Division Performance",
        "Training Without",
        "Incomplete Learning",
        "Training Volume",
        "Employee Training",
        "Skills Development",
        "External Learning",
        "Skills Assessment",
        "Comprehensive Learning",
        "Data Quality",
        "Unified Employee",
        "Manager Oversight",
        "Peer Comparison",
        "Skills Gap",
        "Succession Pipeline",
        "Training Program Optimization",
    )

    for line in text.splitlines():
        t = line.strip()
        if not t.endswith("?") or len(t) < 25:
            continue
        if '"' in t:
            continue
        if any(t.startswith(p) for p in skip_prefixes):
            continue
        if t.startswith("-") or t.startswith("#"):
            continue
        if ":" in t and not t.endswith("?"):
            continue
        out.append(t)

    seen: set[str] = set()
    deduped: list[str] = []
    for q in out:
        if q not in seen:
            seen.add(q)
            deduped.append(q)
    return deduped
