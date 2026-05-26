"""Pipeline smoke tests — no live DB / no live Qdrant.

Verifies:
  - Registry is populated with the expected kinds (active + stubs).
  - Alias resolution works (legacy retrieval_type names route correctly).
  - Cache returns identical results for equivalent inputs.
  - Stub kinds return empty + roadmap diagnostic.
  - Input-schema validation rejects bad payloads.
  - Source resolution surfaces clear errors when a required source is missing.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel

from ontology_retrieval.pipeline import (
    LRUCache,
    NullCache,
    RetrievalContext,
    RetrievalKind,
    RetrievalPipeline,
    RetrievalResult,
    register_kind,
    registry,
)
from ontology_retrieval.pipeline.sources import Source


class _FakeSource:
    name = "fake"
    kind = "fake"

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def echo(self, payload: Any) -> Any:
        self.calls.append(payload)
        return payload


class _Echo(BaseModel):
    msg: str


def _echo_fetcher(ctx: RetrievalContext) -> RetrievalResult:
    fake: _FakeSource = ctx.source("fake")  # type: ignore[assignment]
    out = fake.echo(ctx.input.msg)  # type: ignore[attr-defined]
    return RetrievalResult(
        kind=ctx.kind,
        data={"echoed": out},
        formatted_output={"documents": [out]},
        metadata={"call_count": len(fake.calls)},
    )


# ───────────────────────────────────────────────────────────────────────────
# Registry shape
# ───────────────────────────────────────────────────────────────────────────

class TestRegistry:
    def test_active_kinds_registered(self) -> None:
        kinds = set(registry.list_ids())
        for required in ("asset_search", "asset_by_rk", "asset_list", "lineage_trace"):
            assert required in kinds

    def test_stub_kinds_registered(self) -> None:
        kinds = set(registry.list_ids())
        for stub in (
            "sql_pairs_search",
            "instructions_search",
            "historical_qa_search",
            "cards_search",
            "metrics_search",
            "claims_by_asset",
        ):
            assert stub in kinds

    def test_aliases_resolve(self) -> None:
        # Legacy retrieval_type names must round-trip
        for alias, canonical in [
            ("database_schemas", "asset_search"),
            ("views", "asset_search"),
            ("historical_questions", "historical_qa_search"),
            ("instructions", "instructions_search"),
            ("sql_pairs", "sql_pairs_search"),
            ("metrics", "metrics_search"),
            ("lineage_upstream", "lineage_trace"),
            ("lineage_downstream", "lineage_trace"),
        ]:
            kind = registry.get(alias)
            assert kind.id == canonical, f"alias {alias} should resolve to {canonical}"


# ───────────────────────────────────────────────────────────────────────────
# Pipeline behavior — registry-driven, with a fake source
# ───────────────────────────────────────────────────────────────────────────

@pytest.fixture()
def echo_kind() -> RetrievalKind:
    """Register a temporary echo kind for these tests; tear down after."""
    kind = RetrievalKind(
        id="_test_echo",
        description="Echo back the msg.",
        input_schema=_Echo,
        sources_required=("fake",),
        fetcher=_echo_fetcher,
        cache_ttl_seconds=60,
        status="active",
    )
    register_kind(kind)
    yield kind
    # Manual cleanup — registry has no public remove, so reach into _by_id.
    registry._by_id.pop(kind.id, None)  # type: ignore[attr-defined]


class TestPipeline:
    def test_run_invokes_fetcher_with_resolved_source(self, echo_kind: RetrievalKind) -> None:
        fake = _FakeSource()
        pipe = RetrievalPipeline(sources={"fake": fake}, cache=NullCache())
        result = asyncio.run(pipe.run("_test_echo", msg="hello"))
        assert result.kind == "_test_echo"
        assert result.data == {"echoed": "hello"}
        assert result.formatted_output == {"documents": ["hello"]}
        assert fake.calls == ["hello"]
        assert result.cache_hit is False
        assert "sources_used" in result.metadata
        assert result.metadata["sources_used"] == ["fake"]

    def test_input_validation_rejects_bad_payload(self, echo_kind: RetrievalKind) -> None:
        pipe = RetrievalPipeline(sources={"fake": _FakeSource()}, cache=NullCache())
        with pytest.raises(Exception):  # pydantic ValidationError subclass
            asyncio.run(pipe.run("_test_echo"))  # missing 'msg'

    def test_unknown_kind_raises(self) -> None:
        pipe = RetrievalPipeline(sources={}, cache=NullCache())
        with pytest.raises(KeyError):
            asyncio.run(pipe.run("nope_not_a_kind", whatever=1))

    def test_missing_source_raises_runtime_error(self, echo_kind: RetrievalKind) -> None:
        pipe = RetrievalPipeline(sources={}, cache=NullCache())  # no 'fake' source!
        with pytest.raises(RuntimeError):
            asyncio.run(pipe.run("_test_echo", msg="hello"))

    def test_cache_returns_same_payload_for_same_input(self, echo_kind: RetrievalKind) -> None:
        fake = _FakeSource()
        pipe = RetrievalPipeline(sources={"fake": fake}, cache=LRUCache(max_entries=10))

        # First call — miss, fetcher runs
        r1 = asyncio.run(pipe.run("_test_echo", msg="hi"))
        assert r1.cache_hit is False
        assert fake.calls == ["hi"]

        # Second call with identical input — hit, fetcher NOT re-run
        r2 = asyncio.run(pipe.run("_test_echo", msg="hi"))
        assert r2.cache_hit is True
        assert r2.data == r1.data
        # Source was not invoked again
        assert fake.calls == ["hi"]

        # Different msg — miss, fetcher runs once more
        r3 = asyncio.run(pipe.run("_test_echo", msg="there"))
        assert r3.cache_hit is False
        assert fake.calls == ["hi", "there"]

    def test_alias_routes_to_canonical(self) -> None:
        # asset_search has alias 'database_schemas' (active kind from kinds.py)
        # We don't have a postgres source wired here so the pipeline will
        # surface a "missing source" error — but the routing should resolve
        # the alias before the error happens.
        pipe = RetrievalPipeline(sources={}, cache=NullCache())
        with pytest.raises(RuntimeError) as exc_info:
            asyncio.run(pipe.run("database_schemas", query="x",
                                 scope={"org_id": "acme-corp"}))
        # Error mentions the canonical kind id, not the alias
        assert "asset_search" in str(exc_info.value)


# ───────────────────────────────────────────────────────────────────────────
# Stubs — return empty + roadmap diagnostic
# ───────────────────────────────────────────────────────────────────────────

class TestStubs:
    def test_sql_pairs_stub_returns_empty(self) -> None:
        pipe = RetrievalPipeline(sources={}, cache=NullCache())
        result = asyncio.run(pipe.run("sql_pairs_search",
                                      query="revenue",
                                      scope={"org_id": "acme-corp"},
                                      k=5))
        assert result.data == []
        assert result.metadata.get("stub") is True
        assert "roadmap" in result.metadata

    def test_legacy_historical_questions_alias_works(self) -> None:
        pipe = RetrievalPipeline(sources={}, cache=NullCache())
        result = asyncio.run(pipe.run("historical_questions",
                                      query="anything",
                                      scope={"org_id": "acme-corp"}))
        assert result.kind == "historical_qa_search"
        assert result.metadata.get("stub") is True


# ───────────────────────────────────────────────────────────────────────────
# Cache TTL expiry
# ───────────────────────────────────────────────────────────────────────────

class TestCache:
    def test_lru_evicts_oldest(self) -> None:
        cache = LRUCache(max_entries=2)
        asyncio.run(cache.set("a", 1))
        asyncio.run(cache.set("b", 2))
        asyncio.run(cache.set("c", 3))
        assert asyncio.run(cache.get("a")) is None  # evicted
        assert asyncio.run(cache.get("b")) == 2
        assert asyncio.run(cache.get("c")) == 3

    def test_lru_ttl_expires(self) -> None:
        cache = LRUCache(max_entries=10)
        asyncio.run(cache.set("k", "v", ttl_seconds=0))
        # immediately past TTL of 0
        import time as _t
        _t.sleep(0.01)
        assert asyncio.run(cache.get("k")) is None
