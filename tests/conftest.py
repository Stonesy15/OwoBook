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


@pytest.fixture
def db() -> Session:
    """A fresh in-memory database per test."""
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()
