"""Alembic env. Reads DB URL from ONTOLOGY_STORE_URL env var by default."""
from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import the Base + all models so autogenerate sees them
from ontology_store.db.engine import Base
from ontology_store.db import models  # noqa: F401 — imported for metadata side-effect
# Workers register additional tables (reindex_queue, etc.) on the same Base
try:
    from ontology_store.workers import queue as _workers_queue  # noqa: F401
except Exception:
    pass

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Inject the URL from env if alembic.ini's sqlalchemy.url is empty
if not config.get_main_option("sqlalchemy.url"):
    db_url = os.environ.get("ONTOLOGY_STORE_URL")
    if not db_url:
        raise RuntimeError(
            "ONTOLOGY_STORE_URL env var must be set "
            "(or sqlalchemy.url in alembic.ini) before running migrations"
        )
    # Escape % so ConfigParser interpolation doesn't choke on percent-encoded
    # passwords (e.g. ...%24...%25...). ConfigParser de-escapes %% → % on read,
    # so SQLAlchemy still receives the correct URL.
    config.set_main_option("sqlalchemy.url", db_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
