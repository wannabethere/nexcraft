from __future__ import annotations

import json
import re
from typing import Any, Literal

AnswerKind = Literal["integer", "float", "percent", "text"]


def parse_model_json(raw: str) -> dict[str, Any]:
    text = raw.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", text)
    if fence:
        text = fence.group(1).strip()
    return json.loads(text)


def _normalize_number(s: str) -> float | None:
    s = s.strip().replace("%", "").replace(",", "")
    if not s or s.upper() == "UNKNOWN":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def grade_answer(
    *,
    parsed: dict[str, Any],
    gold: str,
    kind: AnswerKind,
    rel_tol: float = 0.02,
) -> tuple[bool, str]:
    """Return (correct, normalized_answer)."""
    answer = str(parsed.get("answer", "")).strip()
    if not answer or answer.upper() == "UNKNOWN":
        return False, answer

    if kind == "text":
        ok = answer.lower() == gold.lower()
        return ok, answer

    pred = _normalize_number(answer)
    ref = _normalize_number(gold)
    if pred is None or ref is None:
        return False, answer

    if kind == "integer":
        ok = int(round(pred)) == int(round(ref))
        return ok, answer

    if kind == "percent":
        # Accept 23.4 vs 0.234 when model reports fraction
        candidates = [pred, pred * 100.0]
        ok = any(abs(c - ref) <= max(0.15, rel_tol * max(abs(ref), 1.0)) for c in candidates)
        return ok, answer

    # float
    ok = abs(pred - ref) <= max(0.01, rel_tol * max(abs(ref), abs(pred), 1.0))
    return ok, answer
