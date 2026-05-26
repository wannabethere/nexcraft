"""ORM models for semantic-layer cards in Postgres.

Cards were originally filesystem-only — `semantic_layer/<kind>s/<id>.card.md`
files with YAML frontmatter. As the foundry took shape, two needs pushed cards
into the database:

  1. Enrichment stages (CausalDependencyEnricher, AnnotationEnricher) need to
     load tenant vocabulary efficiently per asset. A scan of the filesystem
     directory per pipeline run is fine for small tenants but doesn't scale to
     multi-tenant operation.
  2. Cards mutate (new causal_nodes drafted, deprecations) and need an audit
     trail + idempotent re-loading — git is fine for authoring but the runtime
     needs a query interface.

Tables:

  - `card`     — one row per `(org_id, kind, card_id)`. Stores full body +
                 frontmatter so the row is self-sufficient; `content_hash`
                 makes re-loading the same `.card.md` a no-op.
  - `card_ref` — directed reference between cards (e.g., a causal_node
                 references another node it moderates). Lets the retrieval
                 layer follow a card's mentions without parsing markdown.

Cards are NOT MDL assets (no `rk`, no schema, no columns). They live alongside
the spine but in their own namespace. The vector layer (`vector.CARDS`
collection) reads from this table when indexing.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from ontology_store.db.engine import Base


# Recognised card kinds — kept in sync with semantic_layer_card_spec.md §3.
# Stored as a Text column with a CHECK constraint at migration time rather
# than a Postgres ENUM, so adding a kind doesn't require an ALTER TYPE.
KNOWN_CARD_KINDS: tuple[str, ...] = (
    "object_type",
    "interface",
    "causal_node",
    "derived_state",
    "action",
    "metric",
    "event",
    "instruction",
    "key_area",
)


class Card(Base):
    """One semantic-layer card (object_type, causal_node, key_area, …).

    Natural key: `(org_id, kind, card_id)`. Re-loading a `.card.md` file with
    unchanged content is a no-op via `content_hash`.

    `frontmatter` preserves the original YAML dict so card-kind-specific fields
    (e.g., a causal_node's `mediators` list) survive a round-trip without
    needing a new column per kind.
    """
    __tablename__ = "card"

    card_pk: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    org_id: Mapped[str] = mapped_column(
        ForeignKey("organization.org_id", ondelete="CASCADE"), nullable=False,
    )
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    card_id: Mapped[str] = mapped_column(Text, nullable=False)
    layer: Mapped[str] = mapped_column(Text, nullable=False, default="semantic")
    title: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    frontmatter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    markings: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    origin: Mapped[str] = mapped_column(Text, nullable=False, default="tenant")
    # origin ∈ {tenant, vendor, core, imported, llm_draft}
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    deprecated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    source_path: Mapped[str | None] = mapped_column(Text)
    # SHA256 of `frontmatter + body` so loader can skip unchanged files.
    content_hash: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "card_id", name="uq_card_natural_key"),
        # Mirror the migration's CHECK so `Base.metadata.create_all` test
        # environments enforce the kind vocabulary too. Keep in sync with
        # KNOWN_CARD_KINDS above + the migration in 0006_add_card_storage.
        CheckConstraint(
            "kind IN ('object_type','interface','causal_node','derived_state',"
            "'action','metric','event','instruction','key_area')",
            name="ck_card_kind_known",
        ),
    )


class CardRef(Base):
    """Directed reference from one card to another card.

    Use cases:
      - A `causal_node` card mentions another `causal_node` as a moderator.
      - An `object_type` card aliases an interface.
      - A `key_area` card groups other key_area cards.

    The target side is stored by `(to_kind, to_card_id)` rather than by FK to
    `card.card_pk` because:
      - Card import is not strictly DAG-ordered — a card may reference one that
        hasn't been loaded yet.
      - Cross-tenant references would otherwise require an `(org_id, ...)`
        join; we keep refs implicitly scoped to the from-card's org.
    """
    __tablename__ = "card_ref"

    ref_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    from_card_pk: Mapped[int] = mapped_column(
        ForeignKey("card.card_pk", ondelete="CASCADE"), nullable=False,
    )
    to_kind: Mapped[str] = mapped_column(Text, nullable=False)
    to_card_id: Mapped[str] = mapped_column(Text, nullable=False)
    relation: Mapped[str] = mapped_column(Text, nullable=False, default="mentions")
    # relation ∈ {mentions, moderates, mediated_by, alias_of, deprecates, …}
    extra: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "from_card_pk", "to_kind", "to_card_id", "relation",
            name="uq_card_ref_natural_key",
        ),
    )
