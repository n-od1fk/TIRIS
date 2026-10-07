from functools import lru_cache
from pathlib import Path
from typing import Literal
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """Validated application settings loaded from the repository-level .env file."""

    app_name: str = "TIRIS API"
    environment: str = "development"

    postgres_host: str = "localhost"
    postgres_port: int = 5432
    postgres_db: str
    postgres_user: str
    postgres_password: str = Field(repr=False)
    user_id: UUID = UUID("ea83e192-1223-4755-a219-33ca3bce34bc")
    redis_url: str = "redis://localhost:6379/0"
    github_token: str = Field(default="", repr=False)
    github_api_version: str = "2026-03-10"
    fetch_timeout_seconds: int = Field(default=15, ge=1, le=60)
    fetch_max_bytes: int = Field(default=2_000_000, ge=1000, le=10_000_000)
    fetch_max_items: int = Field(default=100, ge=1, le=500)
    fetch_domain_interval_seconds: float = Field(default=1.0, ge=0.0)
    fetch_dns_mode: Literal["system", "doh"] = "system"
    embedding_cache_dir: Path = PROJECT_ROOT / ".model-cache"
    bailian_api_key: str = Field(default="", repr=False)
    # Use the region/workspace-specific base URL from your own Bailian console.
    bailian_base_url: str = ""
    bailian_embedding_revision: str = Field(default="service-v1", min_length=1, max_length=100)
    embedding_timeout_seconds: int = Field(default=30, ge=5, le=60)

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )


@lru_cache
def get_settings() -> Settings:
    """Load settings once per application process."""

    return Settings()
