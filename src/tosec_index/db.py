from __future__ import annotations

from collections.abc import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings


def make_engine(settings: Settings):
    return create_engine(settings.database_url, pool_pre_ping=True)


def make_session_factory(settings: Settings) -> sessionmaker[Session]:
    return sessionmaker(make_engine(settings), expire_on_commit=False)


def session_scope(settings: Settings) -> Iterator[Session]:
    factory = make_session_factory(settings)
    with factory.begin() as session:
        yield session
