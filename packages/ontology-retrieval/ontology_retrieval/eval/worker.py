"""EvalWorker — background runner that drives `eval_run` rows to completion.

Lifecycle of one run:
  1. Pull row in `pending` status; mark `running`.
  2. Load eligible eval_case rows (filtered by `case_filter`).
  3. For each case:
       a. Call `pipeline.run(retrieval_kind, query=case.question, scope=case.scope_payload)`.
       b. For each scorer:
            scorer.score(...) → ScorerResult
            persist EvalResult.
  4. Aggregate metrics across cases → write EvalMetric rows.
  5. Mark `done`. On exception: mark `failed` and stash the error.

The worker accepts a `RetrievalPipeline` instance + a list of scorers. Stateless
per-run; safe to run multiple workers concurrently against the same Postgres
(each pulls a disjoint run via `FOR UPDATE SKIP LOCKED`).
"""
from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select, text as sql_text, update
from sqlalchemy.orm import Session

from ontology_store import Database
from ontology_store.db import EvalCase, EvalMetric, EvalResult, EvalRun

from ontology_retrieval.eval.scorers import (
    HistoricalComparisonScorer,
    RetrievedItem,
    Scorer,
    ScorerResult,
)

logger = logging.getLogger(__name__)


@dataclass
class EvalWorkerStats:
    runs_processed: int = 0
    runs_succeeded: int = 0
    runs_failed: int = 0
    cases_evaluated: int = 0


