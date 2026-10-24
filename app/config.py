from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application configuration loaded from environment or .env file."""
    APP_NAME: str = "URL Shortener API"
    APP_VERSION: str = "2.0.0"
    DEBUG: bool = False

    DATABASE_URL: str = "sqlite:///./url_shortener.db"

    REDIS_URL: str = "redis://localhost:6379/0"
    CACHE_TTL_SECONDS: int = 3600

    RATE_LIMIT_REQUESTS: int = 10
    RATE_LIMIT_WINDOW_SECONDS: int = 60

    BASE_URL: str = "http://localhost:8000"

    MAX_BATCH_SIZE: int = 20
    DEFAULT_PAGE_LIMIT: int = 20
    RESERVED_SHORT_CODES: set[str] = {
        "api",
        "docs",
        "redoc",
        "openapi.json",
        "health",
        "metrics",
        "dashboard",
        "static",
        "favicon.ico",
    }

    @field_validator("DATABASE_URL")
    @classmethod
    def normalize_database_url(cls, v: str) -> str:
        """Pin the psycopg2 driver explicitly.

        Railway/Heroku may supply postgres://, which SQLAlchemy 2.x rejects, and
        SQLAlchemy 2.1+ maps bare postgresql:// to psycopg (v3), which isn't installed.
        """
        for prefix in ("postgres://", "postgresql://"):
            if v.startswith(prefix):
                return "postgresql+psycopg2://" + v[len(prefix):]
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
