"""Management-DB-backed connection model.

A ``ManagementStore`` is the integration seam between nexcraft's federation
runtime and a host system's "connections" or "datasources" table (e.g. the
GenieML ConnectionDetails / DataSources schema). The store is responsible for
returning ``ConnectionDetails`` rows on demand; nexcraft uses those rows to
build typed ``SourceDescriptor``s (via ``DBCatalog``) and pooled driver
connections (via ``PooledConnectionProvider``).

Credentials are intentionally separated from descriptors: a row carries an
opaque ``secret_ref`` (e.g. an AWS Secrets Manager ARN, a Vault path, or a
``env:VAR_NAME`` reference) which a ``SecretResolver`` translates into the
fields the driver actually needs (password, private key, OAuth token, ...).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol, runtime_checkable

from nexcraft.errors import ConfigurationError


@dataclass(frozen=True)
class ConnectionDetails:
    """Mirrors a host-system connection row.

    Pool sizing intentionally lives elsewhere (``PoolConfigProvider``) because
    it is operator-tunable per source kind, not per row. Field intent:

      - ``source_id``: the same opaque id callers pass to ``FedSQLClient``.
      - ``tenant_id``: enforced against ``QueryContext.tenant_id`` at acquire().
      - ``kind``: must match a registered executor (``postgres``, ``snowflake``).
      - ``config``: non-secret driver parameters (host, database, warehouse).
      - ``secret_ref``: opaque pointer resolved by a ``SecretResolver``.
      - ``tags``: free-form metadata (team, environment, ...).
    """

    source_id: str
    tenant_id: str
    kind: str
    display_name: str
    config: Mapping[str, Any]
    secret_ref: str | None = None
    tags: Mapping[str, str] = field(default_factory=dict)


@runtime_checkable
class ManagementStore(Protocol):
    """Abstract over the host system's connections table.

    Implementations typically wrap a SQLAlchemy session, an async DB client,
    or an HTTP API. The store should be safe to call concurrently.
    """

    async def get_connection_details(self, source_id: str) -> ConnectionDetails: ...

    async def list_connection_details(
        self, tenant_id: str | None = None
    ) -> list[ConnectionDetails]: ...


@runtime_checkable
class SecretResolver(Protocol):
    """Translate a ``secret_ref`` into driver-ready credential fields.

    Returns a mapping like ``{"password": "...", "private_key": "..."}``
    that the per-kind ``DriverPoolFactory`` merges into the connection
    parameters before opening a real driver connection.
    """

    async def resolve(
        self, secret_ref: str, *, source_id: str, tenant_id: str
    ) -> Mapping[str, str]: ...


class NullSecretResolver:
    """Resolver that always returns an empty mapping. Useful when ``config``
    already carries everything the driver needs (or for source kinds that
    authenticate via the environment, e.g. AWS IAM)."""

    async def resolve(
        self, secret_ref: str, *, source_id: str, tenant_id: str
    ) -> Mapping[str, str]:
        return {}


class EnvSecretResolver:
    """Reads credentials from environment variables.

    Supports two ``secret_ref`` shapes:

      - ``env:VAR_NAME`` → returns ``{"password": os.environ["VAR_NAME"]}``
      - ``env-json:VAR_NAME`` → expects a JSON object in the env var and returns it.
    """

    def __init__(self, *, password_field: str = "password") -> None:
        self._password_field = password_field

    async def resolve(
        self, secret_ref: str, *, source_id: str, tenant_id: str
    ) -> Mapping[str, str]:
        if secret_ref.startswith("env:"):
            var = secret_ref[len("env:") :]
            value = os.environ.get(var)
            if value is None:
                raise ConfigurationError(
                    f"EnvSecretResolver: env var {var!r} unset (source_id={source_id!r})"
                )
            return {self._password_field: value}
        if secret_ref.startswith("env-json:"):
            import json

            var = secret_ref[len("env-json:") :]
            value = os.environ.get(var)
            if value is None:
                raise ConfigurationError(
                    f"EnvSecretResolver: env var {var!r} unset (source_id={source_id!r})"
                )
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ConfigurationError(
                    f"EnvSecretResolver: env var {var!r} is not valid JSON"
                ) from exc
            if not isinstance(parsed, dict):
                raise ConfigurationError(
                    f"EnvSecretResolver: env var {var!r} JSON must decode to an object"
                )
            return {str(k): str(v) for k, v in parsed.items()}
        raise ConfigurationError(
            f"EnvSecretResolver does not understand secret_ref={secret_ref!r}; "
            "expected 'env:NAME' or 'env-json:NAME'."
        )


class InMemoryManagementStore:
    """Reference store that holds rows in a dict — useful for tests and demos."""

    def __init__(self, rows: list[ConnectionDetails]) -> None:
        self._by_id: dict[str, ConnectionDetails] = {}
        for row in rows:
            if row.source_id in self._by_id:
                raise ConfigurationError(
                    f"Duplicate source_id in InMemoryManagementStore: {row.source_id!r}"
                )
            self._by_id[row.source_id] = row

    async def get_connection_details(self, source_id: str) -> ConnectionDetails:
        try:
            return self._by_id[source_id]
        except KeyError as e:
            raise ConfigurationError(f"Unknown source_id={source_id!r}") from e

    async def list_connection_details(
        self, tenant_id: str | None = None
    ) -> list[ConnectionDetails]:
        rows = list(self._by_id.values())
        if tenant_id is not None:
            rows = [r for r in rows if r.tenant_id == tenant_id]
        return rows


__all__ = [
    "ConnectionDetails",
    "EnvSecretResolver",
    "InMemoryManagementStore",
    "ManagementStore",
    "NullSecretResolver",
    "SecretResolver",
]
