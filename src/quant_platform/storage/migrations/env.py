"""Alembic environment; connection URL is supplied in memory, not in an ini file."""

from alembic import context
from sqlalchemy import create_engine

engine = create_engine(context.config.attributes["url"], pool_pre_ping=True)
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
