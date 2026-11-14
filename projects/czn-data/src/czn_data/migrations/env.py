"""Alembic environment. Runs only through ``czn_data.migrate``, which passes the connection."""

from alembic import context

context.configure(connection=context.config.attributes["connection"])
with context.begin_transaction():
    context.run_migrations()
