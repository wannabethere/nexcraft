"""Event-sourced vector layer — envelope + event-kind taxonomy.

Inference outputs (cards, causal candidates, relation types, data-protection
hints) are written to Qdrant as **immutable events** rather than as
point-in-place updates. Every enrichment run / validation / human edit emits
new events; retrieval reconstructs "current state" by grouping and ranking
events at query time.

Why event-sourced for inference + doc-per-row for the spine:

  - Inference outputs evolve: a `causal_candidate_proposed` event today may
    be `causal_candidate_validated` next week (statistical validator) and
    `causal_candidate_promoted_to_claim` later (human review). Each is a
    distinct fact at a distinct time. Doc-per-row would force us to
    re-embed on every status change and would lose the history.
  - Spine rows (assets, columns) are stable identities. Doc-per-row is the
    right shape — one point per asset_rk, updated when the asset changes.

This module owns the envelope + taxonomy. The collections themselves are
declared in `collections.py`; the indexer (`hierarchy.py`) consumes this
envelope when calling `append_event(...)`.
"""
from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


# ───────────────────────────────────────────────────────────────────────────
# Event kinds — the closed vocabulary of "what happened"
# ───────────────────────────────────────────────────────────────────────────


class EventKind(str, Enum):
    """Stable identifiers for every kind of event we emit.

    The string form is what's stored in `payload["event_kind"]` on each
    Qdrant point. Adding a new kind here is the canonical way to extend
    the event taxonomy — never inline a string elsewhere.
    """

    # ── CAUSAL_EVENTS ────────────────────────────────────────────────
    CAUSAL_CANDIDATE_PROPOSED = "causal_candidate_proposed"
    CAUSAL_CANDIDATE_VALIDATED = "causal_candidate_validated"
    CAUSAL_CANDIDATE_REJECTED = "causal_candidate_rejected"
    CAUSAL_CANDIDATE_INCONCLUSIVE = "causal_candidate_inconclusive"
    CAUSAL_CANDIDATE_PROMOTED_TO_CLAIM = "causal_candidate_promoted_to_claim"

    # ── RELATION_EVENTS ──────────────────────────────────────────────
    RELATION_TYPE_OBSERVED = "relation_type_observed"
    RELATION_TYPE_CANONICALIZED = "relation_type_canonicalized"
    PREDICATE_ATTACHED_TO_EDGE = "predicate_attached_to_edge"

    # ── PROTECTION_EVENTS ────────────────────────────────────────────
    DATA_PROTECTION_HINT_PROPOSED = "data_protection_hint_proposed"
    DATA_PROTECTION_HINT_APPLIED = "data_protection_hint_applied"
    PII_CLASSIFIED = "pii_classified"
    SENSITIVITY_ASSIGNED = "sensitivity_assigned"

    # ── CARD_EVENTS ──────────────────────────────────────────────────
    CARD_AUTHORED = "card_authored"
    CARD_REVISED = "card_revised"
    CARD_DEPRECATED = "card_deprecated"
    CARD_ALIASED = "card_aliased"

    @classmethod
    def for_collection(cls, collection_tier_id: str) -> tuple["EventKind", ...]:
        """Return the event kinds that belong in a given collection."""
        mapping = {
            "causal_events": (
                cls.CAUSAL_CANDIDATE_PROPOSED,
                cls.CAUSAL_CANDIDATE_VALIDATED,
                cls.CAUSAL_CANDIDATE_REJECTED,
                cls.CAUSAL_CANDIDATE_INCONCLUSIVE,
                cls.CAUSAL_CANDIDATE_PROMOTED_TO_CLAIM,
            ),
            "relation_events": (
                cls.RELATION_TYPE_OBSERVED,
                cls.RELATION_TYPE_CANONICALIZED,
                cls.PREDICATE_ATTACHED_TO_EDGE,
            ),
            "protection_events": (
                cls.DATA_PROTECTION_HINT_PROPOSED,
                cls.DATA_PROTECTION_HINT_APPLIED,
                cls.PII_CLASSIFIED,
                cls.SENSITIVITY_ASSIGNED,
            ),
            "card_events": (
                cls.CARD_AUTHORED,
                cls.CARD_REVISED,
                cls.CARD_DEPRECATED,
                cls.CARD_ALIASED,
            ),
        }
        return mapping.get(collection_tier_id, ())


