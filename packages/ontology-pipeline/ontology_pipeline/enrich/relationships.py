"""RelationshipInferenceEnricher — propose FKs the source DDL didn't declare.

For tables that the introspector found zero declared FKs on, this stage asks
the LLM to identify likely foreign-key references based on column names + the
broader schema context (other tables in the same MDL run, when available).

Output lands as `InferredRelationship` records in the result's `side_output`.
The orchestrator routes these into `lineage_edge` rows with:

    edge_kind = 'depends_on'
    evidence_kind = 'inferred_relationship'
    confidence = <LLM-reported>

The MDL column's `properties.references` is also set when the inference is
high-confidence (≥ 0.8 default) so it round-trips through bundle emission.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.enrich.base import EnrichmentContext, EnrichmentResult
from ontology_pipeline.models import GeneratedMDL

logger = logging.getLogger(__name__)

LLM_PROVENANCE = "llm_inferred_relationship"


@dataclass
class InferredRelationship:
    """One proposed FK. Becomes a `lineage_edge` with `evidence_kind='inferred_relationship'`."""
    from_table_rk: str
    from_column: str
    to_table_qualified: str        # 'schema.table' shape; orchestrator resolves to rk
    to_column: str
    confidence: float              # 0..1
    reason: str
    cardinality_hint: str = ""     # 'many_to_one' | 'one_to_one' | 'one_to_many' | ''


class _InferredRelationshipResponse(BaseModel):
    relationships: list["_RelationshipEntry"] = Field(default_factory=list)
    rationale: str = ""


class _RelationshipEntry(BaseModel):
    from_column: str
    to_table: str = Field(description="'schema.table' shape")
    to_column: str
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str = ""
    cardinality_hint: str = ""


_InferredRelationshipResponse.model_rebuild()


class RelationshipInferenceEnricher:
    """Suggest FKs for tables that lack declared ones."""

    name = "relationship_inference"

    def __init__(
        self,
        *,
        role: ModelRole = ModelRole.RELATION_EXTRACTOR,
        min_confidence_to_apply: float = 0.8,
        run_when_any_fk_declared: bool = False,
    ) -> None:
        self._role = role
        self._min_confidence = min_confidence_to_apply
        self._run_when_any_fk_declared = run_when_any_fk_declared

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        if not mdl.models or ctx.provider is None:
            if ctx.provider is None:
                result.warnings.append("no LLM provider; relationship inference skipped")
            return result

        model = mdl.models[0]
        # Detect declared FKs by looking for any column with properties.references set.
        already_declared = sum(
            1 for c in model.columns if c.properties.references
        )
        if already_declared > 0 and not self._run_when_any_fk_declared:
            result.warnings.append(
                f"{already_declared} declared FK(s) present; skipping inference (set run_when_any_fk_declared=True to override)"
            )
            return result

        # Identify likely FK columns: end with '_id' / '_key' / '_fk' and aren't the table's PK.
        candidate_cols = [
            c for c in model.columns
            if _looks_like_fk_column(c.name) and not c.properties.is_primary_key
        ]
        if not candidate_cols:
            result.warnings.append("no FK-shaped column names detected")
            return result

        t0 = time.perf_counter()
        prompt = self._build_prompt(model=model, candidates=candidate_cols, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _InferredRelationshipResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning("RelationshipInferenceEnricher LLM failed for %s: %s", model.rk, exc)
            result.warnings.append(f"llm error: {exc}")
            return result

        inferred: list[InferredRelationship] = []
        applied_to_mdl = 0
        for entry in response.relationships:
            inferred.append(InferredRelationship(
                from_table_rk=model.rk,
                from_column=entry.from_column,
                to_table_qualified=entry.to_table,
                to_column=entry.to_column,
                confidence=float(entry.confidence),
                reason=entry.reason,
                cardinality_hint=entry.cardinality_hint,
            ))
            # Apply only high-confidence inferences to the MDL columns
            if entry.confidence >= self._min_confidence:
                target_col = next(
                    (c for c in model.columns if c.name == entry.from_column),
                    None,
                )
                if target_col is not None and not target_col.properties.references:
                    target_col.properties.references = (
                        f"{entry.to_table}.{entry.to_column}"
                    )
                    # Stamp provenance via extras
                    extras = dict(target_col.properties.model_extra or {})
                    extras["references_provenance"] = LLM_PROVENANCE
                    extras["references_confidence"] = float(entry.confidence)
                    from ontology_pipeline.models import MDLColumnProperties
                    props = target_col.properties.model_dump()
                    props.update(extras)
                    target_col.properties = MDLColumnProperties.model_validate(props)
                    applied_to_mdl += 1

        if inferred:
            # Surface the from-side asset so event narratives + relation
            # induction downstream can reason about asset identity without
            # parsing rk URIs.
            from ontology_pipeline.enrich.asset_surface import (
                build_column_lookup,
                render_asset_one_liner,
                render_asset_surface,
            )
            from_asset_surface = render_asset_surface(model)
            from_one_liner = render_asset_one_liner(model)
            from_column_lookup = build_column_lookup(model)
            result.side_output["inferred_relationships"] = [
                {
                    "from_table_rk": r.from_table_rk,
                    "from_table_name": model.name,
                    "from_table_description": model.description,
                    "from_asset_surface": from_asset_surface,
                    "from_one_liner": from_one_liner,
                    "from_column": r.from_column,
                    # The FK column with full annotation — type + native
                    # description + PII flag if classified. Downstream event
                    # narratives use this to describe WHAT the join key is,
                    # not just its name.
                    "from_column_brief": from_column_lookup.get(
                        r.from_column, {},
                    ).get("brief"),
                    "to_table_qualified": r.to_table_qualified,
                    "to_column": r.to_column,
                    # to_column_brief is filled by the orchestrator when the
                    # to-side MDL is available (it isn't visible to this
                    # per-asset enricher).
                    "to_column_brief": None,
                    "confidence": r.confidence,
                    "reason": r.reason,
                    "cardinality_hint": r.cardinality_hint,
                }
                for r in inferred
            ]
            result.fields_updated.append(
                f"inferred_relationships[{len(inferred)} proposed, {applied_to_mdl} applied]"
            )
        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    @staticmethod
    def _build_prompt(*, model: Any, candidates: list[Any], ctx: EnrichmentContext) -> str:
        candidates_block = "\n".join(
            f"  - {c.name} ({c.type}) — "
            + (c.properties.description or "(no description)")
            for c in candidates
        )
        all_cols_block = "\n".join(f"  - {c.name} ({c.type})" for c in model.columns)
        return f"""You infer database foreign-key relationships from column naming conventions. Output JSON only.

