"""MDL v2 generation from introspection results.

Two stages:
  1. Deterministic mapping  — `build_mdl(table) -> GeneratedMDL`
     Preserves native COMMENTs verbatim; no LLM.
  2. LLM gap-fill           — `fill_descriptions(mdl, provider) -> GeneratedMDL`
     Generates table description (rare; most sources lack table COMMENTs) and
     column descriptions ONLY where the native COMMENT was absent.

rk convention matches T2–T6 spec §3.1:
    table column rk =  postgres://{source_id}.{catalog}/{schema}/{table}
                       postgres://{source_id}.{catalog}/{schema}/{table}/{column}
"""
from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.models import (
    ColumnInfo,
    GeneratedMDL,
    MDLColumn,
    MDLColumnProperties,
    MDLMaterialization,
    MDLModel,
    MDLViewDefinition,
    TableInfo,
)

logger = logging.getLogger(__name__)

EXTRACTOR_PROVENANCE = "extractor:postgres_information_schema"
LLM_PROVENANCE = "llm_doc_gap_fill"


# ───────────────────────────────────────────────────────────────────────────
# Stage 1 — deterministic mapping
# ───────────────────────────────────────────────────────────────────────────

def asset_rk(source_id: str, catalog: str | None, table: TableInfo) -> str:
    """Build the canonical rk for a tabular asset.

    Uses the convention: `postgres://{source_id}.{catalog}/{schema}/{table}`.
    """
    catalog_part = f".{catalog}" if catalog else ""
    return f"postgres://{source_id}{catalog_part}/{table.schema_name}/{table.name}"


def column_rk(table_rk: str, column_name: str) -> str:
    return f"{table_rk}/{column_name}"


def build_mdl(*, source_id: str, catalog: str | None, table: TableInfo) -> GeneratedMDL:
    """Produce an MDL v2 envelope carrying one table's model entry.

    Deterministic — no LLM. Native COMMENTs are preserved with provenance.
    Missing descriptions are left as None for the LLM gap-filler to handle.
    """
    rk = asset_rk(source_id, catalog, table)

    mdl_columns: list[MDLColumn] = []
    for col in table.columns:
        # references_table from introspect is "schema.table"; append column to get full FK path
        ref: str | None = None
        if col.references_table:
            ref = col.references_table
            if col.references_column:
                ref = f"{ref}.{col.references_column}"
        props = MDLColumnProperties(
            displayName=_humanize(col.name),
            description=col.description,
            description_provenance=EXTRACTOR_PROVENANCE if col.description else None,
            is_primary_key=col.is_primary_key,
            references=ref,
        )
        mdl_columns.append(
            MDLColumn(
                name=col.name,
                type=col.sql_type,
                notNull=not col.nullable,
                rk=column_rk(rk, col.name),
                properties=props,
            )
        )

    materialization = MDLMaterialization(
        kind="view" if table.is_view else "table",
        is_materialized=False,  # detection of materialized views deferred to live mode
    )
    view_def = None
    if table.is_view and table.view_definition:
        view_def = MDLViewDefinition(
            language="sql",
            query=table.view_definition,
            depends_on=[],  # parsing view DDL to extract depends_on deferred
        )

    model = MDLModel(
        name=table.name,
        rk=rk,
        description=table.description,
        description_provenance=EXTRACTOR_PROVENANCE if table.description else None,
        is_view=table.is_view,
        tableReference={"table": table.name},
        materialization=materialization,
        view_definition=view_def,
        columns=mdl_columns,
    )

    return GeneratedMDL(
        mdl_version="2.0",
        source_id=source_id,
        catalog=catalog,
        schema=table.schema_name,
        models=[model],
    )


def _humanize(name: str) -> str:
    """Turn `address_line_1` into `Address Line 1` for displayName.

    Cheap heuristic; the LLM gap-filler can override when description is generated.
    """
    return " ".join(part.capitalize() for part in name.split("_") if part)


