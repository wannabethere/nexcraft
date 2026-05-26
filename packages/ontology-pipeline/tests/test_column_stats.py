"""Tests for the foundry-backed column stats pipeline.

Layers covered:
  - TableProfiler with a stub sample_loader → bundle shape from foundry
  - bundle_to_aggregates / bundle_to_table_facts / bundle_to_top_frequencies
  - resolve_cardinality_tier (identifier / low / medium / high)
  - PII-gated sample persistence path (filesystem sink)
  - format_tabular_grounding (full vs aggregates-only)

ORM/DAO behaviour against Postgres is covered by `tests/test_column_stats_dao.py`
in ontology-store (Postgres-gated, skips without ONTOLOGY_STORE_TEST_URL).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from ontology_pipeline.enrich.base import EnrichmentContext
from ontology_pipeline.enrich.grounding import format_tabular_grounding
from ontology_pipeline.profile import (
    TableProfiler,
    bundle_to_aggregates,
    bundle_to_table_facts,
    bundle_to_top_frequencies,
    resolve_cardinality_tier,
)


# ───────────────────────────────────────────────────────────────────────────
# Fixtures
# ───────────────────────────────────────────────────────────────────────────


def _employee_df() -> pd.DataFrame:
    return pd.DataFrame({
        "id":          list(range(1, 21)),       # identifier
        "department":  ["Eng"] * 10 + ["HR"] * 6 + ["Sales"] * 4,  # low cardinality
        "salary":      [50_000.0 + i * 1000 for i in range(20)],   # high cardinality
        "is_active":   [True] * 18 + [False] * 2,
    })


def _stub_loader(df: pd.DataFrame):
    """sample_loader stand-in: ignores args, returns the given DataFrame."""

    def _load(source_id, schema, table, limit):
        return df.head(limit) if len(df) > limit else df

    return _load


def _column_rks(table_rk: str, names: list[str]) -> dict[str, str]:
    return {n: f"{table_rk}/{n}" for n in names}


# ───────────────────────────────────────────────────────────────────────────
# TableProfiler
# ───────────────────────────────────────────────────────────────────────────


class TestTableProfiler:
    def test_profile_builds_bundle_from_loader(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_id="postgres://csod-pg/testdb/public/csod_employee",
            table_description="Employee master.",
        )
        assert bundle is not None
        assert bundle.table_id.endswith("csod_employee")
        # 4 columns
        assert {c.name for c in bundle.columns} == {"id", "department", "salary", "is_active"}
        # sample_rows captured
        assert len(bundle.sample_rows) > 0
        # source_system uses the requested format
        assert bundle.source_system == "postgres:csod-pg"

    def test_profile_returns_none_on_empty_dataframe(self):
        profiler = TableProfiler(sample_loader=_stub_loader(pd.DataFrame()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        assert bundle is None

    def test_profile_returns_none_on_loader_failure(self):
        def _boom(source_id, schema, table, limit):
            raise RuntimeError("simulated")

        profiler = TableProfiler(sample_loader=_boom)
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        assert bundle is None

    def test_requires_loader_or_dsn(self):
        with pytest.raises(ValueError, match="sample_loader= or dsn_for="):
            TableProfiler()


# ───────────────────────────────────────────────────────────────────────────
# bundle_to_aggregates
# ───────────────────────────────────────────────────────────────────────────


class TestBundleToAggregates:
    def test_projects_each_known_column(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        rks = _column_rks("rk", ["id", "department", "salary", "is_active"])
        aggs = bundle_to_aggregates(
            table_rk="rk", bundle=bundle, column_rk_by_name=rks,
        )
        assert len(aggs) == 4
        # id column → identifier tier (high uniqueness, name hint)
        id_agg = next(a for a in aggs if a.column_rk.endswith("/id"))
        assert id_agg.cardinality_tier == "identifier"
        assert id_agg.distinct_count == 20
        # department → low tier
        dept_agg = next(a for a in aggs if a.column_rk.endswith("/department"))
        assert dept_agg.cardinality_tier == "low"

    def test_skips_columns_not_in_mdl(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        # MDL only knows about 2 of the 4 columns — others should be dropped.
        partial_rks = _column_rks("rk", ["id", "salary"])
        aggs = bundle_to_aggregates(
            table_rk="rk", bundle=bundle, column_rk_by_name=partial_rks,
        )
        assert {a.column_rk for a in aggs} == {"rk/id", "rk/salary"}


class TestResolveCardinalityTier:
    def test_identifier_for_unique_id_column(self):
        # Reuse foundry's actual ColumnContext via a tiny stub matching its shape.
        from ontology_foundry.context.table_bundle import ColumnContext
        from ontology_foundry.analysis.models import NumericColumnProfile
        col = ColumnContext(
            name="employee_id",
            stats=NumericColumnProfile(
                column="employee_id", n_rows=100, null_rate=0.0, distinct_count=100,
            ),
        )
        assert resolve_cardinality_tier(col) == "identifier"

    def test_low_for_few_distinct(self):
        from ontology_foundry.context.table_bundle import ColumnContext
        from ontology_foundry.analysis.models import NumericColumnProfile
        col = ColumnContext(
            name="status",
            stats=NumericColumnProfile(
                column="status", n_rows=100, null_rate=0.0, distinct_count=3,
            ),
        )
        assert resolve_cardinality_tier(col) == "low"

    def test_high_for_mostly_unique(self):
        from ontology_foundry.context.table_bundle import ColumnContext
        from ontology_foundry.analysis.models import NumericColumnProfile
        col = ColumnContext(
            name="amount",
            stats=NumericColumnProfile(
                column="amount", n_rows=1000, null_rate=0.0, distinct_count=700,
            ),
        )
        assert resolve_cardinality_tier(col) == "high"

    def test_none_when_stats_missing(self):
        from ontology_foundry.context.table_bundle import ColumnContext
        col = ColumnContext(name="anon")  # no stats
        assert resolve_cardinality_tier(col) is None


# ───────────────────────────────────────────────────────────────────────────
# bundle_to_table_facts / bundle_to_top_frequencies
# ───────────────────────────────────────────────────────────────────────────


class TestBundleProjections:
    def test_table_facts_carry_sample_rows(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        facts = bundle_to_table_facts(table_rk="rk", bundle=bundle)
        assert facts.table_rk == "rk"
        assert len(facts.sample_rows) > 0
        assert facts.sample_row_count == len(facts.sample_rows)

    def test_top_frequencies_keyed_by_column_rk(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        rks = _column_rks("rk", ["id", "department", "salary", "is_active"])
        freqs = bundle_to_top_frequencies(bundle=bundle, column_rk_by_name=rks)
        # department has only 3 distinct values → should have top_frequencies
        dept_freqs = freqs["rk/department"]
        assert len(dept_freqs) == 3
        # Each entry has {value, count, share}
        assert all({"value", "count", "share"} <= set(f.keys()) for f in dept_freqs)


# ───────────────────────────────────────────────────────────────────────────
# format_tabular_grounding
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class _MinimalCtx:
    """Stand-in for EnrichmentContext that carries only what the helper reads."""
    tabular_bundle: Any = None


class TestGrounding:
    def test_empty_string_when_no_bundle(self):
        ctx = _MinimalCtx(tabular_bundle=None)
        assert format_tabular_grounding(ctx) == ""

    def test_renders_full_bundle_by_default(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        ctx = _MinimalCtx(tabular_bundle=bundle)
        out = format_tabular_grounding(ctx)
        assert "TABULAR GROUNDING" in out
        # Default mode includes sample rows
        assert "Sample rows" in out

    def test_aggregates_only_strips_samples_and_freqs(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        ctx = _MinimalCtx(tabular_bundle=bundle)
        full = format_tabular_grounding(ctx)
        aggregates = format_tabular_grounding(ctx, aggregates_only=True)
        assert "Sample rows" in full
        assert "Sample rows" not in aggregates
        # Top frequencies for `department` are present in full, removed in agg-only
        assert "Top frequencies" in full
        assert "Top frequencies" not in aggregates
        # Aggregates-only mode advertises itself in the header
        assert "aggregates only" in aggregates
        # Bundle is NOT mutated (the second call still has top frequencies on
        # render, proving model_copy didn't touch the original)
        again = format_tabular_grounding(ctx)
        assert "Sample rows" in again

    def test_max_chars_truncates(self):
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="x", schema="s", table="t", table_id="rk",
        )
        ctx = _MinimalCtx(tabular_bundle=bundle)
        out = format_tabular_grounding(ctx, max_chars=500)
        assert len(out) <= 700  # leeway for header + truncation marker
        assert "truncated" in out


# ───────────────────────────────────────────────────────────────────────────
# Filesystem sink — stats persistence + PII gating
# ───────────────────────────────────────────────────────────────────────────


class TestFilesystemSinkStatsPaths:
    def _make_sink(self, tmp_path: Path):
        from ontology_pipeline.config import OutputConfig
        from ontology_pipeline.output import FilesystemSink
        return FilesystemSink(OutputConfig(kind="filesystem", base_dir=tmp_path))

    def test_aggregates_written_to_disk(self, tmp_path: Path):
        sink = self._make_sink(tmp_path)
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_id="rk",
        )
        rks = _column_rks("rk", ["id", "department", "salary", "is_active"])
        aggs = bundle_to_aggregates(
            table_rk="rk", bundle=bundle, column_rk_by_name=rks,
        )
        sink.write_table_aggregates(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_rk="rk", aggregates=aggs,
        )
        out = tmp_path / "column_stats" / "csod-pg" / "public" / "csod_employee.aggregates.json"
        assert out.exists()
        data = json.loads(out.read_text())
        assert data["table_rk"] == "rk"
        assert len(data["columns"]) == 4
        ids = [c["column_rk"] for c in data["columns"]]
        assert all(rk in ids for rk in rks.values())

    def test_samples_gated_by_pii_safe_set(self, tmp_path: Path):
        sink = self._make_sink(tmp_path)
        profiler = TableProfiler(sample_loader=_stub_loader(_employee_df()))
        bundle = profiler.profile(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_id="rk",
        )
        rks = _column_rks("rk", ["id", "department", "salary", "is_active"])
        facts = bundle_to_table_facts(table_rk="rk", bundle=bundle)
        freqs = bundle_to_top_frequencies(bundle=bundle, column_rk_by_name=rks)

        # Mark `salary` as PII (sensitive) — its top_frequencies should be dropped
        safe = {rk for rk in rks.values() if not rk.endswith("/salary")}

        sink.write_table_samples(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_facts=facts,
            column_top_frequencies=freqs,
            pii_safe_column_rks=safe,
        )
        out = tmp_path / "column_stats" / "csod-pg" / "public" / "csod_employee.samples.json"
        assert out.exists()
        data = json.loads(out.read_text())
        # Salary's frequency block omitted; others present
        assert "rk/salary" not in data["top_frequencies"]
        assert "rk/department" in data["top_frequencies"]
        assert data["pii_safe_columns"] == sorted(safe)

    def test_no_samples_written_when_all_flagged_and_no_rows(self, tmp_path: Path):
        """If every column is flagged AND the bundle has no sample_rows, the
        sink should skip the file entirely — there's literally nothing to write."""
        sink = self._make_sink(tmp_path)
        # Use a synthetic empty-sample bundle
        from ontology_foundry.context.table_bundle import TabularContextBundle
        bundle = TabularContextBundle(table_id="rk")
        facts = bundle_to_table_facts(table_rk="rk", bundle=bundle)
        sink.write_table_samples(
            source_id="csod-pg", schema="public", table="csod_employee",
            table_facts=facts,
            column_top_frequencies={},
            pii_safe_column_rks=set(),
        )
        out = tmp_path / "column_stats" / "csod-pg" / "public" / "csod_employee.samples.json"
        assert not out.exists()
