"""Qdrant client factory.

Connection lifecycle is managed here so callers don't sprinkle URL parsing or
auth handling across the codebase. Singleton per (url, api_key) tuple.
"""
from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

try:
    from qdrant_client import QdrantClient
    _QDRANT_AVAILABLE = True
except ImportError:
    QdrantClient = None  # type: ignore[assignment]
    _QDRANT_AVAILABLE = False


class QdrantClientFactory:
    """Caches Qdrant clients keyed by (url, api_key). Thread-safe enough for
    single-process services; for multi-process, each process holds its own cache.
    """

    _cache: dict[tuple[str, str | None], "QdrantClient"] = {}

    @classmethod
    def get(
        cls,
        *,
        url: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
        port: int | None = None,
        prefer_grpc: bool = False,
        timeout: float = 30.0,
    ) -> "QdrantClient":
        """Get (or create) a Qdrant client.

        Resolution order:
          1. `url` (e.g. http://localhost:6333 or https://acct.cloud.qdrant.io)
          2. `host`+`port`
          3. env var QDRANT_URL → url
          4. env var QDRANT_HOST + QDRANT_PORT → host+port
          5. fallback: localhost:6333
        """
        if not _QDRANT_AVAILABLE:
            raise ImportError(
                "qdrant-client not installed. Install with: pip install 'ontology-store[vector]'"
            )

        resolved_url, resolved_host, resolved_port = cls._resolve_target(url, host, port)
        resolved_api_key = api_key or os.environ.get("QDRANT_API_KEY")
        cache_key = (resolved_url or f"{resolved_host}:{resolved_port}", resolved_api_key)

        if cache_key in cls._cache:
            return cls._cache[cache_key]

        kwargs: dict[str, Any] = {"timeout": timeout, "prefer_grpc": prefer_grpc}
        if resolved_url:
            kwargs["url"] = resolved_url
        else:
            kwargs["host"] = resolved_host
            kwargs["port"] = resolved_port
        if resolved_api_key:
            kwargs["api_key"] = resolved_api_key

        client = QdrantClient(**kwargs)  # type: ignore[arg-type]
        cls._cache[cache_key] = client
        logger.info("Qdrant client created (target=%s)", _redact(resolved_url or f"{resolved_host}:{resolved_port}"))
        return client

    @classmethod
    def clear_cache(cls) -> None:
        """Drop cached clients (close their connections)."""
        for client in cls._cache.values():
            try:
                close = getattr(client, "close", None)
                if callable(close):
                    close()
            except Exception:
                pass
        cls._cache.clear()

    @staticmethod
    def _resolve_target(
        url: str | None,
        host: str | None,
        port: int | None,
    ) -> tuple[str | None, str | None, int | None]:
        if url:
            return url, None, None
        if host:
            return None, host, port or 6333

        env_url = os.environ.get("QDRANT_URL")
        if env_url:
            return env_url, None, None
        env_host = os.environ.get("QDRANT_HOST")
        if env_host:
            env_port = int(os.environ.get("QDRANT_PORT", "6333"))
            return None, env_host, env_port

        return None, "localhost", 6333


def get_qdrant_client(**kwargs: Any) -> "QdrantClient":
    """Convenience: same as `QdrantClientFactory.get(**kwargs)`."""
    return QdrantClientFactory.get(**kwargs)


def _redact(target: str | None) -> str:
    if not target:
        return "(unknown)"
    try:
        parsed = urlparse(target)
        if parsed.password:
            return target.replace(parsed.password, "***")
    except Exception:
        pass
    return target
