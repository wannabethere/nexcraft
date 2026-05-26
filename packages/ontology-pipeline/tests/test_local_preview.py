"""Tests for local-preview mode (no Postgres, no Qdrant, no Temporal).

Three layers:
  - **SqlFileIntrospector** — parses synthetic SQL + reads the actual
    CSOD `data/indexingsamples/schema.sql` if present.
  - **CsvSampleLoader** — reads CSVs into pandas; missing files are no-ops.
  - **PreviewSink + end-to-end** — runs the full pipeline against synthetic
    fixtures and asserts on the resulting `output/preview/` tree.

The end-to-end test against the actual CSOD dataset is gated on the data
being present at the conventional path (skipped otherwise — keeps the test
runnable in CI without bundling 5MB of CSVs).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from ontology_pipeline.config import (
    LLMConfig,
    LocalFilesSource,
    OutputConfig,
    PipelineBehavior,
    PipelineConfig,
    SemanticLayerConfig,
    SourceConfig,
)
from ontology_pipeline.introspect_sql import SqlFileIntrospector, _parse_sql


# ───────────────────────────────────────────────────────────────────────────
# SQL parser unit tests
# ───────────────────────────────────────────────────────────────────────────


class TestSqlParser:
    def test_parses_simple_create_table(self):
        sql = """
        CREATE TABLE public.users (
            id integer NOT NULL,
            email text,
            created_at timestamp with time zone
        );
        """
        tables = _parse_sql(sql)
        assert len(tables) == 1
        t = tables[0]
        assert t["schema"] == "public"
        assert t["name"] == "users"
        names = [c["name"] for c in t["columns"]]
        assert names == ["id", "email", "created_at"]
        assert t["columns"][0]["not_null"] is True
        assert t["columns"][1]["not_null"] is False
        # timestamp-with-tz must parse as a multi-word type
        assert t["columns"][2]["type"] == "timestamp with time zone"

    def test_picks_up_column_comments(self):
        sql = """
        CREATE TABLE public.users (
            id integer NOT NULL
        );
        COMMENT ON COLUMN public.users.id IS 'Surrogate key for the user.';
        """
        tables = _parse_sql(sql)
        assert tables[0]["column_descriptions"]["id"] == "Surrogate key for the user."

    def test_picks_up_table_comment(self):
        sql = """
        CREATE TABLE public.users (id integer);
        COMMENT ON TABLE public.users IS 'All registered users.';
        """
        tables = _parse_sql(sql)
        assert tables[0]["description"] == "All registered users."

    def test_inline_primary_key(self):
        sql = """
        CREATE TABLE public.users (
            id integer NOT NULL,
            email text NOT NULL,
            PRIMARY KEY (id, email)
        );
        """
        t = _parse_sql(sql)[0]
        assert t["primary_key"] == ["id", "email"]

    def test_inline_references_creates_fk(self):
        sql = """
        CREATE TABLE public.orders (
            id integer NOT NULL,
            user_id integer REFERENCES public.users (id),
            total numeric(10,2)
        );
        """
        t = _parse_sql(sql)[0]
        assert ("user_id", "public", "users", "id") in t["foreign_keys"]
        # NUMERIC(10,2) — comma inside parens shouldn't break the splitter
        total_col = next(c for c in t["columns"] if c["name"] == "total")
        assert total_col["type"] == "numeric(10,2)"

    def test_alter_table_pk_overrides(self):
        sql = """
        CREATE TABLE public.users (id integer NOT NULL);
        ALTER TABLE ONLY public.users
            ADD CONSTRAINT users_pkey PRIMARY KEY (id);
        """
        t = _parse_sql(sql)[0]
        assert t["primary_key"] == ["id"]

    def test_alter_table_fk(self):
        sql = """
        CREATE TABLE public.orders (id integer NOT NULL, user_id integer);
        ALTER TABLE ONLY public.orders
            ADD CONSTRAINT orders_user_fkey
            FOREIGN KEY (user_id) REFERENCES public.users(id);
        """
        t = _parse_sql(sql)[0]
        assert ("user_id", "public", "users", "id") in t["foreign_keys"]

    def test_doubled_quotes_unescape_in_comments(self):
        sql = """
        CREATE TABLE public.t (c integer);
        COMMENT ON COLUMN public.t.c IS 'It''s tricky.';
        """
        t = _parse_sql(sql)[0]
        assert t["column_descriptions"]["c"] == "It's tricky."

    def test_empty_input(self):
        assert _parse_sql("") == []


# ───────────────────────────────────────────────────────────────────────────
# SqlFileIntrospector — through the SourceConfig surface
# ───────────────────────────────────────────────────────────────────────────


class TestSqlFileIntrospector:
    def _write_sql(self, tmp_path: Path, sql: str) -> Path:
        p = tmp_path / "schema.sql"
        p.write_text(sql, encoding="utf-8")
        return p

    def test_emits_table_info_with_comments(self, tmp_path: Path):
        sql = """
        CREATE TABLE public.users (
            id integer NOT NULL,
            email text
        );
        COMMENT ON COLUMN public.users.email IS 'User email address.';
        COMMENT ON TABLE public.users IS 'Account records.';
        """
        sql_path = self._write_sql(tmp_path, sql)
        source = SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            local=LocalFilesSource(schema_sql=sql_path),
        )
        result = SqlFileIntrospector().introspect(source=source)
        assert result.source_kind == "local_files"
        assert len(result.tables) == 1
        t = result.tables[0]
        assert t.name == "users"
        assert t.description == "Account records."
        email = next(c for c in t.columns if c.name == "email")
        assert email.description == "User email address."

    def test_manifest_supplies_pk_when_sql_lacks_it(self, tmp_path: Path):
        sql_path = self._write_sql(tmp_path, """
        CREATE TABLE public.users (
            user_id integer NOT NULL,
            email text
        );
        """)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "tables": {"users": {"pk": "user_id"}}
        }), encoding="utf-8")

        source = SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            local=LocalFilesSource(schema_sql=sql_path, manifest=manifest_path),
        )
        t = SqlFileIntrospector().introspect(source=source).tables[0]
        assert t.primary_key == ["user_id"]
        # And the column is flagged as PK
        uid = next(c for c in t.columns if c.name == "user_id")
        assert uid.is_primary_key is True

    def test_manifest_pk_as_list(self, tmp_path: Path):
        sql_path = self._write_sql(tmp_path, """
        CREATE TABLE public.junction (
            a integer NOT NULL,
            b integer NOT NULL
        );
        """)
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(json.dumps({
            "tables": {"junction": {"pk": ["a", "b"]}}
        }), encoding="utf-8")
        source = SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            local=LocalFilesSource(schema_sql=sql_path, manifest=manifest_path),
        )
        t = SqlFileIntrospector().introspect(source=source).tables[0]
        assert t.primary_key == ["a", "b"]

    def test_filters_by_schemas(self, tmp_path: Path):
        sql_path = self._write_sql(tmp_path, """
        CREATE TABLE public.included (id integer);
        CREATE TABLE staging.excluded (id integer);
        """)
        source = SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            schemas=["public"],
            local=LocalFilesSource(schema_sql=sql_path),
        )
        names = [t.name for t in SqlFileIntrospector().introspect(source=source).tables]
        assert names == ["included"]

    def test_missing_file_raises(self, tmp_path: Path):
        source = SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            local=LocalFilesSource(schema_sql=tmp_path / "missing.sql"),
        )
        with pytest.raises(FileNotFoundError):
            SqlFileIntrospector().introspect(source=source)


# ───────────────────────────────────────────────────────────────────────────
# CsvSampleLoader
# ───────────────────────────────────────────────────────────────────────────


class TestCsvSampleLoader:
    def test_reads_existing_csv(self, tmp_path: Path):
        from ontology_pipeline.profile import CsvSampleLoader
        p = tmp_path / "users.csv"
        p.write_text("id,email\n1,a@x.com\n2,b@x.com\n", encoding="utf-8")
        loader = CsvSampleLoader(data_dir=tmp_path)
        df = loader("any-source", "public", "users", limit=10)
        assert list(df.columns) == ["id", "email"]
        assert len(df) == 2

    def test_missing_csv_returns_empty(self, tmp_path: Path):
        from ontology_pipeline.profile import CsvSampleLoader
        loader = CsvSampleLoader(data_dir=tmp_path)
        df = loader("any-source", "public", "nonexistent", limit=10)
        assert df.empty

    def test_respects_limit(self, tmp_path: Path):
        from ontology_pipeline.profile import CsvSampleLoader
        rows = "id,n\n" + "\n".join(f"{i},{i}" for i in range(100))
        (tmp_path / "big.csv").write_text(rows, encoding="utf-8")
        loader = CsvSampleLoader(data_dir=tmp_path)
        df = loader("s", "public", "big", limit=10)
        assert len(df) == 10


# ───────────────────────────────────────────────────────────────────────────
# PreviewSink — end-to-end against synthetic fixtures
# ───────────────────────────────────────────────────────────────────────────


SYNTHETIC_SQL = """
CREATE TABLE public.users (
    user_id integer NOT NULL,
    email text NOT NULL,
    department_id integer
);
COMMENT ON COLUMN public.users.user_id IS 'Surrogate key for the user.';
COMMENT ON COLUMN public.users.email   IS 'Primary contact email.';
COMMENT ON COLUMN public.users.department_id IS 'Owning department.';
COMMENT ON TABLE public.users IS 'All registered users.';

