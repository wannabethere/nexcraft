"""VocabSource — pluggable backend for SemanticVocab loading.

Enrichment stages (annotate, CausalDependencyEnricher) need a tenant's card
vocabulary at apply-time. v1 read everything from the filesystem. v2 reads
from Postgres. Both implement the same `VocabSource` Protocol so the call
sites in `pipeline.py` and `annotate.py` are agnostic to where cards live.

Implementations:
  - `FilesystemVocabSource(cards_dir, key_areas_vocab_path)`
        Original behaviour — walks `<cards_dir>/{kind}s/*.card.md` per call.
        Best for single-tenant local dev where the filesystem IS the source
        of truth.
  - `DatabaseVocabSource(session_factory, org_id, key_areas_vocab_path=None)`
        Reads from the `card` table via CardDAO. Best for multi-tenant
        production where the loader has already promoted cards into Postgres.
        Falls back to filesystem key_areas YAML if no key_area cards exist
        in the DB (transitional bridge).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class VocabSource(Protocol):
    """Returns a `SemanticVocab` populated for a given tenant."""

    def load(self) -> Any:
        """Return a `SemanticVocab` (annotate.SemanticVocab) instance."""
        ...


@dataclass
class FilesystemVocabSource:
    """Filesystem-backed vocab loader.

    Same behaviour as `annotate.load_vocab` — delegates to it for parity.
    """
    cards_dir: Path | None = None
    key_areas_vocab_path: Path | None = None

    def load(self) -> Any:
        # Local import — avoids circulars at module-import time.
        from ontology_pipeline.annotate import load_vocab
        from ontology_pipeline.config import SemanticLayerConfig
        cfg = SemanticLayerConfig(
            cards_dir=self.cards_dir,
            key_areas_vocab_path=self.key_areas_vocab_path,
        )
        return load_vocab(cfg)


@dataclass
class DatabaseVocabSource:
    """Postgres-backed vocab loader.

    Reads `card` rows scoped to `org_id`, transforming into the same
    `SemanticVocab` shape the filesystem loader produces — so existing
    enricher prompt code is unchanged.

    Excerpt length matches the filesystem loader (300 chars) so prompt token
    budgets behave identically.

    `key_areas` resolution priority:
      1. `card` rows with kind='key_area' for this org.
      2. If none found, falls back to `key_areas_vocab_path` (transitional).
    """
    session_factory: Any  # callable that yields a session context manager
    org_id: str
    key_areas_vocab_path: Path | None = None

    def load(self) -> Any:
        from ontology_pipeline.annotate import (
            CardSummary,
            KeyAreaEntry,
            SemanticVocab,
            _load_key_areas,
        )
        from ontology_store.dao import CardDAO

        vocab = SemanticVocab()
        with self.session_factory() as session:
            dao = CardDAO(session)
            # object_type + causal_node cards used as candidate vocabularies.
            for ot in dao.list_summaries(org_id=self.org_id, kind="object_type"):
                vocab.object_types.append(CardSummary(
                    id=ot.card_id, kind=ot.kind, title=ot.title,
                    body_excerpt=ot.body_excerpt,
                ))
            for cn in dao.list_summaries(org_id=self.org_id, kind="causal_node"):
                vocab.causal_nodes.append(CardSummary(
                    id=cn.card_id, kind=cn.kind, title=cn.title,
                    body_excerpt=cn.body_excerpt,
                ))
            ka_rows = dao.list_summaries(org_id=self.org_id, kind="key_area")
            for ka in ka_rows:
                vocab.key_areas.append(KeyAreaEntry(
                    id=ka.card_id,
                    description=ka.body_excerpt or "",
                ))

        # Bridge: if no key_area cards exist in DB, fall back to the YAML
        # vocab file (the format predates the cards table).
        if not vocab.key_areas and self.key_areas_vocab_path and self.key_areas_vocab_path.exists():
            logger.info(
                "DatabaseVocabSource: no key_area cards for org=%s; falling back to YAML at %s",
                self.org_id, self.key_areas_vocab_path,
            )
            vocab.key_areas = _load_key_areas(self.key_areas_vocab_path)

        return vocab
