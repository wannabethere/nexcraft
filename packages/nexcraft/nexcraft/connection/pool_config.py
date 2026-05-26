"""Pool sizing configuration — external to the management DB, keyed by source kind.

The intent is operator-tunable knobs (a Postgres pool may want 20 connections
in prod and 2 in dev; a Snowflake pool typically wants far fewer because each
session is heavyweight). Defaults live per ``kind``; specific ``source_id``
overrides exist for the rare case of a hot-spot source that needs its own
budget.

Resolution order in ``PoolConfigProvider.get(kind, source_id)``:
  1. ``overrides[source_id]`` if present
  2. ``defaults[kind]`` if present
  3. fallback ``PoolConfig()`` (1/5/5s)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from nexcraft.errors import ConfigurationError


@dataclass(frozen=True)
class PoolConfig:
    """Driver-agnostic pool sizing knobs.

    Driver-specific extras live in ``extras`` so a YAML/JSON file can pass
    things like asyncpg's ``statement_cache_size`` or Snowflake's
    ``client_session_keep_alive`` without us encoding every knob here.
    """

    min_size: int = 1
    max_size: int = 5
    acquire_timeout_s: float = 5.0
    idle_timeout_s: float | None = None
    max_lifetime_s: float | None = None
    extras: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class PoolConfigProvider(Protocol):
    def get(self, *, kind: str, source_id: str) -> PoolConfig: ...


class StaticPoolConfig:
    """Per-kind defaults plus optional per-source overrides — held in memory."""

    def __init__(
        self,
        defaults: Mapping[str, PoolConfig] | None = None,
        overrides: Mapping[str, PoolConfig] | None = None,
    ) -> None:
        self._defaults = dict(defaults or {})
        self._overrides = dict(overrides or {})

    def get(self, *, kind: str, source_id: str) -> PoolConfig:
        if source_id in self._overrides:
            return self._overrides[source_id]
        if kind in self._defaults:
            return self._defaults[kind]
        return PoolConfig()


def _coerce_pool_config(d: Mapping[str, Any]) -> PoolConfig:
    known = {
        "min_size",
        "max_size",
        "acquire_timeout_s",
        "idle_timeout_s",
        "max_lifetime_s",
    }
    extras = {k: v for k, v in d.items() if k not in known}
    return PoolConfig(
        min_size=int(d.get("min_size", 1)),
        max_size=int(d.get("max_size", 5)),
        acquire_timeout_s=float(d.get("acquire_timeout_s", 5.0)),
        idle_timeout_s=(
            None if d.get("idle_timeout_s") is None else float(d["idle_timeout_s"])
        ),
        max_lifetime_s=(
            None if d.get("max_lifetime_s") is None else float(d["max_lifetime_s"])
        ),
        extras=extras,
    )


class YamlPoolConfig(StaticPoolConfig):
    """Load per-kind pool defaults (and per-source overrides) from YAML.

    Format::

        defaults:
          postgres:
            min_size: 2
            max_size: 20
            acquire_timeout_s: 5
            statement_cache_size: 1024   # passed via extras to the driver
          snowflake:
            min_size: 1
            max_size: 8
        overrides:
          prod_pg_high_traffic:
            min_size: 5
            max_size: 50
    """

    @classmethod
    def from_string(cls, text: str) -> "YamlPoolConfig":
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:
            raise ConfigurationError(
                "YamlPoolConfig requires PyYAML; install with `pip install pyyaml`."
            ) from exc

        doc = yaml.safe_load(text) or {}
        if not isinstance(doc, dict):
            raise ConfigurationError(
                "YamlPoolConfig expects a mapping at the top level."
            )

        defaults_raw = doc.get("defaults") or {}
        if not isinstance(defaults_raw, dict):
            raise ConfigurationError("YamlPoolConfig 'defaults' must be a mapping.")
        overrides_raw = doc.get("overrides") or {}
        if not isinstance(overrides_raw, dict):
            raise ConfigurationError("YamlPoolConfig 'overrides' must be a mapping.")

        defaults = {
            str(kind): _coerce_pool_config(v if isinstance(v, dict) else {})
            for kind, v in defaults_raw.items()
        }
        overrides = {
            str(sid): _coerce_pool_config(v if isinstance(v, dict) else {})
            for sid, v in overrides_raw.items()
        }
        return cls(defaults=defaults, overrides=overrides)

    @classmethod
    def from_file(cls, path: str | Path) -> "YamlPoolConfig":
        return cls.from_string(Path(path).read_text())


__all__ = [
    "PoolConfig",
    "PoolConfigProvider",
    "StaticPoolConfig",
    "YamlPoolConfig",
]
