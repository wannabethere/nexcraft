"""Source introspection — Postgres.

Reads `information_schema` and `pg_catalog` to assemble an `IntrospectionResult`.
COMMENT ON TABLE / COLUMN values are preserved verbatim — they're the most
valuable seed signal the LLM gap-filler doesn't need to invent.

v1 covers Postgres only. The connector pattern is structured so Snowflake /
Salesforce / ServiceNow can plug in as additional implementations.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Protocol

import psycopg
from psycopg.rows import dict_row

from ontology_pipeline.config import PostgresConnection, SourceConfig
from ontology_pipeline.models import ColumnInfo, IntrospectionResult, TableInfo

logger = logging.getLogger(__name__)


class Introspector(Protocol):
    """Source introspection contract. Implementations: Postgres, ... (future)."""

    def introspect(self, *, source: SourceConfig) -> IntrospectionResult: ...


class PostgresIntrospector:
    """Introspects a Postgres database for tables + columns + comments + FKs."""

    def introspect(self, *, source: SourceConfig) -> IntrospectionResult:
        conn_cfg: PostgresConnection = source.connection  # type: ignore[assignment]
        logger.info(
            "Introspecting Postgres source %s (host=%s db=%s schemas=%s)",
            source.source_id,
            conn_cfg.host,
            conn_cfg.database,
            source.schemas,
        )
        with psycopg.connect(conn_cfg.dsn(), row_factory=dict_row) as conn:
            tables = self._fetch_tables(conn, source.schemas)
            columns_by_table = self._fetch_columns(conn, source.schemas)
            pks_by_table = self._fetch_primary_keys(conn, source.schemas)
            fks_by_table = self._fetch_foreign_keys(conn, source.schemas)
            views_def = self._fetch_view_definitions(conn, source.schemas)

        out: list[TableInfo] = []
        for tbl_row in tables:
            schema_name = tbl_row["table_schema"]
            name = tbl_row["table_name"]
            qn = f"{schema_name}.{name}"
            cols_rows = columns_by_table.get(qn, [])
            pk_cols = set(pks_by_table.get(qn, []))
            fk_map = fks_by_table.get(qn, {})  # {column -> (target_table, target_column)}

            columns = []
            for c in cols_rows:
                ref = fk_map.get(c["column_name"])
                columns.append(
                    ColumnInfo(
                        name=c["column_name"],
                        sql_type=c["data_type_full"],
                        nullable=c["is_nullable"] == "YES",
                        default=c["column_default"],
                        description=c["description"],
                        is_primary_key=c["column_name"] in pk_cols,
                        references_table=ref[0] if ref else None,
                        references_column=ref[1] if ref else None,
                    )
                )

            out.append(
                TableInfo(
                    schema_name=schema_name,
                    name=name,
                    description=tbl_row["description"],
                    columns=columns,
                    primary_key=list(pk_cols),
                    is_view=(tbl_row["table_type"] == "VIEW"),
                    view_definition=views_def.get(qn),
                    row_count_estimate=tbl_row["row_count_estimate"],
                )
            )

        result = IntrospectionResult(
            source_id=source.source_id,
            source_kind="postgres",
            catalog=conn_cfg.database,
            extracted_at=datetime.now(timezone.utc),
            tables=out,
        )
        logger.info(
            "Introspected %d tables/views from %s.%s",
            len(out),
            conn_cfg.database,
            ",".join(source.schemas),
        )
        return result

    # ── private helpers ──────────────────────────────────────────────────

    @staticmethod
    def _fetch_tables(conn: psycopg.Connection, schemas: list[str]) -> list[dict]:
        sql = """
            SELECT
                t.table_schema,
                t.table_name,
                t.table_type,
                pg_catalog.obj_description(c.oid, 'pg_class')           AS description,
                c.reltuples::bigint                                     AS row_count_estimate
            FROM information_schema.tables t
            LEFT JOIN pg_catalog.pg_class c
                   ON c.relname = t.table_name
            LEFT JOIN pg_catalog.pg_namespace n
                   ON n.oid = c.relnamespace AND n.nspname = t.table_schema
            WHERE t.table_schema = ANY(%s)
              AND t.table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY t.table_schema, t.table_name
        """
        with conn.cursor() as cur:
            cur.execute(sql, [schemas])
            return cur.fetchall()

    @staticmethod
    def _fetch_columns(conn: psycopg.Connection, schemas: list[str]) -> dict[str, list[dict]]:
        sql = """
            SELECT
                c.table_schema,
                c.table_name,
                c.column_name,
                c.ordinal_position,
                c.is_nullable,
                c.column_default,
                -- full type incl. length/precision so VARCHAR(220), NUMERIC(10,2) etc survive
                CASE
                    WHEN c.character_maximum_length IS NOT NULL
                        THEN UPPER(c.data_type) || '(' || c.character_maximum_length::text || ')'
                    WHEN c.numeric_precision IS NOT NULL AND c.numeric_scale IS NOT NULL
                        THEN UPPER(c.data_type) || '(' || c.numeric_precision::text
                             || ',' || c.numeric_scale::text || ')'
                    ELSE UPPER(c.data_type)
                END AS data_type_full,
                pg_catalog.col_description(pgc.oid, c.ordinal_position::int) AS description
            FROM information_schema.columns c
            LEFT JOIN pg_catalog.pg_class pgc ON pgc.relname = c.table_name
            LEFT JOIN pg_catalog.pg_namespace n ON n.oid = pgc.relnamespace AND n.nspname = c.table_schema
            WHERE c.table_schema = ANY(%s)
            ORDER BY c.table_schema, c.table_name, c.ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(sql, [schemas])
            rows = cur.fetchall()
        by_table: dict[str, list[dict]] = {}
        for r in rows:
            qn = f"{r['table_schema']}.{r['table_name']}"
            by_table.setdefault(qn, []).append(r)
        return by_table

    @staticmethod
    def _fetch_primary_keys(conn: psycopg.Connection, schemas: list[str]) -> dict[str, list[str]]:
        sql = """
            SELECT
                tc.table_schema,
                tc.table_name,
                kcu.column_name
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                   ON kcu.constraint_name = tc.constraint_name
                  AND kcu.table_schema    = tc.table_schema
            WHERE tc.constraint_type = 'PRIMARY KEY'
              AND tc.table_schema    = ANY(%s)
            ORDER BY tc.table_schema, tc.table_name, kcu.ordinal_position
        """
        with conn.cursor() as cur:
            cur.execute(sql, [schemas])
            rows = cur.fetchall()
        by_table: dict[str, list[str]] = {}
        for r in rows:
            qn = f"{r['table_schema']}.{r['table_name']}"
            by_table.setdefault(qn, []).append(r["column_name"])
        return by_table

    @staticmethod
    def _fetch_foreign_keys(
        conn: psycopg.Connection, schemas: list[str]
    ) -> dict[str, dict[str, tuple[str, str]]]:
        sql = """
            SELECT
                tc.table_schema      AS from_schema,
                tc.table_name        AS from_table,
                kcu.column_name      AS from_column,
                ccu.table_schema     AS to_schema,
                ccu.table_name       AS to_table,
                ccu.column_name      AS to_column
            FROM information_schema.table_constraints tc
            JOIN information_schema.key_column_usage kcu
                   ON kcu.constraint_name = tc.constraint_name
                  AND kcu.table_schema    = tc.table_schema
            JOIN information_schema.constraint_column_usage ccu
                   ON ccu.constraint_name = tc.constraint_name
                  AND ccu.table_schema    = tc.table_schema
            WHERE tc.constraint_type = 'FOREIGN KEY'
              AND tc.table_schema    = ANY(%s)
        """
        with conn.cursor() as cur:
            cur.execute(sql, [schemas])
            rows = cur.fetchall()
        by_table: dict[str, dict[str, tuple[str, str]]] = {}
        for r in rows:
            qn = f"{r['from_schema']}.{r['from_table']}"
            target_qn = f"{r['to_schema']}.{r['to_table']}"
            by_table.setdefault(qn, {})[r["from_column"]] = (target_qn, r["to_column"])
        return by_table

    @staticmethod
    def _fetch_view_definitions(conn: psycopg.Connection, schemas: list[str]) -> dict[str, str]:
        sql = """
            SELECT table_schema, table_name, view_definition
            FROM information_schema.views
            WHERE table_schema = ANY(%s)
        """
        with conn.cursor() as cur:
            cur.execute(sql, [schemas])
            rows = cur.fetchall()
        return {f"{r['table_schema']}.{r['table_name']}": r["view_definition"] for r in rows}


def make_introspector(kind: str) -> Introspector:
    """Factory dispatching on `source.kind`.

    Supported:
      - 'postgres':    `PostgresIntrospector` (live DB via psycopg).
      - 'local_files': `SqlFileIntrospector` (parses a pg_dump .sql file).
    """
    if kind == "postgres":
        return PostgresIntrospector()
    if kind == "local_files":
        # Imported lazily so callers without the local-preview module's
        # imports (notably pandas) can still build a Postgres introspector.
        from ontology_pipeline.introspect_sql import SqlFileIntrospector
        return SqlFileIntrospector()
    raise ValueError(
        f"Unsupported source kind {kind!r}. Supported: 'postgres', 'local_files'."
    )
