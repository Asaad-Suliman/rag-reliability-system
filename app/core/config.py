"""Application settings.

Secrets have no defaults on purpose: a missing one must stop the process with a
readable message, never start a half-configured app that fails later under load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict


class SettingsError(RuntimeError):
    """Raised when the environment cannot produce a valid Settings object."""


class Settings(BaseSettings):
    """Values read from the environment (and `.env` in local development)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- application ---
    app_env: Literal["local", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- postgres (required) ---
    database_url: SecretStr
    db_pool_size: int = Field(default=10, ge=1)
    db_max_overflow: int = Field(default=5, ge=0)
    db_pool_timeout: int = Field(default=30, ge=1)

    # --- chroma ---
    chroma_persist_dir: Path = Path("data/chroma")

    # --- llm provider (required) ---
    llm_api_key: SecretStr
    llm_model: str = "claude-sonnet-5"

    # --- embedding provider (required) ---
    voyage_api_key: SecretStr
    voyage_model: str = "voyage-4-lite"
    voyage_dimensions: int = 1024

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Build Settings once per process, converting pydantic noise into one clear error."""
    try:
        return Settings()  # type: ignore[call-arg]  # values come from the environment
    except ValidationError as exc:
        missing = [
            ".".join(str(part) for part in err["loc"]).upper()
            for err in exc.errors()
            if err["type"] == "missing"
        ]
        invalid = [
            f"{'.'.join(str(part) for part in err['loc']).upper()} ({err['msg']})"
            for err in exc.errors()
            if err["type"] != "missing"
        ]
        problems: list[str] = []
        if missing:
            problems.append("missing required environment variables: " + ", ".join(missing))
        if invalid:
            problems.append("invalid environment variables: " + ", ".join(invalid))
        raise SettingsError(
            "; ".join(problems) + ". Copy .env.example to .env and fill it in."
        ) from exc
