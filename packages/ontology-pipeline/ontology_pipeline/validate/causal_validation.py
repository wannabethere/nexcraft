"""CausalValidator — statistical validation of LLM-proposed causal candidates.

The pipeline's enrichment stages (CausalDependencyEnricher,
CrossAssetCausalEnricher) emit `causal_candidate` rows with `status='proposed'`.
The validator drains these rows, pulls sample data for each, runs structural
discovery + bivariate tests via `ontology_foundry.causal`, and writes the
decision back via `InferenceDAO.apply_validation_result`.

Decisions:
  - `validated`   — algorithm consensus AGREES with the proposed direction
                    (subject → object) at a configured significance threshold.
  - `rejected`    — algorithms agree the relationship does NOT hold, or agree
                    on the OPPOSITE direction.
  - `inconclusive` — not enough data, cross-asset join required, candidate
                    references a causal_node card (not column data), or
                    optional dependency missing. Operator review path.

Same-asset candidates (subject_asset_rk == object_asset_rk == candidate.asset_rk)
are testable directly. Cross-asset candidates need entity-level joins that v1
of the validator does not attempt — they are recorded as `inconclusive` with
`reason='cross_asset_join_required'` so a follow-up pass can pick them up.

Tests inject stubs for `CausalSampler` + `CausalTestSuite` to keep the unit
tests free of pandas / statsmodels / causal-learn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Literal, Protocol

from ontology_foundry.causal.consensus import edge_consensus
from ontology_foundry.causal.models import CausalEdgeFinding

if TYPE_CHECKING:  # pragma: no cover
    import numpy as np
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Public types
# ───────────────────────────────────────────────────────────────────────────

Decision = Literal["validated", "rejected", "inconclusive"]


@dataclass
class ValidationOutcome:
    """One candidate's validation result. Persisted via InferenceDAO."""
    candidate_id: int
    decision: Decision
    diagnostics: dict[str, Any] = field(default_factory=dict)


class CausalSampler(Protocol):
    """Pulls sample data for one asset's columns from the actual source.

    Implementations:
      - `PsycopgCausalSampler` (default, in this module) — issues
        `SELECT col_a, col_b FROM <table> LIMIT n` against the source DSN.
      - Test stubs — return pre-built arrays so unit tests don't need a DB.

    Returns `(data, columns)` where `data` is a 2-D float array (n_rows × k_cols)
    and `columns` matches the original `columns` argument 1:1 (same order). Rows
    containing NULL are dropped before return.
    """

    def sample_columns(
        self, *, asset_rk: str, columns: list[str], limit: int,
    ) -> tuple["np.ndarray", list[str]]:
        ...


class CausalTestSuite(Protocol):
    """Runs structural discovery + bivariate tests for a (subject, object) pair.

    Returns a list of CausalEdgeFinding from one or more algorithms. The
    validator passes the list into `edge_consensus` to decide whether the
    proposed direction is supported.
    """

    def run_pair(
        self,
        *,
        data: "np.ndarray",
        columns: list[str],
        subject_col: str,
        object_col: str,
    ) -> list[CausalEdgeFinding]:
        ...


# ───────────────────────────────────────────────────────────────────────────
# Default implementations
# ───────────────────────────────────────────────────────────────────────────


