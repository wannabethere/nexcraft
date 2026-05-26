"""Engine + session management.

A `Database` instance wraps a SQLAlchemy engine and supplies sessions. Callers
typically do:

    db = Database.from_url(os.environ["ONTOLOGY_STORE_URL"])
    with db.session() as session:
        ...

The `get_session` helper is convenient for ad-hoc use (one-shot scripts).
"""
from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for all models in this package."""


class Database:
    """Encapsulates the SQLAlchemy engine + session factory."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._SessionLocal: sessionmaker[Session] = sessionmaker(
            bind=engine,
            autoflush=False,
            expire_on_commit=False,
        )

    @classmethod
    def from_url(cls, url: str, *, echo: bool = False, pool_size: int = 5) -> "Database":
        engine = create_engine(url, echo=echo, pool_size=pool_size, future=True)
        return cls(engine)

    @classmethod
    def from_env(cls, var: str = "ONTOLOGY_STORE_URL", **kwargs: object) -> "Database":
        url = os.environ.get(var)
        if not url:
            raise RuntimeError(f"Environment variable {var} must be set")
        return cls.from_url(url, **kwargs)  # type: ignore[arg-type]

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


@contextmanager
def get_session(url: str | None = None, *, var: str = "ONTOLOGY_STORE_URL") -> Iterator[Session]:
    """Convenience: build a session for a one-shot operation.

    Prefer instantiating Database once and reusing it for production code.
    """
    if url is None:
        url = os.environ.get(var)
        if not url:
            raise RuntimeError(f"Environment variable {var} must be set or url passed explicitly")
    db = Database.from_url(url)
    with db.session() as s:
        yield s
