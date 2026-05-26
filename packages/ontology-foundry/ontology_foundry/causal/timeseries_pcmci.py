from __future__ import annotations

from typing import Any

import numpy as np

from ontology_foundry.causal.models import PcmciEdgeFinding


def pcmci_discovery(
    data: np.ndarray,
    *,
    tau_max: int = 3,
    pc_alpha: float = 0.05,
) -> tuple[list[PcmciEdgeFinding], dict[str, Any]]:
    """
    PCMCI time-lagged causal discovery (`tigramite`, ingestion §5.3 time-series column).

    ``data`` shape: ``(time, variables)``.
    Significant links derived from ``p_matrix`` when present.
    """
    try:
        from tigramite import data_processing as pp
        from tigramite.independence_tests import ParCorr
        from tigramite.pcmci import PCMCI
    except ImportError as e:
        raise ImportError(
            "PCMCI requires tigramite. Install: ontology-foundry[timeseries]"
        ) from e

    arr = np.asarray(data, dtype=np.float64)
    if arr.ndim != 2:
        raise ValueError("data must be 2D (time x variables)")
    n_time, n_vars = arr.shape

    dataframe = pp.DataFrame(
        arr,
        datatime=np.arange(n_time),
        var_names=list(range(n_vars)),
    )
    cond_ind_test = ParCorr(significance="analytic")
    pcmci = PCMCI(dataframe=dataframe, cond_ind_test=cond_ind_test)
    results = pcmci.run_pcmci(tau_max=tau_max, pc_alpha=pc_alpha)

    findings: list[PcmciEdgeFinding] = []
    p_matrix = results.get("p_matrix")
    val_matrix = results.get("val_matrix")
    if p_matrix is not None:
        for j in range(n_vars):
            for i in range(n_vars):
                for lag in range(p_matrix.shape[2]):
                    p = float(p_matrix[i, j, lag])
                    if p < pc_alpha:
                        findings.append(
                            PcmciEdgeFinding(
                                source_idx=i,
                                target_idx=j,
                                lag=int(lag),
                                val_matrix_entry=float(val_matrix[i, j, lag])
                                if val_matrix is not None
                                else None,
                                p_value=p,
                            )
                        )

    return findings, results
