from __future__ import annotations

from typing import Any

import numpy as np

from ontology_foundry.causal.models import CausalEdgeFinding


def _require_pc() -> Any:
    try:
        from causallearn.search.ConstraintBased.PC import pc
    except ImportError as e:
        raise ImportError(
            "PC discovery requires causal-learn. Install: ontology-foundry[causal]"
        ) from e
    return pc


def discover_edges_pc(
    data: np.ndarray,
    column_names: list[str],
    *,
    alpha: float = 0.05,
) -> tuple[list[CausalEdgeFinding], Any]:
    """
    PC algorithm (ingestion §5.3, `causal-learn`).
    Uses parent sets on the returned graph for stable directed-edge enumeration.
    """
    if data.ndim != 2:
        raise ValueError("data must be a 2D numeric array")
    n_vars = data.shape[1]
    if len(column_names) != n_vars:
        raise ValueError("column_names length must match number of columns")

    pc = _require_pc()
    cg = pc(data.astype(np.float64), alpha=alpha)
    G = cg.G
    get_parents = getattr(G, "get_parents", None)
    if get_parents is None:
        return [], cg

    nodes = G.get_nodes()
    node_to_idx = {node: idx for idx, node in enumerate(nodes)}

    edges: list[CausalEdgeFinding] = []
    for child in nodes:
        j = node_to_idx[child]
        try:
            parents = get_parents(child)
        except Exception:
            parents = []
        for parent in parents:
            i = node_to_idx.get(parent)
            if i is None:
                continue
            edges.append(
                CausalEdgeFinding(
                    source=column_names[i],
                    target=column_names[j],
                    algorithm="PC",
                    diagnostics={"alpha": alpha},
                )
            )

    return edges, cg
