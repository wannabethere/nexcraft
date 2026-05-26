from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from ontology_foundry.relations.seeds import (
    RelationSeed,
    SeedPack,
    default_pack_dirs,
)
from ontology_foundry.relations.seeds.cdm import (
    _predicate_from_attribute,
    import_cdm_folder,
)
from ontology_foundry.relations.seeds.loader import (
    PackLoadError,
    compose,
    load_packs,
)


def test_default_packs_load_and_contain_expected_predicates() -> None:
    packs = load_packs([default_pack_dirs()[0]])
    assert "common" in packs
    assert "billing" in packs

    common_preds = set(packs["common"].predicates())
    billing_preds = set(packs["billing"].predicates())

    assert {"has_part", "member_of", "caused_by"} <= common_preds
    assert {"has_contract", "has_payment", "filed_claim"} <= billing_preds


def test_compose_merges_and_lets_later_packs_override(tmp_path: Path) -> None:
    (tmp_path / "a.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "a",
                "seeds": [
                    {"predicate": "has_x", "description": "from a"},
                    {"predicate": "only_in_a"},
                ],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "b.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "b",
                "seeds": [
                    {"predicate": "has_x", "description": "from b (wins)"},
                    {"predicate": "only_in_b"},
                ],
            }
        ),
        encoding="utf-8",
    )

    packs = load_packs([tmp_path])
    merged = compose(packs, names=["a", "b"])

    by_pred = {s.predicate: s for s in merged.seeds}
    assert by_pred["has_x"].description == "from b (wins)"
    assert "only_in_a" in by_pred
    assert "only_in_b" in by_pred
    assert merged.name == "a+b"


def test_later_directory_overrides_earlier_for_same_pack_name(tmp_path: Path) -> None:
    early = tmp_path / "early"
    late = tmp_path / "late"
    early.mkdir()
    late.mkdir()

    (early / "billing.yaml").write_text(
        yaml.safe_dump(
            {"name": "billing", "seeds": [{"predicate": "has_contract"}]}
        ),
        encoding="utf-8",
    )
    (late / "billing.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "billing",
                "seeds": [{"predicate": "has_contract"}, {"predicate": "custom_relation"}],
            }
        ),
        encoding="utf-8",
    )

    packs = load_packs([early, late])
    assert "custom_relation" in packs["billing"].predicates()


def test_compose_unknown_pack_names_are_dropped(tmp_path: Path) -> None:
    (tmp_path / "real.yaml").write_text(
        yaml.safe_dump({"name": "real", "seeds": [{"predicate": "p"}]}),
        encoding="utf-8",
    )
    packs = load_packs([tmp_path])
    merged = compose(packs, names=["real", "nonexistent"])
    assert merged.name == "real"
    assert {s.predicate for s in merged.seeds} == {"p"}


def test_missing_directory_is_silently_skipped(tmp_path: Path) -> None:
    packs = load_packs([tmp_path / "does-not-exist"])
    assert packs == {}


def test_malformed_pack_raises_pack_load_error(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(yaml.safe_dump({"seeds": []}), encoding="utf-8")  # missing 'name'
    with pytest.raises(PackLoadError):
        load_packs([tmp_path])


def test_seed_without_predicate_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text(
        yaml.safe_dump({"name": "x", "seeds": [{"description": "no predicate"}]}),
        encoding="utf-8",
    )
    with pytest.raises(PackLoadError):
        load_packs([tmp_path])


def test_predicate_from_attribute_handles_common_cdm_styles() -> None:
    assert _predicate_from_attribute("primarycontactid", "Contact") == "has_primarycontact"
    assert _predicate_from_attribute("primaryContactId", "Contact") == "has_primary_contact"
    assert _predicate_from_attribute("parentAccountId", "Account") == "has_parent_account"
    assert _predicate_from_attribute("", "Account") == "has_account"
    assert _predicate_from_attribute("has_invoice", "Invoice") == "has_invoice"


def test_cdm_round_trip(tmp_path: Path) -> None:
    """Synthesize a minimal CDM-shaped JSON, import it, load it back as a pack."""
    cdm_dir = tmp_path / "cdm"
    cdm_dir.mkdir()
    (cdm_dir / "Account.cdm.json").write_text(
        json.dumps(
            {
                "definitions": [
                    {
                        "entityName": "Account",
                        "hasAttributes": [
                            {
                                "name": "primaryContactId",
                                "entity": {"entityReference": "Contact"},
                                "description": "Primary contact for the account.",
                            },
                            {
                                "name": "parentAccountId",
                                "entity": {"entityReference": "Account"},
                            },
                            {"name": "name"},  # scalar — should be ignored
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    pack_dict = import_cdm_folder(cdm_dir, pack_name="sales")
    assert pack_dict["name"] == "sales"
    assert pack_dict["source"] == "cdm"

    predicates = {s["predicate"] for s in pack_dict["seeds"]}
    assert predicates == {"has_primary_contact"}  # self-ref to Account dropped, scalar ignored

    out_path = tmp_path / "packs" / "sales.yaml"
    out_path.parent.mkdir()
    out_path.write_text(yaml.safe_dump(pack_dict, sort_keys=False), encoding="utf-8")

    loaded = load_packs([out_path.parent])
    assert "sales" in loaded
    primary_contact = next(
        s for s in loaded["sales"].seeds if s.predicate == "has_primary_contact"
    )
    assert primary_contact.preferred_domain == ("Account",)
    assert primary_contact.preferred_range == ("Contact",)


def test_seed_pack_predicates_returns_tuple() -> None:
    pack = SeedPack(
        name="x",
        seeds=(RelationSeed(predicate="a"), RelationSeed(predicate="b")),
    )
    assert pack.predicates() == ("a", "b")
