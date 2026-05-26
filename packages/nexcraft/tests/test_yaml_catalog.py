from __future__ import annotations

import pytest

from nexcraft.errors import ConfigurationError

yaml = pytest.importorskip("yaml")  # PyYAML is an optional install for v0.1


from nexcraft.catalog.yaml_catalog import YAMLCatalog  # noqa: E402


SAMPLE = """
sources:
  - source_id: prod_pg
    kind: postgres
    display_name: Production Postgres
    tenant_id: default
    config:
      host: pg.internal
      database: prod
    tags:
      team: data
  - source_id: warehouse
    kind: snowflake
    display_name: Warehouse
    tenant_id: default
    config:
      account: acme
"""


@pytest.mark.asyncio
async def test_yaml_catalog_round_trip() -> None:
    cat = YAMLCatalog.from_string(SAMPLE)
    pg = await cat.get_source("prod_pg")
    assert pg.kind == "postgres"
    assert pg.config["host"] == "pg.internal"
    assert pg.tags == {"team": "data"}
    sources = await cat.list_sources(tenant_id="default")
    assert {s.source_id for s in sources} == {"prod_pg", "warehouse"}


def test_yaml_catalog_rejects_missing_fields() -> None:
    with pytest.raises(ConfigurationError):
        YAMLCatalog.from_string(
            """
            sources:
              - source_id: incomplete
                kind: postgres
            """
        )


def test_yaml_catalog_rejects_duplicate_source_id() -> None:
    with pytest.raises(ConfigurationError):
        YAMLCatalog.from_string(
            """
            sources:
              - source_id: dup
                kind: postgres
                display_name: a
                tenant_id: t
              - source_id: dup
                kind: postgres
                display_name: b
                tenant_id: t
            """
        )
