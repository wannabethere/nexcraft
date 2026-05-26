"""RelationTypeDAO — predicate-class TBox writes + lineage_edge linkage.

Inputs come from `ontology_foundry.relations.induce_schema`:

  - `RelationSchema.types` → one `RelationType` ORM row per `(predicate, domain, range)`.
  - `InducedPredicate.surfaces` / `support` / `avg_confidence` → audit-quality
    metadata persisted alongside the TBox row so retrieval can filter by
    minimum support and trace canonicalization back to surface forms.

After the TBox is persisted, callers walk the corresponding ABox rows
(`lineage_edge`) and call `attach_predicate_to_edge(...)` to link each
concrete edge to its canonical predicate row.

The DAO never deletes — re-running induction over a refreshed corpus
overwrites in place via the natural key `(org_id, predicate, domain, range_type)`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.db.models import HierarchyAudit, LineageEdge
from ontology_store.db.relation_models import RelationType

logger = logging.getLogger(__name__)


# ───────────────────────────────────────────────────────────────────────────
# Input shape — keeps the pipeline DAO-caller free of ORM imports.
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class RelationTypeIn:
    """One predicate-class row to upsert. Mirrors `foundry.relations.RelationType`
    plus the audit metadata `InducedPredicate` carries."""
    predicate: str
    domain: str
    range_type: str
    inverse: str | None = None
    functional: bool = False
    confidence: float | None = None
    evidence_count: int = 0
    surfaces: list[str] | None = None
    provenance: str = "induce_schema"


# ───────────────────────────────────────────────────────────────────────────
# DAO
# ───────────────────────────────────────────────────────────────────────────


class RelationTypeDAO:
    """Write paths for the predicate TBox. Caller manages the session."""

    def __init__(self, session: Session, *, actor: str = "induce_schema") -> None:
        self.s = session
        self.actor = actor

    # ── Upsert TBox rows ─────────────────────────────────────────────────

    def upsert_relation_type(
        self,
        *,
        org_id: str,
        spec: RelationTypeIn,
    ) -> tuple[RelationType, str]:
        """Upsert on `(org_id, predicate, domain, range_type)`. Returns (row, outcome).

        outcome ∈ {"inserted", "updated"} — re-running with the same triple
        but new evidence_count / confidence updates the row.
        """
        now = datetime.now(timezone.utc)
        stmt = select(RelationType).where(
            RelationType.org_id == org_id,
            RelationType.predicate == spec.predicate,
            RelationType.domain == spec.domain,
            RelationType.range_type == spec.range_type,
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        surfaces_str = (
            ",".join(sorted(set(spec.surfaces))) if spec.surfaces else None
        )
        if existing is None:
            row = RelationType(
                org_id=org_id,
                predicate=spec.predicate,
                domain=spec.domain,
                range_type=spec.range_type,
                inverse=spec.inverse,
                functional=spec.functional,
                confidence=spec.confidence,
                evidence_count=spec.evidence_count,
                surfaces=surfaces_str,
                provenance=spec.provenance,
            )
            self.s.add(row)
            self._audit(
                action="create",
                entity_uid=f"relation_type:{org_id}:{spec.predicate}:{spec.domain}->{spec.range_type}",
                new_value={
                    "predicate": spec.predicate,
                    "domain": spec.domain, "range_type": spec.range_type,
                    "confidence": spec.confidence,
                    "evidence_count": spec.evidence_count,
                    "provenance": spec.provenance,
                },
            )
            return row, "inserted"
        # Update in place — newer induction runs supersede older counts.
        existing.inverse = spec.inverse
        existing.functional = spec.functional
        if spec.confidence is not None:
            existing.confidence = spec.confidence
        existing.evidence_count = max(existing.evidence_count, spec.evidence_count)
        if surfaces_str:
            existing.surfaces = surfaces_str
        existing.provenance = spec.provenance
        existing.updated_at = now
        return existing, "updated"

    def upsert_many(
        self,
        *,
        org_id: str,
        specs: list[RelationTypeIn],
    ) -> dict[str, int]:
        """Batch upsert. Returns counters keyed by outcome."""
        counts = {"inserted": 0, "updated": 0}
        for spec in specs:
            _, outcome = self.upsert_relation_type(org_id=org_id, spec=spec)
            counts[outcome] += 1
        return counts

    # ── Attach predicate to ABox row ─────────────────────────────────────

    def attach_predicate_to_edge(
        self,
        *,
        from_rk: str,
        to_rk: str,
        edge_kind: str,
        predicate_id: int,
    ) -> int:
        """Set `lineage_edge.predicate_id` for the edge identified by its
        natural key. Returns 1 on update, 0 when the edge isn't found.

        Idempotent: re-setting the same predicate_id is a no-op.
        """
        stmt = select(LineageEdge).where(
            LineageEdge.from_rk == from_rk,
            LineageEdge.to_rk == to_rk,
            LineageEdge.edge_kind == edge_kind,
        )
        edge = self.s.execute(stmt).scalar_one_or_none()
        if edge is None:
            logger.debug(
                "attach_predicate_to_edge: no lineage_edge for %s -[%s]-> %s",
                from_rk, edge_kind, to_rk,
            )
            return 0
        if edge.predicate_id == predicate_id:
            return 0
        edge.predicate_id = predicate_id
        return 1

    def attach_many(
        self,
        *,
        items: list[dict[str, Any]],
    ) -> dict[str, int]:
        """Batch attach. Each item: `{from_rk, to_rk, edge_kind, predicate_id}`."""
        counts = {"attached": 0, "missing": 0}
        for item in items:
            n = self.attach_predicate_to_edge(
                from_rk=item["from_rk"],
                to_rk=item["to_rk"],
                edge_kind=item["edge_kind"],
                predicate_id=item["predicate_id"],
            )
            counts["attached" if n == 1 else "missing"] += 1
        return counts

    # ── Read paths ───────────────────────────────────────────────────────

    def get_relation_type(
        self,
        *,
        org_id: str,
        predicate: str,
        domain: str,
        range_type: str,
    ) -> RelationType | None:
        return self.s.execute(
            select(RelationType).where(
                RelationType.org_id == org_id,
                RelationType.predicate == predicate,
                RelationType.domain == domain,
                RelationType.range_type == range_type,
            )
        ).scalar_one_or_none()

    def list_relation_types(
        self,
        *,
        org_id: str,
        predicate: str | None = None,
        min_evidence_count: int = 0,
    ) -> list[RelationType]:
        stmt = select(RelationType).where(RelationType.org_id == org_id)
        if predicate is not None:
            stmt = stmt.where(RelationType.predicate == predicate)
        if min_evidence_count > 0:
            stmt = stmt.where(RelationType.evidence_count >= min_evidence_count)
        stmt = stmt.order_by(
            RelationType.predicate.asc(), RelationType.evidence_count.desc(),
        )
        return list(self.s.execute(stmt).scalars().all())

    # ── Audit ───────────────────────────────────────────────────────────

    def _audit(
        self,
        *,
        action: str,
        entity_uid: str,
        new_value: dict[str, Any] | None = None,
    ) -> None:
        self.s.add(HierarchyAudit(
            actor=self.actor,
            action=action,
            tier="relation_type",
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
