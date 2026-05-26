"""Scorers — `historical_comparison` (deterministic) + `llm_judge` (LLM-as-judge)."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, Field

from ontology_retrieval.eval.metrics import (
    forbidden_violations,
    hit_rate,
    mean_reciprocal_rank,
    ndcg,
    precision_at_k,
    recall_at_k,
)

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Public protocol + result envelope
# ───────────────────────────────────────────────────────────────────────────

@dataclass
class RetrievedItem:
    """A single item from a retrieval call, in rank order."""
    rk: str
    name: str = ""
    description: str = ""
    score: float | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_asset_hit(cls, hit: dict[str, Any]) -> "RetrievedItem":
        """Build from an `AssetHit`-shaped dict (the retrieval pipeline's wire shape)."""
        return cls(
            rk=hit.get("asset_rk") or hit.get("rk", ""),
            name=hit.get("name", ""),
            description=hit.get("description") or hit.get("snippet") or "",
            score=hit.get("score"),
            payload=hit,
        )


@dataclass
class ScorerResult:
    """Standard scorer output."""
    scorer_name: str
    metrics: dict[str, float] = field(default_factory=dict)
    llm_judgment: dict[str, Any] | None = None
    pass_gate: bool | None = None
    notes: str = ""


class Scorer(Protocol):
    name: str

    def score(
        self,
        *,
        question: str,
        retrieved: list[RetrievedItem],
        expected_rks: list[str],
        forbidden_rks: list[str] | None = None,
        expected_anchors: list[str] | None = None,
        k: int = 10,
    ) -> ScorerResult: ...


# ───────────────────────────────────────────────────────────────────────────
# HistoricalComparisonScorer — deterministic
# ───────────────────────────────────────────────────────────────────────────

class HistoricalComparisonScorer:
    """Compares retrieval output against authored ground truth.

    Metrics computed (all stored under `ScorerResult.metrics`):
      - precision_at_1, precision_at_3, precision_at_5, precision_at_10
      - recall_at_5, recall_at_10
      - mrr
      - ndcg_at_5, ndcg_at_10
      - hit_rate (binary; 1.0 if ANY expected rk appears in retrieved)
      - forbidden_violations (count)

    Pass gate (default): `hit_rate == 1.0 AND forbidden_violations == 0`.
    Can be tightened via `pass_min_recall_at_5` / `pass_min_mrr`.
    """

    name = "historical_comparison"

    def __init__(
        self,
        *,
        pass_min_recall_at_5: float | None = None,
        pass_min_mrr: float | None = None,
        require_no_forbidden: bool = True,
        require_hit: bool = True,
    ) -> None:
        self.pass_min_recall_at_5 = pass_min_recall_at_5
        self.pass_min_mrr = pass_min_mrr
        self.require_no_forbidden = require_no_forbidden
        self.require_hit = require_hit

    def score(
        self,
        *,
        question: str,
        retrieved: list[RetrievedItem],
        expected_rks: list[str],
        forbidden_rks: list[str] | None = None,
        expected_anchors: list[str] | None = None,
        k: int = 10,
    ) -> ScorerResult:
        rks = [r.rk for r in retrieved]
        forbidden_rks = forbidden_rks or []

        metrics = {
            "precision_at_1":  precision_at_k(rks, relevant=expected_rks, k=1),
            "precision_at_3":  precision_at_k(rks, relevant=expected_rks, k=3),
            "precision_at_5":  precision_at_k(rks, relevant=expected_rks, k=5),
            "precision_at_10": precision_at_k(rks, relevant=expected_rks, k=10),
            "recall_at_5":     recall_at_k(rks, relevant=expected_rks, k=5),
            "recall_at_10":    recall_at_k(rks, relevant=expected_rks, k=10),
            "mrr":             mean_reciprocal_rank(rks, relevant=expected_rks),
            "ndcg_at_5":       ndcg(rks, relevant=expected_rks, k=5),
            "ndcg_at_10":      ndcg(rks, relevant=expected_rks, k=10),
            "hit_rate":        hit_rate(rks, relevant=expected_rks, k=k),
            "forbidden_violations": float(forbidden_violations(rks, forbidden=forbidden_rks, k=k)),
            "retrieved_count": float(len(rks)),
            "expected_count":  float(len(expected_rks)),
        }

        passed = True
        reasons: list[str] = []
        if self.require_hit and metrics["hit_rate"] < 1.0:
            passed = False
            reasons.append("no expected rk in retrieved")
        if self.require_no_forbidden and metrics["forbidden_violations"] > 0:
            passed = False
            reasons.append(
                f"{int(metrics['forbidden_violations'])} forbidden rk(s) returned"
            )
        if self.pass_min_recall_at_5 is not None and metrics["recall_at_5"] < self.pass_min_recall_at_5:
            passed = False
            reasons.append(
                f"recall_at_5 {metrics['recall_at_5']:.2f} < {self.pass_min_recall_at_5}"
            )
        if self.pass_min_mrr is not None and metrics["mrr"] < self.pass_min_mrr:
            passed = False
            reasons.append(f"mrr {metrics['mrr']:.2f} < {self.pass_min_mrr}")

        return ScorerResult(
            scorer_name=self.name,
            metrics=metrics,
            pass_gate=passed,
            notes=("; ".join(reasons) if reasons else "ok"),
        )


# ───────────────────────────────────────────────────────────────────────────
# LLMJudgeScorer — structured LLM rating
# ───────────────────────────────────────────────────────────────────────────

class _LLMItemRating(BaseModel):
    rk: str
    rating: int = Field(ge=0, le=2, description="0=irrelevant, 1=partial, 2=relevant")
    rationale: str = ""


class _LLMJudgmentResponse(BaseModel):
    per_item: list[_LLMItemRating]
    coverage_rating: int = Field(
        ge=0, le=5,
        description="Holistic 0..5 — would this retrieval support answering the question?",
    )
    missing_concepts: list[str] = Field(default_factory=list)
    rationale: str = ""


class LLMJudgeScorer:
    """Asks an LLM to grade each retrieved item against the question.

    Metrics stored:
      - judge_mean_rating          — mean of per-item 0..2 grades
      - judge_relevant_rate        — fraction of items rated 2
      - judge_partial_rate         — fraction rated 1
      - judge_coverage_rating     — holistic 0..5
      - judge_coverage_rate        — coverage_rating / 5 (normalized)
      - judge_items_rated          — count

    Pass gate: configurable; default `coverage_rating >= 3 AND judge_relevant_rate >= 0.3`.

    LLM provider: any `ontology_foundry.llm.ModelProvider`. The constructor
    accepts either a provider directly or an `openai_model` name (auto-builds
    an `OpenAIChatProvider`).
    """

    name = "llm_judge"

    def __init__(
        self,
        *,
        provider: Any = None,
        openai_model: str | None = "deepseek-chat",
        role: Any = None,
        pass_min_coverage: int = 3,
        pass_min_relevant_rate: float = 0.3,
        item_limit: int = 10,
    ) -> None:
        self._provider = provider
        self._openai_model = openai_model
        # Default to the validator role from ontology-foundry when available.
        if role is None:
            try:
                from ontology_foundry.llm.provider import ModelRole
                role = ModelRole.VALIDATOR
            except ImportError:
                role = "validator"
        self._role = role
        self.pass_min_coverage = pass_min_coverage
        self.pass_min_relevant_rate = pass_min_relevant_rate
        self.item_limit = item_limit

    def _get_provider(self) -> Any:
        if self._provider is not None:
            return self._provider
        try:
            from ontology_foundry.llm.openai_provider import OpenAIChatProvider
        except ImportError as exc:
            raise ImportError(
                "LLMJudgeScorer requires ontology-foundry[llm] for the default OpenAI provider. "
                "Install with: pip install 'ontology-foundry[llm]' or pass provider= explicitly."
            ) from exc
        if not self._openai_model:
            raise RuntimeError("openai_model must be set when no provider is supplied")
        self._provider = OpenAIChatProvider(model=self._openai_model)
        return self._provider

    def score(
        self,
        *,
        question: str,
        retrieved: list[RetrievedItem],
        expected_rks: list[str],
        forbidden_rks: list[str] | None = None,
        expected_anchors: list[str] | None = None,
        k: int = 10,
    ) -> ScorerResult:
        items = retrieved[: self.item_limit]
        if not items:
            return ScorerResult(
                scorer_name=self.name,
                metrics={
                    "judge_mean_rating": 0.0,
                    "judge_relevant_rate": 0.0,
                    "judge_partial_rate": 0.0,
                    "judge_coverage_rating": 0.0,
                    "judge_coverage_rate": 0.0,
                    "judge_items_rated": 0.0,
                },
                pass_gate=False,
                notes="no retrieved items to judge",
            )

        provider = self._get_provider()
        prompt = self._build_prompt(
            question=question, items=items,
            expected_anchors=expected_anchors or [],
        )
        try:
            from ontology_foundry.llm.transform import llm_structured_transform
            response = llm_structured_transform(provider, self._role, prompt, _LLMJudgmentResponse)
        except Exception as exc:
            logger.warning("LLM judge invocation failed: %s", exc)
            return ScorerResult(
                scorer_name=self.name,
                metrics={
                    "judge_mean_rating": 0.0,
                    "judge_relevant_rate": 0.0,
                    "judge_partial_rate": 0.0,
                    "judge_coverage_rating": 0.0,
                    "judge_coverage_rate": 0.0,
                    "judge_items_rated": float(len(items)),
                },
                pass_gate=False,
                notes=f"llm error: {exc}",
            )

        # Reconcile LLM's per-item list back to retrieved items by rk
        rated_by_rk = {r.rk: r for r in response.per_item}
        ratings: list[int] = []
        per_item_dump: list[dict[str, Any]] = []
        for item in items:
            r = rated_by_rk.get(item.rk)
            grade = r.rating if r is not None else 0
            ratings.append(grade)
            per_item_dump.append({
                "rk": item.rk,
                "name": item.name,
                "rating": grade,
                "rationale": (r.rationale if r else ""),
            })

        relevant_rate = sum(1 for x in ratings if x == 2) / len(ratings)
        partial_rate = sum(1 for x in ratings if x == 1) / len(ratings)
        mean_rating = sum(ratings) / len(ratings)
        coverage = max(0, min(5, response.coverage_rating))

        passed = (
            coverage >= self.pass_min_coverage
            and relevant_rate >= self.pass_min_relevant_rate
        )

        return ScorerResult(
            scorer_name=self.name,
            metrics={
                "judge_mean_rating":     float(mean_rating),
                "judge_relevant_rate":   float(relevant_rate),
                "judge_partial_rate":    float(partial_rate),
                "judge_coverage_rating": float(coverage),
                "judge_coverage_rate":   float(coverage) / 5.0,
                "judge_items_rated":     float(len(ratings)),
            },
            llm_judgment={
                "per_item": per_item_dump,
                "coverage_rating": coverage,
                "missing_concepts": list(response.missing_concepts),
                "rationale": response.rationale,
            },
            pass_gate=passed,
            notes=("ok" if passed else f"coverage={coverage} relevant_rate={relevant_rate:.2f}"),
        )

    @staticmethod
    def _build_prompt(
        *,
        question: str,
        items: list[RetrievedItem],
        expected_anchors: list[str],
    ) -> str:
        item_block = "\n".join(
            f"  [{i+1}] rk={item.rk}\n"
            f"      name={item.name}\n"
            f"      description={(item.description or '')[:280]}"
            for i, item in enumerate(items)
        )
        anchors_block = ", ".join(expected_anchors) if expected_anchors else "(none specified)"

        return f"""You are evaluating a data-retrieval system. Output JSON only.

QUESTION:
{question}

RETRIEVED ITEMS (in rank order):
{item_block}

EXPECTED ANCHOR CONCEPTS (the question is about these — if any retrieved
item helps explore these concepts, it counts as at least partially relevant):
{anchors_block}

TASK:
1. For each item, rate its relevance to the question:
     0 = irrelevant (unrelated to the question or anchors)
     1 = partial   (touches the topic but not directly answering)
     2 = relevant  (directly useful for answering the question)
2. Give a holistic coverage rating 0–5: would these items collectively let
   a downstream LLM answer the question well?
     0 = no useful information
     5 = comprehensive coverage
3. If concepts are missing that would have helped, list them.

OUTPUT JSON STRICTLY MATCHING THIS SHAPE:
{{
  "per_item": [
    {{ "rk": "<rk from above>", "rating": 0|1|2, "rationale": "one-sentence reason" }}
  ],
  "coverage_rating": 0..5,
  "missing_concepts": ["concept_id_1", "concept_id_2"],
  "rationale": "one-paragraph overall assessment"
}}

Include every item from the retrieved list in `per_item`, in the order shown.
Use exactly the `rk` values as provided.
"""
