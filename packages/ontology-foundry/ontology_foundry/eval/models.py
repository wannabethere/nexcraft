from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GateVerdict(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


class EvalIssue(BaseModel):
    code: str
    message: str
    severity: str = "error"


class SpanGroundingResult(BaseModel):
    """eval_strategy §5.1 — span-level grounding score."""

    claim_text: str
    source_span: str
    lexical_overlap: float
    numbers_in_claim: list[float] = Field(default_factory=list)
    numbers_in_source: list[float] = Field(default_factory=list)
    numbers_aligned: bool
    grounding_strength: float
    passed: bool


class QuantitativeIntegrityResult(BaseModel):
    """eval_strategy §5.3 — numeric claim vs reference."""

    claim_value: float | None
    reference_value: float | None
    relative_tolerance: float
    absolute_tolerance: float
    aligned: bool


class RetrievalMetricsResult(BaseModel):
    """eval_strategy §8.1 — retrieval vs gold relevance set."""

    context_precision: float
    context_recall: float
    retrieved_count: int
    relevant_retrieved: int
    gold_relevant_count: int


class CausalResponseCheckResult(BaseModel):
    """eval_strategy §8.3 — reported vs card."""

    reported_weight: float | None
    card_weight: float | None
    weight_aligned: bool
    reported_ci_low: float | None = None
    reported_ci_high: float | None = None
    card_ci_low: float | None = None
    card_ci_high: float | None = None
    ci_aligned: bool = False


class RegressionGateReport(BaseModel):
    """eval_strategy §9.2 — release gate comparison."""

    metric_name: str
    baseline: float
    current: float
    max_regression: float | None = None
    zero_tolerance: bool = False
    allowed: bool


class HallucinationProbeCase(BaseModel):
    """eval_strategy §7.1 — adversarial probe fixture."""

    probe_id: str
    category: str
    prompt: str
    expected_behavior: str
    metadata: dict[str, Any] = Field(default_factory=dict)
