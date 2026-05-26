"""Tabular profiling — wraps `ontology_foundry.context.bundle_from_pandas`.

Runs at introspect time for every table. Output lands in two places:

  - `ontology_store.column_stat` / `table_stat` — durable profile rows used by
    retrieval + audit + cross-run diff.
  - `EnrichmentContext.bundle` — the in-memory `TabularContextBundle` every
    enricher sees in the same run, so downstream prompts can ground in stats
    without re-fetching.

The profiler does NOT decide PII safety on its own. Aggregates (null rate,
distinct count, min/max/mean/stddev) are written eagerly. Value-bearing
fields (sample_rows, top_frequencies) flip on only after the
`data_protection` enricher has cleared the column.
"""
from ontology_pipeline.profile.csv_sample_loader import (
    CsvSampleLoader,
    build_csv_sample_loader,
)
from ontology_pipeline.profile.profiler import (
    TableProfiler,
    bundle_to_aggregates,
    bundle_to_table_facts,
    bundle_to_top_frequencies,
    resolve_cardinality_tier,
)

__all__ = [
    "TableProfiler",
    "bundle_to_aggregates",
    "bundle_to_table_facts",
    "bundle_to_top_frequencies",
    "resolve_cardinality_tier",
    "CsvSampleLoader",
    "build_csv_sample_loader",
]
