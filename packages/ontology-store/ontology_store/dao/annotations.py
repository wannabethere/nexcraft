"""AnnotationDAO — write concepts/key_areas/causal_relations with no-clobber + provenance.

Per `mdl_table_concept_annotation_spec.md` §5.3:
- LLM-proposed annotations (`source='llm_enrichment'`) auto-apply ONLY when no
  prior provenance row exists for that (asset_rk, field) with a higher-trust
  source (`human` or `rule_*`).
- Service annotations (`source='rule_<service>'`) overwrite LLM-prior but not
  human-prior.
- Human annotations always win and disable subsequent LLM overwrites.

Every write produces an `asset_annotation_provenance` row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from ontology_store.db.models import (
    AssetAnnotationProvenance,
    HierarchyAudit,
    TableExt,
)
from ontology_store.schemas import AssetAnnotations

logger = logging.getLogger(__name__)


_FIELDS = ("concepts", "key_areas", "causal_relations")


def _trust(source: str) -> int:
    """Trust ranking — higher wins. Compared field-by-field."""
    if source == "human":
        return 3
    if source.startswith("rule_"):
        return 2
    if source == "llm_enrichment":
        return 1
    return 0


class AnnotationDAO:
    def __init__(self, session: Session, *, actor: str = "system") -> None:
        self.s = session
        self.actor = actor

    def write(self, anno: AssetAnnotations) -> dict[str, str]:
        """Write annotations with no-clobber. Returns per-field outcome.

        Per-field outcome values:
          - 'applied'        — written.
          - 'skipped_clobber' — preserved higher-trust prior write.
          - 'noop_empty'     — no value to write.
        """
        target = self.s.get(TableExt, anno.asset_rk)
        if target is None:
            raise ValueError(
                f"No table_ext row for asset {anno.asset_rk!r}; "
                "ensure the spine + table_ext rows are created first."
            )

        outcomes: dict[str, str] = {}
        proposed = {
            "concepts": anno.concepts,
            "key_areas": anno.key_areas,
            "causal_relations": anno.causal_relations,
        }
        new_trust = _trust(anno.source)

        for field in _FIELDS:
            value = proposed[field]
            if not value:
                outcomes[field] = "noop_empty"
                continue

            prior = self._latest_provenance(anno.asset_rk, field)
            if prior is not None and _trust(prior.source) > new_trust:
                outcomes[field] = "skipped_clobber"
                self._audit_skip(anno=anno, field=field, prior=prior)
                continue

            # Apply the write to table_ext (atomic column-level set)
            setattr(target, field, value)
            target.updated_at = datetime.now(timezone.utc)

            # Record provenance
            self.s.add(AssetAnnotationProvenance(
                asset_rk=anno.asset_rk,
                field=field,
                source=anno.source,
                source_model=anno.source_model,
                confidence=anno.confidence,
                rationale=anno.rationale,
                written_by=anno.written_by or self.actor,
                written_at=anno.written_at,
            ))
            self._audit_apply(anno=anno, field=field, new_value=value)
            outcomes[field] = "applied"

        # Annotations affect the asset's payload filters (concepts/key_areas/causal_relations)
        # AND the narrative when bound card text changes — enqueue a reindex.
        if any(o == "applied" for o in outcomes.values()):
            try:
                from ontology_store.workers.queue import enqueue_asset_reindex
                enqueue_asset_reindex(self.s, asset_rk=anno.asset_rk)
            except Exception as exc:
                logger.debug("Skipping reindex enqueue after annotation write: %s", exc)
        return outcomes

    def latest_provenance_for(self, asset_rk: str, field: str) -> AssetAnnotationProvenance | None:
        return self._latest_provenance(asset_rk, field)

    # ── helpers ─────────────────────────────────────────────────────────

    def _latest_provenance(self, asset_rk: str, field: str) -> AssetAnnotationProvenance | None:
        stmt = (
            select(AssetAnnotationProvenance)
            .where(
                AssetAnnotationProvenance.asset_rk == asset_rk,
                AssetAnnotationProvenance.field == field,
            )
            .order_by(desc(AssetAnnotationProvenance.written_at))
            .limit(1)
        )
        return self.s.execute(stmt).scalar_one_or_none()

    def _audit_apply(self, *, anno: AssetAnnotations, field: str, new_value: list[str]) -> None:
        self.s.add(HierarchyAudit(
            actor=anno.written_by or self.actor,
            action="update",
            tier="asset_annotation",
            entity_uid=anno.asset_rk,
            field_path=field,
            new_value={"value": new_value, "source": anno.source, "confidence": anno.confidence},
        ))

    def _audit_skip(
        self, *, anno: AssetAnnotations, field: str, prior: AssetAnnotationProvenance,
    ) -> None:
        self.s.add(HierarchyAudit(
            actor=anno.written_by or self.actor,
            action="sync_skipped_clobber",
            tier="asset_annotation",
            entity_uid=anno.asset_rk,
            field_path=field,
            new_value={
                "would_have_written": getattr(anno, field),
                "preserved_source": prior.source,
                "preserved_written_by": prior.written_by,
                "preserved_at": prior.written_at.isoformat() if prior.written_at else None,
            },
        ))
