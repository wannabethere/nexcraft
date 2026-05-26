"""DataProtectionEnricher — PII / sensitivity classification + RLS/CLS hints.

Per `mdl_table_concept_annotation_spec.md` and `T0_T1_organization_source_spec.md`,
sensitivity flows top-down (org → source → catalog → schema → asset → column) with
explicit overrides at each tier. This stage proposes the column-level overrides
that AREN'T inherited from above:

  - `is_pii`              bool
  - `pii_categories`      list[str] from {names, contact, financial, payment, health,
                                          government_id, biometric, location,
                                          employment, behavioral}
  - `sensitivity_class`   one of {public, internal, confidential, restricted}

Plus an optional asset-level RLS/CLS hint block (the legacy `DataProtectionAgent`'s
output, packaged as a structured side_output the orchestrator can persist
separately).

Native COMMENTs + human-authored entries are preserved (no-clobber).
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

LLM_PROVENANCE = "llm_data_protection"

PII_CATEGORIES = (
    "names", "contact", "financial", "payment", "health",
    "government_id", "biometric", "location", "employment", "behavioral",
)
SENSITIVITY_CLASSES = ("public", "internal", "confidential", "restricted")


class _ColumnClassification(BaseModel):
    name: str
    is_pii: bool = False
    pii_categories: list[str] = Field(default_factory=list)
    sensitivity_class: str = Field(default="internal")
    reason: str = ""


class _AssetLevelHints(BaseModel):
    suggested_rls_predicates: list[str] = Field(default_factory=list)
    suggested_cls_columns: list[str] = Field(default_factory=list)
    rationale: str = ""


class _DataProtectionResponse(BaseModel):
    columns: list[_ColumnClassification]
    asset_hints: _AssetLevelHints = Field(default_factory=_AssetLevelHints)


class DataProtectionEnricher:
    """Column-level PII / sensitivity classification + asset-level RLS/CLS suggestions."""

    name = "data_protection"

    def __init__(self, *, role: ModelRole = ModelRole.VALIDATOR) -> None:
        self._role = role

    def apply(self, mdl: GeneratedMDL, ctx: EnrichmentContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        if not mdl.models or ctx.provider is None:
            if ctx.provider is None:
                result.warnings.append("no LLM provider; data_protection skipped")
            return result

        t0 = time.perf_counter()
        model = mdl.models[0]
        # Skip when every column already has is_pii / sensitivity_class set.
        candidates = [
            c for c in model.columns
            if (c.properties.model_extra or {}).get("is_pii") is None
               or (c.properties.model_extra or {}).get("sensitivity_class") is None
        ]
        if not candidates:
            result.warnings.append("all columns already classified")
            return result

        prompt = self._build_prompt(model=model, candidates=candidates, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _DataProtectionResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning("DataProtectionEnricher LLM failed for %s: %s", model.rk, exc)
            result.warnings.append(f"llm error: {exc}")
            return result

        # Post-filter: only known PII categories + sensitivity classes
        by_name = {c.name: c for c in response.columns}
        applied = 0
        from ontology_pipeline.models import MDLColumnProperties
        for col in candidates:
            classification = by_name.get(col.name)
            if classification is None:
                continue
            extras: dict[str, Any] = dict(col.properties.model_extra or {})
            updated = False
            # is_pii — preserve human edits
            if extras.get("is_pii") is None:
                extras["is_pii"] = bool(classification.is_pii)
                updated = True
            # pii_categories — filter to known
            if extras.get("pii_categories") is None and classification.pii_categories:
                cats = [c for c in classification.pii_categories if c in PII_CATEGORIES]
                if cats:
                    extras["pii_categories"] = cats
                    updated = True
            # sensitivity_class — filter to known
            if extras.get("sensitivity_class") is None:
                sclass = (
                    classification.sensitivity_class
                    if classification.sensitivity_class in SENSITIVITY_CLASSES
                    else "internal"
                )
                extras["sensitivity_class"] = sclass
                updated = True
            if classification.reason and not extras.get("data_protection_rationale"):
                extras["data_protection_rationale"] = classification.reason
            if updated:
                extras["data_protection_provenance"] = LLM_PROVENANCE
                props_data = col.properties.model_dump()
                props_data.update(extras)
                col.properties = MDLColumnProperties.model_validate(props_data)
                applied += 1

        if applied:
            result.fields_updated.append(f"columns.data_protection[{applied}]")

        # Asset-level RLS/CLS hints — stash as side_output for downstream routing
        if (
            response.asset_hints.suggested_rls_predicates
            or response.asset_hints.suggested_cls_columns
        ):
            # Surface fields + per-column lookup let the protection event
            # narrative render the asset and each CLS column with full
            # native description + type. Downstream compliance Q&A LLMs see
            # the actual sensitive columns with their semantics, not just
            # opaque names.
            from ontology_pipeline.enrich.asset_surface import (
                build_column_lookup,
                render_asset_one_liner,
                render_asset_surface,
            )
            result.side_output["data_protection_hints"] = {
                "asset_rk": model.rk,
                "asset_name": model.name,
                "asset_description": model.description,
                "asset_one_liner": render_asset_one_liner(model),
                "asset_surface": render_asset_surface(model),
                "column_lookup": build_column_lookup(model),
                "rls_predicates": list(response.asset_hints.suggested_rls_predicates),
                "cls_columns": list(response.asset_hints.suggested_cls_columns),
                "rationale": response.asset_hints.rationale,
                "provenance": LLM_PROVENANCE,
            }
            result.fields_updated.append("asset_hints")

        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    @staticmethod
    def _build_prompt(*, model: Any, candidates: list[Any], ctx: EnrichmentContext) -> str:
        from ontology_pipeline.enrich.grounding import format_tabular_grounding
        cols_block = "\n".join(
            f"  - {c.name} ({c.type}) — "
            + (c.properties.description or "(no description)")
            for c in candidates
        )
        # Aggregates-only grounding: shape facts (null rate, distinct count,
        # numeric ranges) help classify is_pii without exposing raw values
        # to the LLM before the column's sensitivity is known.
        grounding = format_tabular_grounding(ctx, aggregates_only=True)
        return f"""You classify database columns for data protection. Output JSON only.

