"""Anomaly / correlation helpers aligned with genieml ``anomalydetection``."""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "find_correlated_metrics",
        "calculate_lag_correlation",
        "decompose_impact_by_dimension",
        "build_anomaly_explanation_payload",
        "detect_anomalies",
    }
)

__all__ = ["FUNCTION_NAMES"]
