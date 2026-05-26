"""Eval module tests — metrics + scorers (no live DB / LLM required).

End-to-end worker tests against live Postgres are gated on
ONTOLOGY_STORE_TEST_URL; the worker is driven with a stub pipeline + stub LLM
provider that we control here.
"""
from __future__ import annotations

import json
import os
from typing import Any

import pytest

from ontology_retrieval.eval.metrics import (
    forbidden_violations,
    hit_rate,
    mean_reciprocal_rank,
    ndcg,
    precision_at_k,
    recall_at_k,
)
from ontology_retrieval.eval.scorers import (
    HistoricalComparisonScorer,
    LLMJudgeScorer,
    RetrievedItem,
)


# ───────────────────────────────────────────────────────────────────────────
# Pure metric tests
# ───────────────────────────────────────────────────────────────────────────

class TestPrecisionAtK:
    def test_all_relevant_in_top_k(self) -> None:
        assert precision_at_k(["a", "b", "c"], relevant=["a", "b", "c"], k=3) == 1.0

    def test_partial(self) -> None:
        assert precision_at_k(["a", "x", "b"], relevant=["a", "b"], k=3) == pytest.approx(2 / 3)

    def test_empty_retrieved(self) -> None:
        assert precision_at_k([], relevant=["a"], k=5) == 0.0

    def test_k_zero(self) -> None:
        assert precision_at_k(["a"], relevant=["a"], k=0) == 0.0


class TestRecallAtK:
    def test_full_recall(self) -> None:
        assert recall_at_k(["a", "b", "c"], relevant=["a", "b"], k=3) == 1.0

    def test_partial_recall(self) -> None:
        # 1 of 2 expected items found in top-2
        assert recall_at_k(["a", "x"], relevant=["a", "b"], k=2) == 0.5

    def test_empty_relevant_set(self) -> None:
        assert recall_at_k(["a", "b"], relevant=[], k=5) == 0.0


class TestMRR:
    def test_first_position(self) -> None:
        assert mean_reciprocal_rank(["a", "b", "c"], relevant=["a"]) == 1.0

    def test_second_position(self) -> None:
        assert mean_reciprocal_rank(["x", "a"], relevant=["a"]) == 0.5

    def test_not_found(self) -> None:
        assert mean_reciprocal_rank(["x", "y"], relevant=["a"]) == 0.0

    def test_empty_relevant(self) -> None:
        assert mean_reciprocal_rank(["a"], relevant=[]) == 0.0


class TestNDCG:
    def test_perfect_ranking(self) -> None:
        # All relevant items at the top
        assert ndcg(["a", "b", "c"], relevant=["a", "b", "c"]) == pytest.approx(1.0)

    def test_worst_ranking(self) -> None:
        # No retrieved items match relevant → 0
        assert ndcg(["x", "y"], relevant=["a", "b"]) == 0.0

    def test_graded_relevance(self) -> None:
        # a=2 (highly relevant), b=1 (partial), c=2 (highly) — order [a,c,b] should be ideal
        actual = ndcg(["a", "b", "c"], relevance_grades={"a": 2, "b": 1, "c": 2})
        # actual ranks a,b,c → less than ideal (which would put both 2s first)
        assert 0.0 < actual <= 1.0


class TestHitRate:
    def test_any_relevant_present(self) -> None:
        assert hit_rate(["x", "a", "y"], relevant=["a"]) == 1.0

    def test_none_relevant(self) -> None:
        assert hit_rate(["x", "y"], relevant=["a"]) == 0.0


class TestForbiddenViolations:
    def test_counts_forbidden_hits(self) -> None:
        assert forbidden_violations(["a", "b", "c"], forbidden=["b"]) == 1
        assert forbidden_violations(["b", "b"], forbidden=["b"]) == 2

    def test_no_forbidden(self) -> None:
        assert forbidden_violations(["a", "b"], forbidden=[]) == 0


# ───────────────────────────────────────────────────────────────────────────
# HistoricalComparisonScorer behavior
# ───────────────────────────────────────────────────────────────────────────

