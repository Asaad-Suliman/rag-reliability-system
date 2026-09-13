"""Application settings.

Secrets have no defaults on purpose: a missing one must stop the process with a
readable message, never start a half-configured app that fails later under load.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# app/core/config.py -> app/core -> app -> repo root. Anchoring here, not to
# the process's CWD, matters because `ingest_root` is a security boundary:
# resolving a relative default against CWD would let the allowlist silently
# point somewhere else if the CLI is ever launched from outside the repo.
_REPO_ROOT = Path(__file__).resolve().parents[2]


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

    # --- ingestion ---
    # The controlled directory parsing must stay inside — the CLI's `ingest`
    # resolves its path argument and rejects anything outside this root.
    # Resolved below to an absolute, symlink-canonical path at load time, so
    # the security boundary itself can never be ambiguous or CWD-dependent.
    ingest_root: Path = Path("data/uploads")

    @field_validator("ingest_root")
    @classmethod
    def _resolve_ingest_root(cls, v: Path) -> Path:
        if v.is_absolute():
            return v.resolve()
        return (_REPO_ROOT / v).resolve()

    # --- reranker ---
    # "local" is the default implementation: when a reranker runs it is the
    # local int8 ONNX cross-encoder, never a hosted API. The benchmark
    # (`app.cli evaluate`) still defaults to "none" — its baseline arm is
    # deliberately no-rerank, and chunk 5 turns it on per arm explicitly.
    #
    # Weights are baked into the image and verified at load. If they are missing
    # or do not match the manifest, startup FAILS — it never falls back to
    # "none". A reranker that silently is not there produces answers that look
    # reranked and are not.
    reranker: Literal["none", "local"] = "local"
    reranker_model_dir: Path = Path("models/reranker")
    reranker_manifest_path: Path = Path("scripts/reranker_model.sha256")
    reranker_intra_op_threads: int = Field(default=4, ge=1)

    @field_validator("reranker_model_dir", "reranker_manifest_path")
    @classmethod
    def _resolve_reranker_path(cls, v: Path) -> Path:
        # Anchored to the repo root like `ingest_root`, not to the process CWD:
        # `app.cli` and `uvicorn` get launched from different directories, and a
        # relative default resolved against CWD would find the weights from one
        # and fail from the other. An absolute override (a container mount)
        # passes through untouched.
        if v.is_absolute():
            return v.resolve()
        return (_REPO_ROOT / v).resolve()

    # --- llm provider (required) ---
    llm_api_key: SecretStr
    llm_model: str = "claude-sonnet-5"
    llm_timeout: float = 60.0
    # Must not exceed `ContextBudget.answer_reserve` -- asserted at startup in
    # `app/main.py`, not here: this module must not import the budgeter.
    llm_max_tokens: int = 1024

    # --- client credential and request limits (chunk 8.10) ---
    # An interim MACHINE credential, not a user identity: one shared key, sent in
    # the `X-API-Key` header. Deliberately not `Authorization: Bearer`, which is
    # reserved for Step 04's JWTs, so Step 04 can delete this path in one commit
    # rather than untangle it from a header it needs.
    #
    # Required with no default, like every other secret here, and additionally
    # length-checked: a 4-character shared key is a credential in name only, and
    # the whole point of this setting is that the route stops being open. 32
    # characters is the floor, not a recommendation -- generate one with
    # `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
    #
    # Only its sha256 digest is kept at runtime (`app/main.py` lifespan); the raw
    # value is never logged or echoed.
    client_api_key: SecretStr = Field(min_length=32)

    # 20/minute, from API Contract section 7's generation figure for
    # `POST /conversations/*/messages` -- not the 120 "everything else" default.
    # This route calls the same paid model per request, so the generation number
    # is the one that describes its cost.
    rate_limit_per_minute: int = Field(default=20, ge=1)

    # A per-UTC-day ceiling on ADMITTED requests. The per-minute limit alone
    # bounds a burst, not a bill: 20/minute sustained is 28,800 paid calls a day.
    # Charged on admission, before retrieval, so a request that later fails in
    # embedding or generation still counts -- it spent money either way.
    daily_request_cap: int = Field(default=200, ge=1)

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
