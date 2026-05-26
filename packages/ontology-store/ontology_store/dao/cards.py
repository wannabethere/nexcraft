"""CardDAO — read/write paths for Postgres-backed semantic-layer cards.

Cards are tenant-scoped by `org_id`. Re-loading a `.card.md` file with
unchanged content is a no-op via `content_hash`. The DAO returns ORM rows
(Card) and a small read-side shape (CardSummary) for callers that only need
the prompt-friendly subset.

Method surface:

  - `upsert_card(org_id, kind, card_id, …)` — idempotent on (org_id, kind, card_id)
  - `upsert_card_ref(from_card_pk, to_kind, to_card_id, relation, …)` —
    idempotent on the natural key
  - `list_cards(org_id, kind=…, include_deprecated=False)` — vocab loading path
  - `get_card(org_id, kind, card_id)` — single fetch
  - `mark_deprecated(card_pk, *, when=None)` — soft-delete

The DAO writes audit rows to `hierarchy_audit` so card mutations have the
same trail as the rest of the spine.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ontology_store.db.card_models import Card, CardRef, KNOWN_CARD_KINDS
from ontology_store.db.models import HierarchyAudit

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CardSummary:
    """Prompt-friendly read shape — what an enricher actually needs.

    Mirrors `ontology_pipeline.annotate.CardSummary` so the pipeline can swap
    its filesystem loader for the DB one without touching prompt code.
    """
    card_id: str
    kind: str
    title: str | None
    body_excerpt: str


# Default excerpt length matches the filesystem loader (annotate.py).
_DEFAULT_EXCERPT_CHARS = 300


def _excerpt(body: str, *, max_chars: int = _DEFAULT_EXCERPT_CHARS) -> str:
    body = (body or "").strip()
    if len(body) <= max_chars:
        return body
    cut = body[:max_chars]
    last_space = cut.rfind(" ")
    if last_space > 100:
        cut = cut[:last_space]
    return cut + "…"


def compute_content_hash(*, frontmatter: dict[str, Any] | None, body: str) -> str:
    """Stable SHA256 over (frontmatter, body). Used to skip unchanged re-loads."""
    payload = {
        "frontmatter": frontmatter or {},
        "body": body or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class CardDAO:
    """Card read/write surface. Caller manages the session."""

    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.s = session
        self.actor = actor

    # ── Write paths ─────────────────────────────────────────────────────

    def upsert_card(
        self,
        *,
        org_id: str,
        kind: str,
        card_id: str,
        body: str,
        frontmatter: dict[str, Any] | None = None,
        layer: str = "semantic",
        title: str | None = None,
        aliases: list[str] | None = None,
        markings: list[str] | None = None,
        origin: str = "tenant",
        source_path: str | None = None,
    ) -> tuple[Card, str]:
        """Insert or update a card. Returns (row, outcome).

        outcome ∈ {"inserted", "updated", "unchanged"} — "unchanged" lets the
        loader cheaply skip re-indexing when the file's content is the same.

        Idempotent on `(org_id, kind, card_id)`. Body+frontmatter hash drives
        the unchanged shortcut.
        """
        if kind not in KNOWN_CARD_KINDS:
            raise ValueError(
                f"Unknown card kind {kind!r}; allowed: {sorted(KNOWN_CARD_KINDS)}"
            )

        content_hash = compute_content_hash(frontmatter=frontmatter, body=body)
        stmt = select(Card).where(
            Card.org_id == org_id, Card.kind == kind, Card.card_id == card_id,
        )
        existing = self.s.execute(stmt).scalar_one_or_none()

        if existing is None:
            row = Card(
                org_id=org_id, kind=kind, card_id=card_id,
                layer=layer, title=title,
                body=body or "",
                frontmatter=frontmatter,
                aliases=list(aliases or []),
                markings=list(markings or []),
                origin=origin,
                source_path=source_path,
                content_hash=content_hash,
            )
            self.s.add(row)
            self._audit(
                action="create",
                entity_uid=f"card:{org_id}:{kind}:{card_id}",
                new_value={
                    "kind": kind, "card_id": card_id, "layer": layer,
                    "origin": origin, "content_hash": content_hash,
                },
            )
            return row, "inserted"

        if existing.content_hash == content_hash:
            # Nothing changed — touch nothing, don't write an audit row.
            return existing, "unchanged"

        # Update in place — mutable fields only. Created_at stays put.
        existing.layer = layer
        existing.title = title
        existing.body = body or ""
        existing.frontmatter = frontmatter
        existing.aliases = list(aliases or [])
        existing.markings = list(markings or [])
        existing.origin = origin
        existing.source_path = source_path
        existing.content_hash = content_hash
        existing.updated_at = datetime.now(timezone.utc)
        self._audit(
            action="update",
            entity_uid=f"card:{org_id}:{kind}:{card_id}",
            new_value={"content_hash": content_hash, "origin": origin},
        )
        return existing, "updated"

    def upsert_card_ref(
        self,
        *,
        from_card_pk: int,
        to_kind: str,
        to_card_id: str,
        relation: str = "mentions",
        extra: dict[str, Any] | None = None,
    ) -> tuple[CardRef, str]:
        """Insert or update a directed card→card reference. Idempotent."""
        stmt = select(CardRef).where(
            CardRef.from_card_pk == from_card_pk,
            CardRef.to_kind == to_kind,
            CardRef.to_card_id == to_card_id,
            CardRef.relation == relation,
        )
        existing = self.s.execute(stmt).scalar_one_or_none()
        if existing is None:
            row = CardRef(
                from_card_pk=from_card_pk,
                to_kind=to_kind, to_card_id=to_card_id,
                relation=relation,
                extra=extra,
            )
            self.s.add(row)
            return row, "inserted"
        if extra is not None and extra != (existing.extra or {}):
            existing.extra = extra
            return existing, "updated"
        return existing, "unchanged"

    def mark_deprecated(
        self,
        *,
        card_pk: int,
        when: datetime | None = None,
    ) -> Card | None:
        """Soft-delete a card. Vocab loaders skip deprecated rows by default."""
        row = self.s.get(Card, card_pk)
        if row is None:
            return None
        row.deprecated = True
        row.deprecated_at = when or datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)
        self._audit(
            action="update",
            entity_uid=f"card:{row.org_id}:{row.kind}:{row.card_id}",
            new_value={"deprecated": True},
        )
        return row

    # ── Read paths ──────────────────────────────────────────────────────

    def get_card(
        self, *, org_id: str, kind: str, card_id: str,
    ) -> Card | None:
        stmt = select(Card).where(
            Card.org_id == org_id, Card.kind == kind, Card.card_id == card_id,
        )
        return self.s.execute(stmt).scalar_one_or_none()

    def list_cards(
        self,
        *,
        org_id: str,
        kind: str | None = None,
        include_deprecated: bool = False,
        origin: str | None = None,
        limit: int | None = None,
    ) -> list[Card]:
        """Vocab-loading entry. Filtered by kind, sorted by card_id."""
        stmt = select(Card).where(Card.org_id == org_id)
        if kind is not None:
            if kind not in KNOWN_CARD_KINDS:
                raise ValueError(f"Unknown card kind {kind!r}")
            stmt = stmt.where(Card.kind == kind)
        if not include_deprecated:
            stmt = stmt.where(Card.deprecated.is_(False))
        if origin is not None:
            stmt = stmt.where(Card.origin == origin)
        stmt = stmt.order_by(Card.kind.asc(), Card.card_id.asc())
        if limit is not None:
            stmt = stmt.limit(limit)
        return list(self.s.execute(stmt).scalars().all())

    def list_summaries(
        self,
        *,
        org_id: str,
        kind: str,
        include_deprecated: bool = False,
        excerpt_chars: int = _DEFAULT_EXCERPT_CHARS,
    ) -> list[CardSummary]:
        """Vocab-loading shortcut used by enrichment stages.

        Returns the prompt-friendly subset (id, kind, title, body_excerpt) so
        callers don't carry SQLAlchemy session lifetime into prompt code.
        """
        rows = self.list_cards(
            org_id=org_id, kind=kind, include_deprecated=include_deprecated,
        )
        return [
            CardSummary(
                card_id=r.card_id, kind=r.kind, title=r.title,
                body_excerpt=_excerpt(r.body, max_chars=excerpt_chars),
            )
            for r in rows
        ]

    def list_refs_from(self, *, card_pk: int) -> list[CardRef]:
        stmt = select(CardRef).where(CardRef.from_card_pk == card_pk).order_by(
            CardRef.relation.asc(), CardRef.to_card_id.asc(),
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
            tier="card",
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