# ───────────────────────────────────────────────────────────────────────────
# Event envelope
# ───────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class EventEnvelope:
    """The fixed-shape outer wrapper for every event.

    Fields:
      - `event_id`: time-sortable unique id (ULID-style — timestamp prefix +
        128 bits of randomness). Use `EventEnvelope.new_id()` to generate.
      - `event_kind`: the closed-vocab `EventKind` value.
      - `subject_rk`: WHAT this event is about. Asset_rk for spine-related
        events, `predicate:domain→range` for relation events, etc.
      - `produced_at`: when the producer emitted this event (UTC).
      - `provenance`: WHO produced it. Free-text but conventional values:
        `llm_cross_asset_causal`, `llm_causal_dependency`, `induce_schema`,
        `stat_validator`, `human:<actor>`, `card_loader`.
      - `run_id`: optional pipeline / workflow run id for correlation.
      - `confidence`: 0..1 — the producer's confidence in this event's
        truthfulness. Validators may emit `confidence=1.0` for facts they
        directly measured.
      - `supersedes`: optional `event_id` of a prior event this one
        replaces. Used to chain corrections without rewriting history.
      - `payload`: event-kind-specific shape. See per-kind builders in
        `workers/event_narrative.py`.
    """
    event_id: str
    event_kind: EventKind
    subject_rk: str
    produced_at: datetime
    provenance: str
    run_id: str | None = None
    confidence: float | None = None
    supersedes: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def new_id(*, kind: EventKind, at: datetime | None = None) -> str:
        """Generate a time-sortable event id.

        Format: `evt_<ISO8601_basic>_<6char_rand>_<kind_short>`. The ISO prefix
        makes the id lexicographically sortable by time, which lines up with
        Qdrant's order-by behavior on payload-indexed strings. We don't use a
        UUID because we want the time-prefix property for free.
        """
        ts = (at or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
        rand = secrets.token_hex(3)  # 6 hex chars
        # Short kind tag — drop after the last underscore for compactness
        short = kind.value.rsplit("_", 1)[-1][:8]
        return f"evt_{ts}_{rand}_{short}"

    def to_qdrant_payload(self) -> dict[str, Any]:
        """Flatten the envelope into a Qdrant payload dict.

        Qdrant payload values must be JSON-scalar / list / dict. Envelope
        fields hoisted to top-level so they can serve as payload filters;
        event-kind-specific data nests under `data:`.
        """
        return {
            "event_id": self.event_id,
            "event_kind": self.event_kind.value,
            "subject_rk": self.subject_rk,
            "produced_at": self.produced_at.isoformat(),
            "provenance": self.provenance,
            "run_id": self.run_id,
            "confidence": self.confidence,
            "supersedes": self.supersedes,
            **self.payload,  # event-kind-specific keys hoisted for filtering
        }


# ───────────────────────────────────────────────────────────────────────────
# Common payload-key conventions
# ───────────────────────────────────────────────────────────────────────────


# These are the keys producers SHOULD use inside `payload` so retrieval can
# filter consistently across event kinds. Not enforced — just convention.
COMMON_PAYLOAD_KEYS = (
    # Routing
    "org_id",
    "source_id",
    # Causal-specific
    "predicate",      # e.g. "leading_indicator_of"
    "subject_ref",    # asset_rk[.column]
    "object_ref",     # asset_rk[.column] or causal_node_id
    "status",         # proposed / validated / rejected / inconclusive
    # Relation-specific
    "domain",         # subject type from concept index
    "range_type",     # object type
    "evidence_count",
    # Protection-specific
    "asset_rk",
    "sensitivity_class",
    "is_pii",
    "pii_categories",
    # Card-specific
    "card_kind",
    "card_id",
)
