"""Pipeline orchestrator — introspect → generate MDL → fill descriptions → annotate → write.

Sequential v1. Re-runnable: per-table content-hash idempotency. Skips unchanged
tables unless `pipeline.re_enrich_unchanged=True` in config.

Future: Temporal workflow over this orchestrator; per-table tasks become
Temporal activities.
"""
from __future__ import annotations

import fnmatch
import logging
import time
from datetime import datetime, timezone

from ontology_foundry.llm.provider import ModelProvider
from ontology_foundry.llm.openai_provider import OpenAIChatProvider

from ontology_pipeline.annotate import SemanticVocab, enrich_annotations, load_vocab
from ontology_pipeline.config import LLMConfig, PipelineConfig, TableFilter
from ontology_pipeline.enrich import (
    CausalDependencyEnricher,
    ClusterContext,
    ColumnSemanticsEnricher,
    CrossAssetCausalEnricher,
    DataProtectionEnricher,
    EnrichmentContext,
    EnrichmentResult,
    EnrichmentStage,
    RelationshipInferenceEnricher,
    RichDescriptionEnricher,
)
from ontology_pipeline.introspect import make_introspector
from ontology_pipeline.mdl import asset_rk, build_mdl, fill_descriptions
from ontology_pipeline.models import (
    AssetAnnotations,
    GeneratedMDL,
    IntrospectionResult,
    PipelineRunResult,
    TableInfo,
    TableRunResult,
)
from ontology_pipeline.output import Sink, make_sink
from ontology_pipeline.state import RunState, content_hash

logger = logging.getLogger(__name__)


def run(config: PipelineConfig) -> PipelineRunResult:
    """Execute one pipeline run. Idempotent across runs (same source → same outcomes).

    Steps:
      1. Introspect source.
      2. Filter tables per config.tables.
      3. For each surviving table:
           a. Compute content hash.
           b. If hash matches stored AND not forced, mark unchanged + skip.
           c. Else: build MDL → optionally fill descriptions → optionally annotate.
           d. Write MDL + annotations to the sink.
           e. Update run state with new hash.
      4. Flush run state.
    """
    started_at = datetime.now(timezone.utc)
    logger.info("Pipeline run starting for source %s", config.source.source_id)

    # ── Set up dependencies ──────────────────────────────────────────────
    introspector = make_introspector(config.source.kind)
    sink = make_sink(config.output, org_id=config.source.org_id)
    vocab = load_vocab(config.semantic_layer)
    # `annotate` only forces LLM if it isn't running in NER-only mode — NER
    # is deterministic and runs entirely against the tenant card lexicon.
    annotate_needs_llm = (
        config.pipeline.annotate and config.pipeline.concepts_source != "ner_only"
    )
    needs_llm = any([
        config.pipeline.fill_descriptions,
        config.pipeline.rich_description,
        config.pipeline.enrich_column_semantics,
        config.pipeline.enrich_data_protection,
        config.pipeline.infer_relationships,
        config.pipeline.enrich_causal_dependencies,
        config.pipeline.enrich_cross_asset_causal,
        config.pipeline.induce_relation_schema,
        annotate_needs_llm,
    ])
    provider = _build_llm_provider(config.llm) if needs_llm else None
    if provider is None and needs_llm:
        logger.warning(
            "LLM behaviors enabled but no provider could be built; pipeline will "
            "run deterministic-only."
        )
    run_state = RunState(sink.base_dir())

    # ── Introspect ───────────────────────────────────────────────────────
    introspection = introspector.introspect(source=config.source)
    logger.info(
        "Introspected %d tables/views from %s", len(introspection.tables), config.source.source_id
    )

    # ── Filter ───────────────────────────────────────────────────────────
    filtered = _filter_tables(introspection.tables, config.tables)
    logger.info(
        "After filter: %d table(s) (filter configured=%s)",
        len(filtered),
        config.tables.is_configured(),
    )

    # ── Per-table processing ─────────────────────────────────────────────
    per_table: list[TableRunResult] = []
    tables_skipped_unchanged = 0
    tables_errored = 0
    total_llm_calls = 0

    # ── Profiler (foundry bundle_from_pandas) — built once, used per table ─
    profiler = _build_profiler(config) if config.pipeline.compute_column_stats else None

    built_mdls: list[GeneratedMDL] = []
    # Cross-table accumulators for post-passes (cross-asset causal + relation
    # induction). Each per-table call appends; the post-passes drain them.
    accumulated_relationships: list[dict[str, Any]] = []
    asset_concept_index: dict[str, str] = {}

    for table in filtered:
        result, mdl_out = _process_table(
            table=table,
            introspection=introspection,
            config=config,
            sink=sink,
            run_state=run_state,
            vocab=vocab,
            provider=provider,
            profiler=profiler,
            relationship_accumulator=accumulated_relationships,
            concept_index=asset_concept_index,
        )
        per_table.append(result)
        if mdl_out is not None and result.outcome in {"created", "updated"}:
            built_mdls.append(mdl_out)
        if result.outcome == "unchanged":
            tables_skipped_unchanged += 1
        elif result.outcome == "error":
            tables_errored += 1
        total_llm_calls += result.llm_calls

    # ── Cross-asset causal pass (runs once over the accumulated MDL set) ─
    if config.pipeline.enrich_cross_asset_causal and len(built_mdls) >= 2:
        cross_calls = _run_cross_asset_causal(
            built_mdls=built_mdls,
            config=config,
            vocab=vocab,
            provider=provider,
            sink=sink,
            introspection_catalog=introspection.catalog,
        )
        total_llm_calls += cross_calls

    # ── Relation-schema induction (foundry.relations.induce_schema) ──────
    # Runs once over every inferred relationship the per-table loop produced.
    if (
        config.pipeline.induce_relation_schema
        and accumulated_relationships
        and provider is not None
    ):
        induction_calls = _run_relation_induction(
            relationships=accumulated_relationships,
            concept_index=asset_concept_index,
            config=config,
            provider=provider,
            sink=sink,
        )
        total_llm_calls += induction_calls

    # ── Flush state ──────────────────────────────────────────────────────
    run_state.flush()
    finished_at = datetime.now(timezone.utc)

    summary = PipelineRunResult(
        source_id=config.source.source_id,
        started_at=started_at,
        finished_at=finished_at,
        tables_seen=len(introspection.tables),
        tables_processed=len(per_table) - tables_skipped_unchanged - tables_errored,
        tables_skipped_unchanged=tables_skipped_unchanged,
        tables_errored=tables_errored,
        total_llm_calls=total_llm_calls,
        per_table=per_table,
    )
    logger.info(
        "Pipeline run complete: seen=%d processed=%d unchanged=%d errored=%d llm_calls=%d wall=%.1fs",
        summary.tables_seen,
        summary.tables_processed,
        summary.tables_skipped_unchanged,
        summary.tables_errored,
        summary.total_llm_calls,
        summary.wall_time_seconds,
    )
    return summary


