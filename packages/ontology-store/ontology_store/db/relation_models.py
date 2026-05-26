"""ORM for the relation_type TBox.

Produced by `ontology_foundry.relations.induce_schema` running over the
ABox (concrete `lineage_edge` rows). One row per `(predicate, domain, range)`
triple — the canonical predicate vocabulary plus observed dominant types.

Distinct from `card`:
  - `card` describes WHAT an entity is (a concept/object_type, a causal_node).
  - `relation_type` describes HOW concepts are related (the predicate +
    its expected subject/object types).

The two link via `relation_type.domain` / `range_type` carrying a card_id
when the dominant type resolves to an authored object_type card. When it
doesn't (the corpus surfaced an unmodelled type), the column holds the
surface string for traceability — it's not an FK.
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from ontology_store.db.engine import Base


class RelationType(Base):
    """One row of the induced predicate TBox.

    Natural key: `(org_id, predicate, domain, range_type)`. Re-running
    induction over a refreshed corpus overwrites in place.
    """
    __tablename__ = "relation_type"

    relation_type_pk: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True,
    )
    org_id: Mapped[str] = mapped_column(
        ForeignKey("organization.org_id", ondelete="CASCADE"), nullable=False,
    )
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    domain: Mapped[str] = mapped_column(Text, nullable=False)
    range_type: Mapped[str] = mapped_column(Text, nullable=False)
    inverse: Mapped[str | None] = mapped_column(Text)
    functional: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    confidence: Mapped[float | None] = mapped_column(Float)
    evidence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Comma-separated list of original surface predicates that canonicalized to
    # this row. Useful for retrieval debug + the novel-promotion loop.
    surfaces: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(
        Text, nullable=False, default="induce_schema",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "org_id", "predicate", "domain", "range_type",
            name="uq_relation_type_natural_key",
        ),
    )
