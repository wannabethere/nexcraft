"""Tests for the statistical causal-candidate validator.

Two layers of coverage:
  - Per-candidate decision logic — drives `CausalValidator._validate_one` with
    stubbed samplers + test suites and asserts the resulting `ValidationOutcome`.
  - Run-loop orchestration — patches the InferenceDAO inside `run_once` with a
    recording stand-in and verifies counts + per-candidate write-back.

No real DB / pandas / statsmodels / causal-learn needed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pytest

from ontology_foundry.causal.models import CausalEdgeFinding
from ontology_pipeline.validate.causal_validation import (
    CausalValidator,
    DefaultCausalTestSuite,
    ValidationOutcome,
    _parse_ref,
    _split_asset_rk,
)


# ───────────────────────────────────────────────────────────────────────────
# Stubs — keep tests independent of DB + stats deps
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class _Candidate:
    """Mimics the `CausalCandidate` ORM row's attribute surface."""
    candidate_id: int
    asset_rk: str
    subject_ref: str
    predicate: str
    object_ref: str


@dataclass
class _StubSampler:
    """Returns pre-baked (data, columns) tuples keyed by asset_rk."""
    by_rk: dict[str, tuple[np.ndarray, list[str]]] = field(default_factory=dict)
    raise_for: set[str] = field(default_factory=set)

    def sample_columns(self, *, asset_rk, columns, limit):
        if asset_rk in self.raise_for:
            raise RuntimeError("simulated source failure")
        if asset_rk not in self.by_rk:
            raise KeyError(f"no fixture data for {asset_rk}")
        data, all_cols = self.by_rk[asset_rk]
        idxs = [all_cols.index(c) for c in columns]
        return data[:, idxs], list(columns)


@dataclass
class _StubTestSuite:
    """Returns canned findings regardless of input data."""
    findings: list[CausalEdgeFinding] = field(default_factory=list)

    def run_pair(self, *, data, columns, subject_col, object_col):
        return list(self.findings)


def _candidate(**overrides) -> _Candidate:
    """Default candidate — same-asset subject/object, both with columns."""
    base = dict(
        candidate_id=1,
        asset_rk="postgres://csod-pg/testdb/public/csod_employee",
        subject_ref="postgres://csod-pg/testdb/public/csod_employee.due_date",
        predicate="leading_indicator_of",
        object_ref="postgres://csod-pg/testdb/public/csod_employee.completion",
    )
    base.update(overrides)
    return _Candidate(**base)


def _build(sampler, suite, **kwargs) -> CausalValidator:
    """A validator with a no-op session_factory (no DB use in unit layer)."""

    def session_factory():
        class _S:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *exc): return None
            def commit(self_inner): pass
        return _S()

    return CausalValidator(
        session_factory=session_factory,
        sampler=sampler,
        test_suite=suite,
        **kwargs,
    )


# ───────────────────────────────────────────────────────────────────────────
# _parse_ref / _split_asset_rk
# ───────────────────────────────────────────────────────────────────────────


class TestParseRef:
    def test_asset_with_column(self):
        rk, col, kind = _parse_ref(
            "postgres://csod-pg/testdb/public/csod_employee.due_date",
        )
        assert rk == "postgres://csod-pg/testdb/public/csod_employee"
        assert col == "due_date"
        assert kind == "asset_column"

    def test_asset_only(self):
        rk, col, kind = _parse_ref("postgres://csod-pg/testdb/public/csod_employee")
        assert rk == "postgres://csod-pg/testdb/public/csod_employee"
        assert col is None
        assert kind == "asset"

    def test_causal_node_bare_id(self):
        rk, col, kind = _parse_ref("compliance_gap")
        assert rk is None
        assert col is None
        assert kind == "causal_node"

    def test_dotted_source_id_in_rk_is_not_mistaken_for_column(self):
        # source_id contains '.', but the column suffix only kicks in if `.`
        # appears AFTER the last `/`.
        rk, col, kind = _parse_ref("postgres://csod-pg.testdb/public/csod_employee")
        assert col is None
        assert kind == "asset"

    def test_empty(self):
        rk, col, kind = _parse_ref("")
        assert kind == "causal_node"


class TestSplitAssetRk:
    def test_standard_four_segment(self):
        sid, schema, table = _split_asset_rk(
            "postgres://csod-pg/testdb/public/csod_employee",
        )
        assert sid == "csod-pg"
        assert schema == "public"
        assert table == "csod_employee"

    def test_non_postgres_returns_empty(self):
        assert _split_asset_rk("not-an-rk") == ("", "", "")


