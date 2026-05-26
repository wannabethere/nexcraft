"""Temporal activities for the ontology-pipeline ingestion workflow.

Each activity is a thin wrapper around the corresponding piece of the
existing pipeline. Idempotency is inherited from the underlying functions
(content_hash for per-table, natural-key upserts for DAO writes).

Why activities and not workflow-internal calls:

  - Activities are the unit of retry. A failed LLM call to OpenAI is a
    network failure, not a workflow failure — Temporal retries the activity
    according to the configured policy.
  - Activities are the unit of observability. Each step shows up in Temporal
    UI with its own input / output / wall time.
  - Activities cross the deterministic-execution boundary. Anything that
    talks to a database, the network, or the filesystem MUST be in an
    activity, not directly in the workflow body.

Per-table activity (`process_one_table_activity`) runs the full per-table
pipeline: build MDL → profile → run all configured enrichers → write through
the configured sink. It returns a JSON-serialisable result (`PerTableResult`)
so the workflow can accumulate cross-table state for the post-passes.

This module imports `temporalio.activity` lazily inside `_activity_defn` so
the module is importable without the optional `[temporal]` extra installed
(tests that don't use Temporal can still construct PerTableResult objects).
"""
from __future__ import annotations

import logging
import time
from typing import Any

from ontology_pipeline.temporal.inputs import (
    OntologyIngestionInput,
    PerTableResult,
    PostPassResult,
    TableSpec,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Activity bodies (pure Python; the `@activity.defn` decorator is applied
# at import time below).
# ───────────────────────────────────────────────────────────────────────────


def introspect_source(input: OntologyIngestionInput) -> list[TableSpec]:
    """List the tables the workflow should process.

    Runs the existing introspector synchronously inside the activity. For
    large sources (hundreds of tables) this is still a single connection
    and a single information_schema query, so doing it in one activity is
    fine — the per-table fan-out below is where parallelism pays off.
    """
    from ontology_pipeline.mdl import asset_rk
    from ontology_pipeline.introspect import make_introspector

    cfg = input.to_pipeline_config()
    introspector = make_introspector(cfg.source.kind)
    introspection = introspector.introspect(source=cfg.source)
    return [
        TableSpec(
            schema_name=t.schema_name,
            name=t.name,
            qualified_name=t.qualified_name,
            asset_rk=asset_rk(cfg.source.source_id, introspection.catalog, t),
            catalog=introspection.catalog,
        )
        for t in introspection.tables
    ]


def process_one_table(
    input: OntologyIngestionInput,
    table_spec: TableSpec,
) -> PerTableResult:
    """Run the per-table pipeline for one table. Idempotent.

    Re-runs that hit an unchanged content_hash short-circuit with
    outcome='unchanged'. Per-table exceptions are caught and returned as
    `outcome='error'` so a single bad table doesn't abort the workflow
    (Temporal's retry policy still applies *before* we get here — by the
    time we catch, the activity has exhausted its retries).
    """
    from ontology_pipeline.annotate import load_vocab
    from ontology_pipeline.mdl import asset_rk as _asset_rk
    from ontology_pipeline.introspect import make_introspector
    from ontology_pipeline.output import make_sink
    from ontology_pipeline.pipeline import (
        _build_profiler,
        _build_llm_provider,
        _process_table,
    )
    from ontology_pipeline.state import RunState

    cfg = input.to_pipeline_config()
    started = time.perf_counter()

    # Build dependencies fresh per activity — they're cheap and side-effect-free
    # apart from filesystem touches we already do idempotently.
    introspector = make_introspector(cfg.source.kind)
    introspection = introspector.introspect(source=cfg.source)
    target_table = next(
        (t for t in introspection.tables
         if t.schema_name == table_spec.schema_name and t.name == table_spec.name),
        None,
    )
    if target_table is None:
        return PerTableResult(
            qualified_name=table_spec.qualified_name,
            asset_rk=table_spec.asset_rk,
            outcome="error",
            error=f"table {table_spec.qualified_name} not found at introspect time",
        )

    sink = make_sink(cfg.output, org_id=cfg.source.org_id)
    vocab = load_vocab(cfg.semantic_layer)
    needs_llm = any([
        cfg.pipeline.fill_descriptions, cfg.pipeline.rich_description,
        cfg.pipeline.enrich_column_semantics, cfg.pipeline.enrich_data_protection,
        cfg.pipeline.infer_relationships, cfg.pipeline.enrich_causal_dependencies,
        cfg.pipeline.annotate and cfg.pipeline.concepts_source != "ner_only",
    ])
    provider = _build_llm_provider(cfg.llm) if needs_llm else None
    profiler = _build_profiler(cfg) if cfg.pipeline.compute_column_stats else None
    run_state = RunState(sink.base_dir())

    # Cross-table accumulators are NOT shared across activities; we collect
    # per-table results here and return them so the workflow can aggregate.
    local_relationships: list[dict[str, Any]] = []
    local_concept_index: dict[str, str] = {}

    table_result, mdl_out = _process_table(
        table=target_table,
        introspection=introspection,
        config=cfg,
        sink=sink,
        run_state=run_state,
        vocab=vocab,
        provider=provider,
        profiler=profiler,
        relationship_accumulator=local_relationships,
        concept_index=local_concept_index,
    )
    run_state.flush()

    primary_concept = local_concept_index.get(table_spec.asset_rk)

    return PerTableResult(
        qualified_name=table_result.qualified_name,
        asset_rk=table_result.asset_rk,
        outcome=table_result.outcome,
        native_columns_preserved=getattr(
            table_result, "native_column_comments_preserved", 0,
        ),
        llm_calls=getattr(table_result, "llm_calls", 0),
        wall_time_s=round(time.perf_counter() - started, 3),
        error=getattr(table_result, "error", None),
        inferred_relationships=local_relationships,
        primary_concept=primary_concept,
    )


def run_cross_asset_causal(
    input: OntologyIngestionInput,
    asset_rks: list[str],
) -> PostPassResult:
    """Workflow-level post-pass — runs once over all built MDLs.

    Currently the cross-asset stage re-builds MDLs via introspect; that's
    fine when the workflow is end-of-run. Future optimisation: pass the
    actual MDLs through Temporal payloads (they're already JSON-safe).
    """
    cfg = input.to_pipeline_config()
    if not cfg.pipeline.enrich_cross_asset_causal or len(asset_rks) < 2:
        return PostPassResult(stage="cross_asset_causal", counts={"skipped": 1})

    # The existing post-pass in pipeline.run() takes a list of built MDLs.
    # Rebuild them quickly via introspect (cheap; we already paid the cost).
    from ontology_pipeline.annotate import load_vocab
    from ontology_pipeline.mdl import asset_rk as _asset_rk
    from ontology_pipeline.introspect import make_introspector
    from ontology_pipeline.mdl import build_mdl
    from ontology_pipeline.output import make_sink
    from ontology_pipeline.pipeline import _build_llm_provider, _run_cross_asset_causal

    introspector = make_introspector(cfg.source.kind)
    introspection = introspector.introspect(source=cfg.source)
    rk_set = set(asset_rks)
    built_mdls = [
        build_mdl(source_id=cfg.source.source_id, catalog=introspection.catalog, table=t)
        for t in introspection.tables
        if _asset_rk(cfg.source.source_id, introspection.catalog, t) in rk_set
    ]
    if len(built_mdls) < 2:
        return PostPassResult(stage="cross_asset_causal", counts={"skipped": 1})

    sink = make_sink(cfg.output, org_id=cfg.source.org_id)
    vocab = load_vocab(cfg.semantic_layer)
    provider = _build_llm_provider(cfg.llm)
    if provider is None:
        return PostPassResult(
            stage="cross_asset_causal",
            counts={"skipped": 1}, error="no LLM provider",
        )

    n_calls = _run_cross_asset_causal(
        built_mdls=built_mdls, config=cfg, vocab=vocab,
        provider=provider, sink=sink,
        introspection_catalog=introspection.catalog,
    )
    return PostPassResult(
        stage="cross_asset_causal",
        llm_calls=n_calls,
        counts={"mdls_in_scope": len(built_mdls), "llm_calls": n_calls},
    )


def run_relation_induction(
    input: OntologyIngestionInput,
    relationships: list[dict[str, Any]],
    concept_index: dict[str, str],
) -> PostPassResult:
    """Workflow-level post-pass: foundry.induce_schema over the accumulated edges."""
    cfg = input.to_pipeline_config()
    if not cfg.pipeline.induce_relation_schema or not relationships:
        return PostPassResult(stage="induce_relation_schema", counts={"skipped": 1})

    from ontology_pipeline.output import make_sink
    from ontology_pipeline.pipeline import _build_llm_provider, _run_relation_induction

    provider = _build_llm_provider(cfg.llm)
    if provider is None:
        return PostPassResult(
            stage="induce_relation_schema",
            counts={"skipped": 1}, error="no LLM provider",
        )
    sink = make_sink(cfg.output, org_id=cfg.source.org_id)
    n_calls = _run_relation_induction(
        relationships=relationships,
        concept_index=concept_index,
        config=cfg, provider=provider, sink=sink,
    )
    return PostPassResult(
        stage="induce_relation_schema",
        llm_calls=n_calls,
        counts={
            "edges_in": len(relationships),
            "llm_calls": n_calls,
        },
    )


def run_causal_validation(
    input: OntologyIngestionInput,
    asset_rk_prefix: str | None = None,
    limit: int = 50,
) -> PostPassResult:
    """Workflow-level post-pass: statistical validation of causal_candidate rows.

    Validators need DB access — only runs against `hierarchy_store` / `tee`
    sinks. Reports `skipped` for filesystem-only output configurations.
    """
    cfg = input.to_pipeline_config()
    if cfg.output.kind not in ("hierarchy_store", "tee"):
        return PostPassResult(
            stage="validate_causal_candidates",
            counts={"skipped": 1}, error="filesystem sink — DB validator unavailable",
        )
    try:
        from ontology_store import Database
        from ontology_pipeline.validate import (
            CausalValidator,
            PsycopgCausalSampler,
        )
    except ImportError as exc:
        return PostPassResult(
            stage="validate_causal_candidates",
            counts={"skipped": 1}, error=f"deps missing: {exc}",
        )

    db = Database.from_env("ONTOLOGY_STORE_URL")
    sampler = PsycopgCausalSampler(
        dsn_for=lambda sid: cfg.source.connection.dsn(),
    )
    validator = CausalValidator(
        session_factory=db.session,
        sampler=sampler,
    )
    counts = validator.run_once(
        asset_rk_prefix=asset_rk_prefix, limit=limit,
    )
    return PostPassResult(
        stage="validate_causal_candidates",
        llm_calls=0, counts=counts,
    )


# ───────────────────────────────────────────────────────────────────────────
# @activity.defn registration — only when temporalio is importable.
# ───────────────────────────────────────────────────────────────────────────


def _register_activities() -> list[Any] | None:
    """Decorate the public bodies as Temporal activities.

    Returned for the worker bootstrap. None when temporalio isn't installed —
    activities can still be called directly for tests in that case.
    """
    try:
        from temporalio import activity
    except ImportError:
        return None

    registered = [
        activity.defn(name="ontology.introspect_source")(introspect_source),
        activity.defn(name="ontology.process_one_table")(process_one_table),
        activity.defn(name="ontology.run_cross_asset_causal")(run_cross_asset_causal),
        activity.defn(name="ontology.run_relation_induction")(run_relation_induction),
        activity.defn(name="ontology.run_causal_validation")(run_causal_validation),
    ]
    return registered


ACTIVITIES = _register_activities()
