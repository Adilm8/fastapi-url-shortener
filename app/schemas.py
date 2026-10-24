from datetime import datetime, timezone
from typing import Annotated, Optional
from pydantic import AfterValidator, BaseModel, HttpUrl, Field, field_validator

from app.config import settings


def _as_utc(value: datetime) -> datetime:
    """Mark naive datetimes (stored as UTC) as UTC so they serialize with a 'Z' suffix."""
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _check_url_length(value: HttpUrl) -> HttpUrl:
    if len(str(value)) > settings.MAX_LONG_URL_LENGTH:
        raise ValueError(f"URL must be at most {settings.MAX_LONG_URL_LENGTH} characters")
    return value


UTCDateTime = Annotated[datetime, AfterValidator(_as_utc)]
LongURL = Annotated[HttpUrl, AfterValidator(_check_url_length)]


class URLCreate(BaseModel):
    """Request payload to create a short URL."""
    long_url: LongURL = Field(
        ...,
        description="Original long URL to shorten (must be valid HTTP/HTTPS URL)",
        examples=["https://github.com/fastapi/fastapi"]
    )
    custom_code: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=16,
        pattern="^[a-zA-Z0-9_-]+$",
        description="Optional custom alias for the shortened URL (alphanumeric, dash, underscore)",
        examples=["my-link"]
    )
    expires_at: Optional[datetime] = Field(
        default=None,
        description="Optional UTC expiration timestamp"
    )
    expires_in_seconds: Optional[int] = Field(
        default=None,
        gt=0,
        le=31536000,
        description="Optional TTL in seconds from creation"
    )
    max_clicks: Optional[int] = Field(
        default=None,
        gt=0,
        description="Optional maximum allowed clicks before automatic deactivation"
    )


class URLUpdate(BaseModel):
    """Request payload to update an existing short URL."""
    long_url: Optional[LongURL] = None
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None
    max_clicks: Optional[int] = Field(default=None, gt=0)

    @field_validator("long_url", "is_active")
    @classmethod
    def reject_explicit_null(cls, value):
        """These fields can be omitted but not cleared; null would violate NOT NULL columns."""
        if value is None:
            raise ValueError("may be omitted but cannot be null")
        return value


class URLResponse(BaseModel):
    """Response returned when a URL is shortened or fetched."""
    short_code: str
    short_url: str
    long_url: str
    created_at: UTCDateTime
    expires_at: Optional[UTCDateTime] = None
    is_active: bool = True
    max_clicks: Optional[int] = None
    click_count: int = 0
    qr_code_url: Optional[str] = None
    manage_token: Optional[str] = Field(
        default=None,
        description="Secret for PATCH/DELETE on this link. Returned only once, at creation; store it."
    )

    model_config = {
        "from_attributes": True
    }


class URLBatchCreate(BaseModel):
    """Request payload for batch URL shortening."""
    urls: list[URLCreate] = Field(..., min_length=1, max_length=20)


class URLBatchResponse(BaseModel):
    """Response returned for batch URL creation."""
    items: list[URLResponse]
    count: int


class URLListResponse(BaseModel):
    """Paginated list of short URLs."""
    items: list[URLResponse]
    total: int
    skip: int
    limit: int


class URLStatsResponse(BaseModel):
    """Response containing analytics and telemetry for a short URL."""
    short_code: str
    short_url: str
    long_url: str
    click_count: int
    created_at: UTCDateTime
    is_active: bool = True
    is_expired: bool = False
    expires_at: Optional[UTCDateTime] = None
    max_clicks: Optional[int] = None
    last_clicked_at: Optional[UTCDateTime] = None
    unique_visitors: int = 0
    clicks_by_date: dict[str, int] = Field(default_factory=dict)
    top_referrers: dict[str, int] = Field(default_factory=dict)
    device_breakdown: dict[str, int] = Field(default_factory=dict)
    browser_breakdown: dict[str, int] = Field(default_factory=dict)
    os_breakdown: dict[str, int] = Field(default_factory=dict)

    model_config = {
        "from_attributes": True
    }


class HealthResponse(BaseModel):
    """Health check response schema."""
    status: str
    version: str
    database: str
    cache_backend: str
    timestamp: UTCDateTime


class MetricsResponse(BaseModel):
    """Telemetry and operational metrics response schema."""
    total_urls: int
    active_urls: int
    total_clicks: int
    cache_hits: int
    cache_misses: int
    cache_hit_ratio: float
    cache_backend: str