# ───────────────────────────────────────────────────────────────────────────
# Per-candidate decision logic — calls _validate_one directly
# ───────────────────────────────────────────────────────────────────────────


class TestValidateOneDecisions:
    def test_consensus_supports_direction_validates(self):
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(0).randn(200, 2), ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="due_date", target="completion", algorithm="granger"),
            CausalEdgeFinding(source="due_date", target="completion", algorithm="PC"),
        ])
        outcome = _build(sampler, suite)._validate_one(_candidate())
        assert outcome.decision == "validated"
        assert outcome.diagnostics["reason"] == "consensus_supports_direction"
        assert set(outcome.diagnostics["algorithms"]) == {"granger", "PC"}
        assert outcome.diagnostics["sample_rows"] == 200

    def test_consensus_supports_reverse_rejects(self):
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(1).randn(200, 2), ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="completion", target="due_date", algorithm="granger"),
            CausalEdgeFinding(source="completion", target="due_date", algorithm="PC"),
        ])
        outcome = _build(sampler, suite)._validate_one(_candidate())
        assert outcome.decision == "rejected"
        assert outcome.diagnostics["reason"] == "consensus_supports_reverse_direction"

    def test_no_findings_rejects(self):
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(2).randn(200, 2), ["due_date", "completion"],
            ),
        })
        outcome = _build(sampler, _StubTestSuite(findings=[]))._validate_one(_candidate())
        assert outcome.decision == "rejected"
        assert outcome.diagnostics["reason"] == "no_signal_in_any_algorithm"

    def test_only_one_algorithm_no_consensus_inconclusive(self):
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(3).randn(200, 2), ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="due_date", target="completion", algorithm="granger"),
        ])
        outcome = _build(sampler, suite)._validate_one(_candidate())
        assert outcome.decision == "inconclusive"
        assert outcome.diagnostics["reason"] == "no_consensus"

    def test_three_algorithm_consensus_when_threshold_lowered(self):
        # Bump algorithms above threshold to exercise the threshold knob.
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(7).randn(200, 2), ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="due_date", target="completion", algorithm="granger"),
        ])
        outcome = _build(
            sampler, suite, min_distinct_algorithms=1,
        )._validate_one(_candidate())
        assert outcome.decision == "validated"

    def test_insufficient_sample_is_inconclusive(self):
        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(4).randn(5, 2), ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="due_date", target="completion", algorithm="granger"),
            CausalEdgeFinding(source="due_date", target="completion", algorithm="PC"),
        ])
        outcome = _build(sampler, suite, min_sample_rows=30)._validate_one(_candidate())
        assert outcome.decision == "inconclusive"
        assert outcome.diagnostics["reason"] == "insufficient_sample"

    def test_object_is_causal_node_is_inconclusive(self):
        outcome = _build(_StubSampler(), _StubTestSuite())._validate_one(
            _candidate(object_ref="compliance_gap"),
        )
        assert outcome.decision == "inconclusive"
        assert outcome.diagnostics["reason"] == "object_is_causal_node_card"

    def test_cross_asset_is_inconclusive(self):
        outcome = _build(_StubSampler(), _StubTestSuite())._validate_one(
            _candidate(
                subject_ref="postgres://csod-pg/testdb/public/csod_employee.due_date",
                object_ref="postgres://csod-pg/testdb/public/training_assignment.id",
            ),
        )
        assert outcome.decision == "inconclusive"
        diags = outcome.diagnostics
        assert diags["reason"] == "cross_asset_join_required"
        assert "subject_asset_rk" in diags and "object_asset_rk" in diags

    def test_sampler_failure_is_inconclusive(self):
        sampler = _StubSampler(
            raise_for={"postgres://csod-pg/testdb/public/csod_employee"},
        )
        outcome = _build(sampler, _StubTestSuite())._validate_one(_candidate())
        assert outcome.decision == "inconclusive"
        assert outcome.diagnostics["reason"] == "sampler_failed"
        assert "error" in outcome.diagnostics

    def test_missing_column_granularity_is_inconclusive(self):
        outcome = _build(_StubSampler(), _StubTestSuite())._validate_one(
            _candidate(
                subject_ref="postgres://csod-pg/testdb/public/csod_employee",
                object_ref="postgres://csod-pg/testdb/public/csod_employee.completion",
            ),
        )
        assert outcome.decision == "inconclusive"
        assert outcome.diagnostics["reason"] == "missing_column_granularity"


# ───────────────────────────────────────────────────────────────────────────
# run_once orchestration — verifies DAO is driven correctly
# ───────────────────────────────────────────────────────────────────────────


