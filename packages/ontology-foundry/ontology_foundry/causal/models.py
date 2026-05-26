from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class CausalEdgeFinding(BaseModel):
    """Directed edge candidate from structure discovery (ingestion §5.3)."""

    source: str
    target: str
    algorithm: str
    weight: float | None = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class GrangerFinding(BaseModel):
    """Pairwise Granger result for one lag window."""

    cause_column: str
    effect_column: str
    max_lag: int
    min_p_value: float
    best_lag: int | None = None
    significant: bool = False


class PcmciEdgeFinding(BaseModel):
    """Conditionally independent / lagged link from Tigramite PCMCI."""

    source_idx: int
    target_idx: int
    lag: int
    val_matrix_entry: float | None = None
    p_value: float | None = None


class RefutationSummary(BaseModel):
    """DoWhy refutation outcome for a single estimate path."""

    refutation_type: str
    is_invalid: bool | None = None
    refutation_result: Any = None
    diagnostics: dict[str, Any] = Field(default_factory=dict)
