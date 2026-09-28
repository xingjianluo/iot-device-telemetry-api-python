"""Pytest fixtures and logging configuration.

Logging is configured so every unit-test run prints the request *input* and
the service's *decision basis* (accept/reject reasons, credential status
transitions) to stdout -- run ``uv run pytest -s -o log_cli=true`` to watch
them live.
"""

from __future__ import annotations

import logging
import sys

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.store import get_store


@pytest.fixture(autouse=True)
def _isolated_store():
    """Each test starts from empty local state."""
    store = get_store()
    store.reset()
    yield store
    store.reset()


@pytest.fixture()
def client():
    return TestClient(app)


def _configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ))
    root = logging.getLogger("iot")
    if not any(getattr(h, "_iot_configured", False) for h in root.handlers):
        handler._iot_configured = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(logging.INFO)
    root.propagate = True


_configure_logging()
