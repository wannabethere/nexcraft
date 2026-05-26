from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CorrelationFinding(BaseModel):
    """Pairwise correlation finding emitted toward the findings bus (extraction §3.3)."""

    column_a: str
    column_b: str
    method: str
    effect_size: float
    p_value: float | None = None
    n: int = 0
    significant: bool = False
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class NumericColumnProfile(BaseModel):
    """Lightweight column profiler payload (extraction §3.2 column profilers)."""

    column: str
    n_rows: int
    null_rate: float
    distinct_count: int | None = None
    mean: float | None = None
    std: float | None = None
    min: float | None = None
    max: float | None = None


class BootstrapResult(BaseModel):
    statistic_name: str
    point_estimate: float
    ci_low: float
    ci_high: float
    n_bootstrap: int


class CandidatePairArtifact(BaseModel):
    """
    extraction_design §3.3 Tier 1 output — pairs queued for Tier 2 screening.
    """

    column_a: str
    column_b: str
    tier: int = 1
    priority: float = 0.0
    seed_prior_boost: bool = False
    drop_reason: str | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class CorrelationFindingArtifact(CorrelationFinding):
    """Tier 2+ correlation row with FDR and effect flags (extraction §3.3)."""

    tier: int = 2
    p_value_fdr: float | None = None
    effect_meets_threshold: bool = True
    stratification_key: str | None = None


class ValidatedCorrelationArtifact(CorrelationFindingArtifact):
    """Tier 3 — bootstrap CI, refutation hooks (extraction §3.3)."""

    tier: int = 3
    bootstrap_ci_low: float | None = None
    bootstrap_ci_high: float | None = None
    refutation_summaries: list[dict[str, Any]] = Field(default_factory=list)
