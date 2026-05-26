"""CardLoader — sync `semantic_layer/<kind>s/*.card.md` files into Postgres.

This is the canonical authoring → DB path. Cards remain authored on the
filesystem (git workflow). The loader is the bridge that promotes them into
`ontology_store.db.card_models.Card` rows so that:

  - Enrichers (CausalDependencyEnricher, annotate) can read them by SQL.
  - The vector indexer can index from a single source of truth.
  - Cards from multiple authors / sources land in one place with audit.

Idempotency:
  - The DAO computes a content_hash over `(frontmatter, body)` so re-uploading
    unchanged files is a no-op (no audit row, no UPDATE).
  - Re-running the loader against the same directory is safe and cheap.

Conventions:
  - Directory layout: `<cards_dir>/<kind>s/<id>.card.md` (e.g. `causal_nodes/compliance_gap.card.md`).
  - The trailing `s` on the directory matches the existing filesystem loader
    (`annotate.load_vocab`). The loader scans every kind in `KNOWN_CARD_KINDS`.
  - Frontmatter keys recognised: `id`, `kind`, `title`, `aliases`, `markings`,
    `origin`, `layer`, `deprecated`. All optional except an `id` (falls back to
    the filename stem).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass
class CardLoaderStats:
    """Per-run counter set returned by `sync_directory`. Keyed by outcome."""
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    skipped: int = 0
    by_kind: dict[str, dict[str, int]] = field(default_factory=dict)

    def _bump(self, kind: str, outcome: str) -> None:
        bucket = self.by_kind.setdefault(
            kind, {"inserted": 0, "updated": 0, "unchanged": 0, "skipped": 0},
        )
        bucket[outcome] = bucket.get(outcome, 0) + 1
        if outcome == "inserted":
            self.inserted += 1
        elif outcome == "updated":
            self.updated += 1
        elif outcome == "unchanged":
            self.unchanged += 1
        else:
            self.skipped += 1


class CardLoader:
    """Filesystem → Postgres sync for semantic-layer cards.

    Args:
        session_factory: A context-manager factory that yields an open
            SQLAlchemy session (typically `Database.session`).
        org_id: The tenant the cards belong to.
        actor: Audit-trail actor string. Default "card_loader".

    Usage:
        loader = CardLoader(session_factory=db.session, org_id="acme")
        stats = loader.sync_directory(Path("./semantic_layer"))
        # → CardLoaderStats(inserted=12, updated=2, unchanged=8, skipped=0, by_kind=…)
    """

    # Filesystem dir name → card kind (kept as `<kind>s` plural for backward compat).
    # Add new dirs here when KNOWN_CARD_KINDS grows.
    _DIRNAME_TO_KIND: dict[str, str] = {
        "object_types": "object_type",
        "interfaces": "interface",
        "causal_nodes": "causal_node",
        "derived_states": "derived_state",
        "actions": "action",
        "metrics": "metric",
        "events": "event",
        "instructions": "instruction",
        "key_areas": "key_area",
    }

    def __init__(
        self,
        *,
        session_factory: Any,
        org_id: str,
        actor: str = "card_loader",
    ) -> None:
        self._session_factory = session_factory
        self._org_id = org_id
        self._actor = actor

    # ── Entry point ────────────────────────────────────────────────────

    def sync_directory(self, cards_dir: Path) -> CardLoaderStats:
        """Scan `cards_dir` and sync every recognised kind into Postgres.

        Per-file exceptions are caught and counted as `skipped` so one
        malformed card never blocks the rest of the load.
        """
        # Local import to avoid forcing ontology-store at module load time.
        from ontology_store.dao import CardDAO

        stats = CardLoaderStats()
        if not cards_dir.exists() or not cards_dir.is_dir():
            logger.warning("CardLoader: cards_dir=%s does not exist", cards_dir)
            return stats

        with self._session_factory() as session:
            dao = CardDAO(session, actor=self._actor)
            for sub in sorted(cards_dir.iterdir()):
                if not sub.is_dir():
                    continue
                kind = self._DIRNAME_TO_KIND.get(sub.name)
                if kind is None:
                    logger.debug("CardLoader: skipping unrecognised dir %s", sub.name)
                    continue
                for path in sorted(sub.glob("*.card.md")):
                    try:
                        outcome = self._sync_file(dao, kind=kind, path=path)
                    except Exception as exc:  # noqa: BLE001
                        logger.warning(
                            "CardLoader: failed to sync %s: %s", path, exc,
                        )
                        outcome = "skipped"
                    stats._bump(kind, outcome)
            session.commit()
        logger.info(
            "CardLoader synced org=%s: inserted=%d updated=%d unchanged=%d skipped=%d",
            self._org_id, stats.inserted, stats.updated, stats.unchanged, stats.skipped,
        )
        return stats

    # ── Per-file work ──────────────────────────────────────────────────

    def _sync_file(
        self, dao: Any, *, kind: str, path: Path,
    ) -> str:
        """Parse one `.card.md` file and upsert. Returns the DAO's outcome string.

        When the upsert produces a real change (insert OR update), enqueues a
        CARD_EVENTS append task so the vector layer picks it up. Unchanged
        files don't emit events — saves Qdrant churn on idempotent re-loads.
        """
        frontmatter, body = _parse_card_file(path)
        card_id = (
            str(frontmatter.get("id")).strip()
            if frontmatter.get("id")
            else path.stem.replace(".card", "")
        )
        if not card_id:
            raise ValueError(f"card file {path} has no usable id")

        fm_kind = frontmatter.get("kind")
        if fm_kind and fm_kind != kind:
            logger.info(
                "CardLoader: frontmatter kind=%r overrides dir-kind=%r for %s",
                fm_kind, kind, path,
            )
            kind = fm_kind

        aliases = frontmatter.get("aliases") or []
        if not isinstance(aliases, list):
            aliases = [str(aliases)]
        markings = frontmatter.get("markings") or []
        if not isinstance(markings, list):
            markings = [str(markings)]

        row, outcome = dao.upsert_card(
            org_id=self._org_id,
            kind=kind,
            card_id=card_id,
            body=body,
            frontmatter=frontmatter or None,
            layer=str(frontmatter.get("layer") or "semantic"),
            title=(
                str(frontmatter.get("title"))
                if frontmatter.get("title") is not None else None
            ),
            aliases=[str(a) for a in aliases],
            markings=[str(m) for m in markings],
            origin=str(frontmatter.get("origin") or "tenant"),
            source_path=str(path),
        )

        # Emit a CARD event for inserts / updates only (skip unchanged).
        # Three failure modes are tolerated silently: workers/queue isn't
        # installed, the DAO is a test stub without a `.s` session, or the
        # session-based enqueue itself raises. None of them should break
        # the filesystem sync — the worst case is that the vector layer
        # misses this card update and a later reindex run picks it up.
        if outcome in ("inserted", "updated"):
            session = getattr(dao, "s", None)
            card_pk = getattr(row, "card_pk", None)
            if session is not None and card_pk is not None:
                try:
                    from ontology_store.workers.queue import QueueDAO, TaskKind  # type: ignore[import]
                    QueueDAO(session).enqueue(
                        task_kind=TaskKind.EVENT_CARD,
                        payload={
                            "tenant_id": self._org_id,
                            "row_id": card_pk,
                            "is_new": outcome == "inserted",
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — defensive
                    logger.debug(
                        "CardLoader: failed to enqueue CARD event for %s: %s",
                        card_id, exc,
                    )
        return outcome


# ───────────────────────────────────────────────────────────────────────────
# Frontmatter parsing — shared with `annotate.py` historically, duplicated
# here to keep the loader module self-contained.
# ───────────────────────────────────────────────────────────────────────────


def _parse_card_file(path: Path) -> tuple[dict[str, Any], str]:
    text = path.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text.strip()
    fm_block, body = m.group(1), m.group(2)
    fm = yaml.safe_load(fm_block) or {}
    if not isinstance(fm, dict):
        fm = {}
    return fm, body.strip()
