from ontology_foundry.analysis.stats import profile_categorical_column, profile_numeric_column, top_k_freq
from ontology_foundry.context import (
    TabularContextBundle,
    column_context_from_profile,
    render_tabular_context,
    tabular_context_as_document,
)


def test_render_includes_cardinality_and_sample() -> None:
    tenure_p = profile_numeric_column("tenure_months", [12.0, 24.0, None, 48.0])
    region_vals = ["west", "west", "east", "east", "east", "north"]
    region_p = profile_categorical_column("region", region_vals)
    uid_p = profile_categorical_column("user_id", [f"u-{i}" for i in range(100)])

    bundle = TabularContextBundle(
        table_id="hr.employees",
        table_description="Employee roster",
        source_system="postgres:hr.employees",
        population_row_count=50_000,
        sample_description="random sample n=100 (example)",
        extra_metadata={"owner": "analytics"},
        columns=[
            column_context_from_profile("user_id", uid_p, declared_type="varchar(36)", role="primary_key"),
            column_context_from_profile(
                "region",
                region_p,
                declared_type="text",
                top_frequencies=top_k_freq(region_vals, k=5),
            ),
            column_context_from_profile("tenure_months", tenure_p, declared_type="double precision"),
        ],
        sample_rows=[
            {"user_id": "u-0", "region": "west", "tenure_months": 12.0},
            {"user_id": "u-1", "region": "east", "tenure_months": 24.0},
        ],
    )
    text = render_tabular_context(bundle, max_sample_rows=10)
    assert "hr.employees" in text
    assert "50000" in text
    assert "user_id" in text
    assert "identifier" in text.lower()
    assert "region" in text
    assert "Top frequencies" in text
    assert "tenure_months" in text
    assert "mean=" in text or "min=" in text
    assert "```json" in text
    assert "u-0" in text

    doc = tabular_context_as_document(bundle, doc_id="ctx-1")
    assert doc.doc_id == "ctx-1"
    assert doc.metadata.get("table_id") == "hr.employees"
    assert "Tabular context" in doc.text
