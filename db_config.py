"""
Database configuration module.
Supports SQLite (default) and PostgreSQL via SQLAlchemy.
Database URL is configurable via environment variable DATABASE_URL.
"""
from __future__ import annotations

import os
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL")

if not DATABASE_URL:
    db_path = Path(__file__).parent / "mf_dashboard.db"
    DATABASE_URL = f"sqlite:///{db_path}"

ENGINE_PARAMS = {}

if DATABASE_URL.startswith("sqlite"):
    ENGINE_PARAMS = {
        "connect_args": {"check_same_thread": False},
    }