TABLE: {model.name}  ({ctx.source_id}/{ctx.schema_name})
TABLE DESCRIPTION: {model.description or "(none)"}
{grounding}
COLUMNS TO CLASSIFY:
{cols_block}

For each column produce:
  - is_pii: TRUE if the column carries personal information (about real people).
  - pii_categories: subset of [{", ".join(PII_CATEGORIES)}]. Empty when is_pii=false.
  - sensitivity_class: one of [{", ".join(SENSITIVITY_CLASSES)}].
      public        — safe to share broadly
      internal      — default for ordinary business data
      confidential  — restricted to authorized users
      restricted    — strongly access-controlled (PHI, payment, gov_id)
  - reason: one-sentence justification.

Plus asset-level suggestions:
  - suggested_rls_predicates: example RLS predicates appropriate to this table
      (e.g., "user_id = current_setting('app.user_id')::int").
      Empty array when no RLS makes sense.
  - suggested_cls_columns: list of column names that should be column-level masked
      for low-clearance roles. Empty when none.
  - rationale: one paragraph.

Output JSON STRICTLY:
{{
  "columns": [
    {{ "name": "<col>", "is_pii": false, "pii_categories": [],
       "sensitivity_class": "internal", "reason": "..." }}
  ],
  "asset_hints": {{
    "suggested_rls_predicates": [],
    "suggested_cls_columns": [],
    "rationale": ""
  }}
}}

Rules:
- Include every listed column in `columns`; preserve order.
- Conservative defaults: when uncertain, prefer is_pii=false + sensitivity_class=internal.
- Health/medical columns → pii_categories: [health], sensitivity_class: restricted.
- Government IDs (SSN/passport) → pii_categories: [government_id], restricted.
- Payment card → pii_categories: [payment], restricted.
"""