# ───────────────────────────────────────────────────────────────────────────
# Stage 2 — LLM gap-fill for missing descriptions
# ───────────────────────────────────────────────────────────────────────────

class _ColumnFill(BaseModel):
    name: str
    description: str


class _TableFillResponse(BaseModel):
    table_description: str = Field(..., description="One- to three-sentence prose description of the table.")
    columns: list[_ColumnFill] = Field(default_factory=list,
                                       description="Descriptions for the columns whose native description was absent.")


def fill_descriptions(
    mdl: GeneratedMDL,
    *,
    provider: ModelProvider | None,
    role: ModelRole = ModelRole.SUMMARIZER,
) -> tuple[GeneratedMDL, int, int, bool]:
    """Fill missing table/column descriptions via the LLM.

    Returns (updated_mdl, native_column_comments_preserved, llm_filled_columns, table_description_generated).
    When `provider` is None, returns the input unchanged with zero counts.
    """
    if provider is None:
        # No LLM configured; deterministic-only mode.
        return mdl, _count_native_descriptions(mdl), 0, False

    if not mdl.models:
        return mdl, 0, 0, False

    model = mdl.models[0]
    columns_missing_desc = [c for c in model.columns if not c.properties.description]
    native_count = sum(1 for c in model.columns if c.properties.description)

    if not columns_missing_desc and model.description:
        # Nothing to fill.
        return mdl, native_count, 0, False

    prompt = _build_fill_prompt(model, columns_missing_desc)
    try:
        resp = llm_structured_transform(provider, role, prompt, _TableFillResponse)
    except Exception as exc:  # nosec: broad exception is fine here; we log and move on
        logger.warning("LLM description fill failed for %s: %s", model.rk, exc)
        return mdl, native_count, 0, False

    table_desc_generated = False
    if not model.description and resp.table_description:
        model.description = resp.table_description
        model.description_provenance = LLM_PROVENANCE
        table_desc_generated = True

    by_name = {c.name: c for c in resp.columns}
    filled = 0
    for col in columns_missing_desc:
        fill = by_name.get(col.name)
        if fill and fill.description:
            col.properties.description = fill.description
            col.properties.description_provenance = LLM_PROVENANCE
            filled += 1

    return mdl, native_count, filled, table_desc_generated


def _count_native_descriptions(mdl: GeneratedMDL) -> int:
    if not mdl.models:
        return 0
    return sum(
        1 for c in mdl.models[0].columns
        if c.properties.description and c.properties.description_provenance == EXTRACTOR_PROVENANCE
    )


def _build_fill_prompt(model: MDLModel, columns_missing: list[MDLColumn]) -> str:
    cols_known = "\n".join(
        f"  - {c.name} ({c.type}) — {c.properties.description}"
        for c in model.columns
        if c.properties.description
    ) or "  (none)"
    cols_missing = "\n".join(f"  - {c.name} ({c.type})" for c in columns_missing) or "  (none)"

    table_has_desc = bool(model.description)

    return f"""You are documenting a database table. Output JSON only.

TABLE: {model.name}  (kind: {"view" if model.is_view else "table"})
{"EXISTING TABLE DESCRIPTION: " + model.description if table_has_desc else "EXISTING TABLE DESCRIPTION: (missing — please generate)"}

COLUMNS WITH KNOWN DESCRIPTIONS (do NOT replace these):
{cols_known}

COLUMNS NEEDING DESCRIPTIONS (provide one per entry):
{cols_missing}

Produce a JSON object matching this schema:
{{
  "table_description": "one to three sentences describing what this table represents in business terms; required",
  "columns": [
    {{ "name": "<column_name>", "description": "concise one-sentence description in business terms" }}
  ]
}}

Rules:
- If the table already has a description above, repeat it verbatim as table_description.
- Only include entries in "columns" for the columns listed as needing descriptions.
- Use the column type and name as your starting points; infer business meaning from common conventions.
- Stay concise; avoid speculation about data this table does not contain.
"""