# ───────────────────────────────────────────────────────────────────────────
# Per-table processing
# ───────────────────────────────────────────────────────────────────────────

def _process_table(
    *,
    table: TableInfo,
    introspection: IntrospectionResult,
    config: PipelineConfig,
    sink: Sink,
    run_state: RunState,
    vocab: SemanticVocab,
    provider: ModelProvider | None,
    profiler: Any | None = None,
    relationship_accumulator: list[dict[str, Any]] | None = None,
    concept_index: dict[str, str] | None = None,
) -> tuple[TableRunResult, GeneratedMDL | None]:
    t0 = time.perf_counter()
    rk = asset_rk(config.source.source_id, introspection.catalog, table)
    qn = table.qualified_name
    new_hash = content_hash(table)
    prev_hash = run_state.lookup(
        source_id=config.source.source_id,
        schema=table.schema_name,
        table=table.name,
    )

    if (
        prev_hash == new_hash
        and not config.pipeline.re_enrich_unchanged
    ):
        run_state.record(
            source_id=config.source.source_id,
            schema=table.schema_name,
            table=table.name,
            content_hash_value=new_hash,
            outcome="unchanged",
        )
        return (
            TableRunResult(
                qualified_name=qn,
                asset_rk=rk,
                outcome="unchanged",
                native_column_comments_preserved=sum(1 for c in table.columns if c.description),
                wall_time_seconds=time.perf_counter() - t0,
            ),
            None,
        )

    try:
        # Build MDL (deterministic)
        mdl = build_mdl(
            source_id=config.source.source_id,
            catalog=introspection.catalog,
            table=table,
        )
        native_count = sum(1 for c in table.columns if c.description)

        llm_calls = 0
        column_fill = 0
        table_desc_generated = False

        # ── Stage 1b: foundry profiling pre-pass ─────────────────────────
        # Always runs before any enricher when compute_column_stats=True.
        # Aggregates persist immediately; samples wait for data_protection.
        tabular_bundle: Any | None = None
        column_rk_by_name: dict[str, str] = {}
        if profiler is not None and mdl.models:
            tabular_bundle = profiler.profile(
                source_id=config.source.source_id,
                schema=table.schema_name,
                table=table.name,
                table_id=rk,
                table_description=mdl.models[0].description,
            )
            column_rk_by_name = {c.name: c.rk for c in mdl.models[0].columns}
            if tabular_bundle is not None:
                # column_stat/table_stat FK to the spine, so the MDL must be persisted
                # FIRST. Write the spine up front (idempotent — the fully-enriched MDL is
                # re-upserted at the end). Only runs when profiling will persist stats.
                sink.write_mdl(
                    source_id=config.source.source_id,
                    schema=table.schema_name, table=table.name, mdl=mdl,
                )
                _persist_aggregates(
                    sink=sink, source_id=config.source.source_id,
                    schema=table.schema_name, table_name=table.name,
                    table_rk=rk, bundle=tabular_bundle,
                    column_rk_by_name=column_rk_by_name,
                )

        # Stage 2a: LLM description fill — basic, only when rich_description is OFF
        if config.pipeline.fill_descriptions and not config.pipeline.rich_description:
            mdl, native_after, column_fill, table_desc_generated = fill_descriptions(
                mdl, provider=provider,
            )
            native_count = max(native_count, native_after)
            if column_fill or table_desc_generated:
                llm_calls += 1

        # Stage 2b/c/d/e: configurable enrichment stages
        enrichment_ctx = EnrichmentContext(
            source_id=config.source.source_id,
            catalog=introspection.catalog,
            schema_name=table.schema_name,
            provider=provider,
            llm_model_id=config.llm.model,
            tabular_bundle=tabular_bundle,
        )
        enrichment_results: list[EnrichmentResult] = []
        enrichment_stages: list[EnrichmentStage] = _build_enrichment_stages(config, vocab=vocab)
        for stage in enrichment_stages:
            res = stage.apply(mdl, enrichment_ctx)
            llm_calls += res.llm_calls
            if res.warnings:
                for w in res.warnings:
                    logger.debug("enrichment[%s] %s: %s", stage.name, table.qualified_name, w)
            if res.side_output:
                # Route stage side-output through the sink (inferred FKs →
                # lineage_edge, causal candidates → causal_candidate, data-
                # protection hints → data_protection_hint).
                _route_enrichment_side_output(
                    table=table,
                    side_output=res.side_output,
                    sink=sink,
                    source_id=config.source.source_id,
                    asset_rk=rk,
                )
            enrichment_results.append(res)

        # ── Capture inferred relationships for the relation-induction post-pass ─
        if relationship_accumulator is not None:
            for r in enrichment_results:
                if r.stage_name != "relationship_inference":
                    continue
                items = r.side_output.get("inferred_relationships") or []
                for item in items:
                    # Normalize to absolute rks so induce_schema can canonicalize
                    # without re-resolving names. `to_table_qualified` is
                    # 'schema.table'; resolve against the cluster prefix.
                    cluster_prefix = rk.rsplit("/", 2)[0] if "/" in rk else rk
                    to_qual = item.get("to_table_qualified") or ""
                    if not to_qual:
                        continue
                    to_rk = cluster_prefix + "/" + to_qual.replace(".", "/")
                    relationship_accumulator.append({
                        "from_rk": item.get("from_table_rk") or rk,
                        "to_rk": to_rk,
                        "edge_kind": item.get("edge_kind") or "depends_on",
                        "predicate_surface": item.get("predicate") or "references",
                        "confidence": float(item.get("confidence") or 0.0),
                        "reason": item.get("reason") or item.get("rationale"),
                        # Surface fields propagated from the relationship
                        # enricher's side_output (RelationshipInferenceEnricher
                        # populates them). Used to enrich event narratives in
                        # the post-pass attach step.
                        "from_one_liner": item.get("from_one_liner"),
                        "from_asset_surface": item.get("from_asset_surface"),
                        "from_table_name": item.get("from_table_name"),
                        # FK columns + briefs — the predicate-attached event
                        # narrative renders these so consumers see WHAT the
                        # join key is (type + description) not just the name.
                        "from_column": item.get("from_column"),
                        "from_column_brief": item.get("from_column_brief"),
                        "to_column": item.get("to_column"),
                        "to_column_brief": item.get("to_column_brief"),
                    })
        # Record the asset's primary concept for use as subject_type during
        # relation induction (rk → first concept on the MDL model).
        if concept_index is not None and mdl.models:
            primary = (mdl.models[0].concepts or [None])[0]
            if primary:
                concept_index[rk] = primary

        # ── Phase 2 of stats persistence: PII-gated sample values ─────────
        # The data_protection enricher's side_output tells us which columns
        # are sensitive; everything NOT flagged is safe to persist samples for.
        if tabular_bundle is not None and column_rk_by_name:
            _persist_pii_gated_samples(
                sink=sink, source_id=config.source.source_id,
                schema=table.schema_name, table_name=table.name,
                table_rk=rk, bundle=tabular_bundle,
                column_rk_by_name=column_rk_by_name,
                enrichment_results=enrichment_results,
                mdl=mdl,
            )

        # Track whether description fields landed via the rich enricher
        rich_did_fill = any(
            r.stage_name == "rich_description" and r.fields_updated
            for r in enrichment_results
        )
        if rich_did_fill:
            table_desc_generated = True

        # Stage 3: annotation enrichment — NER pre-pass (foundry) + optional LLM
        annotations: AssetAnnotations | None = None
        if config.pipeline.annotate:
            annotations = enrich_annotations(
                mdl,
                vocab=vocab,
                provider=provider,
                source_model=config.llm.model,
                concepts_source=config.pipeline.concepts_source,
            )
            if annotations is not None and annotations.source != "ner_pre_pass":
                llm_calls += 1

        # Write outputs
        sink.write_mdl(
            source_id=config.source.source_id,
            schema=table.schema_name,
            table=table.name,
            mdl=mdl,
        )
        if annotations is not None:
            sink.write_annotations(
                source_id=config.source.source_id,
                schema=table.schema_name,
                table=table.name,
                annotations=annotations,
            )

        outcome = "created" if prev_hash is None else "updated"
        run_state.record(
            source_id=config.source.source_id,
            schema=table.schema_name,
            table=table.name,
            content_hash_value=new_hash,
            outcome=outcome,
        )

        return (
            TableRunResult(
                qualified_name=qn,
                asset_rk=rk,
                outcome=outcome,
                native_column_comments_preserved=native_count,
                column_descriptions_generated_by_llm=column_fill,
                table_description_generated_by_llm=table_desc_generated,
                annotation_concepts_count=len(annotations.concepts) if annotations else 0,
                annotation_key_areas_count=len(annotations.key_areas) if annotations else 0,
                annotation_causal_relations_count=len(annotations.causal_relations) if annotations else 0,
                llm_calls=llm_calls,
                wall_time_seconds=time.perf_counter() - t0,
            ),
            mdl,
        )

    except Exception as exc:
        logger.exception("Failed processing %s", qn)
        return (
            TableRunResult(
                qualified_name=qn,
                asset_rk=rk,
                outcome="error",
                wall_time_seconds=time.perf_counter() - t0,
                error=str(exc),
            ),
            None,
        )


