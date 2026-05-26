import pytest

from ontology_foundry.context.from_tables import bundle_from_arrow_table, bundle_from_pandas
from ontology_foundry.context.table_bundle import render_tabular_context


def test_bundle_from_pandas_profiles_and_samples() -> None:
    pd = pytest.importorskip("pandas")

    df = pd.DataFrame(
        {
            "employee_id": [10, 20, 30],
            "region": ["east", "east", "west"],
            "score": [1.0, 2.0, None],
        }
    )
    bundle = bundle_from_pandas(
        df,
        table_id="hr.sample",
        population_row_count=10_000,
        max_top_k=10,
        max_sample_rows=2,
        column_roles={"employee_id": "primary_key"},
    )
    assert len(bundle.columns) == 3
    assert len(bundle.sample_rows) == 2
    rendered = render_tabular_context(bundle)
    assert "hr.sample" in rendered
    assert "10000" in rendered
    assert "region" in rendered
    assert "score" in rendered


def test_bundle_from_arrow_table_profiles_and_samples() -> None:
    pa = pytest.importorskip("pyarrow")

    table = pa.table(
        {
            "n": [1, 2, 3],
            "label": ["x", "y", "x"],
        }
    )
    bundle = bundle_from_arrow_table(
        table, table_id="t.arrow", max_top_k=10, max_sample_rows=5
    )
    assert len(bundle.columns) == 2
    assert len(bundle.sample_rows) == 3
    text = render_tabular_context(bundle)
    assert "t.arrow" in text
    assert "label" in text


def test_bundle_from_pandas_and_arrow_consistent_for_small_frame() -> None:
    pd = pytest.importorskip("pandas")
    pa = pytest.importorskip("pyarrow")

    df = pd.DataFrame({"a": [1, 2], "b": ["k", "k"]})
    b_pd = bundle_from_pandas(df, table_id="x", max_sample_rows=10)
    b_pa = bundle_from_arrow_table(pa.Table.from_pandas(df), table_id="x", max_sample_rows=10)
    assert len(b_pd.columns) == len(b_pa.columns)
    assert b_pd.columns[0].stats is not None and b_pa.columns[0].stats is not None
    assert b_pd.columns[0].stats.distinct_count == b_pa.columns[0].stats.distinct_count


def test_bundle_from_arrow_table_rejects_wrong_type() -> None:
    pytest.importorskip("pyarrow")

    with pytest.raises(TypeError, match="pyarrow.Table"):
        bundle_from_arrow_table([], table_id="nope")  # type: ignore[arg-type]