class TestRunOnce:
    """Patches `InferenceDAO` inside the validator module so we can verify the
    counts dict + write-back interactions without standing up a real DB."""

    def test_drains_pending_and_writes_each_decision(self, monkeypatch):
        recorded: list[dict[str, Any]] = []

        class _FakeDAO:
            def __init__(self, session, *, actor=None):
                self.session = session
                self.actor = actor

            def list_pending_causal_candidates(self, **kw):
                return [
                    _candidate(candidate_id=1),
                    _candidate(
                        candidate_id=2,
                        object_ref="compliance_gap",  # → inconclusive
                    ),
                ]

            def apply_validation_result(self, *, candidate_id, decision, diagnostics):
                recorded.append({
                    "candidate_id": candidate_id,
                    "decision": decision,
                    "reason": diagnostics.get("reason"),
                })

        # Patch the local import inside run_once.
        import ontology_store.dao.inferences as inferences_mod
        monkeypatch.setattr(inferences_mod, "InferenceDAO", _FakeDAO)

        sampler = _StubSampler(by_rk={
            "postgres://csod-pg/testdb/public/csod_employee": (
                np.random.RandomState(0).randn(200, 2),
                ["due_date", "completion"],
            ),
        })
        suite = _StubTestSuite(findings=[
            CausalEdgeFinding(source="due_date", target="completion", algorithm="granger"),
            CausalEdgeFinding(source="due_date", target="completion", algorithm="PC"),
        ])

        validator = _build(sampler, suite)
        counts = validator.run_once(limit=10)

        assert counts["processed"] == 2
        assert counts["validated"] == 1
        assert counts["inconclusive"] == 1
        assert counts["rejected"] == 0
        # Check what got written
        assert [r["candidate_id"] for r in recorded] == [1, 2]
        decisions = {r["candidate_id"]: r["decision"] for r in recorded}
        assert decisions == {1: "validated", 2: "inconclusive"}
        reasons = {r["candidate_id"]: r["reason"] for r in recorded}
        assert reasons[2] == "object_is_causal_node_card"

    def test_per_candidate_exception_is_caught_as_inconclusive(self, monkeypatch):
        recorded: list[dict[str, Any]] = []

        class _FakeDAO:
            def __init__(self, session, *, actor=None): pass
            def list_pending_causal_candidates(self, **kw):
                return [_candidate(candidate_id=99)]
            def apply_validation_result(self, *, candidate_id, decision, diagnostics):
                recorded.append({
                    "candidate_id": candidate_id, "decision": decision,
                    "reason": diagnostics.get("reason"),
                })

        import ontology_store.dao.inferences as inferences_mod
        monkeypatch.setattr(inferences_mod, "InferenceDAO", _FakeDAO)

        # A sampler that crashes with a non-Exception fallback path:
        class _BoomSampler:
            def sample_columns(self, **kw):
                raise RuntimeError("kaboom")

        # Also patch _validate_one to raise BEFORE the inner sampler try/except so
        # the outer guard in run_once handles it. Easiest: subclass.
        class _BoomValidator(CausalValidator):
            def _validate_one(self, cand):
                raise RuntimeError("outer kaboom")

        validator = _BoomValidator(
            session_factory=_build(_StubSampler(), _StubTestSuite())._session_factory,
            sampler=_BoomSampler(),
            test_suite=_StubTestSuite(),
        )
        counts = validator.run_once(limit=1)
        assert counts["errors"] == 1
        assert counts["inconclusive"] == 1
        assert recorded[0]["decision"] == "inconclusive"
        assert recorded[0]["reason"] == "validator_exception"


# ───────────────────────────────────────────────────────────────────────────
# DefaultCausalTestSuite — graceful degradation
# ───────────────────────────────────────────────────────────────────────────


class TestDefaultCausalTestSuite:
    def test_returns_empty_when_columns_missing_from_data(self):
        suite = DefaultCausalTestSuite()
        data = np.random.RandomState(0).randn(50, 2)
        result = suite.run_pair(
            data=data, columns=["a", "b"], subject_col="zzz", object_col="b",
        )
        assert result == []

    def test_optional_deps_do_not_crash(self):
        """Whether statsmodels / causallearn are installed or not, the suite
        must not raise — it should swallow ImportError and any algorithm errors
        and return whatever findings did succeed (possibly none)."""
        suite = DefaultCausalTestSuite()
        data = np.random.RandomState(0).randn(60, 2)
        # Should not raise regardless of installed deps.
        suite.run_pair(
            data=data, columns=["x", "y"], subject_col="x", object_col="y",
        )