# ───────────────────────────────────────────────────────────────────────────
# Filtering
# ───────────────────────────────────────────────────────────────────────────

def _build_profiler(config: PipelineConfig) -> Any | None:
    """Build the foundry-backed TableProfiler if column stats are enabled.

    Dispatches on `source.kind`:
      - 'postgres':    wraps a psycopg sample loader using `source.connection`.
      - 'local_files': wraps a `CsvSampleLoader` over `source.local.data_dir`.

    Returns None when the relevant optional deps aren't installed OR when
    the source config doesn't include enough info to sample (e.g. local
    source with no `data_dir`). The pipeline falls through gracefully.
    """
    try:
        from ontology_pipeline.profile import TableProfiler
    except ImportError as exc:
        logger.warning("compute_column_stats=True but profile module unavailable: %s", exc)
        return None

    try:
        if config.source.kind == "postgres":
            if config.source.connection is None:
                return None
            source_dsn = config.source.connection.dsn()
            source_id = config.source.source_id

            def dsn_for(sid: str) -> str:
                if sid != source_id:
                    raise ValueError(
                        f"profiler dsn_for: only source_id={source_id!r} is "
                        f"configured; got {sid!r}"
                    )
                return source_dsn

            return TableProfiler(
                dsn_for=dsn_for,
                sample_limit=config.pipeline.column_stats_sample_limit,
            )

        if config.source.kind == "local_files":
            from ontology_pipeline.profile import build_csv_sample_loader
            local = config.source.local
            if local is None or local.data_dir is None:
                logger.info(
                    "compute_column_stats=True with local_files source but no "
                    "data_dir provided; skipping profile pre-pass.",
                )
                return None
            return TableProfiler(
                sample_loader=build_csv_sample_loader(local.data_dir),
                sample_limit=config.pipeline.column_stats_sample_limit,
            )

        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning("TableProfiler init failed: %s", exc)
        return None