class DefaultCausalTestSuite:
    """Default suite: bivariate Granger (time-aware) + PC (cross-sectional).

    Each algorithm runs independently; failures from one don't block the other.
    Both contribute to consensus voting.

    Configurable thresholds:
      - `granger_max_lag` (default 4)
      - `granger_alpha`   (default 0.05)
      - `pc_alpha`        (default 0.05)
    """

    def __init__(
        self,
        *,
        granger_max_lag: int = 4,
        granger_alpha: float = 0.05,
        pc_alpha: float = 0.05,
    ) -> None:
        self.granger_max_lag = granger_max_lag
        self.granger_alpha = granger_alpha
        self.pc_alpha = pc_alpha

    def run_pair(
        self,
        *,
        data: "np.ndarray",
        columns: list[str],
        subject_col: str,
        object_col: str,
    ) -> list[CausalEdgeFinding]:
        findings: list[CausalEdgeFinding] = []
        col_to_idx = {c: i for i, c in enumerate(columns)}
        s_idx = col_to_idx.get(subject_col)
        o_idx = col_to_idx.get(object_col)
        if s_idx is None or o_idx is None:
            return findings

        # Granger (time-aware)
        try:
            from ontology_foundry.causal.timeseries_granger import granger_pair
            g = granger_pair(
                data[:, s_idx], data[:, o_idx],
                cause_name=subject_col, effect_name=object_col,
                max_lag=self.granger_max_lag, alpha=self.granger_alpha,
            )
            if g.significant:
                findings.append(CausalEdgeFinding(
                    source=subject_col, target=object_col,
                    algorithm="granger",
                    diagnostics={
                        "p_value": g.min_p_value, "best_lag": g.best_lag,
                        "max_lag": g.max_lag,
                    },
                ))
        except ImportError as exc:
            logger.info("granger unavailable (%s); skipping", exc)
        except Exception as exc:
            logger.info("granger failed for %s→%s: %s", subject_col, object_col, exc)

        # PC (cross-sectional structural discovery)
        try:
            from ontology_foundry.causal.structure_pc import discover_edges_pc
            edges, _cg = discover_edges_pc(
                data[:, [s_idx, o_idx]],
                column_names=[subject_col, object_col],
                alpha=self.pc_alpha,
            )
            findings.extend(edges)
        except ImportError as exc:
            logger.info("PC unavailable (%s); skipping", exc)
        except Exception as exc:
            logger.info("PC failed for %s→%s: %s", subject_col, object_col, exc)

        return findings


class PsycopgCausalSampler:
    """Pulls column samples from a Postgres source via psycopg.

    Maps an `asset_rk` of the form `postgres://<source_id>/<catalog>/<schema>/<table>`
    to `SELECT col_a, col_b FROM <schema>.<table> WHERE col_a IS NOT NULL AND
    col_b IS NOT NULL LIMIT n`. The caller supplies the `dsn_for` lookup so
    secrets stay outside this module.
    """

    def __init__(self, dsn_for: Any) -> None:
        # dsn_for: Callable[[str], str] — maps source_id → DSN string
        self._dsn_for = dsn_for

    def sample_columns(
        self, *, asset_rk: str, columns: list[str], limit: int,
    ) -> tuple["np.ndarray", list[str]]:
        import numpy as np
        import psycopg

        source_id, schema, table = _split_asset_rk(asset_rk)
        if not (schema and table):
            raise ValueError(f"cannot derive schema.table from asset_rk={asset_rk!r}")
        if not columns:
            raise ValueError("at least one column required")

        # Identifier-safe quoting via psycopg.sql
        from psycopg import sql as psql
        select_cols = psql.SQL(", ").join(psql.Identifier(c) for c in columns)
        where_not_null = psql.SQL(" AND ").join(
            psql.SQL("{} IS NOT NULL").format(psql.Identifier(c)) for c in columns
        )
        query = psql.SQL(
            "SELECT {cols} FROM {schema}.{table} WHERE {where} LIMIT {limit}"
        ).format(
            cols=select_cols,
            schema=psql.Identifier(schema),
            table=psql.Identifier(table),
            where=where_not_null,
            limit=psql.Literal(limit),
        )

        dsn = self._dsn_for(source_id)
        rows: list[tuple[Any, ...]] = []
        with psycopg.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()

        arr = np.asarray(rows, dtype=float)
        if arr.ndim == 1:  # single-column result
            arr = arr.reshape(-1, 1)
        return arr, list(columns)


# ───────────────────────────────────────────────────────────────────────────
# Validator
# ───────────────────────────────────────────────────────────────────────────


