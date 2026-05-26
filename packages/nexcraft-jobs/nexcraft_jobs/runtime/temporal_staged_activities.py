"""Staged Temporal activities: Parquet handles between extract, compute, and persist."""

from __future__ import annotations

import time
from typing import Any, Mapping

import pyarrow as pa
import pyarrow.parquet as pq
from temporalio import activity

from nexcraft.errors import BudgetExceededError

from nexcraft_jobs.compute.duckdb_setup import (
    register_extract_views_from_parquet,
    setup_duckdb,
)
from nexcraft_jobs.runtime.parquet_extract import write_named_dataset_to_parquet
from nexcraft_jobs.runtime.registry import GLOBAL_REGISTRY
from nexcraft_jobs.runtime.staging_paths import (
    compute_parquet_path,
    compute_parquet_uri,
    extract_parquet_path,
    extract_parquet_uri,
    local_path_from_uri,
)
from nexcraft_jobs.runtime.staging_types import ExtractedDataset, ExtractResults
from nexcraft_jobs.runtime.temporal_codec import job_context_from_submit_payload
from nexcraft_jobs.runtime.temporal_errors import raise_application_error
from nexcraft_jobs.runtime.temporal_payloads import SubmitJobPayload
from nexcraft_jobs.runtime.worker_config import get_worker_fedsql, get_worker_store
from nexcraft_jobs.types import ComputeResult, ComputeResultHandle, ResultRef


def _sanitize_metadata(meta: Mapping[str, Any]) -> dict[str, Any]:
    """Coerce numpy/pyarrow scalars to plain Python so JSON serialization works."""
    out: dict[str, Any] = {}
    for k, v in meta.items():
        key = str(k)
        if hasattr(v, "item"):
            try:
                out[key] = v.item()
                continue
            except Exception:
                pass
        out[key] = v
    return out


def _maybe_cancel(ctx) -> None:
    cancel_fn = getattr(activity, "is_cancelled", None)
    if callable(cancel_fn) and cancel_fn():
        ctx.cancel.set()


def _open_parquet_as_reader(uri: str) -> pa.RecordBatchReader:
    pf = pq.ParquetFile(local_path_from_uri(uri))
    return pa.RecordBatchReader.from_batches(pf.schema_arrow, pf.iter_batches())


def _read_parquet_as_table(uri: str) -> pa.Table:
    return pq.read_table(local_path_from_uri(uri))


@activity.defn
async def run_extract_to_parquet_activity(payload: SubmitJobPayload) -> ExtractResults:
    """Stream `recipe.extract` outputs to per-dataset Parquet; return lightweight handles only."""
    if not payload.staging_root:
        raise_application_error(
            ValueError("staging_root must be set on SubmitJobPayload for staged workflows"),
            non_retryable=True,
        )
    t0 = time.perf_counter()
    recipe = GLOBAL_REGISTRY.get(payload.recipe_name, payload.recipe_version)
    ctx = job_context_from_submit_payload(payload)
    fedsql = get_worker_fedsql()

    try:
        streams = await recipe.extract(payload.params, ctx, fedsql)
    except BaseException as exc:
        raise_application_error(exc)

    datasets: dict[str, ExtractedDataset] = {}
    bytes_total = 0

    for name, table_or_reader in streams.items():
        path = extract_parquet_path(
            payload.staging_root, payload.tenant_id, payload.job_id, name
        )

        def make_on_batch(dataset_name: str):
            def on_batch(_batch, rows: int, nbytes: int) -> None:
                activity.heartbeat(
                    {
                        "phase": "extract",
                        "dataset": dataset_name,
                        "rows": rows,
                        "bytes": nbytes,
                    }
                )
                _maybe_cancel(ctx)

            return on_batch

        try:
            rows, nbytes = write_named_dataset_to_parquet(
                path,
                table_or_reader,
                on_batch=make_on_batch(name),
            )
        except BaseException as exc:
            raise_application_error(exc)

        bytes_total += nbytes
        uri = extract_parquet_uri(path)
        datasets[name] = ExtractedDataset(
            storage_uri=uri,
            schema_json=str(table_or_reader.schema),
            row_count_estimate=rows,
        )

    duration_ms = int((time.perf_counter() - t0) * 1000)
    return ExtractResults(
        datasets=datasets,
        bytes_total=bytes_total,
        duration_ms=duration_ms,
    )


