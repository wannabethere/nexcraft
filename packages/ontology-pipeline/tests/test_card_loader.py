"""Tests for CardLoader, VocabSource implementations, and CausalDependencyEnricher's
DB-vocab path.

All DAO interactions go through a recording stub — no real Postgres required.
ORM/DAO behaviour is covered by `ontology-store/tests/test_cards.py` (which
runs against a live Postgres via ONTOLOGY_STORE_TEST_URL).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from ontology_pipeline.annotate import CardSummary as AnnotateCardSummary
from ontology_pipeline.annotate import SemanticVocab
from ontology_pipeline.cards.loader import CardLoader, _parse_card_file
from ontology_pipeline.cards.vocab_source import (
    DatabaseVocabSource,
    FilesystemVocabSource,
)


# ───────────────────────────────────────────────────────────────────────────
# Stubs — recording DAO + session factory, no DB
# ───────────────────────────────────────────────────────────────────────────


@dataclass
class _RecordingDAO:
    """Stand-in for CardDAO. Records upserts; serves list_summaries from store."""
    upserts: list[dict[str, Any]] = field(default_factory=list)
    store: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    summaries_by_kind: dict[tuple[str, str], list[dict[str, Any]]] = field(default_factory=dict)

    def upsert_card(
        self, *, org_id, kind, card_id, body, frontmatter=None,
        layer="semantic", title=None, aliases=None, markings=None,
        origin="tenant", source_path=None,
    ):
        from ontology_store.dao.cards import compute_content_hash
        key = (org_id, kind, card_id)
        new_hash = compute_content_hash(frontmatter=frontmatter, body=body)
        if key in self.store and self.store[key]["content_hash"] == new_hash:
            outcome = "unchanged"
        else:
            outcome = "updated" if key in self.store else "inserted"
            self.store[key] = {
                "content_hash": new_hash, "body": body,
                "frontmatter": frontmatter, "title": title,
                "aliases": list(aliases or []),
                "markings": list(markings or []), "origin": origin,
            }
        self.upserts.append({
            "org_id": org_id, "kind": kind, "card_id": card_id,
            "outcome": outcome, "title": title, "aliases": aliases,
            "markings": markings, "origin": origin,
        })
        return object(), outcome

    def list_summaries(self, *, org_id, kind, include_deprecated=False, excerpt_chars=300):
        # Return preconfigured fixture data; tests prime via `summaries_by_kind`.
        from ontology_store.dao.cards import CardSummary
        return [
            CardSummary(
                card_id=row["card_id"], kind=kind,
                title=row.get("title"), body_excerpt=row.get("body_excerpt", ""),
            )
            for row in self.summaries_by_kind.get((org_id, kind), [])
        ]


class _RecordingSession:
    def __init__(self):
        self.commits = 0

    def __enter__(self): return self
    def __exit__(self, *exc): return None
    def commit(self): self.commits += 1


@pytest.fixture
def patched_dao(monkeypatch):
    """Monkey-patch CardDAO with a single _RecordingDAO across both loader + vocab source."""
    dao = _RecordingDAO()

    def _factory(*args, **kwargs):
        return dao

    monkeypatch.setattr("ontology_store.dao.CardDAO", _factory)
    monkeypatch.setattr("ontology_store.dao.cards.CardDAO", _factory)
    return dao


@pytest.fixture
def session_factory():
    sessions: list[_RecordingSession] = []

    def factory():
        s = _RecordingSession()
        sessions.append(s)
        return s

    factory.sessions = sessions  # type: ignore[attr-defined]
    return factory


# ───────────────────────────────────────────────────────────────────────────
# Frontmatter parsing
# ───────────────────────────────────────────────────────────────────────────


class TestParseCardFile:
    def test_parses_frontmatter_and_body(self, tmp_path: Path):
        path = tmp_path / "compliance_gap.card.md"
        path.write_text(
            "---\nid: compliance_gap\nkind: causal_node\ntitle: Compliance Gap\n---\n"
            "A measurable shortfall between current and required state.\n",
            encoding="utf-8",
        )
        fm, body = _parse_card_file(path)
        assert fm == {
            "id": "compliance_gap",
            "kind": "causal_node",
            "title": "Compliance Gap",
        }
        assert body == "A measurable shortfall between current and required state."

    def test_missing_frontmatter_returns_empty_dict_and_body(self, tmp_path: Path):
        path = tmp_path / "no_frontmatter.card.md"
        path.write_text("Just body content here.\n", encoding="utf-8")
        fm, body = _parse_card_file(path)
        assert fm == {}
        assert body == "Just body content here."

    def test_non_dict_frontmatter_treated_as_empty(self, tmp_path: Path):
        path = tmp_path / "bad.card.md"
        path.write_text("---\n- just\n- a\n- list\n---\nbody\n", encoding="utf-8")
        fm, body = _parse_card_file(path)
        assert fm == {}
        assert body == "body"


# ───────────────────────────────────────────────────────────────────────────
# CardLoader
# ───────────────────────────────────────────────────────────────────────────


def _seed_cards_dir(tmp_path: Path) -> Path:
    """Build a `semantic_layer` tree with one card per recognised kind."""
    root = tmp_path / "semantic_layer"
    (root / "object_types").mkdir(parents=True)
    (root / "object_types" / "employee.card.md").write_text(
        "---\nid: employee\nkind: object_type\ntitle: Employee\n---\n"
        "A person employed by the organisation.\n",
        encoding="utf-8",
    )
    (root / "causal_nodes").mkdir()
    (root / "causal_nodes" / "compliance_gap.card.md").write_text(
        "---\nid: compliance_gap\ntitle: Compliance Gap\n"
        "aliases: [comp_gap]\nmarkings: [internal]\n---\n"
        "Gap between current and required compliance state.\n",
        encoding="utf-8",
    )
    (root / "key_areas").mkdir()
    (root / "key_areas" / "training_compliance.card.md").write_text(
        "---\nid: training_compliance\n---\nMandatory training completion.\n",
        encoding="utf-8",
    )
    # Unknown dir — should be skipped without error.
    (root / "weird_dir").mkdir()
    (root / "weird_dir" / "thing.card.md").write_text("---\nid: thing\n---\nbody\n")
    return root


class TestCardLoader:
    def test_sync_directory_inserts_each_card_under_correct_kind(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        cards_dir = _seed_cards_dir(tmp_path)
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        stats = loader.sync_directory(cards_dir)

        assert stats.inserted == 3  # employee + compliance_gap + training_compliance
        assert stats.updated == 0
        assert stats.unchanged == 0
        assert stats.skipped == 0
        # by_kind breakdown
        assert stats.by_kind["object_type"]["inserted"] == 1
        assert stats.by_kind["causal_node"]["inserted"] == 1
        assert stats.by_kind["key_area"]["inserted"] == 1
        # DAO received correct kinds
        kinds_seen = {(u["kind"], u["card_id"]) for u in patched_dao.upserts}
        assert kinds_seen == {
            ("object_type", "employee"),
            ("causal_node", "compliance_gap"),
            ("key_area", "training_compliance"),
        }

    def test_sync_directory_idempotent_on_second_run(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        cards_dir = _seed_cards_dir(tmp_path)
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        loader.sync_directory(cards_dir)
        stats2 = loader.sync_directory(cards_dir)
        assert stats2.inserted == 0
        assert stats2.updated == 0
        assert stats2.unchanged == 3
        assert stats2.skipped == 0

    def test_frontmatter_kind_overrides_directory_kind(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        root = tmp_path / "sl"
        (root / "object_types").mkdir(parents=True)
        # Card lives under object_types/ but declares kind: causal_node.
        (root / "object_types" / "weird.card.md").write_text(
            "---\nid: weird\nkind: causal_node\n---\nA misplaced card.\n",
            encoding="utf-8",
        )
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        loader.sync_directory(root)
        assert patched_dao.upserts[0]["kind"] == "causal_node"

    def test_aliases_and_markings_pass_through(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        cards_dir = _seed_cards_dir(tmp_path)
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        loader.sync_directory(cards_dir)
        compliance = next(
            u for u in patched_dao.upserts if u["card_id"] == "compliance_gap"
        )
        assert compliance["aliases"] == ["comp_gap"]
        assert compliance["markings"] == ["internal"]

    def test_falls_back_to_filename_id_when_no_frontmatter_id(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        root = tmp_path / "sl"
        (root / "causal_nodes").mkdir(parents=True)
        (root / "causal_nodes" / "fallback_id.card.md").write_text(
            "---\nkind: causal_node\n---\nNo id in frontmatter.\n",
            encoding="utf-8",
        )
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        loader.sync_directory(root)
        assert patched_dao.upserts[0]["card_id"] == "fallback_id"

    def test_missing_dir_returns_empty_stats(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        loader = CardLoader(session_factory=session_factory, org_id="acme")
        stats = loader.sync_directory(tmp_path / "does_not_exist")
        assert stats.inserted == stats.updated == stats.unchanged == stats.skipped == 0


# ───────────────────────────────────────────────────────────────────────────
# DatabaseVocabSource
# ───────────────────────────────────────────────────────────────────────────


class TestDatabaseVocabSource:
    def test_loads_object_types_and_causal_nodes_from_db(
        self, patched_dao: _RecordingDAO, session_factory,
    ):
        patched_dao.summaries_by_kind = {
            ("acme", "object_type"): [
                {"card_id": "employee", "title": "Employee",
                 "body_excerpt": "A person employed by the organisation."},
            ],
            ("acme", "causal_node"): [
                {"card_id": "compliance_gap", "title": "Compliance Gap",
                 "body_excerpt": "Gap between current and required compliance state."},
            ],
            ("acme", "key_area"): [
                {"card_id": "training_compliance", "title": None,
                 "body_excerpt": "Mandatory training completion."},
            ],
        }
        source = DatabaseVocabSource(
            session_factory=session_factory, org_id="acme",
        )
        vocab = source.load()
        assert isinstance(vocab, SemanticVocab)
        assert {c.id for c in vocab.object_types} == {"employee"}
        assert {c.id for c in vocab.causal_nodes} == {"compliance_gap"}
        assert {k.id for k in vocab.key_areas} == {"training_compliance"}
        # Description threaded through from body_excerpt
        assert vocab.key_areas[0].description == "Mandatory training completion."

    def test_falls_back_to_yaml_for_key_areas_when_db_has_none(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        # No key_area cards in DB; provide a YAML file as fallback.
        ka_path = tmp_path / "key_areas_vocab.yaml"
        ka_path.write_text(
            "key_areas:\n  - id: hipaa\n    description: HIPAA compliance.\n"
            "  - workforce\n",
            encoding="utf-8",
        )
        patched_dao.summaries_by_kind = {
            ("acme", "object_type"): [],
            ("acme", "causal_node"): [],
            ("acme", "key_area"): [],
        }
        source = DatabaseVocabSource(
            session_factory=session_factory, org_id="acme",
            key_areas_vocab_path=ka_path,
        )
        vocab = source.load()
        assert {k.id for k in vocab.key_areas} == {"hipaa", "workforce"}

    def test_no_fallback_when_db_has_key_areas(
        self, tmp_path: Path, patched_dao: _RecordingDAO, session_factory,
    ):
        # DB has key_area cards AND a YAML file is provided. DB wins.
        ka_path = tmp_path / "key_areas_vocab.yaml"
        ka_path.write_text(
            "key_areas:\n  - id: yaml_only\n",
            encoding="utf-8",
        )
        patched_dao.summaries_by_kind = {
            ("acme", "object_type"): [],
            ("acme", "causal_node"): [],
            ("acme", "key_area"): [
                {"card_id": "db_only", "title": None, "body_excerpt": ""},
            ],
        }
        source = DatabaseVocabSource(
            session_factory=session_factory, org_id="acme",
            key_areas_vocab_path=ka_path,
        )
        vocab = source.load()
        assert {k.id for k in vocab.key_areas} == {"db_only"}


# ───────────────────────────────────────────────────────────────────────────
# CausalDependencyEnricher — vocab_source path
# ───────────────────────────────────────────────────────────────────────────


class _FakeVocabSource:
    def __init__(self, causal_nodes: list[AnnotateCardSummary]):
        self._cn = causal_nodes
        self.load_calls = 0

    def load(self) -> SemanticVocab:
        self.load_calls += 1
        return SemanticVocab(causal_nodes=self._cn)


class TestCausalEnricherVocabSource:
    def test_loads_vocab_lazily_on_first_apply(self):
        from ontology_pipeline.enrich.causal import CausalDependencyEnricher
        vs = _FakeVocabSource([
            AnnotateCardSummary(
                id="compliance_gap", kind="causal_node",
                title="Compliance Gap", body_excerpt="Some excerpt.",
            ),
        ])
        enricher = CausalDependencyEnricher(vocab_source=vs)
        # Not loaded until refresh_vocab / _ensure_vocab_loaded is called.
        assert vs.load_calls == 0
        enricher.refresh_vocab()
        assert vs.load_calls == 1
        assert enricher._known_ids == ["compliance_gap"]
        assert enricher._known_excerpts == {"compliance_gap": "Some excerpt."}

    def test_vocab_source_overrides_explicit_args(self):
        from ontology_pipeline.enrich.causal import CausalDependencyEnricher
        vs = _FakeVocabSource([
            AnnotateCardSummary(
                id="from_db", kind="causal_node",
                title=None, body_excerpt="",
            ),
        ])
        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["from_explicit"],
            vocab_source=vs,
        )
        enricher.refresh_vocab()
        # After refresh, DB vocab replaces explicit.
        assert enricher._known_ids == ["from_db"]

    def test_failed_vocab_load_falls_back_to_explicit_ids(self):
        from ontology_pipeline.enrich.causal import CausalDependencyEnricher

        class _Boom:
            def load(self):
                raise RuntimeError("db down")

        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["safe_id"],
            known_causal_node_excerpts={"safe_id": "fallback"},
            vocab_source=_Boom(),
        )
        enricher.refresh_vocab()
        assert enricher._known_ids == ["safe_id"]
        assert enricher._known_excerpts == {"safe_id": "fallback"}

    def test_no_vocab_source_preserves_explicit_args(self):
        from ontology_pipeline.enrich.causal import CausalDependencyEnricher
        enricher = CausalDependencyEnricher(
            known_causal_node_ids=["a", "b"],
            known_causal_node_excerpts={"a": "x"},
        )
        # No vocab source — explicit args should be loaded immediately.
        assert enricher._known_ids == ["a", "b"]
        assert enricher._known_excerpts == {"a": "x"}
        assert enricher._vocab_loaded is True


# ───────────────────────────────────────────────────────────────────────────
# FilesystemVocabSource — parity with annotate.load_vocab
# ───────────────────────────────────────────────────────────────────────────


class TestFilesystemVocabSource:
    def test_load_returns_semantic_vocab_from_disk(self, tmp_path: Path):
        cards_dir = _seed_cards_dir(tmp_path)
        source = FilesystemVocabSource(cards_dir=cards_dir)
        vocab = source.load()
        assert {c.id for c in vocab.object_types} == {"employee"}
        assert {c.id for c in vocab.causal_nodes} == {"compliance_gap"}
        # key_areas are NOT loaded via the cards dir path (filesystem loader
        # historically expects a separate YAML for key_areas).
        assert vocab.key_areas == []
