from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# Settings are read at import time, so the environment has to be set up first.
_TMP = tempfile.mkdtemp(prefix="crm-tests-")
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_TMP}/test.db",
        "MEDIA_ROOT": f"{_TMP}/media",
        "OPERATOR_NAME": "Dana Quest",
        "OPERATOR_BUSINESS": "Quest Web Services",
        "OPERATOR_EMAIL": "dana@questws.example",
        "OPERATOR_ALIASES": "info@questws.example",
        "OPERATOR_NUMBERS": "+16135550100,+16135550101",
        "DEFAULT_REGION": "CA",
        "TIMEZONE": "America/Toronto",
        "ANTHROPIC_API_KEY": "",
    }
)

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from crm.models import Base  # noqa: E402


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    s = maker()
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def tmp_media() -> Path:
    return Path(_TMP) / "media"
