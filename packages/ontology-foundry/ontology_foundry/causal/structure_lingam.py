from __future__ import annotations

from typing import Any

import numpy as np

from ontology_foundry.causal.models import CausalEdgeFinding


def discover_edges_direct_lingam(
    data: np.ndarray,
    column_names: list[str],
    *,
    threshold: float = 1e-6,
) -> tuple[list[CausalEdgeFinding], Any]:
    """
    DirectLiNGAM (ingestion §5.3, `lingam`).
    Adjacency matrix entries encode directed strengths; edge exists if |W_ij| > threshold.
    """
    try:
        import lingam
    except ImportError as e:
        raise ImportError("DirectLiNGAM requires lingam. Install: ontology-foundry[causal]") from e

    if data.ndim != 2:
        raise ValueError("data must be a 2D numeric array")
    n_vars = data.shape[1]
    if len(column_names) != n_vars:
        raise ValueError("column_names length must match number of columns")

    model = lingam.DirectLiNGAM()
    model.fit(data.astype(np.float64))
    # Lingam: adjacency_matrix_[i, j] is the coefficient of variable j in the equation for i (edge j -> i).
    w = np.asarray(model.adjacency_matrix_)
    edges: list[CausalEdgeFinding] = []
    for i in range(n_vars):
        for j in range(n_vars):
            coef = float(w[i, j])
            if abs(coef) <= threshold:
                continue
            edges.append(
                CausalEdgeFinding(
                    source=column_names[j],
                    target=column_names[i],
                    algorithm="DirectLiNGAM",
                    weight=coef,
                    diagnostics={"threshold": threshold},
                )
            )
    return edges, model