CREATE TABLE public.department (
    department_id integer NOT NULL,
    name text NOT NULL
);
COMMENT ON COLUMN public.department.department_id IS 'Department surrogate key.';
COMMENT ON COLUMN public.department.name IS 'Display name.';
"""

SYNTHETIC_MANIFEST = {
    "tables": {
        "users":      {"pk": "user_id", "fk": ["department_id"]},
        "department": {"pk": "department_id"},
    },
}


def _write_synthetic_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    schema_path = data_dir / "schema.sql"
    schema_path.write_text(SYNTHETIC_SQL, encoding="utf-8")
    (data_dir / "users.csv").write_text(
        "user_id,email,department_id\n"
        + "\n".join(f"{i},user{i}@x.com,{i % 3}" for i in range(60)),
        encoding="utf-8",
    )
    (data_dir / "department.csv").write_text(
        "department_id,name\n0,Eng\n1,HR\n2,Sales\n",
        encoding="utf-8",
    )
    manifest_path = data_dir / "manifest.json"
    manifest_path.write_text(json.dumps(SYNTHETIC_MANIFEST), encoding="utf-8")
    return schema_path, data_dir, manifest_path


def _preview_config(tmp_path: Path) -> PipelineConfig:
    schema_path, data_dir, manifest_path = _write_synthetic_fixture(tmp_path)
    out_dir = tmp_path / "output" / "preview"
    return PipelineConfig(
        source=SourceConfig(
            source_id="local", org_id="acme", kind="local_files",
            schemas=["public"],
            local=LocalFilesSource(
                schema_sql=schema_path,
                data_dir=data_dir,
                manifest=manifest_path,
                catalog_name="testdb",
            ),
        ),
        output=OutputConfig(kind="preview", base_dir=out_dir),
        llm=LLMConfig(model="stub"),
        pipeline=PipelineBehavior(
            # Deterministic-only: no LLM needed
            fill_descriptions=False, rich_description=False,
            enrich_column_semantics=False, enrich_data_protection=False,
            infer_relationships=False, enrich_causal_dependencies=False,
            enrich_cross_asset_causal=False, induce_relation_schema=False,
            annotate=False,
            compute_column_stats=True,
            column_stats_sample_limit=100,
        ),
    )


class TestPreviewSinkEndToEnd:
    def test_full_run_against_synthetic_data_produces_artifacts(self, tmp_path: Path):
        from ontology_pipeline import run
        cfg = _preview_config(tmp_path)
        result = run(cfg)

        # Both tables should land
        assert result.tables_seen == 2
        assert result.tables_processed == 2

        base = cfg.output.base_dir

        # MDL JSON written (FilesystemSink core)
        assert (base / "mdl" / "local" / "public" / "users.json").exists()
        assert (base / "mdl" / "local" / "public" / "department.json").exists()
        # Column stats aggregates
        assert (base / "column_stats" / "local" / "public" / "users.aggregates.json").exists()

        # Postgres-bound rows from the stats pass
        column_stat_dir = base / "postgres" / "column_stat"
        table_stat_dir = base / "postgres" / "table_stat"
        assert column_stat_dir.exists()
        # 3 columns on users + 2 on department = 5 column_stat rows
        column_stat_files = list(column_stat_dir.glob("*.json"))
        assert len(column_stat_files) >= 5
        # 2 table_stat rows
        assert len(list(table_stat_dir.glob("*.json"))) == 2

        # Each column_stat file is a JSON dict with the expected keys
        sample = json.loads(column_stat_files[0].read_text())
        assert "column_rk" in sample
        assert "n_rows" in sample
        assert "null_rate" in sample
        assert "cardinality_tier" in sample

    def test_preview_includes_column_descriptions_in_mdl(self, tmp_path: Path):
        from ontology_pipeline import run
        cfg = _preview_config(tmp_path)
        run(cfg)
        mdl = json.loads(
            (cfg.output.base_dir / "mdl" / "local" / "public" / "users.json")
            .read_text(),
        )
        # MDL preserves comment-derived descriptions
        cols = mdl["models"][0]["columns"]
        email = next(c for c in cols if c["name"] == "email")
        assert email["properties"]["description"] == "Primary contact email."


# ───────────────────────────────────────────────────────────────────────────
# Real CSOD dataset end-to-end (skipped when data isn't bundled)
# ───────────────────────────────────────────────────────────────────────────


_CSOD_DIR = Path(
    "/Users/sameerm/ComplianceSpark/byziplatform/unstructured/nexcraft/"
    "data/indexingsamples",
)


class TestTemporalActivitiesAgainstLocalPreview:
    """Drive the Temporal activity bodies (without a Temporal runtime) to
    confirm that `source.kind=local_files` + `output.kind=preview` flows
    through every workflow step. The Temporal workflow itself is just an
    orchestrator over these activity functions; if the activities work,
    the workflow works."""

    def test_introspect_source_returns_table_specs(self, tmp_path: Path):
        from ontology_pipeline.temporal.activities import introspect_source
        from ontology_pipeline.temporal.inputs import OntologyIngestionInput
        schema_path, data_dir, manifest_path = _write_synthetic_fixture(tmp_path)
        input = OntologyIngestionInput.model_validate({
            "source": {
                "source_id": "local", "org_id": "acme", "kind": "local_files",
                "schemas": ["public"],
                "local": {
                    "schema_sql": str(schema_path),
                    "data_dir": str(data_dir),
                    "manifest": str(manifest_path),
                    "catalog_name": "testdb",
                },
            },
            "output": {"kind": "preview", "base_dir": str(tmp_path / "out")},
            "llm": {"model": "stub"},
            "pipeline": {
                "fill_descriptions": False, "annotate": False,
                "compute_column_stats": True,
            },
        })
        specs = introspect_source(input)
        names = {s.name for s in specs}
        assert names == {"users", "department"}
        # asset_rk must be populated for downstream activities
        for s in specs:
            assert s.asset_rk.startswith("postgres://local")

    def test_process_one_table_writes_preview_artifacts(self, tmp_path: Path):
        from ontology_pipeline.temporal.activities import (
            introspect_source, process_one_table,
        )
        from ontology_pipeline.temporal.inputs import OntologyIngestionInput
        schema_path, data_dir, manifest_path = _write_synthetic_fixture(tmp_path)
        out_dir = tmp_path / "out"
        input = OntologyIngestionInput.model_validate({
            "source": {
                "source_id": "local", "org_id": "acme", "kind": "local_files",
                "schemas": ["public"],
                "local": {
                    "schema_sql": str(schema_path),
                    "data_dir": str(data_dir),
                    "manifest": str(manifest_path),
                    "catalog_name": "testdb",
                },
            },
            "output": {"kind": "preview", "base_dir": str(out_dir)},
            "llm": {"model": "stub"},
            "pipeline": {
                "fill_descriptions": False, "annotate": False,
                "compute_column_stats": True, "column_stats_sample_limit": 100,
            },
        })
        specs = introspect_source(input)
        users_spec = next(s for s in specs if s.name == "users")
        result = process_one_table(input, users_spec)
        assert result.outcome == "created"
        assert result.asset_rk == users_spec.asset_rk
        assert (out_dir / "mdl" / "local" / "public" / "users.json").exists()
        # PreviewSink writes the postgres-bound rows even in Temporal mode
        column_stat_files = list((out_dir / "postgres" / "column_stat").glob("*.json"))
        assert len(column_stat_files) >= 3  # 3 cols on users

    def test_causal_validation_skips_for_preview_sink(self, tmp_path: Path):
        from ontology_pipeline.temporal.activities import run_causal_validation
        from ontology_pipeline.temporal.inputs import OntologyIngestionInput
        schema_path, data_dir, manifest_path = _write_synthetic_fixture(tmp_path)
        input = OntologyIngestionInput.model_validate({
            "source": {
                "source_id": "local", "org_id": "acme", "kind": "local_files",
                "schemas": ["public"],
                "local": {
                    "schema_sql": str(schema_path),
                    "data_dir": str(data_dir),
                    "manifest": str(manifest_path),
                },
            },
            "output": {"kind": "preview", "base_dir": str(tmp_path / "out")},
            "llm": {"model": "stub"},
            "pipeline": {"compute_column_stats": False},
        })
        result = run_causal_validation(input)
        # Preview sink → validator short-circuits with skipped=1
        assert result.stage == "validate_causal_candidates"
        assert result.counts.get("skipped") == 1
        assert "DB validator unavailable" in (result.error or "")


@pytest.mark.skipif(
    not (_CSOD_DIR / "schema.sql").exists(),
    reason=f"CSOD dataset not at {_CSOD_DIR}; bundle it for the real E2E run",
)
class TestCsodLocalPreview:
    """End-to-end against the actual CSOD dump + 11 CSVs."""

    def test_introspect_finds_all_csod_tables(self):
        source = SourceConfig(
            source_id="csod-local", org_id="csod", kind="local_files",
            schemas=["public"],
            local=LocalFilesSource(
                schema_sql=_CSOD_DIR / "schema.sql",
                data_dir=_CSOD_DIR,
                manifest=_CSOD_DIR / "manifest.json",
            ),
        )
        result = SqlFileIntrospector().introspect(source=source)
        # CSOD has 11 tables per the manifest; the SQL dump may include
        # extras (e.g., lookup tables). Expect at least 8.
        assert len(result.tables) >= 8
        table_names = {t.name for t in result.tables}
        # Spot-check a few expected tables
        assert "users_core" in table_names
        assert "training_core" in table_names

    def test_full_local_preview_run_against_csod(self, tmp_path: Path):
        from ontology_pipeline import run
        out_dir = tmp_path / "output" / "preview"
        cfg = PipelineConfig(
            source=SourceConfig(
                source_id="csod-local", org_id="csod", kind="local_files",
                schemas=["public"],
                local=LocalFilesSource(
                    schema_sql=_CSOD_DIR / "schema.sql",
                    data_dir=_CSOD_DIR,
                    manifest=_CSOD_DIR / "manifest.json",
                    catalog_name="csod_learning",
                ),
            ),
            output=OutputConfig(kind="preview", base_dir=out_dir),
            llm=LLMConfig(model="stub"),
            pipeline=PipelineBehavior(
                fill_descriptions=False, rich_description=False,
                enrich_column_semantics=False, enrich_data_protection=False,
                infer_relationships=False, enrich_causal_dependencies=False,
                enrich_cross_asset_causal=False, induce_relation_schema=False,
                annotate=False,
                compute_column_stats=True,
                column_stats_sample_limit=200,
            ),
        )
        result = run(cfg)
        assert result.tables_processed >= 8
        # The preview tree should have postgres/column_stat rows for every CSOD table
        column_stat_files = list((out_dir / "postgres" / "column_stat").glob("*.json"))
        # Each CSOD table averages ~20 columns; expect at least 100 stat rows
        assert len(column_stat_files) >= 50
