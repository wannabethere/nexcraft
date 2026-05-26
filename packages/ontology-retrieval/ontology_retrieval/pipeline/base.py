"""Core pipeline + registry types.

`RetrievalPipeline.run(kind, **kwargs)` is the single entry point. Internally:

  1. Look up the `RetrievalKind` from the registry.
  2. Validate kwargs against `kind.input_schema`.
  3. Cache check (skipped if any source declares `cache: false`).
  4. Resolve named sources from `kind.sources_required` against the configured
     source map.
  5. Build a `RetrievalContext` (input, sources, scope helpers).
  6. Call `kind.fetcher(ctx)` → `RetrievalResult`.
  7. Cache + return.

Sources are not invoked by the pipeline itself; the fetcher chooses how/when to
use them (sequential, parallel, fallback, fan-in). This keeps the pipeline core
agnostic of source semantics.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

from ontology_retrieval.pipeline.cache import Cache, NullCache
from ontology_retrieval.pipeline.sources import Source

logger = logging.getLogger(__name__)

KindStatus = Literal["active", "stub", "deprecated"]


# ───────────────────────────────────────────────────────────────────────────
# Result envelope
# ───────────────────────────────────────────────────────────────────────────

class RetrievalResult(BaseModel):
    """Standard envelope every fetcher returns.

    `formatted_output` keeps wire compatibility with the legacy pipeline shape
    (`{"documents": [...]}`); `data` is the canonical typed payload; `metadata`
    carries diagnostics + per-source contribution counts.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    kind: str
    formatted_output: dict[str, Any] = Field(default_factory=dict)
    data: Any = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    cache_hit: bool = False
    wall_time_ms: int = 0


# ───────────────────────────────────────────────────────────────────────────
# Context passed to fetchers
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class RetrievalContext:
    """Per-call state passed to fetchers.

    A fetcher inspects `ctx.input` (a validated Pydantic model) and uses the
    `ctx.sources` map (name -> Source) to fan out to the backing stores.
    `kind` is the kind id, for logging/metrics.
    """
    kind: str
    input: BaseModel
    sources: Mapping[str, Source]
    actor: str = "system"
    diagnostics: dict[str, Any] = field(default_factory=dict)

    def source(self, name: str) -> Source:
        try:
            return self.sources[name]
        except KeyError as exc:
            raise KeyError(
                f"Kind {self.kind!r} requires source {name!r}, but it is not configured. "
                f"Available: {list(self.sources)}"
            ) from exc


# ───────────────────────────────────────────────────────────────────────────
# RetrievalKind — the unit of registration
# ───────────────────────────────────────────────────────────────────────────

Fetcher = Callable[[RetrievalContext], "RetrievalResult | Awaitable[RetrievalResult]"]


@dataclass
class RetrievalKind:
    """Declarative kind definition.

    `sources_required` are *names* (not instances). The pipeline resolves these
    against its source map. Fetcher reads them via `ctx.source(name)`.

    `aliases` lets the kind respond to legacy names (e.g. an alias of
    'asset_search' for the legacy 'database_schemas' / 'views' names).
    """
    id: str
    description: str
    input_schema: type[BaseModel]
    sources_required: tuple[str, ...]
    fetcher: Fetcher
    cache_ttl_seconds: int | None = 600
    cacheable: bool = True
    aliases: tuple[str, ...] = ()
    status: KindStatus = "active"


# ───────────────────────────────────────────────────────────────────────────
# Registry — module-level
# ───────────────────────────────────────────────────────────────────────────

class _KindRegistry:
    """Module-level registry. Resolves canonical ids and aliases."""

    def __init__(self) -> None:
        self._by_id: dict[str, RetrievalKind] = {}
        self._alias_to_id: dict[str, str] = {}

    def register(self, kind: RetrievalKind) -> None:
        if kind.id in self._by_id:
            raise ValueError(f"Kind {kind.id!r} is already registered")
        for alias in kind.aliases:
            if alias in self._alias_to_id or alias in self._by_id:
                raise ValueError(f"Alias {alias!r} collides with an existing kind/alias")
        self._by_id[kind.id] = kind
        for alias in kind.aliases:
            self._alias_to_id[alias] = kind.id

    def get(self, id_or_alias: str) -> RetrievalKind:
        canonical = self._alias_to_id.get(id_or_alias, id_or_alias)
        if canonical not in self._by_id:
            raise KeyError(f"Unknown retrieval kind: {id_or_alias!r}")
        return self._by_id[canonical]

    def list_ids(self) -> list[str]:
        return sorted(self._by_id.keys())

    def list_kinds(self) -> list[RetrievalKind]:
        return [self._by_id[k] for k in sorted(self._by_id)]

    def clear(self) -> None:
        """Test helper."""
        self._by_id.clear()
        self._alias_to_id.clear()


registry = _KindRegistry()


