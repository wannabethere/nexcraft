"""InferenceDAO — write paths for LLM-inferred enrichment side-outputs.

Handles:
  - Inferred FKs → `lineage_edge` rows with `evidence_kind='inferred_relationship'`
  - Causal candidates → `causal_candidate` rows (UPSERT on natural key)
  - Data-protection hints → `data_protection_hint` rows (UPSERT per asset+provenance)

Each method is idempotent. Re-running enrichment with the same content produces
the same rows. Re-running with changed content updates in place.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.db.inference_models import CausalCandidate, DataProtectionHint
from ontology_store.db.models import HierarchyAudit, LineageEdge

logger = logging.getLogger(__name__)


class InferenceDAO:
    """Write paths for enrichment side-outputs. Caller manages the session."""

    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.s = session
        self.actor = actor

    # ── Inferred lineage relationships ─────────────────────────────────

    def upsert_inferred_relationship(
        self,
        *,
        from_rk: str,
        to_rk: str,
        confidence: float | None = None,
        reason: str | None = None,
        cardinality_hint: str = "",
        edge_kind: str = "depends_on",
    ) -> int:
        """Upsert a `lineage_edge` row representing an LLM-inferred FK.

        Idempotent on the existing unique key
        `(from_rk, from_kind, to_rk, to_kind, edge_kind)`. Returns 1 on insert,
        0 on no-op update.
        """
        stmt = (
            select(LineageEdge)
            .where(
                LineageEdge.from_rk == from_rk,
                LineageEdge.to_rk == to_rk,
                LineageEdge.edge_kind == edge_kind,
            )
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        evidence_ref = reason if reason else (cardinality_hint or None)
        if existing is None:
            self.s.add(LineageEdge(
                from_rk=from_rk, from_kind="table",
                to_rk=to_rk, to_kind="table",
                edge_kind=edge_kind,
                evidence_kind="inferred_relationship",
                confidence=confidence,
                evidence_ref=evidence_ref,
                active=True,
            ))
            self._audit(
                action="create", entity_uid=f"{from_rk}->{to_rk}",
                new_value={
                    "evidence_kind": "inferred_relationship",
                    "confidence": confidence, "cardinality_hint": cardinality_hint,
                },
            )
            return 1
        # Update fields on existing — confidence may improve over runs
        if confidence is not None:
            existing.confidence = confidence
        existing.active = True
        if evidence_ref:
            existing.evidence_ref = evidence_ref
        return 0

    def upsert_inferred_relationships(
        self, *, items: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Batch helper. Each item has keys:
        from_rk, to_rk, confidence, reason, cardinality_hint (optional).
        Returns counts: inserted + updated.
        """
        inserted = 0
        updated = 0
        for item in items:
            from_rk = item.get("from_rk")
            to_rk = item.get("to_rk")
            if not (from_rk and to_rk):
                logger.debug("Skipping inferred relationship missing rk: %s", item)
                continue
            n = self.upsert_inferred_relationship(
                from_rk=from_rk,
                to_rk=to_rk,
                confidence=item.get("confidence"),
                reason=item.get("reason") or item.get("rationale"),
                cardinality_hint=item.get("cardinality_hint") or "",
            )
            if n == 1:
                inserted += 1
            else:
                updated += 1
        return {"inserted": inserted, "updated": updated}

    # ── Causal candidates ──────────────────────────────────────────────

    def upsert_causal_candidate(
        self,
        *,
        asset_rk: str,
        subject_ref: str,
        predicate: str,
        object_ref: str,
        evidence_columns: list[str] | None = None,
        mechanism_hint: str = "",
        confidence: float | None = None,
        rationale: str | None = None,
        provenance: str = "llm_causal_dependency",
    ) -> CausalCandidate:
        """Upsert on `(asset_rk, subject_ref, predicate, object_ref)`."""
        stmt = (
            select(CausalCandidate)
            .where(
                CausalCandidate.asset_rk == asset_rk,
                CausalCandidate.subject_ref == subject_ref,
                CausalCandidate.predicate == predicate,
                CausalCandidate.object_ref == object_ref,
            )
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        if existing is None:
            row = CausalCandidate(
                asset_rk=asset_rk,
                subject_ref=subject_ref,
                predicate=predicate,
                object_ref=object_ref,
                evidence_columns=list(evidence_columns or []),
                mechanism_hint=mechanism_hint or None,
                confidence=confidence,
                rationale=rationale,
                provenance=provenance,
                status="proposed",
            )
            self.s.add(row)
            self._audit(
                action="create", entity_uid=f"causal:{asset_rk}:{subject_ref}->{object_ref}",
                tier="causal_candidate",
                new_value={
                    "predicate": predicate, "confidence": confidence,
                    "provenance": provenance,
                },
            )
            return row
        # Update in place — keep status (operator may have flipped it)
        existing.evidence_columns = list(evidence_columns or existing.evidence_columns)
        if mechanism_hint:
            existing.mechanism_hint = mechanism_hint
        if confidence is not None:
            existing.confidence = confidence
        if rationale:
            existing.rationale = rationale
        existing.provenance = provenance
        existing.updated_at = datetime.now(timezone.utc)
        return existing

    def list_pending_causal_candidates(
        self,
        *,
        asset_rk_prefix: str | None = None,
        asset_rks: list[str] | None = None,
        limit: int = 50,
    ) -> list[CausalCandidate]:
        """Return causal_candidate rows with `status='proposed'` for the validator.

        Filters:
          - `asset_rk_prefix` — match `asset_rk LIKE '<prefix>%'` (cheap source/db scoping)
          - `asset_rks` — explicit allowlist (overrides prefix when provided)
          - `limit` — max rows returned (default 50)

        Ordered by `created_at ASC` so oldest pending candidates drain first.
        """
        stmt = select(CausalCandidate).where(CausalCandidate.status == "proposed")
        if asset_rks:
            stmt = stmt.where(CausalCandidate.asset_rk.in_(asset_rks))
        elif asset_rk_prefix:
            stmt = stmt.where(CausalCandidate.asset_rk.like(f"{asset_rk_prefix}%"))
        stmt = stmt.order_by(CausalCandidate.created_at.asc()).limit(limit)
        return list(self.s.execute(stmt).scalars().all())

    def apply_validation_result(
        self,
        *,
        candidate_id: int,
        decision: str,
        diagnostics: dict[str, Any] | None = None,
    ) -> CausalCandidate | None:
        """Write the statistical validator's decision back to a candidate row.

        `decision` must be one of {"validated", "rejected", "inconclusive"}.
        Idempotent: re-applying the same decision to the same row simply
        refreshes diagnostics + `validated_at` + `updated_at`.
        """
        if decision not in {"validated", "rejected", "inconclusive"}:
            raise ValueError(
                f"Invalid validation decision {decision!r}; expected one of "
                f"validated|rejected|inconclusive"
            )
        row = self.s.get(CausalCandidate, candidate_id)
        if row is None:
            logger.warning("apply_validation_result: candidate_id=%d not found", candidate_id)
            return None
        prev_status = row.status
        row.status = decision
        if diagnostics is not None:
            row.validation_diagnostics = _json_safe(diagnostics)
        now = datetime.now(timezone.utc)
        row.validated_at = now
        row.updated_at = now
        self._audit(
            action="update",
            entity_uid=f"causal:{row.asset_rk}:{row.subject_ref}->{row.object_ref}",
            tier="causal_candidate",
            new_value={
                "prev_status": prev_status,
                "decision": decision,
                "algorithms": (diagnostics or {}).get("algorithms"),
            },
        )
        return row

    def upsert_causal_candidates(self, *, items: list[dict[str, Any]]) -> dict[str, int]:
        """Batch helper. Each item matches the kwargs of `upsert_causal_candidate`."""
        inserted = 0
        updated = 0
        for item in items:
            existing_before = (
                self.s.execute(
                    select(CausalCandidate)
                    .where(
                        CausalCandidate.asset_rk == item["asset_rk"],
                        CausalCandidate.subject_ref == item["subject_ref"],
                        CausalCandidate.predicate == item["predicate"],
                        CausalCandidate.object_ref == item["object_ref"],
                    )
                ).scalar_one_or_none()
                is not None
            )
            self.upsert_causal_candidate(
                asset_rk=item["asset_rk"],
                subject_ref=item["subject_ref"],
                predicate=item["predicate"],
                object_ref=item["object_ref"],
                evidence_columns=item.get("evidence_columns") or [],
                mechanism_hint=item.get("mechanism_hint") or "",
                confidence=item.get("confidence"),
                rationale=item.get("rationale") or item.get("mechanism_hint"),
                provenance=item.get("provenance", "llm_causal_dependency"),
            )
            if existing_before:
                updated += 1
            else:
                inserted += 1
        return {"inserted": inserted, "updated": updated}

    # ── Data protection hints ──────────────────────────────────────────

    def upsert_data_protection_hint(
        self,
        *,
        asset_rk: str,
        rls_predicates: list[str] | None = None,
        cls_columns: list[str] | None = None,
        rationale: str = "",
        provenance: str = "llm_data_protection",
        extra: dict[str, Any] | None = None,
    ) -> DataProtectionHint:
        """Upsert on `(asset_rk, provenance)`. One hint per provenance per asset."""
        stmt = (
            select(DataProtectionHint)
            .where(
                DataProtectionHint.asset_rk == asset_rk,
                DataProtectionHint.provenance == provenance,
            )
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        if existing is None:
            row = DataProtectionHint(
                asset_rk=asset_rk,
                rls_predicates=list(rls_predicates or []),
                cls_columns=list(cls_columns or []),
                rationale=rationale or None,
                provenance=provenance,
                status="proposed",
                extra=extra,
            )
            self.s.add(row)
            self._audit(
                action="create", entity_uid=f"data_protection:{asset_rk}",
                tier="data_protection_hint",
                new_value={
                    "rls_predicates_count": len(rls_predicates or []),
                    "cls_columns_count": len(cls_columns or []),
                    "provenance": provenance,
                },
            )
            return row
        existing.rls_predicates = list(rls_predicates or [])
        existing.cls_columns = list(cls_columns or [])
        if rationale:
            existing.rationale = rationale
        if extra is not None:
            existing.extra = extra
        existing.updated_at = datetime.now(timezone.utc)
        return existing

    # ── Audit ──────────────────────────────────────────────────────────

    def _audit(
        self,
        *,
        action: str,
        entity_uid: str,
        new_value: dict[str, Any] | None = None,
        tier: str = "lineage_inferred",
    ) -> None:
        self.s.add(HierarchyAudit(
            actor=self.actor,
            action=action,
            tier=tier,
            entity_uid=entity_uid,
            new_value=_json_safe(new_value) if new_value else None,
        ))


def _json_safe(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_json_safe(v) for v in value]
    return value
