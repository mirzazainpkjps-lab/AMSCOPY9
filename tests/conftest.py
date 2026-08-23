"""Shared pytest fixtures.

The application factory is environment driven, so each test gets its own
throw-away SQLite file via ``APP_DB_PATH``.
"""
from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_factory(tmp_path, monkeypatch):
    """Return a callable that builds a fresh app against *db_file*."""

    def _factory(db_file: Path | None = None, **env):
        db_file = db_file or (tmp_path / "test_ams.db")
        monkeypatch.setenv("APP_DB_PATH", str(db_file))
        monkeypatch.setenv("ALLOW_EMPTY_DB", "1")
        monkeypatch.setenv("BACKUP_EMBEDDED_SCHEDULER", "0")
        monkeypatch.setenv("AMS_SCHEMA_VERSION", "v44")
        monkeypatch.setenv("DEFAULT_ADMIN_USER", "Admin")
        monkeypatch.setenv("DEFAULT_ADMIN_PASSWORD", "Admin@fbm12345")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        app_pkg = importlib.import_module("app")
        return app_pkg.create_app()

    return _factory


@pytest.fixture()
def app(app_factory):
    return app_factory()


@pytest.fixture()
def client(app):
    return app.test_client()
