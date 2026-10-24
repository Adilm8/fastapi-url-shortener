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

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )


settings = Settings()