def register_kind(kind: RetrievalKind) -> RetrievalKind:
    """Register a `RetrievalKind` on the module registry. Returns the kind."""
    registry.register(kind)
    return kind


# ───────────────────────────────────────────────────────────────────────────
# RetrievalPipeline
# ───────────────────────────────────────────────────────────────────────────

class RetrievalPipeline:
    """The orchestrator. Constructs once per process; reused across requests."""

    def __init__(
        self,
        *,
        sources: Mapping[str, Source],
        cache: Cache | None = None,
        default_cache_ttl_seconds: int = 600,
        actor: str = "system",
    ) -> None:
        """
        Args:
            sources: Named map of source name → Source instance. Kinds resolve
                     their required sources from this map.
            cache: Cache implementation. If None, no caching is performed.
            default_cache_ttl_seconds: Fallback when a kind doesn't specify TTL.
            actor: Identifier recorded in diagnostics; useful in multi-tenant.
        """
        self._sources = dict(sources)
        self._cache: Cache = cache if cache is not None else NullCache()
        self._default_ttl = default_cache_ttl_seconds
        self._actor = actor
        self._metrics: dict[str, Any] = {}

    # ── Introspection ───────────────────────────────────────────────────

    @property
    def configured_sources(self) -> list[str]:
        return sorted(self._sources)

    @property
    def available_kinds(self) -> list[str]:
        return registry.list_ids()

    def describe_kind(self, kind: str) -> dict[str, Any]:
        k = registry.get(kind)
        return {
            "id": k.id,
            "description": k.description,
            "input_schema": k.input_schema.model_json_schema(),
            "sources_required": list(k.sources_required),
            "cacheable": k.cacheable,
            "cache_ttl_seconds": k.cache_ttl_seconds,
            "aliases": list(k.aliases),
            "status": k.status,
        }

    def metrics(self) -> dict[str, Any]:
        return dict(self._metrics)

    # ── Main entry point ────────────────────────────────────────────────

    async def run(self, kind: str, **kwargs: Any) -> RetrievalResult:
        """Execute one retrieval. Preserves legacy `retrieval_type` accepts via aliases.

        kwargs are validated against the kind's input schema. Caching is applied
        when the kind is `cacheable=True` and a real cache is configured.
        """
        t0 = time.perf_counter()
        try:
            kdef = registry.get(kind)
        except KeyError:
            self._metrics["unknown_kind_calls"] = self._metrics.get("unknown_kind_calls", 0) + 1
            raise

        # Validate input
        try:
            validated = kdef.input_schema.model_validate(kwargs)
        except Exception:
            self._metrics["validation_failures"] = self._metrics.get("validation_failures", 0) + 1
            raise

        # Resolve sources required by this kind
        missing = [n for n in kdef.sources_required if n not in self._sources]
        if missing:
            raise RuntimeError(
                f"Kind {kdef.id!r} requires sources {missing} which are not configured. "
                f"Available: {self.configured_sources}"
            )
        bound_sources = {n: self._sources[n] for n in kdef.sources_required}

        # Cache
        cache_key = None
        if kdef.cacheable:
            cache_key = self._cache_key(kdef.id, validated)
            cached = await self._cache.get(cache_key)
            if cached is not None:
                result = RetrievalResult.model_validate(cached)
                result.cache_hit = True
                result.wall_time_ms = int((time.perf_counter() - t0) * 1000)
                self._metrics["cache_hits"] = self._metrics.get("cache_hits", 0) + 1
                return result

        # Execute the fetcher
        ctx = RetrievalContext(
            kind=kdef.id, input=validated, sources=bound_sources, actor=self._actor,
        )
        out = kdef.fetcher(ctx)
        if hasattr(out, "__await__"):  # awaitable
            out = await out  # type: ignore[misc]
        if not isinstance(out, RetrievalResult):
            raise TypeError(
                f"Fetcher for {kdef.id!r} returned {type(out).__name__}; expected RetrievalResult"
            )
        out.kind = kdef.id
        out.wall_time_ms = int((time.perf_counter() - t0) * 1000)
        out.metadata.setdefault("sources_used", list(bound_sources))
        out.metadata.setdefault("kind_status", kdef.status)

        # Cache the result
        if cache_key is not None:
            ttl = kdef.cache_ttl_seconds or self._default_ttl
            await self._cache.set(cache_key, out.model_dump(mode="json"), ttl_seconds=ttl)

        self._metrics["cache_misses"] = self._metrics.get("cache_misses", 0) + 1
        return out

    # ── Internals ───────────────────────────────────────────────────────

    @staticmethod
    def _cache_key(kind_id: str, validated_input: BaseModel) -> str:
        body = json.dumps(validated_input.model_dump(mode="json"), sort_keys=True, default=str)
        hexdigest = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        return f"retrieval:{kind_id}:{hexdigest}"