CONTEXT TABLE:
  rk:     {model.rk}
  schema: {ctx.schema_name}
  source: {ctx.source_id}
  description: {model.description or "(none)"}

ALL COLUMNS IN THIS TABLE:
{all_cols_block}

CANDIDATE FK COLUMNS (names suggest references):
{candidates_block}

For each candidate, infer:
  - to_table: the likely referenced table in 'schema.table' shape
              (use the SAME schema as the source table unless the column name implies cross-schema)
  - to_column: the referenced column (typically '<entity>_id' or 'id')
  - confidence: 0.0–1.0 (be conservative — 0.5 is "plausible", 0.8+ is "strong signal")
  - cardinality_hint: 'many_to_one' | 'one_to_one' | 'one_to_many' | ''
  - reason: one-sentence justification.

Output JSON:
{{
  "relationships": [
    {{ "from_column": "<col>", "to_table": "<schema.table>", "to_column": "<col>",
       "confidence": 0.8, "cardinality_hint": "many_to_one", "reason": "..." }}
  ],
  "rationale": "one-paragraph overall reasoning"
}}

Rules:
- Only include columns from CANDIDATE FK COLUMNS list.
- Use existing naming conventions in the broader schema (e.g., `customer_id` → `customer.customer_id`).
- Skip columns where you cannot infer a likely target with confidence ≥ 0.4.
- Empty `relationships` is valid when nothing is inferable.
"""


def _looks_like_fk_column(name: str) -> bool:
    n = name.lower()
    if n in {"id", "pk", "sk", "uuid", "guid"}:
        return False
    return n.endswith("_id") or n.endswith("_key") or n.endswith("_fk") or n.endswith("_ref")
