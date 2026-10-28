"""Async engine and sessions (SQLAlchemy 2, psycopg 3). Creating them does not connect."""

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.settings import Settings


def create_engine(settings: Settings, **kwargs: object) -> AsyncEngine:
    return create_async_engine(settings.sqlalchemy_url, pool_pre_ping=True, **kwargs)  # type: ignore[arg-type]


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