@activity.defn
async def run_compute_from_parquet_activity(
    payload: SubmitJobPayload,
    extract_results: ExtractResults,
) -> ComputeResultHandle:
    """Run compute over Parquet-staged inputs and stage primary/auxiliary outputs to Parquet.

    A handle (URIs + JSON-safe metadata) is returned across the activity
    boundary; the persist activity rehydrates a ComputeResult from it.
    """
    if not payload.staging_root:
        raise_application_error(
            ValueError("staging_root must be set on SubmitJobPayload for staged workflows"),
            non_retryable=True,
        )

    ctx = job_context_from_submit_payload(payload)
    recipe = GLOBAL_REGISTRY.get(payload.recipe_name, payload.recipe_version)
    uri_by_name = {k: v.storage_uri for k, v in extract_results.datasets.items()}

    inputs: dict[str, pa.RecordBatchReader | pa.Table] = {
        name: _open_parquet_as_reader(uri) for name, uri in uri_by_name.items()
    }

    con = setup_duckdb(ctx)
    try:
        register_extract_views_from_parquet(con, uri_by_name)
        compute_ctx = ctx.attach_duckdb(con)

        activity.heartbeat({"phase": "compute", "job_id": payload.job_id, "step": "start"})
        try:
            computed = await recipe.compute(inputs, payload.params, compute_ctx)
        except MemoryError as exc:
            raise_application_error(
                BudgetExceededError(
                    "Host memory exceeded during compute",
                    budget_kind="memory",
                    limit=0,
                    observed=0,
                ),
                non_retryable=True,
                chain_from=exc,
            )
        except Exception as exc:
            if type(exc).__name__ == "OutOfMemoryException" or "Out of Memory" in str(exc):
                raise_application_error(
                    BudgetExceededError(
                        "DuckDB exceeded memory budget during compute",
                        budget_kind="memory",
                        limit=0,
                        observed=0,
                    ),
                    non_retryable=True,
                    chain_from=exc,
                )
            raise_application_error(exc)

        if not isinstance(computed, ComputeResult):
            raise_application_error(
                TypeError(
                    "recipe.compute must return a ComputeResult; got "
                    f"{type(computed).__name__}"
                ),
                non_retryable=True,
            )

        primary_path = compute_parquet_path(
            payload.staging_root, payload.tenant_id, payload.job_id, "primary"
        )
        write_named_dataset_to_parquet(primary_path, computed.primary)
        primary_uri = compute_parquet_uri(primary_path)

        aux_uris: dict[str, str] = {}
        for aux_name, aux_table in (computed.auxiliaries or {}).items():
            aux_path = compute_parquet_path(
                payload.staging_root,
                payload.tenant_id,
                payload.job_id,
                f"aux_{aux_name}",
            )
            write_named_dataset_to_parquet(aux_path, aux_table)
            aux_uris[aux_name] = compute_parquet_uri(aux_path)

        activity.heartbeat({"phase": "compute", "job_id": payload.job_id, "step": "done"})
        return ComputeResultHandle(
            primary_uri=primary_uri,
            auxiliary_uris=aux_uris,
            metadata=_sanitize_metadata(computed.metadata or {}),
        )
    finally:
        con.close()


@activity.defn
async def run_persist_activity(
    payload: SubmitJobPayload,
    compute_handle: ComputeResultHandle,
) -> ResultRef:
    ctx = job_context_from_submit_payload(payload)
    recipe = GLOBAL_REGISTRY.get(payload.recipe_name, payload.recipe_version)
    store = get_worker_store()

    primary = _read_parquet_as_table(compute_handle.primary_uri)
    auxiliaries = {
        name: _read_parquet_as_table(uri)
        for name, uri in (compute_handle.auxiliary_uris or {}).items()
    }
    result = ComputeResult(
        primary=primary,
        auxiliaries=auxiliaries,
        metadata=dict(compute_handle.metadata or {}),
    )
    try:
        return await recipe.persist(result, payload.params, ctx, store)
    except BaseException as exc:
        raise_application_error(exc)
