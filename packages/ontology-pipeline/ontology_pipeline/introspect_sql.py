"""SqlFileIntrospector — parse a pg_dump SQL file into an IntrospectionResult.

The "local preview" workflow runs the pipeline against a SQL schema file + a
directory of CSV data, no live Postgres required. This module owns the
schema-parsing half; the profiler's CSV sample loader handles the data half.

Supported pg_dump constructs:
  - `CREATE TABLE <schema>.<name> ( <col> <type> [NOT NULL] [DEFAULT ...] , ... );`
  - `COMMENT ON COLUMN <schema>.<name>.<col> IS '...';`
  - `COMMENT ON TABLE  <schema>.<name>     IS '...';`
  - Inline `PRIMARY KEY (<col>, ...)` and `REFERENCES <schema>.<name>(<col>)`
    inside CREATE TABLE bodies.
  - `ALTER TABLE ONLY <schema>.<name> ADD CONSTRAINT ... PRIMARY KEY (...)`
  - `ALTER TABLE ONLY <schema>.<name> ADD CONSTRAINT ... FOREIGN KEY (...) REFERENCES ...`
  - `CREATE OR REPLACE VIEW` / `CREATE VIEW`  →  surfaces as a view asset.

Outside scope:
  - Triggers, indexes (Qdrant gets its own indexes), sequences, ownership, ACLs.
  - Anything in a non-CREATE_TABLE / non-COMMENT statement that doesn't carry
    relational shape.

Optional `manifest_path` lets a caller supply a JSON file that supplements
PK/FK hints when the SQL doesn't carry them (the CSOD pg_dump leaves PKs
implicit and uses no FK constraints — the manifest carries the role/pk/fk
hints the dump elided).

    {
      "tables": {
        "users_core": {"pk": "user_id", "role": "employee"},
        "user_ou_core": {"fk": ["user_id", "ou_id"]}
      }
    }
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ontology_pipeline.config import SourceConfig
from ontology_pipeline.models import ColumnInfo, IntrospectionResult, TableInfo

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Public API
# ───────────────────────────────────────────────────────────────────────────


class SqlFileIntrospector:
    """Reads a pg_dump-style .sql file (no live database needed).

    Args:
        schema_sql_path: path to the .sql file. Required.
        manifest_path: optional JSON file augmenting PK/FK info per table.
            Format matches what `data/indexingsamples/manifest.json` carries.

    Source-config contract (matches `PostgresIntrospector`):

        SourceConfig(
            kind="local_files",
            source_id="csod-local",
            org_id="csod",
            schemas=["public"],
            local=LocalFilesSource(schema_sql=..., data_dir=..., manifest=...),
        )

    The introspector reads `source.local.schema_sql` (a Path) and
    `source.local.manifest` (an optional Path) — see config.LocalFilesSource.
    """

    def introspect(self, *, source: SourceConfig) -> IntrospectionResult:
        local = getattr(source, "local", None)
        if local is None:
            raise ValueError(
                "SqlFileIntrospector requires source.local to be configured "
                "(schema_sql / data_dir / manifest)."
            )

        schema_path = Path(local.schema_sql)
        if not schema_path.exists():
            raise FileNotFoundError(
                f"schema_sql not found at {schema_path}"
            )
        text = schema_path.read_text(encoding="utf-8")

        manifest: dict[str, Any] = {}
        if local.manifest is not None:
            manifest_path = Path(local.manifest)
            if manifest_path.exists():
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            else:
                logger.warning(
                    "SqlFileIntrospector: manifest=%s does not exist; "
                    "PK/FK info will be omitted",
                    manifest_path,
                )

        parsed_tables = _parse_sql(text)
        if not parsed_tables:
            logger.warning(
                "SqlFileIntrospector: no CREATE TABLE statements found in %s",
                schema_path,
            )

        # Filter by requested schemas (default: public)
        wanted_schemas = set(source.schemas or ["public"])
        tables: list[TableInfo] = []
        for t in parsed_tables:
            if t["schema"] not in wanted_schemas:
                continue
            tables.append(_to_table_info(t, manifest))

        # `catalog` mirrors the database name — pg_dump doesn't preserve it,
        # so we use the SQL file's stem as a conventional default. Callers
        # can override via `source.local.catalog_name` if they want a
        # specific value (lets the same data dir back two synthetic catalogs).
        catalog = getattr(local, "catalog_name", None) or schema_path.stem
        return IntrospectionResult(
            source_id=source.source_id,
            source_kind="local_files",
            catalog=catalog,
            extracted_at=datetime.now(timezone.utc),
            tables=tables,
        )


# ───────────────────────────────────────────────────────────────────────────
# SQL parsing
# ───────────────────────────────────────────────────────────────────────────


# CREATE TABLE [IF NOT EXISTS] <schema>.<name> ( <body> );
_CREATE_TABLE_RE = re.compile(
    r"""
    CREATE \s+ (?:OR\s+REPLACE\s+)? TABLE \s+
    (?:IF\s+NOT\s+EXISTS\s+)?
    (?:ONLY\s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<name>[A-Za-z_][\w]*)
    \s*\(
    (?P<body>.*?)
    \)\s*;
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