def _persist_aggregates(
    *,
    sink: Sink,
    source_id: str,
    schema: str,
    table_name: str,
    table_rk: str,
    bundle: Any,
    column_rk_by_name: dict[str, str],
) -> None:
    """Phase 1 of stats persistence: scalar aggregates (always safe)."""
    from ontology_pipeline.profile import bundle_to_aggregates
    aggregates = bundle_to_aggregates(
        table_rk=table_rk, bundle=bundle, column_rk_by_name=column_rk_by_name,
    )
    if not aggregates:
        return
    try:
        sink.write_table_aggregates(
            source_id=source_id, schema=schema, table=table_name,
            table_rk=table_rk, aggregates=aggregates,
            population_row_count=bundle.population_row_count,
            source_system=bundle.source_system,
        )
    except AttributeError:
        # Older sinks without the stats methods — silently skip.
        logger.debug("Sink does not implement write_table_aggregates; skipping")


def _persist_pii_gated_samples(
    *,
    sink: Sink,
    source_id: str,
    schema: str,
    table_name: str,
    table_rk: str,
    bundle: Any,
    column_rk_by_name: dict[str, str],
    enrichment_results: list[EnrichmentResult],
    mdl: GeneratedMDL,
) -> None:
    """Phase 2 of stats persistence: samples + top-k for PII-cleared columns.

    The gate is the complement of the data_protection enricher's flagged set.
    When data_protection didn't run, NO column is considered cleared — samples
    are withheld by default. Operators flip the gate later via the DAO's
    `attach_sampled_values` (or by configuring the data_protection stage).
    """
    from ontology_pipeline.profile import (
        bundle_to_table_facts,
        bundle_to_top_frequencies,
    )

    # Compute the set of column_rks flagged PII. Multiple signals contribute:
    #  - data_protection enricher's side_output["cls_columns"]
    #  - Per-column properties.is_pii=True on the MDL itself
    flagged_columns: set[str] = set()
    for r in enrichment_results:
        if r.stage_name == "data_protection":
            hints = r.side_output.get("data_protection_hints") or {}
            cls_cols = hints.get("cls_columns") or []
            for col_name in cls_cols:
                rk = column_rk_by_name.get(col_name)
                if rk:
                    flagged_columns.add(rk)
    if mdl.models:
        for col in mdl.models[0].columns:
            if getattr(col.properties, "is_pii", False):
                if col.rk:
                    flagged_columns.add(col.rk)

    # PII-safe = every column in the MDL that wasn't flagged.
    pii_safe_column_rks: set[str] = {
        rk for rk in column_rk_by_name.values() if rk not in flagged_columns
    }

    # Redact sample_rows: drop keys whose column was flagged.
    flagged_column_names: set[str] = {
        name for name, rk in column_rk_by_name.items() if rk in flagged_columns
    }
    table_facts = bundle_to_table_facts(table_rk=table_rk, bundle=bundle)
    if flagged_column_names:
        table_facts.sample_rows = [
            {k: v for k, v in row.items() if k not in flagged_column_names}
            for row in table_facts.sample_rows
        ]

    top_frequencies = bundle_to_top_frequencies(
        bundle=bundle, column_rk_by_name=column_rk_by_name,
    )
    try:
        sink.write_table_samples(
            source_id=source_id, schema=schema, table=table_name,
            table_facts=table_facts,
            column_top_frequencies=top_frequencies,
            pii_safe_column_rks=pii_safe_column_rks,
        )
    except AttributeError:
        logger.debug("Sink does not implement write_table_samples; skipping")


