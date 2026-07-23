"""Typed application configuration loaded from environment variables."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self
from urllib.parse import parse_qs, urlparse

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnvironment(StrEnum):
    """Supported runtime environments."""

    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Validated process configuration.

    Connection URLs are secret values so accidental model representations do not
    disclose credentials. Code that owns a connection may explicitly unwrap them.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnvironment = AppEnvironment.DEVELOPMENT
    app_name: str = "Python RNG Game Platform"
    app_version: str = "0.1.0"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: SecretStr
    redis_url: SecretStr | None = None
    outcome_hmac_secret: SecretStr
    settlement_admin_token: SecretStr | None = None
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    database_pool_size: int = Field(default=10, ge=1, le=100)
    database_max_overflow: int = Field(default=10, ge=0, le=100)
    database_pool_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
    database_statement_timeout_ms: int = Field(default=5000, ge=100, le=120000)
    database_lock_timeout_ms: int = Field(default=2000, ge=100, le=60000)
    redis_connect_timeout_seconds: float = Field(default=1.0, gt=0, le=10)

    @model_validator(mode="after")
    def validate_connection_schemes(self) -> Self:
        """Reject connection URLs incompatible with the selected async clients."""

        database_url = self.database_url.get_secret_value()
        if not database_url.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must use the postgresql+asyncpg scheme")

        if self.redis_url is not None:
            redis_url = self.redis_url.get_secret_value()
            if not redis_url.startswith(("redis://", "rediss://")):
                raise ValueError("REDIS_URL must use the redis or rediss scheme")

        if self.app_env is AppEnvironment.PRODUCTION:
            parsed_database = urlparse(database_url)
            database_query = parse_qs(parsed_database.query)
            if parsed_database.password in (None, "local-password") or database_query.get(
                "ssl"
            ) != ["require"]:
                raise ValueError(
                    "production DATABASE_URL requires a non-development password and ssl=require"
                )
            if self.redis_url is not None and not self.redis_url.get_secret_value().startswith(
                "rediss://"
            ):
                raise ValueError("production REDIS_URL must use rediss")
            outcome_secret = self.outcome_hmac_secret.get_secret_value()
            settlement_token = (
                self.settlement_admin_token.get_secret_value()
                if self.settlement_admin_token is not None
                else None
            )
            if (
                len(outcome_secret) < 32
                or outcome_secret == "development-only-outcome-key-32-bytes-minimum"  # noqa: S105
            ):
                raise ValueError(
                    "production OUTCOME_HMAC_SECRET requires 32 non-development characters"
                )
            if settlement_token is None or len(settlement_token) < 32:
                raise ValueError(
                    "production SETTLEMENT_ADMIN_TOKEN must contain at least 32 characters"
                )
            if settlement_token == "development-only-settlement-token":  # noqa: S105
                raise ValueError(
                    "production SETTLEMENT_ADMIN_TOKEN cannot use the development value"
                )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object for the process."""

    return Settings()