class CausalValidator:
    """Drains pending `causal_candidate` rows; runs statistical tests; decides.

    Usage:
        validator = CausalValidator(
            session_factory=db.session,
            sampler=PsycopgCausalSampler(dsn_for=...),
            test_suite=DefaultCausalTestSuite(),
        )
        stats = validator.run_once(asset_rk_prefix="postgres://csod-pg/", limit=20)

    The validator is intentionally NOT a long-running worker — operators
    schedule it (cron, manual run, downstream of a daily refresh). A worker
    wrapper can be added later if it earns its keep.

    Decision rule (default):
      - At least `min_distinct_algorithms` agree on subject→object → 'validated'
      - Algorithms agree on object→subject only → 'rejected' (opposite direction)
      - Both directions discovered, neither dominates → 'inconclusive'
      - No findings at all → 'rejected' (we tried, found no signal)
      - Sample too small, dependency missing, cross-asset → 'inconclusive'
    """

    def __init__(
        self,
        *,
        session_factory: Any,
        sampler: CausalSampler,
        test_suite: CausalTestSuite | None = None,
        sample_limit: int = 1000,
        min_sample_rows: int = 30,
        min_distinct_algorithms: int = 2,
        actor: str = "causal_validator",
    ) -> None:
        self._session_factory = session_factory
        self._sampler = sampler
        self._test_suite = test_suite or DefaultCausalTestSuite()
        self._sample_limit = sample_limit
        self._min_sample_rows = min_sample_rows
        self._min_distinct_algorithms = min_distinct_algorithms
        self._actor = actor

    # ── public entry ─────────────────────────────────────────────────────

    def run_once(
        self,
        *,
        asset_rk_prefix: str | None = None,
        asset_rks: list[str] | None = None,
        limit: int = 50,
    ) -> dict[str, int]:
        """Validate up to `limit` pending candidates. Returns a counter dict.

        Per-row exceptions are caught and recorded as `inconclusive` with the
        error message in diagnostics — a single bad asset can't block the rest.
        """
        from ontology_store.dao.inferences import InferenceDAO  # local import — opt dep

        counts = {"processed": 0, "validated": 0, "rejected": 0, "inconclusive": 0, "errors": 0}
        with self._session_factory() as session:
            dao = InferenceDAO(session, actor=self._actor)
            pending = dao.list_pending_causal_candidates(
                asset_rk_prefix=asset_rk_prefix, asset_rks=asset_rks, limit=limit,
            )

            for cand in pending:
                counts["processed"] += 1
                try:
                    outcome = self._validate_one(cand)
                except Exception as exc:  # noqa: BLE001 — defense-in-depth
                    logger.exception(
                        "validator crashed on candidate %d: %s", cand.candidate_id, exc,
                    )
                    outcome = ValidationOutcome(
                        candidate_id=cand.candidate_id,
                        decision="inconclusive",
                        diagnostics={"error": str(exc), "reason": "validator_exception"},
                    )
                    counts["errors"] += 1
                dao.apply_validation_result(
                    candidate_id=outcome.candidate_id,
                    decision=outcome.decision,
                    diagnostics=outcome.diagnostics,
                )
                counts[outcome.decision] += 1
            session.commit()
        return counts

    # ── per-candidate decision ──────────────────────────────────────────

    def _validate_one(self, cand: Any) -> ValidationOutcome:
        """One candidate → outcome. Pure-Python where possible; numpy-only deep."""
        subj_rk, subj_col, subj_kind = _parse_ref(cand.subject_ref)
        obj_rk, obj_col, obj_kind = _parse_ref(cand.object_ref)

        diagnostics: dict[str, Any] = {
            "subject_ref": cand.subject_ref,
            "object_ref": cand.object_ref,
            "predicate": cand.predicate,
            "validated_at": datetime.now(timezone.utc).isoformat(),
        }

        # Object is a causal_node card — not testable from column data alone.
        if obj_kind == "causal_node":
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="inconclusive",
                diagnostics={
                    **diagnostics,
                    "reason": "object_is_causal_node_card",
                    "note": "Causal-node objects describe abstract outcomes; statistical "
                            "validation requires resolving the node to a measurable proxy. "
                            "Operator review path.",
                },
            )

        # Cross-asset candidate — v1 doesn't attempt joins.
        if subj_rk and obj_rk and subj_rk != obj_rk:
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="inconclusive",
                diagnostics={
                    **diagnostics,
                    "reason": "cross_asset_join_required",
                    "subject_asset_rk": subj_rk, "object_asset_rk": obj_rk,
                    "note": "Cross-asset validation needs an entity-level join not yet "
                            "implemented. Pending v2.",
                },
            )

        # Need column-level granularity on both sides.
        if not (subj_col and obj_col):
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="inconclusive",
                diagnostics={
                    **diagnostics, "reason": "missing_column_granularity",
                    "note": "Both subject_ref and object_ref must include a column "
                            "(rk.column) to run column-level tests.",
                },
            )

        asset_rk = subj_rk or obj_rk or cand.asset_rk
        # Sample data
        try:
            data, columns = self._sampler.sample_columns(
                asset_rk=asset_rk, columns=[subj_col, obj_col], limit=self._sample_limit,
            )
        except Exception as exc:  # noqa: BLE001
            logger.info("sampler failed for asset %s: %s", asset_rk, exc)
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="inconclusive",
                diagnostics={**diagnostics, "reason": "sampler_failed", "error": str(exc)},
            )

        n_rows = int(data.shape[0]) if hasattr(data, "shape") else len(data)
        diagnostics["sample_rows"] = n_rows
        diagnostics["sample_columns"] = list(columns)

        if n_rows < self._min_sample_rows:
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="inconclusive",
                diagnostics={
                    **diagnostics, "reason": "insufficient_sample",
                    "min_required": self._min_sample_rows,
                },
            )

        # Run tests + consensus.
        findings = self._test_suite.run_pair(
            data=data, columns=list(columns),
            subject_col=subj_col, object_col=obj_col,
        )
        diagnostics["algorithms"] = sorted({f.algorithm for f in findings})
        diagnostics["findings"] = [
            {
                "source": f.source, "target": f.target,
                "algorithm": f.algorithm, "diagnostics": dict(f.diagnostics),
            }
            for f in findings
        ]

        if not findings:
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="rejected",
                diagnostics={**diagnostics, "reason": "no_signal_in_any_algorithm"},
            )

        consensus_edges, tallies = edge_consensus(
            [findings], min_distinct_algorithms=self._min_distinct_algorithms,
        )
        diagnostics["consensus_edges"] = [
            {"source": e.source, "target": e.target, "algorithms": e.diagnostics.get("algorithms", [])}
            for e in consensus_edges
        ]
        diagnostics["tallies"] = [
            {"source": s, "target": t, "n_algorithms": n} for (s, t, n) in tallies
        ]

        # Decide direction
        forward = (subj_col, obj_col)
        reverse = (obj_col, subj_col)
        consensus_pairs = {(e.source, e.target) for e in consensus_edges}

        if forward in consensus_pairs:
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="validated",
                diagnostics={**diagnostics, "reason": "consensus_supports_direction"},
            )
        if reverse in consensus_pairs:
            return ValidationOutcome(
                candidate_id=cand.candidate_id,
                decision="rejected",
                diagnostics={**diagnostics, "reason": "consensus_supports_reverse_direction"},
            )

        # No consensus — but some algorithm did find a signal. Defer to operator.
        return ValidationOutcome(
            candidate_id=cand.candidate_id,
            decision="inconclusive",
            diagnostics={
                **diagnostics, "reason": "no_consensus",
                "min_distinct_algorithms": self._min_distinct_algorithms,
            },
        )


