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


class URLResponse(BaseModel):
    """Response returned when a URL is shortened."""
    short_code: str
    short_url: str
    long_url: str
    created_at: datetime

    model_config = {
        "from_attributes": True
    }


class URLStatsResponse(BaseModel):
    """Response containing analytics for a short URL."""
    short_code: str
    short_url: str
    long_url: str
    click_count: int
    created_at: datetime

    model_config = {
        "from_attributes": True
    }
