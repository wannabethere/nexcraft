"""ORM models for LLM-inferred enrichment side-outputs.

These tables hold the candidate-shaped output of `ontology-pipeline`'s
enrichment stages — inferred FKs, causal candidates, and data-protection
hints. They are persisted as PROPOSALS, not as authoritative records:

  - `causal_candidate`        — proposed causal edges awaiting validation
                                (via statistical methods, human review, or
                                downstream evidence). Promoted to `claim`
                                when validated.
  - `data_protection_hint`    — proposed RLS / CLS policy suggestions.

Inferred FKs already have a home — `lineage_edge` with
`evidence_kind='inferred_relationship'`. No new table needed there.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, Float, Integer, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ontology_store.db.engine import Base


class CausalCandidate(Base):
    """A proposed causal edge with column-level evidence + confidence.

    Natural key: `(asset_rk, subject_ref, predicate, object_ref)` — re-running
    enrichment produces the same row (UPSERT). Promotion to a confirmed claim
    flips `status` and links to the resulting claim_id.
    """
    __tablename__ = "causal_candidate"

    candidate_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_rk: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    object_ref: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_columns: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    mechanism_hint: Mapped[str | None] = mapped_column(Text)
    confidence: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    # status ∈ {proposed, validated, rejected, inconclusive, promoted_to_claim}
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="llm_causal_dependency")
    promoted_to_claim_id: Mapped[str | None] = mapped_column(Text)
    rationale: Mapped[str | None] = mapped_column(Text)
    # Set by the statistical validator (ontology-pipeline.validate.causal_validation).
    # Shape: {"algorithms": [...], "p_values": {...}, "consensus_count": N, ...}
    validation_diagnostics: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "asset_rk", "subject_ref", "predicate", "object_ref",
            name="uq_causal_candidate_natural_key",
        ),
    )


class DataProtectionHint(Base):
    """Per-asset RLS / CLS policy suggestion from the data_protection stage.

    Not a policy itself — a structured PROPOSAL. The downstream policy engine
    (Purview / Unity / a future internal policy table) reads these as input
    suggestions that operators turn into concrete policies.

    One row per (asset_rk, provenance) — re-running enrichment with the same
    provenance updates the same row.
    """
    __tablename__ = "data_protection_hint"

    hint_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_rk: Mapped[str] = mapped_column(Text, nullable=False)
    rls_predicates: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    cls_columns: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    rationale: Mapped[str | None] = mapped_column(Text)
    provenance: Mapped[str] = mapped_column(Text, nullable=False, default="llm_data_protection")
    status: Mapped[str] = mapped_column(Text, nullable=False, default="proposed")
    # status ∈ {proposed, applied, rejected}
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("asset_rk", "provenance", name="uq_data_protection_hint_asset_provenance"),
    )