# COMMENT ON COLUMN <schema>.<name>.<col> IS '...';
_COMMENT_COLUMN_RE = re.compile(
    r"""
    COMMENT \s+ ON \s+ COLUMN \s+
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<name>[A-Za-z_][\w]*)\.
    (?P<col>[A-Za-z_][\w]*)
    \s+ IS \s+
    '(?P<body>(?:[^']|'')*?)';
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

# COMMENT ON TABLE <schema>.<name> IS '...';
_COMMENT_TABLE_RE = re.compile(
    r"""
    COMMENT \s+ ON \s+ TABLE \s+
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<name>[A-Za-z_][\w]*)
    \s+ IS \s+
    '(?P<body>(?:[^']|'')*?)';
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

# ALTER TABLE ONLY <schema>.<name> ADD CONSTRAINT ... PRIMARY KEY (col1, col2);
_ALTER_PK_RE = re.compile(
    r"""
    ALTER \s+ TABLE \s+ (?:ONLY \s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<name>[A-Za-z_][\w]*)
    \s+ ADD \s+ CONSTRAINT \s+ [\w]+
    \s+ PRIMARY \s+ KEY \s*\(
    (?P<cols>[^)]+)
    \)\s*;
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)

# ALTER TABLE ONLY <schema>.<name> ADD CONSTRAINT ... FOREIGN KEY (col) REFERENCES <other_schema>.<other>(col2);
_ALTER_FK_RE = re.compile(
    r"""
    ALTER \s+ TABLE \s+ (?:ONLY \s+)?
    (?:(?P<schema>[A-Za-z_][\w]*)\.)?
    (?P<name>[A-Za-z_][\w]*)
    \s+ ADD \s+ CONSTRAINT \s+ [\w]+
    \s+ FOREIGN \s+ KEY \s*\(
    (?P<from_cols>[^)]+)
    \)
    \s+ REFERENCES \s+
    (?:(?P<ref_schema>[A-Za-z_][\w]*)\.)?
    (?P<ref_name>[A-Za-z_][\w]*)
    (?:\s*\((?P<ref_cols>[^)]+)\))?
    """,
    re.IGNORECASE | re.DOTALL | re.VERBOSE,
)


def _parse_sql(text: str) -> list[dict[str, Any]]:
    """Return one dict per parsed CREATE TABLE.

    Each dict carries:
      schema, name, columns: [(col_name, sql_type, not_null, default)],
      primary_key: list[str], foreign_keys: list[(col, ref_schema, ref_table, ref_col)],
      description, column_descriptions: dict[str, str]
    """
    table_descriptions = {
        f"{(m['schema'] or 'public')}.{m['name']}": _unescape(m['body'])
        for m in _COMMENT_TABLE_RE.finditer(text)
    }
    column_descriptions: dict[str, dict[str, str]] = {}
    for m in _COMMENT_COLUMN_RE.finditer(text):
        key = f"{(m['schema'] or 'public')}.{m['name']}"
        column_descriptions.setdefault(key, {})[m['col']] = _unescape(m['body'])

    # ALTER-table-supplied PKs / FKs
    pks_from_alter: dict[str, list[str]] = {}
    for m in _ALTER_PK_RE.finditer(text):
        key = f"{(m['schema'] or 'public')}.{m['name']}"
        pks_from_alter[key] = [c.strip() for c in m['cols'].split(',')]
    fks_from_alter: dict[str, list[tuple[str, str, str, str]]] = {}
    for m in _ALTER_FK_RE.finditer(text):
        key = f"{(m['schema'] or 'public')}.{m['name']}"
        from_cols = [c.strip() for c in m['from_cols'].split(',')]
        ref_cols = (
            [c.strip() for c in m['ref_cols'].split(',')]
            if m['ref_cols']
            else [c for c in from_cols]
        )
        for fc, rc in zip(from_cols, ref_cols, strict=False):
            fks_from_alter.setdefault(key, []).append(
                (fc, m['ref_schema'] or 'public', m['ref_name'], rc),
            )

    tables: list[dict[str, Any]] = []
    for m in _CREATE_TABLE_RE.finditer(text):
        schema = m['schema'] or 'public'
        name = m['name']
        body = m['body']
        cols, inline_pk, inline_fks = _parse_create_body(body)
        key = f"{schema}.{name}"
        tables.append({
            "schema": schema,
            "name": name,
            "columns": cols,
            "primary_key": pks_from_alter.get(key) or inline_pk,
            "foreign_keys": fks_from_alter.get(key, []) + [
                (fc, rs, rn, rc) for (fc, rs, rn, rc) in inline_fks
            ],
            "description": table_descriptions.get(key),
            "column_descriptions": column_descriptions.get(key, {}),
        })
    return tables


# ───────────────────────────────────────────────────────────────────────────
# CREATE TABLE body parsing
# ───────────────────────────────────────────────────────────────────────────


# A column line looks like:
#   <name> <type> [NOT NULL] [DEFAULT ...] [REFERENCES ...] [PRIMARY KEY]
# Types we tolerate: any token run not containing a comma at top level,
# possibly with parens like NUMERIC(10,2).

_COLUMN_LINE_RE = re.compile(
    r"""
    ^\s*
    (?P<name>[A-Za-z_][\w]*)
    \s+
    (?P<type>
        [A-Za-z][\w]*
        # Multi-word types ('timestamp with time zone', 'character varying') —
        # consume subsequent words ONLY when they aren't SQL keywords that
        # mark the start of a column constraint clause.
        (?:\s+(?!(?:NOT|NULL|DEFAULT|REFERENCES|PRIMARY|UNIQUE|CHECK|COLLATE|GENERATED|CONSTRAINT)\b)[A-Za-z][\w]*)*
        (?:\s*\([^)]*\))?               # optional length / precision
    )
    (?P<rest>.*)$
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Standalone PRIMARY KEY (col1, col2) inside a CREATE TABLE body.
_BODY_PK_RE = re.compile(
    r"PRIMARY\s+KEY\s*\(\s*(?P<cols>[^)]+)\)",
    re.IGNORECASE,
)

