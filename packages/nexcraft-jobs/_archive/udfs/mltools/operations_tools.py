"""Experiment / inference helpers aligned with genieml ``operations_tools``."""

from __future__ import annotations

FUNCTION_NAMES: frozenset[str] = frozenset(
    {
        "calculate_percent_change_comparison",
        "calculate_absolute_change_comparison",
        "calculate_prepost_comparison",
        "calculate_stratified_analysis",
        "calculate_bootstrap_ci",
        "calculate_power_analysis",
        "calculate_effect_sizes",
        "adjust_pvalues_bonferroni",
        "calculate_sequential_analysis",
        "calculate_cuped_adjustment",
    }
)

__all__ = ["FUNCTION_NAMES"]
