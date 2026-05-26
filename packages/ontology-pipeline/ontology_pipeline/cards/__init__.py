"""Card storage helpers — filesystem ↔ Postgres bridge.

The pipeline package historically loaded `.card.md` files from disk and fed
them straight into the annotation enricher. Cards have now graduated to a
proper Postgres table (`card` + `card_ref`); the `cards` subpackage is the
bridge:

  - `CardLoader.sync_directory(...)`  — Idempotent filesystem-to-DB sync.
                                         Reads `<cards_dir>/{kind}s/*.card.md`
                                         and upserts each into the `card`
                                         table. Returns per-kind counters.
  - `DatabaseVocabSource(...)`         — Drop-in replacement for the
                                         filesystem `load_vocab` path that
                                         reads from Postgres instead.

Enrichers receive vocabulary through the same `SemanticVocab` shape regardless
of whether it came from disk or the DB, so prompt code is unchanged.
"""
from ontology_pipeline.cards.loader import CardLoader, CardLoaderStats
from ontology_pipeline.cards.vocab_source import (
    DatabaseVocabSource,
    FilesystemVocabSource,
    VocabSource,
)

__all__ = [
    "CardLoader",
    "CardLoaderStats",
    "VocabSource",
    "FilesystemVocabSource",
    "DatabaseVocabSource",
]
