"""CrossAssetCausalEnricher — multi-asset causal hypothesis generation.

Runs AFTER the per-asset enrichment pass completes. Different shape from the
per-asset stages: takes a LIST of MDLs and a `ClusterContext`, returns a list
of `EnrichmentResult`s (one per cluster examined).

How clustering works (v1):
  - Group assets by shared `concepts[]` (any-overlap).
  - Within each concept-group, sub-cluster by shared `key_areas[]`.
  - Drop clusters of size 1 (no cross-asset pair to compare).
  - Cap cluster size at `max_cluster_size` (default 5). Larger clusters get
    sliced into the first-N most "specific" assets per cluster.

Per cluster, the LLM is shown all assets in the cluster + their columns +
existing bindings + the tenant causal_node vocab, and asked to propose causal
candidates LINKING assets in the cluster. Outputs go to `side_output` and get
routed through the existing sink path as `causal_candidates` rows.

What this stage explicitly does NOT do:
  - Per-pair O(N²) enumeration (too expensive at 100+ tables).
  - Validation of the candidates (statistical refutation is a separate worker).
  - Cross-source clustering (v1 limits to within-source clusters to keep the
    LLM context manageable; cross-source is a roadmap item).
"""
from __future__ import annotations

import logging
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from ontology_foundry.llm.provider import ModelProvider, ModelRole
from ontology_foundry.llm.transform import llm_structured_transform

from ontology_pipeline.enrich.base import EnrichmentContext, EnrichmentResult
from ontology_pipeline.enrich.causal import CAUSAL_PREDICATES
from ontology_pipeline.models import GeneratedMDL

logger = logging.getLogger(__name__)

LLM_PROVENANCE = "llm_cross_asset_causal"


# ───────────────────────────────────────────────────────────────────────────
# LLM response schemas
# ───────────────────────────────────────────────────────────────────────────

class _CrossAssetCandidate(BaseModel):
    """One proposed cross-asset causal edge.

    The LLM is asked to output `subject_asset_name` (the short table name,
    e.g. `"users_core"`) rather than the full asset_rk. Names are easier for
    models to copy-paste correctly and produce cleaner prompts. The
    enricher's post-processing step resolves name → rk against the cluster
    membership.
    """
    subject_asset_name: str = Field(
        description="Short name of the subject (cause) asset, e.g. 'users_core'.",
    )
    subject_column: str = Field(
        default="",
        description="Column in subject asset that anchors the cause. Empty when the asset is the cause as a whole.",
    )
    predicate: str = Field(description=f"One of: {', '.join(CAUSAL_PREDICATES)}")
    object_asset_name: str | None = Field(
        default=None,
        description="Short name of the object (effect) asset when the effect is another asset. None when object_causal_node_id is used.",
    )
    object_column: str = Field(
        default="",
        description="Column in object asset that anchors the effect. Empty when the asset is the effect as a whole.",
    )
    object_causal_node_id: str | None = Field(
        default=None,
        description="If the effect is a causal_node card rather than another asset, use this instead of object_asset_name.",
    )
    evidence_subject_columns: list[str] = Field(default_factory=list)
    evidence_object_columns: list[str] = Field(default_factory=list)
    mechanism_hint: str = Field(default="")
    confidence: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class _CrossAssetResponse(BaseModel):
    candidates: list[_CrossAssetCandidate] = Field(default_factory=list)
    rationale: str = ""


# ───────────────────────────────────────────────────────────────────────────
# Cluster + context types
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class AssetCluster:
    """A group of MDLs that share at least one concept or key_area."""
    cluster_key: str
    members: list[GeneratedMDL]
    shared_concepts: list[str] = field(default_factory=list)
    shared_key_areas: list[str] = field(default_factory=list)

    def size(self) -> int:
        return len(self.members)

    @property
    def primary_rks(self) -> list[str]:
        return [m.models[0].rk for m in self.members if m.models]


@dataclass
class ClusterContext:
    """Context for a cluster-level LLM call. Distinct from per-asset EnrichmentContext."""
    source_id: str
    provider: ModelProvider | None
    llm_model_id: str | None = None
    known_causal_node_ids: list[str] = field(default_factory=list)
    known_causal_node_excerpts: dict[str, str] = field(default_factory=dict)


