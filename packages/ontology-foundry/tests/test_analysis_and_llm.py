import pytest
from pydantic import BaseModel

from ontology_foundry.analysis.stats import profile_numeric_column
from ontology_foundry.llm.provider import ModelRole
from ontology_foundry.llm.stub import StaticJsonProvider
from ontology_foundry.llm.transform import llm_structured_transform


def test_profile_numeric_column_pure_python() -> None:
    prof = profile_numeric_column("x", [1.0, 2.0, None, 4.0])
    assert prof.column == "x"
    assert prof.null_rate > 0
    assert prof.mean is not None


def test_linear_pearson_when_scipy_present() -> None:
    pytest.importorskip("scipy")
    from ontology_foundry.analysis.correlation import linear_pearson_pair

    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [2.0, 4.0, 6.0, 8.0, 10.0]
    finding = linear_pearson_pair("a", "b", x, y, alpha=0.05, min_effect=0.5)
    assert finding is not None
    assert finding.method == "pearson"
    assert abs(finding.effect_size - 1.0) < 1e-6


def test_llm_structured_transform_stub() -> None:
    class Demo(BaseModel):
        answer: str

    provider = StaticJsonProvider(
        {ModelRole.CLAIM_EXTRACTOR_DEFAULT: '{"answer": "ok"}'}
    )
    out = llm_structured_transform(
        provider,
        ModelRole.CLAIM_EXTRACTOR_DEFAULT,
        "Ping",
        Demo,
    )
    assert out.answer == "ok"
