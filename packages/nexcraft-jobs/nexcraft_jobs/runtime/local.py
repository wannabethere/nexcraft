from __future__ import annotations

from typing import Any, Mapping

from nexcraft.client import FedSQLClient

from nexcraft_jobs.compute.duckdb_setup import register_extract_streams, setup_duckdb
from nexcraft_jobs.context import JobContext
from nexcraft_jobs.recipe import Recipe, ResultStore
from nexcraft_jobs.store.null_store import NullResultStore
from nexcraft_jobs.types import ResultRef


class LocalRuntime:
    """Validate → extract → DuckDB compute → persist in-process (jobs/02-temporal.md LocalRuntime)."""

    def __init__(
        self,
        fedsql: FedSQLClient,
        store: ResultStore | None = None,
    ) -> None:
        self._fedsql = fedsql
        self._store = store or NullResultStore()

    async def submit(
        self,
        recipe: Recipe,
        params: Mapping[str, Any],
        ctx: JobContext,
    ) -> ResultRef:
        recipe.validate(params)
        # Stamp recipe identity onto the context so downstream observers
        # (and the recipe itself, if it inspects ctx) see what they ran.
        if not ctx.recipe_name or not ctx.recipe_version:
            from dataclasses import replace as _replace

            ctx = _replace(ctx, recipe_name=recipe.name, recipe_version=recipe.version)

        streams = await recipe.extract(params, ctx, self._fedsql)
        inputs = dict(streams)

        con = setup_duckdb(ctx)
        try:
            register_extract_streams(con, inputs)
            compute_ctx = ctx.attach_duckdb(con)
            computed = await recipe.compute(inputs, params, compute_ctx)
            return await recipe.persist(computed, params, compute_ctx, self._store)
        finally:
            con.close()