def _retrieved(*rks: str) -> list[RetrievedItem]:
    return [RetrievedItem(rk=r, name=r) for r in rks]


class TestHistoricalComparisonScorer:
    def test_perfect_retrieval_passes(self) -> None:
        scorer = HistoricalComparisonScorer()
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("a", "b"),
            expected_rks=["a", "b"],
        )
        assert result.scorer_name == "historical_comparison"
        assert result.pass_gate is True
        assert result.metrics["recall_at_5"] == 1.0
        assert result.metrics["mrr"] == 1.0
        assert result.metrics["forbidden_violations"] == 0.0

    def test_no_hit_fails_default_gate(self) -> None:
        scorer = HistoricalComparisonScorer()
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("x", "y"),
            expected_rks=["a"],
        )
        assert result.pass_gate is False
        assert "no expected rk" in result.notes

    def test_forbidden_hit_fails(self) -> None:
        scorer = HistoricalComparisonScorer()
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("a", "b_forbidden"),
            expected_rks=["a"],
            forbidden_rks=["b_forbidden"],
        )
        assert result.pass_gate is False
        assert "forbidden" in result.notes

    def test_recall_threshold_enforced(self) -> None:
        scorer = HistoricalComparisonScorer(pass_min_recall_at_5=0.9)
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("a"),  # only 1 of 3 expected
            expected_rks=["a", "b", "c"],
        )
        assert result.pass_gate is False
        assert "recall_at_5" in result.notes


# ───────────────────────────────────────────────────────────────────────────
# LLMJudgeScorer with a stub LLM provider
# ───────────────────────────────────────────────────────────────────────────

class _StubProvider:
    """Minimal ModelProvider stand-in that returns a canned JSON judgment."""

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response

    def complete(self, role: Any, prompt: str, *, response_format: Any = None) -> str:
        return json.dumps(self._response)


class TestLLMJudgeScorer:
    def test_judgment_aggregates_metrics(self) -> None:
        provider = _StubProvider({
            "per_item": [
                {"rk": "a", "rating": 2, "rationale": "directly relevant"},
                {"rk": "b", "rating": 1, "rationale": "tangentially related"},
                {"rk": "c", "rating": 0, "rationale": "unrelated"},
            ],
            "coverage_rating": 4,
            "missing_concepts": [],
            "rationale": "Solid coverage with one weak item.",
        })
        scorer = LLMJudgeScorer(provider=provider, role="validator")
        result = scorer.score(
            question="What about employee training?",
            retrieved=_retrieved("a", "b", "c"),
            expected_rks=["a"],
            expected_anchors=["employee", "training_assignment"],
        )
        assert result.scorer_name == "llm_judge"
        assert result.metrics["judge_mean_rating"] == pytest.approx((2 + 1 + 0) / 3)
        assert result.metrics["judge_relevant_rate"] == pytest.approx(1 / 3)
        assert result.metrics["judge_partial_rate"] == pytest.approx(1 / 3)
        assert result.metrics["judge_coverage_rating"] == 4.0
        assert result.metrics["judge_coverage_rate"] == pytest.approx(4 / 5)
        assert result.pass_gate is True

    def test_low_coverage_fails_gate(self) -> None:
        provider = _StubProvider({
            "per_item": [
                {"rk": "a", "rating": 0, "rationale": "off-topic"},
                {"rk": "b", "rating": 1, "rationale": "tangent"},
            ],
            "coverage_rating": 1,
            "missing_concepts": ["employee"],
            "rationale": "Misses the anchor concept.",
        })
        scorer = LLMJudgeScorer(provider=provider, role="validator")
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("a", "b"),
            expected_rks=["x"],
        )
        assert result.pass_gate is False
        assert result.llm_judgment is not None
        assert result.llm_judgment["missing_concepts"] == ["employee"]

    def test_no_retrieved_items_short_circuits(self) -> None:
        scorer = LLMJudgeScorer(provider=_StubProvider({}), role="validator")
        result = scorer.score(
            question="Q?",
            retrieved=[],
            expected_rks=["a"],
        )
        assert result.pass_gate is False
        assert result.metrics["judge_items_rated"] == 0.0
        assert "no retrieved items" in result.notes

    def test_llm_invocation_failure_returns_failed_result(self) -> None:
        class _FailingProvider:
            def complete(self, *a, **kw):
                raise RuntimeError("API down")

        scorer = LLMJudgeScorer(provider=_FailingProvider(), role="validator")
        result = scorer.score(
            question="Q?",
            retrieved=_retrieved("a"),
            expected_rks=["a"],
        )
        assert result.pass_gate is False
        assert "llm error" in result.notes


