"""Process-global wiring for Temporal workers (FedSQL client + store)."""

from __future__ import annotations

from nexcraft.client import FedSQLClient

from nexcraft_jobs.recipe import ResultStore
from nexcraft_jobs.store.null_store import NullResultStore

_fedsql: FedSQLClient | None = None
_store: ResultStore | None = None


def configure_worker(*, fedsql: FedSQLClient, store: ResultStore | None = None) -> None:
    global _fedsql, _store
    _fedsql = fedsql
    _store = store or NullResultStore()


def get_worker_fedsql() -> FedSQLClient:
    if _fedsql is None:
        raise RuntimeError(
            "Worker not configured: call nexcraft_jobs.runtime.worker_config.configure_worker()"
        )
    return _fedsql


def get_worker_store() -> ResultStore:
    global _store
    if _store is None:
        _store = NullResultStore()
    return _store


def reset_worker_config() -> None:
    global _fedsql, _store
    _fedsql = None
    _store = None
