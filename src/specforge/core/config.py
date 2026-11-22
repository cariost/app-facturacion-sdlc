"""Configuración tipada (pydantic-settings, prefijo ``SPECFORGE_``)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Debe coincidir con `vector(1536)` de la migración 0001. Cambiarlo exige una migración.
SCHEMA_EMBEDDING_DIM = 1536

_SQLALCHEMY_SCHEME = "postgresql+psycopg://"
_LIBPQ_SCHEME = "postgresql://"
_ACCEPTED_SCHEMES = ("postgresql+psycopg://", "postgresql://", "postgres://")


def normalize_dsn(value: str) -> str:
    """Devuelve el DSN con esquema libpq (`postgresql://`)."""
    for scheme in _ACCEPTED_SCHEMES:
        if value.startswith(scheme):
            return _LIBPQ_SCHEME + value[len(scheme) :]
    raise ValueError(f"el DSN debe usar uno de los esquemas {_ACCEPTED_SCHEMES}")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SPECFORGE_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    env: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    database_url: str = "postgresql://specforge:specforge@localhost:5433/specforge"
    db_pool_min_size: int = Field(default=1, ge=1)
    db_pool_max_size: int = Field(default=10, ge=1)

    embedding_dim: int = SCHEMA_EMBEDDING_DIM
    max_dev_qa_retries: int = Field(default=3, ge=0, le=3)
    executive_summary_max_lines: int = Field(default=15, ge=1, le=15)

    # ModelRouter: tier FAST para trabajo rutinario, REASONING para trabajo complejo.
    llm_provider: Literal["anthropic"] = "anthropic"
    model_fast: str = "claude-haiku-5-5"
    model_reasoning: str = "claude-opus-5-5"
    effort_fast: Literal["low", "medium", "high"] = "low"
    effort_reasoning: Literal["medium", "high", "xhigh", "max"] = "high"
    llm_max_output_tokens: int = Field(default=16000, ge=1024, le=64000)
    llm_max_repairs: int = Field(default=1, ge=0, le=3)
    llm_refusal_fallback: bool = True
    # Contexto estimado a partir del cual una tarea FAST se escala a REASONING.
    routing_escalate_above_tokens: int = Field(default=24000, ge=1000)

    @field_validator("database_url")
    @classmethod
    def _normalize_dsn(cls, value: str) -> str:
        return normalize_dsn(value)

    @field_validator("embedding_dim")
    @classmethod
    def _match_schema_dim(cls, value: int) -> int:
        if value != SCHEMA_EMBEDDING_DIM:
            raise ValueError(
                f"embedding_dim={value} no coincide con vector({SCHEMA_EMBEDDING_DIM}) del esquema"
            )
        return value

    @model_validator(mode="after")
    def _pool_bounds(self) -> Settings:
        if self.db_pool_min_size > self.db_pool_max_size:
            raise ValueError("db_pool_min_size no puede superar db_pool_max_size")
        return self

    @property
    def libpq_url(self) -> str:
        """DSN para psycopg / AsyncPostgresSaver."""
        return self.database_url

    @property
    def sqlalchemy_url(self) -> str:
        """DSN para SQLAlchemy async con el driver psycopg3."""
        return _SQLALCHEMY_SCHEME + self.database_url[len(_LIBPQ_SCHEME) :]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
