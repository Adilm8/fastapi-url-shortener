from datetime import datetime
from typing import Optional
from pydantic import BaseModel, HttpUrl, Field


class URLCreate(BaseModel):
    """Request payload to create a short URL."""
    long_url: HttpUrl = Field(
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
    long_url: Optional[HttpUrl] = None
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None
    max_clicks: Optional[int] = Field(default=None, gt=0)


class URLResponse(BaseModel):
    """Response returned when a URL is shortened or fetched."""
    short_code: str
    short_url: str
    long_url: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    is_active: bool = True
    max_clicks: Optional[int] = None
    click_count: int = 0
    qr_code_url: Optional[str] = None

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
    created_at: datetime
    is_active: bool = True
    is_expired: bool = False
    expires_at: Optional[datetime] = None
    max_clicks: Optional[int] = None
    last_clicked_at: Optional[datetime] = None
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
    timestamp: datetime


class MetricsResponse(BaseModel):
    """Telemetry and operational metrics response schema."""
    total_urls: int
    active_urls: int
    total_clicks: int
    cache_hits: int
    cache_misses: int
    cache_hit_ratio: float
    cache_backend: str
