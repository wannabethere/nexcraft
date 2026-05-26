"""Factory — turn a PipelineConfig into a wired-up RetrievalPipeline.

Single entry point: `build_pipeline_from_config(cfg, database=..., qdrant_client=...)`.
"""
from __future__ import annotations

from typing import Any

from ontology_store import Database

from ontology_retrieval.pipeline.base import RetrievalPipeline, registry
from ontology_retrieval.pipeline.cache import Cache, LRUCache, NullCache
from ontology_retrieval.pipeline.config import PipelineConfig, default_config
from ontology_retrieval.pipeline.sources import Source, build_source


def build_pipeline_from_config(
    cfg: PipelineConfig | None = None,
    *,
    database: Database | None = None,
    qdrant_client: Any | None = None,
    embedder: Any | None = None,
    cache: Cache | None = None,
    actor: str = "system",
) -> RetrievalPipeline:
    """Construct a RetrievalPipeline from config + injected backends.

    Args:
        cfg: PipelineConfig. If None, uses `default_config()`.
        database: Database for Postgres-backed sources. Required when the
                  config declares any postgres_* source.
        qdrant_client: Qdrant client for vector sources. Required only when
                       the config declares any qdrant source AND the source
                       is actually used (vector kinds).
        cache: Cache implementation. If None, uses an in-memory LRU when
               `cfg.cache_enabled`, else a no-op.
        actor: identifier recorded in diagnostics.

    Applies per-kind cache TTL overrides from `cfg.kinds`.
    """
    cfg = cfg or default_config()

    sources: dict[str, Source] = {}
    for src_cfg in cfg.sources:
        sources[src_cfg.name] = build_source(
            cfg=src_cfg, database=database,
            qdrant_client=qdrant_client, embedder=embedder,
        )

    # Apply per-kind config overrides
    for kc in cfg.kinds:
        try:
            kind_obj = registry.get(kc.id)
        except KeyError:
            continue
        if kc.cache_ttl_seconds is not None:
            kind_obj.cache_ttl_seconds = kc.cache_ttl_seconds
        if not kc.enabled:
            kind_obj.cacheable = False  # disable caching for disabled kinds; the kind still works

    if cache is None:
        cache = LRUCache(max_entries=cfg.cache_max_entries) if cfg.cache_enabled else NullCache()

    return RetrievalPipeline(
        sources=sources,
        cache=cache,
        default_cache_ttl_seconds=cfg.default_cache_ttl_seconds,
        actor=actor,
    )
