"""Smoke tests for CardDAO against a real Postgres.

Same gating as `tests/test_store.py` — requires `ONTOLOGY_STORE_TEST_URL`
pointing at a clean test database. Run locally with:

    export ONTOLOGY_STORE_TEST_URL=postgresql+psycopg://...@localhost/ontology_test
    pytest tests/test_cards.py

ORM-level coverage uses Postgres-specific types (ARRAY, JSONB, CHECK
constraints) so SQLite is not a workable substitute.
"""
from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine

from ontology_store import CardDAO, Database
from ontology_store.dao.cards import compute_content_hash
from ontology_store.db.card_models import KNOWN_CARD_KINDS
from ontology_store.db.engine import Base
from ontology_store.db.models import Organization

pytestmark = pytest.mark.skipif(
    not os.environ.get("ONTOLOGY_STORE_TEST_URL"),
    reason="ONTOLOGY_STORE_TEST_URL must point at a clean Postgres test database",
)


@pytest.fixture(scope="function")
def db() -> Database:
    url = os.environ["ONTOLOGY_STORE_TEST_URL"]
    engine = create_engine(url, future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return Database(engine)


@pytest.fixture(scope="function")
def org(db: Database) -> str:
    with db.session() as s:
        s.add(Organization(org_id="acme", display_name="Acme Inc."))
    return "acme"


class TestUpsertIdempotency:
    def test_first_call_inserts(self, db: Database, org: str):
        with db.session() as s:
            row, outcome = CardDAO(s).upsert_card(
                org_id=org, kind="causal_node",
                card_id="compliance_gap",
                body="A gap between current and required state.",
                frontmatter={"id": "compliance_gap"},
                title="Compliance Gap",
                aliases=["comp_gap"],
                markings=["internal"],
            )
            assert outcome == "inserted"
            assert row.card_pk is not None
            assert row.aliases == ["comp_gap"]
            assert row.markings == ["internal"]
            assert row.content_hash != ""

    def test_same_content_second_call_is_unchanged(self, db: Database, org: str):
        with db.session() as s:
            dao = CardDAO(s)
            kwargs = dict(
                org_id=org, kind="causal_node", card_id="compliance_gap",
                body="A gap.", frontmatter={"id": "compliance_gap"},
            )
            dao.upsert_card(**kwargs)
        with db.session() as s:
            _row, outcome = CardDAO(s).upsert_card(**kwargs)
            assert outcome == "unchanged"

    def test_changed_body_triggers_update(self, db: Database, org: str):
        with db.session() as s:
            CardDAO(s).upsert_card(
                org_id=org, kind="causal_node", card_id="x",
                body="v1", frontmatter={"id": "x"},
            )
        with db.session() as s:
            _row, outcome = CardDAO(s).upsert_card(
                org_id=org, kind="causal_node", card_id="x",
                body="v2 — changed", frontmatter={"id": "x"},
            )
            assert outcome == "updated"
        with db.session() as s:
            row = CardDAO(s).get_card(org_id=org, kind="causal_node", card_id="x")
            assert row is not None
            assert row.body == "v2 — changed"

    def test_unknown_kind_rejected(self, db: Database, org: str):
        with db.session() as s:
            with pytest.raises(ValueError):
                CardDAO(s).upsert_card(
                    org_id=org, kind="not_a_kind", card_id="x", body="",
                )


class TestListCards:
    def test_filters_by_kind(self, db: Database, org: str):
        with db.session() as s:
            dao = CardDAO(s)
            dao.upsert_card(org_id=org, kind="causal_node", card_id="a", body="x")
            dao.upsert_card(org_id=org, kind="causal_node", card_id="b", body="y")
            dao.upsert_card(org_id=org, kind="object_type", card_id="emp", body="z")
        with db.session() as s:
            cn = CardDAO(s).list_cards(org_id=org, kind="causal_node")
            ot = CardDAO(s).list_cards(org_id=org, kind="object_type")
            assert {c.card_id for c in cn} == {"a", "b"}
            assert {c.card_id for c in ot} == {"emp"}

    def test_excludes_deprecated_by_default(self, db: Database, org: str):
        with db.session() as s:
            dao = CardDAO(s)
            dao.upsert_card(org_id=org, kind="causal_node", card_id="active", body="")
            row, _ = dao.upsert_card(org_id=org, kind="causal_node", card_id="dead", body="")
            dao.mark_deprecated(card_pk=row.card_pk)
        with db.session() as s:
            active = CardDAO(s).list_cards(org_id=org, kind="causal_node")
            both = CardDAO(s).list_cards(
                org_id=org, kind="causal_node", include_deprecated=True,
            )
            assert {c.card_id for c in active} == {"active"}
            assert {c.card_id for c in both} == {"active", "dead"}

    def test_list_summaries_returns_prompt_friendly_shape(self, db: Database, org: str):
        with db.session() as s:
            CardDAO(s).upsert_card(
                org_id=org, kind="causal_node", card_id="compliance_gap",
                body="A " + ("very long " * 200) + " body.",
                title="Compliance Gap",
            )
        with db.session() as s:
            summaries = CardDAO(s).list_summaries(
                org_id=org, kind="causal_node",
            )
            assert len(summaries) == 1
            assert summaries[0].card_id == "compliance_gap"
            assert summaries[0].title == "Compliance Gap"
            # Excerpt truncated to ~300 chars
            assert len(summaries[0].body_excerpt) <= 310


class TestCardRefs:
    def test_upsert_ref_and_list(self, db: Database, org: str):
        with db.session() as s:
            dao = CardDAO(s)
            row, _ = dao.upsert_card(
                org_id=org, kind="causal_node", card_id="src", body="",
            )
            dao.upsert_card(org_id=org, kind="causal_node", card_id="tgt", body="")
            _ref, outcome = dao.upsert_card_ref(
                from_card_pk=row.card_pk, to_kind="causal_node",
                to_card_id="tgt", relation="moderates",
            )
            assert outcome == "inserted"
            src_pk = row.card_pk
        with db.session() as s:
            refs = CardDAO(s).list_refs_from(card_pk=src_pk)
            assert len(refs) == 1
            assert refs[0].to_card_id == "tgt"
            assert refs[0].relation == "moderates"

    def test_ref_idempotent(self, db: Database, org: str):
        with db.session() as s:
            dao = CardDAO(s)
            row, _ = dao.upsert_card(org_id=org, kind="causal_node", card_id="a", body="")
            dao.upsert_card(org_id=org, kind="causal_node", card_id="b", body="")
            dao.upsert_card_ref(
                from_card_pk=row.card_pk, to_kind="causal_node",
                to_card_id="b", relation="mentions",
            )
            _ref, outcome = dao.upsert_card_ref(
                from_card_pk=row.card_pk, to_kind="causal_node",
                to_card_id="b", relation="mentions",
            )
            assert outcome == "unchanged"


class TestContentHash:
    def test_stable_across_call_orders(self):
        h1 = compute_content_hash(
            frontmatter={"a": 1, "b": [1, 2, 3]}, body="some body",
        )
        h2 = compute_content_hash(
            frontmatter={"b": [1, 2, 3], "a": 1}, body="some body",
        )
        assert h1 == h2

    def test_body_difference_changes_hash(self):
        h1 = compute_content_hash(frontmatter={"a": 1}, body="v1")
        h2 = compute_content_hash(frontmatter={"a": 1}, body="v2")
        assert h1 != h2

    def test_none_frontmatter_treated_as_empty(self):
        h_none = compute_content_hash(frontmatter=None, body="b")
        h_empty = compute_content_hash(frontmatter={}, body="b")
        assert h_none == h_empty


def test_known_card_kinds_match_check_constraint():
    """If a kind is added to KNOWN_CARD_KINDS but the CHECK constraint isn't
    updated, inserts of that kind will fail at runtime. Catch the drift here.
    """
    expected = {
        "object_type", "interface", "causal_node", "derived_state",
        "action", "metric", "event", "instruction", "key_area",
    }
    assert set(KNOWN_CARD_KINDS) == expected
