"""ColumnSemanticsEnricher — per-column semantic enrichment.

Adds (where missing):
  - semantic_unit         e.g. 'currency_usd', 'percentage', 'count', 'identifier',
                              'datetime', 'enum_status'
  - business_meaning      one-sentence prose (richer than column description)
  - is_business_key       bool — is this a stable lookup key for the entity?

These land on the MDL column's `properties` dict so downstream consumers
(retrieval, semantic-bindings extraction) can pick them up. The no-clobber
rule applies: when properties.description has a `description_provenance` of
`user`, semantic-unit etc. authored by humans are preserved.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.enrich.base import EnrichmentContext, EnrichmentResult
from ontology_pipeline.models import GeneratedMDL

logger = logging.getLogger(__name__)

LLM_PROVENANCE = "llm_column_semantics"


class _ColumnSemantics(BaseModel):
    name: str
    semantic_unit: str = Field(
        default="",
        description="e.g. 'currency_usd' | 'percentage' | 'count' | 'identifier' | 'datetime' | 'enum_status' | 'boolean' | ''",
    )
    business_meaning: str = Field(default="", description="One-sentence business meaning.")
    is_business_key: bool = Field(default=False)


class _SemanticsResponse(BaseModel):
    columns: list[_ColumnSemantics] = Field(default_factory=list)
    rationale: str = ""


class ColumnSemanticsEnricher:
    """LLM-driven column-level semantic enrichment."""

    name = "column_semantics"

    def __init__(self, *, role: ModelRole = ModelRole.SUMMARIZER) -> None:
        self._role = role

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        if not mdl.models or ctx.provider is None:
            if ctx.provider is None:
                result.warnings.append("no LLM provider configured; semantics skipped")
            return result

        t0 = time.perf_counter()
        model = mdl.models[0]
        # Only process columns that don't already have a semantic_unit attached.
        targets = [
            c for c in model.columns
            if (c.properties.model_extra or {}).get("semantic_unit") in (None, "")
        ]
        if not targets:
            result.warnings.append("all columns already have semantic_unit set")
            return result

        prompt = self._build_prompt(model=model, targets=targets, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _SemanticsResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning("ColumnSemanticsEnricher LLM failed for %s: %s", model.rk, exc)
            result.warnings.append(f"llm error: {exc}")
            return result

        by_name = {c.name: c for c in response.columns}
        applied = 0
        for col in targets:
            sem = by_name.get(col.name)
            if sem is None:
                continue
            extras: dict[str, Any] = dict(col.properties.model_extra or {})
            updated = False
            if sem.semantic_unit and not extras.get("semantic_unit"):
                extras["semantic_unit"] = sem.semantic_unit
                updated = True
            if sem.business_meaning and not extras.get("business_meaning"):
                extras["business_meaning"] = sem.business_meaning
                updated = True
            if sem.is_business_key and not extras.get("is_business_key"):
                extras["is_business_key"] = True
                updated = True
            if updated:
                extras["semantics_provenance"] = LLM_PROVENANCE
                # Pydantic v2: re-validate properties with new extras
                from ontology_pipeline.models import MDLColumnProperties
                props_data = col.properties.model_dump()
                props_data.update(extras)
                col.properties = MDLColumnProperties.model_validate(props_data)
                applied += 1

        if applied:
            result.fields_updated.append(f"columns.semantics[{applied} updated]")
        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    @staticmethod
    def _build_prompt(*, model: Any, targets: list[Any], ctx: EnrichmentContext) -> str:
        from ontology_pipeline.enrich.grounding import format_tabular_grounding
        cols_block = "\n".join(
            f"  - {c.name} ({c.type}) — "
            + (c.properties.description or "(no description)")
            for c in targets
        )
        grounding = format_tabular_grounding(ctx, max_sample_rows=8)
        return f"""You annotate database columns with semantic units. Output JSON only.

TABLE: {model.name}  ({ctx.source_id}/{ctx.schema_name})
TABLE DESCRIPTION: {model.description or "(none)"}
{grounding}
COLUMNS TO ANNOTATE:
{cols_block}

For each column produce:
  - semantic_unit: ONE of these tokens (or "" if none apply):
      currency_usd, currency_local, percentage, ratio, count, identifier,
      foreign_key, datetime, date, duration_seconds, duration_minutes,
      enum_status, enum_category, boolean, free_text, geo_country, geo_region,
      url, email, phone_number, address, name_person, name_org
  - business_meaning: one-sentence business meaning (richer than column description)
  - is_business_key: TRUE if this column uniquely identifies a business entity
                     (typically the table's primary entity), else FALSE

Output JSON:
{{
  "columns": [
    {{ "name": "<column_name>", "semantic_unit": "...", "business_meaning": "...", "is_business_key": false }}
  ],
  "rationale": "one-paragraph explanation"
}}

Rules:
- Include one entry per column listed above; preserve order.
- semantic_unit empty string is valid when no unit applies (e.g., free_text already).
- Use TYPE + NAME + DESCRIPTION as your signal. Common conventions:
    *_id        → identifier or foreign_key
    *_at, *_on  → datetime or date
    *_amount, *_cost, *_revenue → currency_*
    *_rate, *_pct → percentage
    is_*, has_*  → boolean
    status, state → enum_status
"""
