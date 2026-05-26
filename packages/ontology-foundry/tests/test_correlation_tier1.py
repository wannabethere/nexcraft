from ontology_foundry.analysis.correlation_pipeline import (
    effect_threshold_for_n,
    emit_candidate_pair,
    type_compatible,
)
from ontology_foundry.analysis.models import NumericColumnProfile


def test_type_compatible() -> None:
    assert type_compatible("numeric", "numeric")
    assert not type_compatible("numeric", "text")


def test_emit_candidate_pair_keeps() -> None:
    profiles = {
        "t.a": NumericColumnProfile(column="t.a", n_rows=100, null_rate=0.1, distinct_count=50),
        "t.b": NumericColumnProfile(column="t.b", n_rows=100, null_rate=0.1, distinct_count=50),
    }
    types = {"t.a": "numeric", "t.b": "numeric"}
    out = emit_candidate_pair(
        "t.a",
        "t.b",
        profiles=profiles,
        types=types,
        qualified_split_a=("training", "a"),
        qualified_split_b=("training", "b"),
        allowed_schema_pairs={("training", "training")},
    )
    assert out is not None
    assert out.column_a == "t.a"


def test_emit_candidate_pair_drops_high_null() -> None:
    profiles = {
        "t.a": NumericColumnProfile(column="t.a", n_rows=100, null_rate=0.96, distinct_count=4),
        "t.b": NumericColumnProfile(column="t.b", n_rows=100, null_rate=0.1, distinct_count=50),
    }
    assert emit_candidate_pair("t.a", "t.b", profiles=profiles) is None


def test_effect_threshold_n() -> None:
    assert effect_threshold_for_n(100) >= 0.1
