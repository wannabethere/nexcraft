"""Smoke tests for ColumnStatDAO against a real Postgres.

Same gating as `test_store.py` — requires ONTOLOGY_STORE_TEST_URL pointing at
a clean test database. Run locally with:

    export ONTOLOGY_STORE_TEST_URL=postgresql+psycopg://...@localhost/ontology_test
    pytest tests/test_column_stats.py

ORM uses Postgres-specific types (JSONB, ARRAY, CHECK constraint), so SQLite
is not a viable substitute.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from ontology_store import Database
from ontology_store.dao.stats import (
    ColumnAggregate,
    ColumnStatDAO,
    TableSampleFacts,
)
from ontology_store.db import (
    ClusterMetadata,
    DatabaseMetadata,
    SchemaMetadata,
    TableMetadata,
)
from ontology_store.db.engine import Base
from ontology_store.db.models import (
    ColumnMetadata,
    Organization,
    Source,
)

pytestmark = pytest.mark.skipif(
    not os.environ.get("ONTOLOGY_STORE_TEST_URL"),
    reason="ONTOLOGY_STORE_TEST_URL must point at a clean Postgres test database",
)


@pytest.fixture(scope="function")
def db() -> Database:
    url = os.environ["ONTOLOGY_STORE_TEST_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Database(engine)


def _seed_spine(db: Database) -> tuple[str, dict[str, str]]:
    """Insert minimal spine rows so FK constraints from column_stat are satisfied.

    Returns (table_rk, {col_name: col_rk}).
    """
    table_rk = "postgres://acme-pg/testdb/public/csod_employee"
    column_names = ["id", "department", "salary"]
    column_rks = {name: f"{table_rk}/{name}" for name in column_names}
    with db.session() as s:
        s.add(Organization(org_id="acme", display_name="Acme"))
        s.add(Source(
            source_id="acme-pg", org_id="acme", kind="postgres",
            instance_name="acme-pg", display_name="acme-pg",
        ))
        s.add(ClusterMetadata(rk="postgres://acme-pg", name="acme-pg"))
        s.add(DatabaseMetadata(rk="postgres://acme-pg/testdb", name="testdb",
                               cluster_rk="postgres://acme-pg"))
        s.add(SchemaMetadata(rk="postgres://acme-pg/testdb/public", name="public",
                             cluster_rk="postgres://acme-pg"))
        s.add(TableMetadata(rk=table_rk, name="csod_employee",
                            schema_rk="postgres://acme-pg/testdb/public",
                            is_view=False))
        for i, name in enumerate(column_names):
            s.add(ColumnMetadata(
                rk=column_rks[name], name=name, table_rk=table_rk,
                col_type="TEXT", sort_order=i, is_nullable=True,
            ))
    return table_rk, column_rks


def _agg(table_rk: str, column_rk: str, **kwargs) -> ColumnAggregate:
    defaults = dict(
        column_rk=column_rk, table_rk=table_rk,
        n_rows=100, null_rate=0.0, distinct_count=10,
        cardinality_tier="low",
    )
    defaults.update(kwargs)
    return ColumnAggregate(**defaults)


class TestUpsertAggregates:
    def test_first_call_inserts_table_stat_and_column_stats(self, db: Database):
        table_rk, rks = _seed_spine(db)
        with db.session() as s:
            dao = ColumnStatDAO(s)
            counts = dao.upsert_aggregates(
                table_rk=table_rk,
                aggregates=[_agg(table_rk, rks[name]) for name in rks],
                population_row_count=1000,
                source_system="postgres:acme-pg",
            )
            assert counts == {"inserted": 3, "updated": 0}
        with db.session() as s:
            dao = ColumnStatDAO(s)
            ts = dao.get_table_stat(table_rk=table_rk)
            assert ts is not None
            assert ts.population_row_count == 1000
            assert ts.samples_persisted is False
            for name in rks:
                cs = dao.get_column_stat(column_rk=rks[name])
                assert cs is not None
                assert cs.samples_persisted is False
                assert cs.cardinality_tier == "low"

    def test_second_call_updates_in_place(self, db: Database):
        table_rk, rks = _seed_spine(db)
        with db.session() as s:
            ColumnStatDAO(s).upsert_aggregates(
                table_rk=table_rk,
                aggregates=[_agg(table_rk, rks["id"], n_rows=50)],
            )
        with db.session() as s:
            counts = ColumnStatDAO(s).upsert_aggregates(
                table_rk=table_rk,
                aggregates=[_agg(table_rk, rks["id"], n_rows=200, distinct_count=200,
                                 cardinality_tier="identifier")],
            )
            assert counts == {"inserted": 0, "updated": 1}
        with db.session() as s:
            cs = ColumnStatDAO(s).get_column_stat(column_rk=rks["id"])
            assert cs.n_rows == 200
            assert cs.cardinality_tier == "identifier"

    def test_invalid_cardinality_tier_rejected(self, db: Database):
        table_rk, rks = _seed_spine(db)
        with db.session() as s:
            with pytest.raises(ValueError, match="Invalid cardinality_tier"):
                ColumnStatDAO(s).upsert_aggregates(
                    table_rk=table_rk,
                    aggregates=[_agg(table_rk, rks["id"], cardinality_tier="bogus")],
                )


class TestAttachSampledValues:
    def test_promotes_only_gated_columns(self, db: Database):
        table_rk, rks = _seed_spine(db)
        with db.session() as s:
            ColumnStatDAO(s).upsert_aggregates(
                table_rk=table_rk,
                aggregates=[_agg(table_rk, rks[n]) for n in rks],
            )
        # Sample rows (caller has already redacted unsafe column keys)
        sample_rows = [{"id": i, "department": "Eng"} for i in range(5)]
        top_freqs = {
            rks["id"]: [{"value": "1", "count": 1, "share": 0.2}],
            rks["department"]: [{"value": "Eng", "count": 5, "share": 1.0}],
            rks["salary"]: [{"value": "50000", "count": 5, "share": 1.0}],
        }
        # salary is flagged PII — gate rejects it
        safe = {rks["id"], rks["department"]}

        with db.session() as s:
            counts = ColumnStatDAO(s).attach_sampled_values(
                table_facts=TableSampleFacts(
                    table_rk=table_rk,
                    sample_rows=sample_rows,
                    sample_row_count=len(sample_rows),
                ),
                column_top_frequencies=top_freqs,
                gate=lambda rk: rk in safe,
            )
            assert counts == {
                "columns_promoted": 2, "columns_blocked": 1, "row_sample_persisted": 1,
            }
        with db.session() as s:
            dao = ColumnStatDAO(s)
            ts = dao.get_table_stat(table_rk=table_rk)
            assert ts.samples_persisted is True
            assert ts.sample_rows == sample_rows
            id_stat = dao.get_column_stat(column_rk=rks["id"])
            dept_stat = dao.get_column_stat(column_rk=rks["department"])
            sal_stat = dao.get_column_stat(column_rk=rks["salary"])
            assert id_stat.samples_persisted is True
            assert dept_stat.samples_persisted is True
            assert sal_stat.samples_persisted is False
            assert sal_stat.top_frequencies == []

    def test_requires_aggregates_first(self, db: Database):
        with db.session() as s:
            with pytest.raises(ValueError, match="table_stat row .* not found"):
                ColumnStatDAO(s).attach_sampled_values(
                    table_facts=TableSampleFacts(table_rk="postgres://nope"),
                    column_top_frequencies={},
                    gate=lambda rk: True,
                )


class TestClearSampledValues:
    def test_clears_table_and_columns(self, db: Database):
        table_rk, rks = _seed_spine(db)
        with db.session() as s:
            ColumnStatDAO(s).upsert_aggregates(
                table_rk=table_rk,
                aggregates=[_agg(table_rk, rks[n]) for n in rks],
            )
            ColumnStatDAO(s).attach_sampled_values(
                table_facts=TableSampleFacts(
                    table_rk=table_rk,
                    sample_rows=[{"id": 1}],
                ),
                column_top_frequencies={rks["id"]: [{"value": "1", "count": 1}]},
                gate=lambda rk: True,
            )
        with db.session() as s:
            cleared = ColumnStatDAO(s).clear_sampled_values(table_rk=table_rk)
            assert cleared >= 2  # table_stat + at least one column
        with db.session() as s:
            ts = ColumnStatDAO(s).get_table_stat(table_rk=table_rk)
            assert ts.samples_persisted is False
            assert ts.sample_rows == []
            cs = ColumnStatDAO(s).get_column_stat(column_rk=rks["id"])
            assert cs.samples_persisted is False
            assert cs.top_frequencies == []
            # Aggregates survive
            assert cs.n_rows == 100