def _filter_tables(tables: list[TableInfo], flt: TableFilter) -> list[TableInfo]:
    """Apply include/exclude rules. If no rules configured, return all."""
    if not flt.is_configured():
        return list(tables)

    out: list[TableInfo] = []
    include_set = set(flt.include)
    exclude_set = set(flt.exclude)
    for t in tables:
        name = t.name
        qn = t.qualified_name

        # exclusion wins over inclusion
        if name in exclude_set or qn in exclude_set:
            continue
        if any(fnmatch.fnmatch(name, p) for p in flt.exclude_patterns):
            continue

        # inclusion: if any include rule is set, table must match one
        has_include_rules = bool(flt.include or flt.include_patterns)
        if has_include_rules:
            matched = (
                name in include_set
                or qn in include_set
                or any(fnmatch.fnmatch(name, p) for p in flt.include_patterns)
            )
            if not matched:
                continue

        out.append(t)
    return out


# ───────────────────────────────────────────────────────────────────────────
# LLM provider construction
# ───────────────────────────────────────────────────────────────────────────

# ───────────────────────────────────────────────────────────────────────────
# Enrichment composition
# ───────────────────────────────────────────────────────────────────────────

def _run_relation_induction(
    *,
    relationships: list[dict[str, Any]],
    concept_index: dict[str, str],
    config: PipelineConfig,
    provider: ModelProvider,
    sink: Sink,
) -> int:
    """Foundry-backed predicate canonicalization post-pass.

    Runs `ontology_foundry.relations.induce_schema` over the relationships
    accumulated during the per-table loop. Each `relationship` is converted
    to a foundry `RelationArtifact` whose `subject_type` / `object_type`
    come from the assets' primary concept (via `concept_index`).

    The induced TBox is persisted via `sink.write_relation_schema`. For each
    induced predicate, the underlying lineage_edge rows that contributed to
    it are linked back via `predicate_id`.

    Returns the LLM-call count from `induce_schema` (1 — single canonicalization).
    """
    try:
        from ontology_foundry.models import RelationArtifact
        from ontology_foundry.relations import (
            SeedPack,
            induce_schema,
        )
    except ImportError as exc:
        logger.warning(
            "induce_relation_schema=True but foundry.relations unavailable (%s); skipping",
            exc,
        )
        return 0

    # Build foundry RelationArtifacts from the captured per-table side_outputs.
    edges: list[RelationArtifact] = []
    edge_meta: list[dict[str, Any]] = []  # parallel — used during attach
    for rel in relationships:
        subj_rk = rel["from_rk"]
        obj_rk = rel["to_rk"]
        subj_type = concept_index.get(subj_rk) or "asset"
        obj_type = concept_index.get(obj_rk) or "asset"
        edges.append(RelationArtifact(
            subject_ref=subj_rk,
            predicate=rel["predicate_surface"],
            object_ref=obj_rk,
            subject_type=subj_type,
            object_type=obj_type,
            chunk_id="pipeline_inferred",
            confidence=rel["confidence"],
            evidence_text=rel.get("reason"),
            source="inferred_relationships",
        ))
        edge_meta.append({
            "from_rk": subj_rk, "to_rk": obj_rk,
            "edge_kind": rel.get("edge_kind", "depends_on"),
            "surface": rel["predicate_surface"],
            "subject_type": subj_type, "object_type": obj_type,
            # Surface metadata for the event narrative on attach. Only the
            # from-side surface is captured by the per-table enricher — the
            # to-side wasn't visible at enrichment time. The event narrative
            # will fall back to rk for the to-side when this is None.
            "from_one_liner": rel.get("from_one_liner"),
            "from_asset_surface": rel.get("from_asset_surface"),
            "from_column": rel.get("from_column"),
            "from_column_brief": rel.get("from_column_brief"),
            "to_column": rel.get("to_column"),
            "to_column_brief": rel.get("to_column_brief"),
        })

    if not edges:
        return 0

    # Empty seed pack works — the canonicalizer just synthesizes snake_case
    # canonical names from the surfaces. Tenants can later author a SeedPack
    # to bias toward their authored predicate vocabulary.
    seeds = SeedPack(name="pipeline-inferred-default")
    try:
        schema, induced = induce_schema(
            edges, provider, seeds,
            min_support=config.pipeline.relation_induction_min_support,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("induce_schema failed: %s", exc)
        return 0

    if not induced:
        logger.info(
            "induce_schema produced no rows above min_support=%d (edges=%d)",
            config.pipeline.relation_induction_min_support, len(edges),
        )
        return 1  # LLM was called once for canonicalization

    # Build the canonical surface→canonical map so we can attach each edge
    # to the right predicate row.
    surface_to_canonical: dict[str, str] = {}
    for p in induced:
        for surf in p.surfaces:
            surface_to_canonical[surf] = p.canonical

    # Prepare DAO-shaped inputs.
    from ontology_store.dao import RelationTypeIn

    types: list[RelationTypeIn] = []
    canonical_to_domain_range: dict[str, tuple[str, str]] = {}
    for p in induced:
        domain = p.dominant_domain() or "Thing"
        range_type = p.dominant_range() or "Thing"
        types.append(RelationTypeIn(
            predicate=p.canonical,
            domain=domain,
            range_type=range_type,
            confidence=p.avg_confidence,
            evidence_count=p.support,
            surfaces=list(p.surfaces),
            provenance="induce_schema",
        ))
        canonical_to_domain_range[p.canonical] = (domain, range_type)

    # Walk every contributing edge and prepare an attachment record.
    attachments: list[dict[str, Any]] = []
    for meta in edge_meta:
        canonical = surface_to_canonical.get(meta["surface"])
        if canonical is None:
            continue
        dom_range = canonical_to_domain_range.get(canonical)
        if dom_range is None:
            continue
        # Only attach when the edge's actual (subject_type, object_type) matches
        # the canonical predicate's dominant domain/range. This prevents an
        # off-type edge from being mis-linked to a predicate that "looks right".
        if (meta["subject_type"], meta["object_type"]) != dom_range:
            continue
        attachments.append({
            "from_rk": meta["from_rk"],
            "to_rk": meta["to_rk"],
            "edge_kind": meta["edge_kind"],
            "predicate": canonical,
            "domain": dom_range[0],
            "range_type": dom_range[1],
            # Surface fields used by the event narrative builder when emitting
            # PREDICATE_ATTACHED_TO_EDGE events. Pass through whatever the
            # per-table enricher captured for the from-side; to-side stays
            # None (rk fallback in the builder).
            "from_one_liner": meta.get("from_one_liner"),
            "from_asset_surface": meta.get("from_asset_surface"),
            "to_one_liner": None,
            "to_asset_surface": None,
            # FK columns + their full briefs (type + native description).
            "from_column": meta.get("from_column"),
            "from_column_brief": meta.get("from_column_brief"),
            "to_column": meta.get("to_column"),
            "to_column_brief": meta.get("to_column_brief"),
        })

    try:
        sink.write_relation_schema(
            source_id=config.source.source_id,
            types=types,
            attachments=attachments,
        )
    except AttributeError:
        logger.debug("Sink does not implement write_relation_schema; skipping")

    logger.info(
        "Relation induction: %d edges → %d predicates → %d edge attachments",
        len(edges), len(types), len(attachments),
    )
    return 1  # one LLM call for canonicalization


def _run_cross_asset_causal(
    *,
    built_mdls: list[GeneratedMDL],
    config: PipelineConfig,
    vocab: SemanticVocab,
    provider: ModelProvider | None,
    sink: Sink,
    introspection_catalog: str | None,
) -> int:
    """Execute the cross-asset causal pass over the run's accumulated MDLs.

    Returns LLM call count. Routes resulting causal_candidates through the sink
    using a synthetic schema/table label so filesystem outputs are per-cluster
    and the DB writer just batches them by candidate `asset_rk`.
    """
    if provider is None:
        logger.info("Skipping cross_asset_causal — no LLM provider")
        return 0

    cluster_ctx = ClusterContext(
        source_id=config.source.source_id,
        provider=provider,
        llm_model_id=config.llm.model,
        known_causal_node_ids=[c.id for c in vocab.causal_nodes],
        known_causal_node_excerpts={c.id: c.body_excerpt for c in vocab.causal_nodes},
    )
    enricher = CrossAssetCausalEnricher(
        max_cluster_size=config.pipeline.cross_asset_cluster_max_size,
    )
    results = enricher.apply_all(built_mdls, cluster_ctx)
    llm_calls = sum(r.llm_calls for r in results)

    # Route candidates per-cluster
    for res in results:
        if res.side_output.get("causal_candidates"):
            candidates = res.side_output["causal_candidates"]
            cluster_key = candidates[0].get("cluster_key", "unknown")
            schema_label = "_cross_asset"
            table_label = _sanitize_for_path(cluster_key)
            try:
                sink.write_causal_candidates(
                    source_id=config.source.source_id,
                    schema=schema_label,
                    table=table_label,
                    candidates=candidates,
                )
                logger.info(
                    "Routed %d cross-asset causal candidate(s) for cluster %s",
                    len(candidates), cluster_key,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to route cross-asset causal candidates for cluster %s: %s",
                    cluster_key, exc,
                )

    if results:
        logger.info(
            "cross_asset_causal: %d cluster(s) examined, %d total LLM call(s)",
            len(results), llm_calls,
        )
    return llm_calls


def _sanitize_for_path(s: str) -> str:
    """Make a cluster_key filesystem-safe."""
    bad = (" ", "/", "\\", ":", "*", "?", "\"", "<", ">", "|", "=")
    out = s
    for ch in bad:
        out = out.replace(ch, "_")
    return out


def _build_enrichment_stages(
    config: PipelineConfig, *, vocab: SemanticVocab,
) -> list[EnrichmentStage]:
    """Order matters — descriptions before semantics before data_protection
    before relationship inference before causal. Each gated by its own
    config flag. The causal stage uses the tenant card vocab as its
    controlled causal_node id set."""
    stages: list[EnrichmentStage] = []
    if config.pipeline.rich_description:
        stages.append(RichDescriptionEnricher())
    if config.pipeline.enrich_column_semantics:
        stages.append(ColumnSemanticsEnricher())
    if config.pipeline.enrich_data_protection:
        stages.append(DataProtectionEnricher())
    if config.pipeline.infer_relationships:
        stages.append(RelationshipInferenceEnricher())
    if config.pipeline.enrich_causal_dependencies:
        known_ids = [c.id for c in vocab.causal_nodes]
        excerpts = {c.id: c.body_excerpt for c in vocab.causal_nodes}
        stages.append(CausalDependencyEnricher(
            known_causal_node_ids=known_ids,
            known_causal_node_excerpts=excerpts,
            propose_new_causal_nodes=config.pipeline.propose_new_causal_nodes,
        ))
    return stages


def _route_enrichment_side_output(
    *, table, side_output: dict, sink, source_id: str, asset_rk: str,
) -> None:
    """Route per-stage side outputs through the sink.

    Sinks may implement none / some / all of the side-output methods. For
    `FilesystemSink`, all methods write JSON files. For `HierarchyStoreSink`,
    they call InferenceDAO to persist into `lineage_edge` (inferred FKs),
    `causal_candidate`, or `data_protection_hint`. For `TeeSink`, fans out
    to every contained sink.

    Failures are caught + logged at the per-route level; one bad route does
    not prevent the others.
    """
    if "inferred_relationships" in side_output:
        rels = side_output["inferred_relationships"]
        if rels:
            try:
                sink.write_inferred_relationships(
                    source_id=source_id,
                    schema=table.schema_name,
                    table=table.name,
                    from_table_rk=asset_rk,
                    items=rels,
                )
                logger.info(
                    "Routed %d inferred relationship(s) for %s through sink.",
                    len(rels), table.qualified_name,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to route inferred relationships for %s: %s",
                    table.qualified_name, exc,
                )

    if "data_protection_hints" in side_output:
        hints = side_output["data_protection_hints"]
        if hints:
            try:
                sink.write_data_protection_hints(
                    source_id=source_id,
                    schema=table.schema_name,
                    table=table.name,
                    asset_rk=asset_rk,
                    hints=hints,
                )
                logger.info(
                    "Routed data_protection hints for %s through sink: "
                    "%d RLS predicates, %d CLS columns",
                    table.qualified_name,
                    len(hints.get("rls_predicates", [])),
                    len(hints.get("cls_columns", [])),
                )
            except Exception as exc:
                logger.warning(
                    "Failed to route data_protection hints for %s: %s",
                    table.qualified_name, exc,
                )

    if "causal_candidates" in side_output:
        candidates = side_output["causal_candidates"]
        if candidates:
            try:
                sink.write_causal_candidates(
                    source_id=source_id,
                    schema=table.schema_name,
                    table=table.name,
                    candidates=candidates,
                )
                avg_conf = (
                    sum(c.get("confidence") or 0.0 for c in candidates) / len(candidates)
                )
                logger.info(
                    "Routed %d causal candidate(s) for %s through sink (avg confidence %.2f).",
                    len(candidates), table.qualified_name, avg_conf,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to route causal candidates for %s: %s",
                    table.qualified_name, exc,
                )

    if "proposed_causal_node_drafts" in side_output:
        drafts = side_output["proposed_causal_node_drafts"]
        # No DB target yet; logged so operators can spot them in run output.
        logger.info(
            "Proposed %d causal_node draft(s) for %s — review-queue persistence pending.",
            len(drafts), table.qualified_name,
        )


def _build_llm_provider(cfg: LLMConfig) -> ModelProvider | None:
    """Build an LLM provider. Returns None if it cannot be constructed."""
    if cfg.provider != "openai":
        logger.warning("LLM provider %r not supported in v1", cfg.provider)
        return None
    import os

    api_key = os.environ.get(cfg.api_key_env)
    if not api_key:
        logger.warning("Env var %s is not set; LLM behaviors will be skipped", cfg.api_key_env)
        return None
    base_url = os.environ.get(cfg.base_url_env, cfg.base_url_default)
    try:
        return OpenAIChatProvider(model=cfg.model, api_key=api_key, base_url=base_url)
    except Exception as exc:
        logger.warning("Could not build OpenAIChatProvider (%s); LLM behaviors disabled", exc)
        return None
