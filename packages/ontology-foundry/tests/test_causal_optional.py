import numpy as np
import pytest

from ontology_foundry.causal.consensus import edge_consensus
from ontology_foundry.causal.models import CausalEdgeFinding


def test_edge_consensus_pure_python() -> None:
    a = [
        CausalEdgeFinding(source="x", target="y", algorithm="PC"),
        CausalEdgeFinding(source="y", target="z", algorithm="PC"),
    ]
    b = [
        CausalEdgeFinding(source="x", target="y", algorithm="DirectLiNGAM"),
    ]
    agreed, tallies = edge_consensus([a, b], min_distinct_algorithms=2)
    assert any(e.source == "x" and e.target == "y" for e in agreed)
    assert tallies


@pytest.mark.parametrize("module", ["causallearn", "lingam", "dowhy"])
def test_optional_causal_import(module: str) -> None:
    pytest.importorskip(module)


def test_discover_edges_pc_smoke() -> None:
    pytest.importorskip("causallearn")
    from ontology_foundry.causal.structure_pc import discover_edges_pc

    rng = np.random.default_rng(3)
    x = rng.standard_normal((300, 4))
    edges, cg = discover_edges_pc(x, ["w", "x", "y", "z"], alpha=0.05)
    assert isinstance(edges, list)
    assert cg is not None


def test_discover_edges_direct_lingam_smoke() -> None:
    pytest.importorskip("lingam")
    from ontology_foundry.causal.structure_lingam import discover_edges_direct_lingam

    rng = np.random.default_rng(0)
    x = rng.standard_normal((200, 3))
    edges, model = discover_edges_direct_lingam(x, ["a", "b", "c"], threshold=0.01)
    assert isinstance(edges, list)
    assert model is not None


def test_granger_pair_smoke() -> None:
    pytest.importorskip("statsmodels")
    from ontology_foundry.causal.timeseries_granger import granger_pair

    rng = np.random.default_rng(1)
    x = np.cumsum(rng.standard_normal(120))
    y = np.roll(x, 1) + rng.standard_normal(120) * 0.1
    out = granger_pair(x, y, cause_name="x", effect_name="y", max_lag=3)
    assert out.min_p_value <= 1.0


def test_pcmci_discovery_smoke() -> None:
    pytest.importorskip("tigramite")
    from ontology_foundry.causal.timeseries_pcmci import pcmci_discovery

    rng = np.random.default_rng(2)
    data = rng.standard_normal((80, 3))
    findings, raw = pcmci_discovery(data, tau_max=2, pc_alpha=0.1)
    assert isinstance(findings, list)
    assert raw is not None