# ───────────────────────────────────────────────────────────────────────────
# End-to-end worker — gated on Postgres
# ───────────────────────────────────────────────────────────────────────────

_LIVE_PG = bool(os.environ.get("ONTOLOGY_STORE_TEST_URL"))


@pytest.mark.skipif(not _LIVE_PG, reason="Set ONTOLOGY_STORE_TEST_URL to run live worker tests")
class TestEvalWorkerE2E:
    @pytest.fixture()
    def db(self):
        from sqlalchemy import create_engine
        from ontology_store import Database
        from ontology_store.db.engine import Base
        # Importing for metadata side-effect
        from ontology_store.db import eval_models  # noqa: F401
        from ontology_store.workers.queue import ReindexQueueRow  # noqa: F401

        engine = create_engine(os.environ["ONTOLOGY_STORE_TEST_URL"], future=True)
        Base.metadata.drop_all(engine)
        Base.metadata.create_all(engine)
        return Database(engine)

    def _seed_case(self, db, case_id: str = "case_1", expected_rks: list[str] | None = None):
        from ontology_store.db import EvalCase
        with db.session() as s:
            s.add(EvalCase(
                case_id=case_id,
                question="employee training compliance",
                intent="causal_reasoning",
                expected_anchors=["employee", "training_assignment"],
                expected_asset_rks=expected_rks or ["postgres://acme/csod/public/csod_employee"],
                forbidden_asset_rks=[],
                scope_payload={"org_id": "acme-corp"},
                retrieval_kind_default=None,
                hardness="medium",
                domain_tags=["HR"],
                enabled=True,
            ))

    class _StubPipelineWithFixedHits:
        """Returns a fixed list of AssetHits regardless of input."""
        def __init__(self, hits: list[dict]) -> None:
            self.hits = hits

        async def run(self, kind: str, **kwargs):
            class _R:
                data = self.hits
            return _R()

    def test_worker_runs_pending_and_records_metrics(self, db) -> None:
        from ontology_retrieval.eval import EvalWorker, HistoricalComparisonScorer
        from ontology_store.db import EvalMetric, EvalResult, EvalRun

        self._seed_case(
            db, case_id="case_1",
            expected_rks=["rk_match"],
        )

        with db.session() as s:
            run = EvalRun(
                status="pending",
                retrieval_kind="asset_search",
                scorer_names=["historical_comparison"],
                case_filter={"case_ids": ["case_1"]},
                trigger="manual",
            )
            s.add(run)
            s.flush()
            run_id = run.run_id

        pipeline = self._StubPipelineWithFixedHits(hits=[
            {"asset_rk": "rk_match", "name": "csod_employee", "score": 0.9},
            {"asset_rk": "rk_extra", "name": "other", "score": 0.4},
        ])
        worker = EvalWorker(
            database=db, pipeline=pipeline,
            scorers=[HistoricalComparisonScorer()],
        )
        stats = worker.run_pending()
        assert stats.runs_processed == 1
        assert stats.runs_succeeded == 1

        with db.session() as s:
            r = s.get(EvalRun, run_id)
            assert r.status == "done"
            assert r.case_count == 1
            assert r.passed_count == 1
            results = s.query(EvalResult).filter_by(run_id=run_id).all()
            assert len(results) == 1
            assert results[0].pass_gate is True
            assert results[0].metrics["hit_rate"] == 1.0
            metrics = s.query(EvalMetric).filter_by(run_id=run_id).all()
            metric_names = {m.metric_name for m in metrics}
            assert "mean_hit_rate" in metric_names
            assert "mean_mrr" in metric_names
            assert "pass_rate" in metric_names
