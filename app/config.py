"""Typed application configuration loaded from environment variables."""

from enum import StrEnum
from functools import lru_cache
from typing import Literal, Self

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
    database_connect_timeout_seconds: float = Field(default=5.0, gt=0, le=60)
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
        return self


@lru_cache
def get_settings() -> Settings:
    """Return one validated settings object for the process."""

    return Settings()
