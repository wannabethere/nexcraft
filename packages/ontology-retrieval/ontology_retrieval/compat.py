"""Legacy compatibility wrappers — stand-in replacements for the genieml-agents
`RetrievalPipeline` / `RetrievalHelper` interfaces.

These wrappers are the swap target for migrating the agents retrieval flows
and the compliance-skill retrieval flows once the ontology store is populated.
They live here (not in those packages) so the migration is import-only on the
caller side:

    # before (in agents / compliance-skill):
    from app.agents.retrieval.retrieval_helper import RetrievalHelper
    from app.agents.pipelines.retrieval_pipeline import RetrievalPipeline

    # after:
    from ontology_retrieval.compat import LegacyRetrievalPipeline as RetrievalPipeline
    # construct using build_legacy_pipeline(...)

Two wrappers are provided:

  - `LegacyRetrievalPipeline.run(retrieval_type, **kwargs)`
        Mirrors the legacy RetrievalPipeline shape. Returns a dict like
        `{"formatted_output": {"documents": [...]}, "metadata": {...}}`.

  - `LegacyRetrievalHelper`
        Mirrors the legacy RetrievalHelper get_* methods that downstream code
        sometimes calls directly. Each method delegates to a pipeline kind.

Both translate `project_id=...` kwargs into a `RetrievalScope` carrying
`legacy_project_id` (plus `org_id` injected at wrapper-construction time).
Other legacy kwargs (`similarity_threshold`, `top_k`, `max_retrieval_size`)
are normalized to the new schemas where possible and dropped otherwise.
"""
from __future__ import annotations

import logging
from typing import Any

from ontology_store.schemas import RetrievalScope

