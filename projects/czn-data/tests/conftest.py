import os

import psycopg
import pytest


@pytest.fixture
def conn():
    """A fresh schema in the Postgres named by TEST_DATABASE_URL (the test drops every table)."""
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set; run `mise run test` to start Postgres")
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("DROP SCHEMA public CASCADE; CREATE SCHEMA public")
        yield connection
