import os

from dotenv import load_dotenv

# Load the real .env first so live tests (-m live) get a valid Azure key;
# then fall back to a dummy so offline runs (faked LLM) still import cleanly.
# Must happen before agents.triage_agent is imported (its module-level
# AzureOpenAI client reads the key at construction; no network call there).
load_dotenv()
os.environ.setdefault("AZURE_OPENAI_API_KEY", "test-key")

import pytest

from db import backend
from db.seed import seed
from governance import audit_logger


@pytest.fixture(scope="session", autouse=True)
def seeded_db():
    seed()


@pytest.fixture(autouse=True)
def sqlite_backend(monkeypatch):
    """Pin every test to the local SQLite backend — offline and deterministic."""
    monkeypatch.setenv("DB_BACKEND", "sqlite")
    backend.reset_backend_cache()
    yield
    backend.reset_backend_cache()


@pytest.fixture(autouse=True)
def isolated_audit_db(monkeypatch, tmp_path):
    """Each test writes audit events to its own throwaway DB, never the real one."""
    monkeypatch.setattr(audit_logger, "AUDIT_DB_PATH", tmp_path / "audit_test.db")