# ───────────────────────────────────────────────────────────────────────────
# Helpers
# ───────────────────────────────────────────────────────────────────────────


def _parse_ref(ref: str) -> tuple[str | None, str | None, str]:
    """Parse a subject_ref / object_ref string.

    Returns `(asset_rk, column, kind)` where `kind` is one of:
      - "asset_column"  — `<asset_rk>.<col>` (rk has a `://` scheme)
      - "asset"         — `<asset_rk>` only
      - "causal_node"   — bare id (no `://`); refers to a causal_node card
    """
    if not ref:
        return None, None, "causal_node"
    if "://" not in ref:
        return None, None, "causal_node"
    # asset_rk contains slashes (e.g. postgres://src/db/schema/table). The
    # column suffix (if present) is the trailing `.<word>` AFTER the last `/`.
    last_slash = ref.rfind("/")
    tail = ref[last_slash + 1:]
    if "." in tail:
        col_pos = ref.rfind(".")
        if col_pos > last_slash:
            return ref[:col_pos], ref[col_pos + 1:], "asset_column"
    return ref, None, "asset"


def _split_asset_rk(asset_rk: str) -> tuple[str, str, str]:
    """`postgres://<source_id>/<catalog>/<schema>/<table>` → (source_id, schema, table).

    Tolerant: catalog segment may be absent in older rks; we always take
    schema as the second-to-last path segment and table as the last.
    """
    if "://" not in asset_rk:
        return "", "", ""
    _scheme, rest = asset_rk.split("://", 1)
    parts = rest.split("/")
    # parts: [source_id, ...path..., table]
    if len(parts) < 3:
        return parts[0] if parts else "", "", parts[-1] if parts else ""
    source_id = parts[0]
    table = parts[-1]
    schema = parts[-2]
    return source_id, schema, table
