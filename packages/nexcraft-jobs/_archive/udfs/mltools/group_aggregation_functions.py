"""Aggregations and rollups aligned with genieml ``group_aggregation_functions``."""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "aggregate_by_time",
        "analyze_distribution",
        "get_top_metrics",
    }
)

__all__ = ["FUNCTION_NAMES"]