class EvalWorker:
    """Polls `eval_run` for pending rows and executes them.

    Args:
        database: ontology-store Database.
        pipeline: a RetrievalPipeline (or compatible object with `async run(kind, **kwargs)`).
        scorers:  list of Scorer instances. Order is preserved in per-case results.
        async_runner: how to invoke the pipeline. Default uses `asyncio.run`; for
                      callers already inside an event loop, pass an alternative.
    """

    def __init__(
        self,
        *,
        database: Database,
        pipeline: Any,
        scorers: list[Scorer] | None = None,
        async_runner: Any = None,
    ) -> None:
        self.db = database
        self.pipeline = pipeline
        self.scorers = scorers or [HistoricalComparisonScorer()]
        # asyncio import is lazy so this module doesn't pull in a runtime dep
        # when used purely synchronously in tests.
        self._async_runner = async_runner

    # ── Public entrypoints ─────────────────────────────────────────────

    def run_pending(self, *, limit: int = 5) -> EvalWorkerStats:
        """Process up to `limit` pending runs. Returns aggregate stats."""
        stats = EvalWorkerStats()
        run_ids = self._claim_runs(limit=limit)
        for run_id in run_ids:
            stats.runs_processed += 1
            try:
                cases_n = self.execute_run(run_id)
                stats.cases_evaluated += cases_n
                stats.runs_succeeded += 1
            except Exception as exc:
                logger.exception("EvalWorker: run %d failed: %s", run_id, exc)
                self._mark_failed(run_id, str(exc))
                stats.runs_failed += 1
        return stats

    def execute_run(self, run_id: int) -> int:
        """Execute one specific run by id. Returns the case count processed."""
        with self.db.session() as session:
            run = session.get(EvalRun, run_id)
            if run is None:
                raise ValueError(f"eval_run id={run_id} not found")
            cases = self._load_cases(session, case_filter=run.case_filter or {})
            run.case_count = len(cases)

        passed = 0
        for case in cases:
            try:
                results = self._evaluate_case(run_id=run_id, run_retrieval_kind=run.retrieval_kind, case=case)
                # A case "passes" when ALL scorers report pass_gate=True (when set).
                gate_results = [r.pass_gate for r in results if r.pass_gate is not None]
                if gate_results and all(gate_results):
                    passed += 1
            except Exception as exc:
                logger.exception("EvalWorker: case %s failed: %s", case.case_id, exc)
                self._persist_error_result(run_id, case.case_id, str(exc))

        # Roll up aggregates
        self._aggregate_metrics(run_id=run_id)

        with self.db.session() as session:
            run = session.get(EvalRun, run_id)
            if run is not None:
                run.passed_count = passed
                run.status = "done"
                run.finished_at = datetime.now(timezone.utc)
        logger.info("EvalWorker: run %d done — %d/%d cases passed", run_id, passed, len(cases))
        return len(cases)

    # ── Per-case orchestration ─────────────────────────────────────────

    def _evaluate_case(
        self,
        *,
        run_id: int,
        run_retrieval_kind: str,
        case: EvalCase,
    ) -> list[ScorerResult]:
        retrieval_kind = case.retrieval_kind_default or run_retrieval_kind
        # 1. Retrieve via the pipeline
        t0 = time.perf_counter()
        scope = dict(case.scope_payload or {})
        retrieved_items = self._do_retrieve(
            kind=retrieval_kind, query=case.question, scope=scope,
        )
        elapsed_ms = int((time.perf_counter() - t0) * 1000)

        # 2. Score with each configured scorer
        scorer_results: list[ScorerResult] = []
        for scorer in self.scorers:
            t1 = time.perf_counter()
            result = scorer.score(
                question=case.question,
                retrieved=retrieved_items,
                expected_rks=list(case.expected_asset_rks or []),
                forbidden_rks=list(case.forbidden_asset_rks or []),
                expected_anchors=list(case.expected_anchors or []),
                k=10,
            )
            scorer_elapsed = int((time.perf_counter() - t1) * 1000)
            scorer_results.append(result)
            # 3. Persist EvalResult row
            self._persist_result(
                run_id=run_id, case=case, scorer=scorer.name,
                result=result, retrieved_rks=[it.rk for it in retrieved_items],
                wall_time_ms=elapsed_ms + scorer_elapsed,
            )
        return scorer_results

    # ── Retrieval invocation ───────────────────────────────────────────

    def _do_retrieve(self, *, kind: str, query: str, scope: dict[str, Any]) -> list[RetrievedItem]:
        """Call pipeline.run(kind, ...) and normalize hits to RetrievedItem."""
        import asyncio

        async def _call() -> Any:
            return await self.pipeline.run(kind, query=query, scope=scope, k=10)

        if self._async_runner is None:
            try:
                result = asyncio.run(_call())
            except RuntimeError as exc:
                # Already inside an event loop — let the caller supply a runner.
                raise RuntimeError(
                    "EvalWorker._do_retrieve called from inside an event loop; "
                    "supply async_runner= at construction."
                ) from exc
        else:
            result = self._async_runner(_call())

        data = result.data if hasattr(result, "data") else result.get("data")
        if data is None:
            return []
        if isinstance(data, list):
            return [RetrievedItem.from_asset_hit(h) for h in data if isinstance(h, dict)]
        # Single-item kind (asset_by_rk) — still wrap as one-item list
        if isinstance(data, dict):
            return [RetrievedItem.from_asset_hit(data)]
        return []

    # ── Persistence ────────────────────────────────────────────────────

    def _persist_result(
        self,
        *,
        run_id: int,
        case: EvalCase,
        scorer: str,
        result: ScorerResult,
        retrieved_rks: list[str],
        wall_time_ms: int,
    ) -> None:
        with self.db.session() as session:
            session.add(EvalResult(
                run_id=run_id,
                case_id=case.case_id,
                scorer_name=scorer,
                retrieved_rks=retrieved_rks,
                metrics={k: float(v) for k, v in result.metrics.items()},
                llm_judgment=result.llm_judgment,
                pass_gate=result.pass_gate,
                notes=result.notes,
                wall_time_ms=wall_time_ms,
            ))

    def _persist_error_result(self, run_id: int, case_id: str, err: str) -> None:
        with self.db.session() as session:
            session.add(EvalResult(
                run_id=run_id, case_id=case_id,
                scorer_name="_error",
                retrieved_rks=[], metrics={}, pass_gate=False,
                notes=f"exception: {err}",
            ))

    # ── Aggregation ────────────────────────────────────────────────────

    def _aggregate_metrics(self, *, run_id: int) -> None:
        """Compute per-(scorer, metric_name) aggregates and write EvalMetric rows."""
        with self.db.session() as session:
            rows = session.execute(
                select(EvalResult).where(EvalResult.run_id == run_id)
            ).scalars().all()
            grouped: dict[tuple[str, str], list[float]] = {}
            for r in rows:
                for metric_name, value in (r.metrics or {}).items():
                    try:
                        fval = float(value)
                    except (TypeError, ValueError):
                        continue
                    key = (r.scorer_name, metric_name)
                    grouped.setdefault(key, []).append(fval)

            for (scorer, name), values in grouped.items():
                if not values:
                    continue
                # Mean aggregation; can extend with median / p95 etc.
                mean_value = statistics.fmean(values)
                session.add(EvalMetric(
                    run_id=run_id,
                    scorer_name=scorer,
                    metric_name=f"mean_{name}",
                    metric_value=mean_value,
                    cardinality=len(values),
                ))

            # Also write a per-scorer pass-rate metric.
            for scorer_name in {r.scorer_name for r in rows}:
                scorer_rows = [r for r in rows if r.scorer_name == scorer_name]
                gated = [r.pass_gate for r in scorer_rows if r.pass_gate is not None]
                if not gated:
                    continue
                pass_rate = sum(1 for x in gated if x) / len(gated)
                session.add(EvalMetric(
                    run_id=run_id,
                    scorer_name=scorer_name,
                    metric_name="pass_rate",
                    metric_value=pass_rate,
                    cardinality=len(gated),
                ))

    # ── Run claim / lifecycle ──────────────────────────────────────────

    def _claim_runs(self, *, limit: int) -> list[int]:
        """Atomically pull pending runs; mark them `running`. Returns the run_ids."""
        sql = sql_text("""
            WITH next_batch AS (
                SELECT run_id FROM eval_run
                WHERE status = 'pending'
                ORDER BY started_at
                LIMIT :limit
                FOR UPDATE SKIP LOCKED
            )
            UPDATE eval_run
            SET status = 'running', started_at = now()
            FROM next_batch nb
            WHERE eval_run.run_id = nb.run_id
            RETURNING eval_run.run_id
        """)
        with self.db.session() as session:
            rows = session.execute(sql, {"limit": limit}).all()
            return [r[0] for r in rows]

    def _mark_failed(self, run_id: int, err: str) -> None:
        with self.db.session() as session:
            session.execute(
                update(EvalRun)
                .where(EvalRun.run_id == run_id)
                .values(status="failed", finished_at=datetime.now(timezone.utc), last_error=err[:2000])
            )

    # ── Case loading ───────────────────────────────────────────────────

    def _load_cases(self, session: Session, *, case_filter: dict[str, Any]) -> list[EvalCase]:
        stmt = select(EvalCase).where(EvalCase.enabled.is_(True))
        case_ids = case_filter.get("case_ids")
        if case_ids:
            stmt = stmt.where(EvalCase.case_id.in_(case_ids))
        hardness = case_filter.get("hardness")
        if hardness:
            if isinstance(hardness, str):
                hardness = [hardness]
            stmt = stmt.where(EvalCase.hardness.in_(hardness))
        domain_tags = case_filter.get("domain_tags")
        if domain_tags:
            stmt = stmt.where(EvalCase.domain_tags.overlap(domain_tags))
        return list(session.execute(stmt).scalars())
