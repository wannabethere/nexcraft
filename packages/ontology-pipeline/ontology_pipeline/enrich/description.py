"""RichDescriptionEnricher — table-level + column-level documentation.

Strict superset of `mdl.fill_descriptions`. Where the existing gap-fill only
generates `description` strings, this stage produces the richer documentation
the legacy `LLMSchemaDocumentationGenerator` did:

  Table-level (lands in MDL model.properties.documentation):
    - business_purpose
    - primary_use_cases
    - key_relationships
    - update_frequency
    - data_retention
    - access_patterns
    - performance_considerations

  Column-level:
    - description (if missing — native COMMENTs are never overwritten)

Native COMMENT-On-TABLE and COMMENT-ON-COLUMN values continue to win — this
stage adds the *enrichment* fields on top.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.enrich.base import EnrichmentContext, EnrichmentResult
from ontology_pipeline.models import GeneratedMDL, MDLColumn

logger = logging.getLogger(__name__)

LLM_DOC_PROVENANCE = "llm_rich_documentation"


class _ColumnDescriptionFill(BaseModel):
    name: str
    description: str


class _TableDocumentationResponse(BaseModel):
    table_description: str = Field(..., description="One-to-three-sentence prose description.")
    business_purpose: str = Field(default="", description="Why this table exists in business terms.")
    primary_use_cases: list[str] = Field(default_factory=list)
    key_relationships: list[str] = Field(
        default_factory=list,
        description="Natural-language descriptions of relationships (e.g., 'one Employee has many TrainingAssignments').",
    )
    update_frequency: str = Field(default="", description="real-time|hourly|daily|weekly|monthly|unknown")
    data_retention: str = Field(default="", description="Free-form retention (e.g., '7 years' or 'until-removed').")
    access_patterns: list[str] = Field(default_factory=list)
    performance_considerations: list[str] = Field(default_factory=list)
    columns: list[_ColumnDescriptionFill] = Field(
        default_factory=list,
        description="Descriptions ONLY for columns that lack one.",
    )


class RichDescriptionEnricher:
    """Generate table-level documentation + gap-fill column descriptions."""

    name = "rich_description"

    def __init__(
        self,
        *,
        role: ModelRole = ModelRole.SUMMARIZER,
        overwrite_native_table_description: bool = False,
    ) -> None:
        self._role = role
        self._overwrite_native_table_description = overwrite_native_table_description

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        if not mdl.models:
            return result
        if ctx.provider is None:
            result.warnings.append("no LLM provider configured; rich description skipped")
            return result

        t0 = time.perf_counter()
        model = mdl.models[0]
        columns_missing = [c for c in model.columns if not c.properties.description]
        table_has_native = bool(model.description) and (
            (model.description_provenance or "").startswith("extractor:")
        )
        # If we already have a strong table description and no column gaps, skip.
        if (
            table_has_native
            and not columns_missing
            and not self._overwrite_native_table_description
        ):
            result.warnings.append("no gaps to fill (native description present)")
            return result

        prompt = self._build_prompt(model=model, columns_missing=columns_missing, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _TableDocumentationResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning("RichDescriptionEnricher LLM call failed for %s: %s", model.rk, exc)
            result.warnings.append(f"llm error: {exc}")
            return result

        # Apply table-level fields
        if not table_has_native or self._overwrite_native_table_description:
            if response.table_description:
                model.description = response.table_description
                model.description_provenance = LLM_DOC_PROVENANCE
                result.fields_updated.append("description")

        # Stash the rich documentation in model.properties (a new sidecar field)
        # using the `properties` extra-allowed dict at the model level.
        # MDLModel doesn't currently carry top-level `properties`; we attach via
        # a `documentation` field on the model dump for downstream consumers.
        # This is preserved through model_dump as long as the type accepts extras.
        if not hasattr(model, "documentation"):
            # Pydantic v2 with model_config extra='allow' lets us set arbitrary attrs.
            try:
                model.__pydantic_extra__ = (model.__pydantic_extra__ or {})  # type: ignore[attr-defined]
            except AttributeError:
                pass
        doc_block: dict[str, Any] = {
            "business_purpose": response.business_purpose,
            "primary_use_cases": list(response.primary_use_cases),
            "key_relationships": list(response.key_relationships),
            "update_frequency": response.update_frequency,
            "data_retention": response.data_retention,
            "access_patterns": list(response.access_patterns),
            "performance_considerations": list(response.performance_considerations),
            "provenance": LLM_DOC_PROVENANCE,
        }
        # Drop empty
        doc_block = {k: v for k, v in doc_block.items() if v}
        try:
            object.__setattr__(model, "documentation", doc_block)
        except Exception:
            pass
        if doc_block:
            result.fields_updated.append("documentation")

        # Apply per-column descriptions where missing
        by_name = {c.name: c.description for c in response.columns}
        filled = 0
        for col in columns_missing:
            new_desc = by_name.get(col.name)
            if new_desc:
                col.properties.description = new_desc
                col.properties.description_provenance = LLM_DOC_PROVENANCE
                filled += 1
        if filled:
            result.fields_updated.append(f"columns.description[{filled} filled]")

        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _build_prompt(
        self, *, model: Any, columns_missing: list[MDLColumn], ctx: EnrichmentContext,
    ) -> str:
        cols_known = "\n".join(
            f"  - {c.name} ({c.type}) — {c.properties.description}"
            for c in model.columns
            if c.properties.description
        ) or "  (none)"
        cols_missing = "\n".join(
            f"  - {c.name} ({c.type})" for c in columns_missing
        ) or "  (none)"
        existing_desc = model.description if model.description else "(missing — generate concise description)"

        return f"""You are documenting a database table for business analysts. Output JSON only.

TABLE: {model.name}  (kind: {"view" if model.is_view else "table"})
SOURCE: {ctx.source_id}  SCHEMA: {ctx.schema_name}

EXISTING DESCRIPTION:
{existing_desc}

COLUMNS WITH KNOWN DESCRIPTIONS (do NOT replace these):
{cols_known}

COLUMNS NEEDING DESCRIPTIONS (one entry per item):
{cols_missing}

Produce a JSON object matching this schema:
{{
  "table_description": "one to three sentences in business terms (REPEAT VERBATIM if EXISTING DESCRIPTION is non-empty)",
  "business_purpose": "why this table exists in business terms (1-3 sentences)",
  "primary_use_cases": ["use_case_1", "use_case_2", ...],
  "key_relationships": ["natural-language description of relationships", ...],
  "update_frequency": "real-time|hourly|daily|weekly|monthly|unknown",
  "data_retention": "free-form (e.g., '7 years' or 'unknown')",
  "access_patterns": ["pattern_1", ...],
  "performance_considerations": ["consideration_1", ...],
  "columns": [
    {{ "name": "<column_name>", "description": "concise one-sentence business meaning" }}
  ]
}}

Rules:
- If the table already has a description above, repeat it verbatim as `table_description`.
- Only include entries in `columns` for those listed as needing descriptions.
- Use business language; avoid speculation about data this table doesn't contain.
- Empty arrays/strings are valid when you don't have signal.
"""
