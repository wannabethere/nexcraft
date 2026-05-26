"""Eval ORM models — eval_case, eval_run, eval_result, eval_metric.

Per the architecture decision: eval STORAGE lives here (ontology-store) so the
schema travels with the rest of the spine and migrations apply uniformly. The
actual SCORERS and WORKER live in ontology-retrieval (which depends on this
package) — the worker reads eval_case + eval_run rows, executes retrieval +
scoring, and writes eval_result + eval_metric rows.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, Text, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ontology_store.db.engine import Base


# ───────────────────────────────────────────────────────────────────────────
# eval_case — one curated evaluation question with ground truth
# ───────────────────────────────────────────────────────────────────────────

class EvalCase(Base):
    """One curated question with its expected outputs.

    `expected_asset_rks` is the ground-truth set the `historical_comparison`
    scorer measures retrieval against. `expected_anchors` carries the cards
    that should be hit when an LLM downstream uses the retrieved context.

    `scope_payload` is a `RetrievalScope` dict — captured here so an eval run
    against `asset_search` doesn't have to guess what scope to apply.
    """
    __tablename__ = "eval_case"

    case_id: Mapped[str] = mapped_column(Text, primary_key=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    intent: Mapped[str | None] = mapped_column(Text)
    expected_anchors: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    expected_asset_rks: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    forbidden_asset_rks: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    scope_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    retrieval_kind_default: Mapped[str | None] = mapped_column(Text)  # e.g. 'asset_search' | 'asset_vector_search'
    hardness: Mapped[str] = mapped_column(Text, nullable=False, default="medium")  # easy|medium|hard
    domain_tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    authored_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_eval_case_enabled", "enabled"),
        Index("idx_eval_case_hardness", "hardness"),
    )


# ───────────────────────────────────────────────────────────────────────────
# eval_run — one execution of the eval suite
# ───────────────────────────────────────────────────────────────────────────

class EvalRun(Base):
    """One execution of the eval suite (some subset of eval_case rows).

    `trigger`: 'manual' (operator CLI), 'scheduled' (cron), 'pre_release' (CI).
    `retrieval_kind`: the pipeline kind to call into.
    `scorer_names`: which scorers to run against retrieved results.
    """
    __tablename__ = "eval_run"

    run_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(Text, nullable=False, default="pending")  # pending|running|done|failed
    retrieval_kind: Mapped[str] = mapped_column(Text, nullable=False)
    scorer_names: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    case_filter: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    case_count: Mapped[int | None] = mapped_column(Integer)
    passed_count: Mapped[int | None] = mapped_column(Integer)
    trigger: Mapped[str] = mapped_column(Text, nullable=False, default="manual")
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB)
    last_error: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        Index("idx_eval_run_status", "status"),
        Index("idx_eval_run_started", "started_at"),
    )


# ───────────────────────────────────────────────────────────────────────────
# eval_result — per (run × case × scorer)
# ───────────────────────────────────────────────────────────────────────────

class EvalResult(Base):
    """One scoring outcome: a case's retrieval scored by one scorer in one run.

    `retrieved_rks` — what the retriever actually returned, in rank order.
    `metrics`      — scorer-specific structured payload (P@k, R@k, MRR, ...)
    `llm_judgment` — for LLM judge: per-item ratings + holistic rating + rationale.
    `pass_gate`    — whether this case passed the run's pass threshold for this scorer.
    """
    __tablename__ = "eval_result"

    result_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_run.run_id", ondelete="CASCADE"), nullable=False,
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("eval_case.case_id", ondelete="CASCADE"), nullable=False,
    )
    scorer_name: Mapped[str] = mapped_column(Text, nullable=False)
    retrieved_rks: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    llm_judgment: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pass_gate: Mapped[bool | None] = mapped_column(Boolean)
    notes: Mapped[str | None] = mapped_column(Text)
    wall_time_ms: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_eval_result_run", "run_id"),
        Index("idx_eval_result_case", "case_id"),
        Index("idx_eval_result_scorer", "scorer_name"),
    )


# ───────────────────────────────────────────────────────────────────────────
# eval_metric — aggregates per run × scorer
# ───────────────────────────────────────────────────────────────────────────

class EvalMetric(Base):
    """A rolled-up aggregate. One row per (run, scorer, metric_name) combination.

    Examples:
      run_id=42, scorer='historical_comparison', metric_name='mean_precision_at_5', value=0.74
      run_id=42, scorer='historical_comparison', metric_name='mrr', value=0.81
      run_id=42, scorer='llm_judge', metric_name='judge_mean_score', value=4.2
      run_id=42, scorer='llm_judge', metric_name='judge_coverage_rate', value=0.92
    """
    __tablename__ = "eval_metric"

    metric_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(
        ForeignKey("eval_run.run_id", ondelete="CASCADE"), nullable=False,
    )
    scorer_name: Mapped[str | None] = mapped_column(Text)
    metric_name: Mapped[str] = mapped_column(Text, nullable=False)
    metric_value: Mapped[float | None] = mapped_column(Float)
    cardinality: Mapped[int | None] = mapped_column(Integer)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        Index("idx_eval_metric_run", "run_id"),
        Index("idx_eval_metric_name", "metric_name"),
    )