from ontology_retrieval.pipeline import (
    PipelineConfig,
    RetrievalPipeline,
    RetrievalResult,
    build_pipeline_from_config,
    default_config,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Legacy → new arg translation
# ───────────────────────────────────────────────────────────────────────────

_LEGACY_K_ALIASES = ("top_k", "max_retrieval_size", "table_retrieval_size")
_LEGACY_DROP_KWARGS = ("similarity_threshold", "histories", "tables", "table_retrieval")


def _translate_kwargs(
    *,
    org_id: str,
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    """Translate legacy kwargs to the new pipeline's input shape.

    - `project_id`         → `scope.legacy_project_id` (org_id injected from wrapper)
    - `top_k` / `max_retrieval_size` / `table_retrieval_size` → `k`
    - Unsupported legacy kwargs (similarity_threshold, histories, tables, table_retrieval)
      are dropped with a debug log entry. They were used by the legacy stores'
      similarity / ranking config — the new pipeline owns ranking internally.
    """
    out = dict(kwargs)

    # If caller already passed a scope, respect it. Else build from project_id.
    if "scope" not in out:
        scope_extras = {}
        for key in ("concepts", "key_areas", "source_ids", "lifecycle_stages"):
            if key in out:
                scope_extras[key] = out.pop(key)
        project_id = out.pop("project_id", None)
        scope = RetrievalScope(
            org_id=org_id,
            legacy_project_id=project_id,
            **scope_extras,
        )
        out["scope"] = scope.model_dump()
    else:
        # Caller passed an explicit scope; still strip a stray project_id.
        out.pop("project_id", None)

    # Normalize k aliases (first one wins; in legacy code they're mutually exclusive).
    for alias in _LEGACY_K_ALIASES:
        if alias in out:
            if "k" not in out:
                out["k"] = out.pop(alias)
            else:
                out.pop(alias)

    for drop in _LEGACY_DROP_KWARGS:
        if drop in out:
            logger.debug("LegacyPipeline: dropping unsupported legacy kwarg %r", drop)
            out.pop(drop, None)

    return out


def _result_to_legacy_dict(result: RetrievalResult) -> dict[str, Any]:
    """Coerce a RetrievalResult into the legacy `{"formatted_output", "metadata"}` shape."""
    return {
        "formatted_output": result.formatted_output or {"documents": []},
        "metadata": {
            **(result.metadata or {}),
            "kind": result.kind,
            "cache_hit": result.cache_hit,
            "wall_time_ms": result.wall_time_ms,
        },
    }


# ───────────────────────────────────────────────────────────────────────────
# LegacyRetrievalPipeline — the swap target for the agents/compliance pipelines
# ───────────────────────────────────────────────────────────────────────────

class LegacyRetrievalPipeline:
    """Stand-in replacement for the legacy `RetrievalPipeline`.

    The legacy class took (name, version, description, llm, retrieval_helper).
    The new ones takes (pipeline, default_org_id). Callers may keep the legacy
    construction shape by ignoring `llm`/`retrieval_helper` — those are
    accepted-and-ignored kwargs for backward-compat at construction time:

        pipeline = LegacyRetrievalPipeline(
            name="csod-retrieval",
            version="v2",
            description="...",
            pipeline=<new RetrievalPipeline>,
            default_org_id="acme-corp",
        )
    """

    def __init__(
        self,
        *,
        pipeline: RetrievalPipeline,
        default_org_id: str,
        # Accepted-and-ignored legacy kwargs:
        name: str = "ontology-retrieval-legacy",
        version: str = "v2",
        description: str = "Legacy wrapper over ontology-retrieval pipeline.",
        llm: Any = None,
        retrieval_helper: Any = None,
    ) -> None:
        self._pipeline = pipeline
        self._default_org_id = default_org_id
        self.name = name
        self.version = version
        self.description = description
        # `llm` and `retrieval_helper` are stored for any caller that introspects them;
        # they are NOT consulted internally.
        self._llm = llm
        self._retrieval_helper = retrieval_helper
        self._initialized = True
        self._metrics: dict[str, Any] = {}

    # ── Legacy lifecycle (no-ops) ────────────────────────────────────────

    @property
    def is_initialized(self) -> bool:
        return True

    async def initialize(self, **kwargs: Any) -> None:
        return None

    async def cleanup(self) -> None:
        return None

    def get_configuration(self) -> dict[str, Any]:
        return {"default_org_id": self._default_org_id}

    def update_configuration(self, config: dict[str, Any]) -> None:
        if "default_org_id" in config:
            self._default_org_id = config["default_org_id"]

    def get_metrics(self) -> dict[str, Any]:
        return {**self._metrics, **self._pipeline.metrics()}

    def reset_metrics(self) -> None:
        self._metrics.clear()

    # ── Main entry — mirrors the legacy signature exactly ───────────────

    async def run(self, retrieval_type: str, **kwargs: Any) -> dict[str, Any]:
        """Legacy contract: `await pipeline.run("database_schemas", query=..., project_id=...)`.

        Translates legacy kwargs to the new pipeline's input shape, invokes
        `pipeline.run(kind, **translated)`, and returns a dict-shaped result
        matching the legacy `{"formatted_output", "metadata"}` envelope.
        """
        translated = _translate_kwargs(org_id=self._default_org_id, kwargs=kwargs)
        result = await self._pipeline.run(retrieval_type, **translated)
        self._metrics["last_kind"] = retrieval_type
        self._metrics["last_cache_hit"] = result.cache_hit
        return _result_to_legacy_dict(result)


# ───────────────────────────────────────────────────────────────────────────
# LegacyRetrievalHelper — for code that calls helper methods directly
# ───────────────────────────────────────────────────────────────────────────

class LegacyRetrievalHelper:
    """Stand-in for the legacy `RetrievalHelper` get_* methods.

    Methods preserve their legacy signatures. Each delegates to a pipeline kind
    and returns a legacy-shaped dict (`{"documents": [...]}` or the per-method
    legacy shape).
    """

    def __init__(self, *, pipeline: RetrievalPipeline, default_org_id: str) -> None:
        self._pipeline = pipeline
        self._default_org_id = default_org_id

    # ── Asset-side ──────────────────────────────────────────────────────

    async def get_database_schemas(
        self,
        project_id: str | None = None,
        query: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        result = await self._pipeline.run("asset_search", **translated)
        return {"schemas": result.data or [], **(result.metadata or {})}

    async def get_views(
        self,
        project_id: str | None = None,
        query: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        # Filter to views via scope.asset_kinds — the kind itself accepts the scope as-is
        scope = translated.get("scope") or {}
        scope["asset_kinds"] = ["view", "materialized_view"]
        translated["scope"] = scope
        result = await self._pipeline.run("asset_search", **translated)
        return {"views": result.data or [], **(result.metadata or {})}

    async def get_metrics(
        self,
        project_id: str | None = None,
        query: str = "",
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        result = await self._pipeline.run("metrics_search", **translated)
        return {"metrics": result.data or [], **(result.metadata or {})}

    async def get_sql_functions(
        self,
        query: str = "",
        project_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        # Once function_metadata is wired into a dedicated kind, swap target id below.
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        scope = translated.get("scope") or {}
        scope["asset_kinds"] = ["function"]
        translated["scope"] = scope
        result = await self._pipeline.run("asset_search", **translated)
        return {"sql_functions": result.data or [], **(result.metadata or {})}

    # ── SQL pairs / instructions / historical (stubs today) ─────────────

    async def get_sql_pairs(
        self,
        query: str = "",
        project_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        result = await self._pipeline.run("sql_pairs_search", **translated)
        return {"sql_pairs": result.data or [], **(result.metadata or {})}

    async def get_instructions(
        self,
        query: str = "",
        project_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        result = await self._pipeline.run("instructions_search", **translated)
        return {"instructions": result.data or [], **(result.metadata or {})}

    async def get_historical_questions(
        self,
        query: str = "",
        project_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        translated = _translate_kwargs(
            org_id=self._default_org_id,
            kwargs={"project_id": project_id, "query": query, **kwargs},
        )
        result = await self._pipeline.run("historical_qa_search", **translated)
        return {"historical_questions": result.data or [], **(result.metadata or {})}

    # ── Lineage (was not in legacy RetrievalHelper; added for new callers) ──

    async def get_lineage(
        self,
        asset_rk: str,
        *,
        direction: str = "both",
        edge_kinds: list[str] | None = None,
        max_hops: int = 1,
    ) -> dict[str, Any]:
        result = await self._pipeline.run(
            "lineage_trace",
            asset_rk=asset_rk,
            direction=direction,
            edge_kinds=edge_kinds,
            max_hops=max_hops,
        )
        return {"lineage": result.data or {"nodes": [], "edges": []}, **(result.metadata or {})}


# ───────────────────────────────────────────────────────────────────────────
# One-shot builder — for callers that want a ready-to-use legacy pipeline
# ───────────────────────────────────────────────────────────────────────────

def build_legacy_pipeline(
    *,
    default_org_id: str,
    database: Any | None = None,
    qdrant_client: Any | None = None,
    config: PipelineConfig | None = None,
) -> LegacyRetrievalPipeline:
    """Construct a `LegacyRetrievalPipeline` end-to-end.

    Typical usage in the migration:

        from ontology_store import Database
        from ontology_retrieval.compat import build_legacy_pipeline

        pipeline = build_legacy_pipeline(
            default_org_id="acme-corp",
            database=Database.from_env(),
        )
        result = await pipeline.run("database_schemas", query="...", project_id="csod_risk_attrition")
        docs = result["formatted_output"]["documents"]
    """
    if database is None:
        from ontology_store import Database as _Database
        database = _Database.from_env()
    cfg = config or default_config()
    pipeline = build_pipeline_from_config(cfg, database=database, qdrant_client=qdrant_client)
    return LegacyRetrievalPipeline(pipeline=pipeline, default_org_id=default_org_id)
