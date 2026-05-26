"""Pipeline configuration — declarative source definitions + per-kind bindings.

A `PipelineConfig` describes:
  - which sources are available (by name) and their kind-specific config
  - per-kind cache TTL overrides (kinds register defaults; this can override)

Usage:

    cfg = PipelineConfig.from_yaml("retrieval_config.yaml")
    pipeline = build_pipeline_from_config(cfg, database=db, qdrant_client=qc)

Or build the pipeline imperatively without a config file — the config is for
deployment-time declarative control.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field


class SourceConfig(BaseModel):
    """Configuration for one named source instance."""
    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["postgres_asset", "postgres_lineage", "postgres_annotation", "qdrant", "filesystem"]
    options: dict[str, Any] = Field(default_factory=dict)


class KindConfig(BaseModel):
    """Per-kind config overrides. Optional — kinds work without this."""
    model_config = ConfigDict(extra="forbid")

    id: str
    cache_ttl_seconds: int | None = None
    enabled: bool = True


class PipelineConfig(BaseModel):
    """Top-level retrieval-pipeline config."""
    model_config = ConfigDict(extra="forbid")

    sources: list[SourceConfig] = Field(default_factory=list)
    kinds: list[KindConfig] = Field(default_factory=list)
    default_cache_ttl_seconds: int = 600
    cache_max_entries: int = 1024
    cache_enabled: bool = True

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineConfig":
        with Path(path).open("r") as fh:
            raw = yaml.safe_load(fh) or {}
        return cls.model_validate(raw)

    def source_by_name(self, name: str) -> SourceConfig | None:
        return next((s for s in self.sources if s.name == name), None)

    def kind_by_id(self, id_: str) -> KindConfig | None:
        return next((k for k in self.kinds if k.id == id_), None)


# ───────────────────────────────────────────────────────────────────────────
# Default config — used when no YAML is provided
# ───────────────────────────────────────────────────────────────────────────

def default_config() -> PipelineConfig:
    """Minimal config wiring Postgres sources + an opportunistic Qdrant assets source.

    The Qdrant source uses `auto_client=true` (its default). If QDRANT_URL /
    QDRANT_HOST and OPENAI_API_KEY are set, vector-backed search via
    `asset_vector_search` works automatically. Otherwise the source is disabled
    and the vector kind falls back to the Postgres `asset_search` path.
    """
    return PipelineConfig(
        sources=[
            SourceConfig(name="postgres_assets", kind="postgres_asset"),
            SourceConfig(name="postgres_lineage", kind="postgres_lineage"),
            SourceConfig(
                name="qdrant_assets",
                kind="qdrant",
                options={
                    "collection": "hier_t4_assets_prod",
                    "auto_client": True,
                    "auto_embedder": True,
                },
            ),
        ],
        kinds=[],
        default_cache_ttl_seconds=600,
        cache_max_entries=1024,
        cache_enabled=True,
    )
