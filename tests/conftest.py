"""Shared test setup: never touch the real database or call real APIs."""

import os

os.environ["DATABASE_URL"] = "sqlite://"
os.environ["LLM_PROVIDER"] = "fake"
os.environ["EXTRACTION_PROVIDER"] = "fake"
os.environ["TIMEZONE"] = "Africa/Lagos"

import pytest  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from owobook import models  # noqa: E402,F401
from owobook.db import Base, make_engine  # noqa: E402


# Set TEST_DATABASE_URL (e.g. a throwaway local Postgres) to run the ledger tests on Postgres too.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "sqlite://")


@pytest.fixture(autouse=True)
def _naira_by_default():
    """Money formats as ₦ unless a test serves a trader from another country."""
    from owobook import currency

    currency.set_current("NGN")
    yield
    currency.set_current("NGN")


@pytest.fixture
def db() -> Session:
    """A fresh, empty database per test (in-memory SQLite unless TEST_DATABASE_URL is set)."""
    engine = make_engine(TEST_DATABASE_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
