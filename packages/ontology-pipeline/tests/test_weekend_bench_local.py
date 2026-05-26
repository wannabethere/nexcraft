"""Offline checks for weekend bench (no API keys)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ontology_foundry.context.table_bundle import render_tabular_context

# scripts/ on path when pytest runs from package root with pythonpath
import sys

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from weekend_bench.bundle_loader import bundle_from_preview_files, default_preview_paths
from weekend_bench.grade import grade_answer, parse_model_json
from weekend_bench.serializers import render_json_context


_PREVIEW = Path(__file__).resolve().parents[1] / "output" / "preview"


@pytest.mark.skipif(
    not (_PREVIEW / "column_stats/csod-local/public/users_core.aggregates.json").is_file(),
    reason="preview artifacts not present",
)
def test_bundle_and_serializers_non_empty() -> None:
    agg, samples = default_preview_paths(_PREVIEW)
    bundle = bundle_from_preview_files(agg, samples, max_sample_rows=3)
    md = render_tabular_context(bundle, max_sample_rows=3)
    js = render_json_context(bundle, max_sample_rows=3)
    assert "user_id" in md
    assert "user_id" in js
    assert bundle.columns


def test_grade_percent_and_integer() -> None:
    parsed = parse_model_json('{"answer": "23.4", "evidence_column": "user_email"}')
    ok, _ = grade_answer(parsed=parsed, gold="23.4", kind="percent")
    assert ok
    parsed2 = parse_model_json('{"answer": "500", "evidence_column": "user_id"}')
    ok2, _ = grade_answer(parsed=parsed2, gold="500", kind="integer")
    assert ok2


def test_questions_file_loads() -> None:
    path = _SCRIPTS / "weekend_questions.json"
    rows = json.loads(path.read_text())
    assert len(rows) >= 10
    assert rows[0]["id"].startswith("q")
