"""Centralized runtime configuration loaded from environment variables and `.env`."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = PROJECT_ROOT / "sql"

# Resolve .env relative to project root so cron/Docker/CLI runs work from any working directory
load_dotenv(PROJECT_ROOT / ".env")


@dataclass(frozen=True)
class DatabaseConfig:
    """PostgreSQL connection settings."""

    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str = "disable"
    connect_timeout: int = 10

    @classmethod
    def from_env(cls) -> DatabaseConfig:
        """Build database configuration from environment variables."""
        return cls(
            host=os.getenv("DB_HOST", "127.0.0.1"),
            port=int(os.getenv("DB_PORT", "5432")),
            dbname=os.getenv("DB_NAME", "nehnutelnosti"),
            user=os.getenv("DB_USER", "scraper"),
            password=os.getenv("DB_PASSWORD", ""),
            sslmode=os.getenv("DB_SSLMODE", "disable"),
            connect_timeout=int(os.getenv("DB_CONNECT_TIMEOUT", "10")),
        )