# ───────────────────────────────────────────────────────────────────────────
# Public stage
# ───────────────────────────────────────────────────────────────────────────

class CrossAssetCausalEnricher:
    """LLM-driven cross-asset causal hypothesis generation."""

    name = "cross_asset_causal"

    def __init__(
        self,
        *,
        role: ModelRole = ModelRole.RELATION_EXTRACTOR,
        max_cluster_size: int = 5,
        min_cluster_size: int = 2,
        max_candidates_per_cluster: int = 10,
        min_confidence_to_emit: float = 0.4,
        cluster_by: tuple[str, ...] = ("concepts", "key_areas"),
    ) -> None:
        """
        Args:
            max_cluster_size: cap on assets per LLM call. Bigger clusters get sliced.
            min_cluster_size: ignore singleton clusters (no pair to compare).
            max_candidates_per_cluster: cap on candidates per cluster (prevents runaway output).
            min_confidence_to_emit: drop candidates below this threshold before emission.
            cluster_by: which fields to cluster on. Tries each in order; first match wins.
        """
        self._role = role
        self.max_cluster_size = max_cluster_size
        self.min_cluster_size = min_cluster_size
        self.max_candidates_per_cluster = max_candidates_per_cluster
        self.min_confidence = min_confidence_to_emit
        self.cluster_by = tuple(cluster_by)

    # ── Public entry points ────────────────────────────────────────────

    def cluster_assets(self, mdls: list[GeneratedMDL]) -> list[AssetCluster]:
        """Group MDLs into clusters whose members share at least one concept or key_area.

        Strategy:
          1. For each (mdl, concept) pair, bucket the mdl under that concept.
          2. For each (mdl, key_area) pair, bucket the mdl under that key_area.
          3. A cluster is the set of mdls sharing one bucket.
          4. Duplicate-suppression: if cluster X is a subset of cluster Y, drop X.
          5. Cap cluster size at max_cluster_size.

        Returns clusters with size >= min_cluster_size, with stable ordering.
        """
        buckets: dict[tuple[str, str], list[GeneratedMDL]] = defaultdict(list)
        for mdl in mdls:
            if not mdl.models:
                continue
            m = mdl.models[0]
            if "concepts" in self.cluster_by:
                for c in (m.concepts or []):
                    buckets[("concept", c)].append(mdl)
            if "key_areas" in self.cluster_by:
                for k in (m.key_areas or []):
                    buckets[("key_area", k)].append(mdl)

        # Build candidate clusters
        seen_rk_sets: set[frozenset[str]] = set()
        clusters: list[AssetCluster] = []
        for (kind, key), members in sorted(buckets.items()):
            if len(members) < self.min_cluster_size:
                continue
            # Cap size — keep the most "specific" (most concepts) first as a heuristic
            members_sorted = sorted(
                members,
                key=lambda x: -(len(x.models[0].concepts or []) + len(x.models[0].key_areas or [])),
            )[: self.max_cluster_size]
            rk_set = frozenset(m.models[0].rk for m in members_sorted)
            if rk_set in seen_rk_sets:
                continue
            seen_rk_sets.add(rk_set)
            cluster = AssetCluster(
                cluster_key=f"{kind}={key}",
                members=members_sorted,
                shared_concepts=[key] if kind == "concept" else [],
                shared_key_areas=[key] if kind == "key_area" else [],
            )
            clusters.append(cluster)
        return clusters

    def apply_all(
        self,
        mdls: list[GeneratedMDL],
        ctx: ClusterContext,
    ) -> list[EnrichmentResult]:
        """Cluster + apply across the full asset list. Returns one result per cluster examined."""
        results: list[EnrichmentResult] = []
        clusters = self.cluster_assets(mdls)
        if not clusters:
            return results
        if ctx.provider is None:
            logger.info("CrossAssetCausalEnricher skipped — no LLM provider")
            return results

        for cluster in clusters:
            res = self._apply_cluster(cluster, ctx)
            results.append(res)
        return results

    # ── Internals ──────────────────────────────────────────────────────

    def _apply_cluster(self, cluster: AssetCluster, ctx: ClusterContext) -> EnrichmentResult:
        result = EnrichmentResult(stage_name=self.name)
        t0 = time.perf_counter()
        prompt = self._build_cluster_prompt(cluster=cluster, ctx=ctx)
        try:
            response = llm_structured_transform(
                ctx.provider, self._role, prompt, _CrossAssetResponse,
            )
            result.llm_calls += 1
        except Exception as exc:
            logger.warning(
                "CrossAssetCausalEnricher LLM failed for cluster %r: %s",
                cluster.cluster_key, exc,
            )
            result.warnings.append(f"llm error for cluster {cluster.cluster_key}: {exc}")
            return result

        # Filter + normalize candidates
        # Build name→model and name→rk lookups so we can resolve the LLM's
        # `subject_asset_name` / `object_asset_name` back to the full rk
        # used for storage. Names within a single cluster are unique by
        # construction (one MDL per table); cross-cluster collisions don't
        # matter — each cluster runs in isolation.
        from ontology_pipeline.enrich.asset_surface import (
            build_column_lookup,
            render_asset_one_liner,
            render_asset_surface,
        )
        name_to_model = {m.models[0].name: m.models[0] for m in cluster.members}
        # Per-asset column lookup cache. Built once per cluster member, reused
        # across all candidates that reference that member.
        column_lookups: dict[str, dict[str, Any]] = {
            m.models[0].rk: build_column_lookup(m.models[0]) for m in cluster.members
        }
        cluster_rks = set(m.models[0].rk for m in cluster.members)
        valid_node_ids = set(ctx.known_causal_node_ids)
        normalized: list[dict[str, Any]] = []
        for c in response.candidates[: self.max_candidates_per_cluster]:
            # Predicate vocab check
            if c.predicate not in CAUSAL_PREDICATES:
                continue
            # Confidence threshold
            if c.confidence < self.min_confidence:
                continue
            # Subject name must resolve to a cluster member
            subject_model = name_to_model.get(c.subject_asset_name)
            if subject_model is None:
                continue
            subject_rk = subject_model.rk
            subject_ref = subject_rk + (f".{c.subject_column}" if c.subject_column else "")
            # Object resolution: either asset-in-cluster (by name) or known causal_node
            object_asset_rk: str | None = None
            object_model = None
            if c.object_asset_name:
                object_model = name_to_model.get(c.object_asset_name)
                if object_model is None:
                    continue
                object_asset_rk = object_model.rk
                object_ref = object_asset_rk + (
                    f".{c.object_column}" if c.object_column else ""
                )
            elif c.object_causal_node_id:
                if valid_node_ids and c.object_causal_node_id not in valid_node_ids:
                    continue
                object_ref = c.object_causal_node_id
            else:
                continue

            subject_col_lookup = column_lookups.get(subject_rk, {})
            object_col_lookup = (
                column_lookups.get(object_asset_rk, {}) if object_asset_rk else {}
            )
            normalized.append({
                "asset_rk": subject_rk,
                "asset_name": subject_model.name,
                "asset_description": subject_model.description,
                "subject_ref": subject_ref,
                "subject_asset_surface": render_asset_surface(subject_model),
                "subject_one_liner": render_asset_one_liner(subject_model),
                "subject_column_brief": (
                    subject_col_lookup.get(c.subject_column, {}).get("brief")
                    if c.subject_column else None
                ),
                "predicate": c.predicate,
                "object_ref": object_ref,
                "object_asset_surface": (
                    render_asset_surface(object_model) if object_model
                    else c.object_causal_node_id or ""
                ),
                "object_one_liner": (
                    render_asset_one_liner(object_model) if object_model
                    else c.object_causal_node_id or ""
                ),
                "object_column_brief": (
                    object_col_lookup.get(c.object_column, {}).get("brief")
                    if c.object_column and object_col_lookup else None
                ),
                "evidence_columns": list(c.evidence_subject_columns),
                # Per-side column lookups: every entry carries type +
                # description + PII/semantic flags so event narratives can
                # render each evidence column fully without re-walking MDL.
                "subject_column_lookup": subject_col_lookup,
                "object_column_lookup": object_col_lookup,
                "mechanism_hint": c.mechanism_hint,
                "confidence": float(c.confidence),
                "status": "proposed",
                "provenance": LLM_PROVENANCE,
                "rationale": c.rationale,
                "cluster_key": cluster.cluster_key,
                "object_asset_rk": object_asset_rk,  # extra context for downstream routing
                "evidence_object_columns": list(c.evidence_object_columns),
            })

        if normalized:
            result.side_output["causal_candidates"] = normalized
            result.fields_updated.append(
                f"cross_asset_causal_candidates[cluster={cluster.cluster_key};"
                f" {len(normalized)} proposed]"
            )
        result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        return result

    def _build_cluster_prompt(self, *, cluster: AssetCluster, ctx: ClusterContext) -> str:
        # Render each member with the canonical asset surface. The surface
        # includes name, description, concepts, key_areas, and every column
        # with its native description / PII flag / semantic_unit.
        # The model receives BOTH name and rk so it can reason on names
        # (short, human-readable) and we keep rks visible as the canonical
        # identifier — but only `subject_asset_name` / `object_asset_name`
        # need to appear in the model's output (we resolve to rks after).
        from ontology_pipeline.enrich.asset_surface import render_asset_surface
        member_blocks: list[str] = []
        asset_names: list[str] = []
        for m in cluster.members:
            model = m.models[0]
            asset_names.append(model.name)
            member_blocks.append(render_asset_surface(model))
        members_text = "\n\n".join(member_blocks)

        names_csv = ", ".join(sorted(set(asset_names)))
        vocab_block = (
            "\n".join(
                f"  - {cid}" + (f" — {ctx.known_causal_node_excerpts[cid]}"
                               if cid in ctx.known_causal_node_excerpts else "")
                for cid in ctx.known_causal_node_ids
            )
            if ctx.known_causal_node_ids
            else "  (no causal_node vocab; object_asset_name must reference another asset in this cluster)"
        )

        return f"""You are a causal-inference assistant analyzing a CLUSTER of related data assets for cross-asset causal dependencies. Output JSON only.

CLUSTER: {cluster.cluster_key} (size={len(cluster.members)})
SHARED CONCEPTS: {', '.join(cluster.shared_concepts) or '(none)'}
SHARED KEY_AREAS: {', '.join(cluster.shared_key_areas) or '(none)'}

ASSETS IN CLUSTER (each block shows the asset's full surface — name, rk,
description, concepts, key_areas, columns with native descriptions):

{members_text}

VALID ASSET NAMES YOU MAY REFERENCE (use these short names — NOT the full rk —
in `subject_asset_name` and `object_asset_name` below):
  {names_csv}

CANDIDATE causal_node CARDS (vocab):
{vocab_block}

CAUSAL PREDICATES (controlled vocab):
  {", ".join(CAUSAL_PREDICATES)}

TASK:
Propose causal candidates that LINK two assets in this cluster, OR link one
asset in the cluster to a causal_node from the vocab. Each candidate must:
  - Reference a `subject_asset_name` that's in the list above.
  - Either `object_asset_name` (another in-cluster asset) OR
    `object_causal_node_id` (from vocab) — never both.
  - Use a predicate from the controlled vocab.
  - Anchor evidence to specific columns (subject + optionally object).
  - Carry a confidence (0..1) and a one-sentence mechanism_hint.

Output JSON STRICTLY (use ASSET NAMES, not rks):
{{
  "candidates": [
    {{
      "subject_asset_name": "<one of the names listed above>",
      "subject_column": "<col in subject asset, optional>",
      "predicate": "<from vocab>",
      "object_asset_name": "<another name listed above>" | null,
      "object_column": "<col in object asset, optional>",
      "object_causal_node_id": "<causal_node id>" | null,
      "evidence_subject_columns": ["<col>", ...],
      "evidence_object_columns": ["<col>", ...],
      "mechanism_hint": "...",
      "confidence": 0.0..1.0,
      "rationale": "..."
    }}
  ],
  "rationale": "one-paragraph overall reasoning"
}}

Guardrails:
- Be conservative. Empty candidates list is valid + preferred when the cluster
  is just dimensional / lookup data.
- Confidence calibration: 0.5 = "plausible, would need data to verify";
                         0.8 = "schema strongly suggests this";
                         0.95 = "structurally explicit (timestamps + status transitions)".
- Do NOT propose self-loops (subject_asset_name == object_asset_name).
- Do NOT invent column names — use ONLY columns listed for the relevant asset.
- For each candidate, EITHER object_asset_rk OR object_causal_node_id — never both.
"""