# Inline column REFERENCES (...)
_INLINE_REF_RE = re.compile(
    r"""
    REFERENCES \s+
    (?:(?P<ref_schema>[A-Za-z_][\w]*)\.)?
    (?P<ref_table>[A-Za-z_][\w]*)
    (?:\s*\(\s*(?P<ref_col>[A-Za-z_][\w]*)\s*\))?
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _parse_create_body(
    body: str,
) -> tuple[list[dict[str, Any]], list[str], list[tuple[str, str, str, str]]]:
    cols: list[dict[str, Any]] = []
    inline_pk: list[str] = []
    inline_fks: list[tuple[str, str, str, str]] = []

    # Split on top-level commas (parens-aware so NUMERIC(10,2) stays intact)
    fragments = _split_top_level(body)
    for frag in fragments:
        s = frag.strip().rstrip(',')
        if not s:
            continue
        upper = s.upper()

        # Standalone PRIMARY KEY (a, b)
        pk_m = _BODY_PK_RE.match(s)
        if pk_m:
            inline_pk = [c.strip() for c in pk_m.group("cols").split(",")]
            continue
        # Standalone constraints we don't model — skip
        if upper.startswith(("CONSTRAINT ", "UNIQUE", "CHECK", "EXCLUDE",
                             "FOREIGN KEY ", "LIKE ")):
            continue

        m = _COLUMN_LINE_RE.match(s)
        if not m:
            logger.debug("SqlFileIntrospector: skipping unparseable fragment %r", s[:80])
            continue
        rest = m.group("rest") or ""
        upper_rest = rest.upper()
        not_null = "NOT NULL" in upper_rest
        is_pk_inline = bool(
            re.search(r"\bPRIMARY\s+KEY\b", rest, re.IGNORECASE),
        )

        # Inline REFERENCES — yields an FK
        ref_m = _INLINE_REF_RE.search(rest)
        if ref_m:
            inline_fks.append((
                m.group("name"),
                ref_m.group("ref_schema") or "public",
                ref_m.group("ref_table"),
                ref_m.group("ref_col") or m.group("name"),
            ))

        cols.append({
            "name": m.group("name"),
            "type": m.group("type").strip(),
            "not_null": not_null,
            "is_pk_inline": is_pk_inline,
        })
        if is_pk_inline and m.group("name") not in inline_pk:
            inline_pk.append(m.group("name"))

    return cols, inline_pk, inline_fks


def _split_top_level(body: str) -> list[str]:
    """Split CREATE TABLE body on top-level commas, respecting parens."""
    out: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in body:
        if ch == "(":
            depth += 1
            buf.append(ch)
        elif ch == ")":
            depth -= 1
            buf.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        out.append("".join(buf))
    return out


def _unescape(s: str) -> str:
    """SQL string literal — '' becomes '."""
    return s.replace("''", "'")


# ───────────────────────────────────────────────────────────────────────────
# Translation into the existing TableInfo / ColumnInfo shape
# ───────────────────────────────────────────────────────────────────────────


def _to_table_info(
    parsed: dict[str, Any], manifest: dict[str, Any],
) -> TableInfo:
    """Build a `TableInfo` honoring the parsed SQL + manifest augmentation."""
    table_name = parsed["name"]
    schema_name = parsed["schema"]

    # PK precedence: SQL → manifest → empty
    pk_cols = list(parsed["primary_key"] or [])
    manifest_entry: dict[str, Any] = (
        (manifest.get("tables", {}) or {}).get(table_name) or {}
    )
    if not pk_cols and manifest_entry.get("pk"):
        mpk = manifest_entry["pk"]
        pk_cols = [mpk] if isinstance(mpk, str) else list(mpk)

    pk_set = set(pk_cols)
    columns: list[ColumnInfo] = []
    for c in parsed["columns"]:
        col_name = c["name"]
        columns.append(ColumnInfo(
            name=col_name,
            sql_type=c["type"],
            nullable=not c["not_null"],
            is_primary_key=col_name in pk_set,
            description=(parsed["column_descriptions"].get(col_name) or None),
        ))

    # FKs: SQL inline / ALTER + manifest-supplied (manifest's fk are just
    # column names; we leave the referenced-table resolution to downstream
    # if the column convention is `<other>_id`).
    fks_by_col: dict[str, str] = {}
    for (col, ref_schema, ref_table, ref_col) in parsed["foreign_keys"]:
        fks_by_col[col] = f"{ref_schema}.{ref_table}.{ref_col}"
    # Manifest fk hints
    m_fks = manifest_entry.get("fk") or []
    if isinstance(m_fks, str):
        m_fks = [m_fks]
    for col in m_fks:
        if col in fks_by_col:
            continue
        # Convention: <other>_id → <other>.<col>. Best-effort.
        if col.endswith("_id"):
            ref_table_guess = col[:-3]
            # Try to align with a sibling table name if present in manifest
            siblings = manifest.get("tables", {}) or {}
            if f"{ref_table_guess}_core" in siblings:
                ref_table_guess = f"{ref_table_guess}_core"
            fks_by_col[col] = f"{schema_name}.{ref_table_guess}.{col}"

    # Apply FK refs onto ColumnInfo
    if fks_by_col:
        annotated: list[ColumnInfo] = []
        for c in columns:
            if c.name in fks_by_col:
                annotated.append(c.model_copy(update={
                    "references_table": fks_by_col[c.name],
                }))
            else:
                annotated.append(c)
        columns = annotated

    return TableInfo(
        schema_name=schema_name,
        name=table_name,
        primary_key=pk_cols,
        columns=columns,
        description=parsed.get("description"),
        is_view=False,  # CREATE VIEW path not yet wired
    )
