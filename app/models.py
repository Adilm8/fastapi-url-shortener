from datetime import datetime, timezone
from sqlalchemy import Column, Integer, String, DateTime, Boolean, ForeignKey
from app.database import Base


class URLItem(Base):
    """Shortened URL database model."""
    __tablename__ = "urls"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    short_code = Column(String(16), unique=True, index=True, nullable=False)
    long_url = Column(String(2048), nullable=False)
    click_count = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), nullable=False)
    expires_at = Column(DateTime, nullable=True, index=True)
    is_active = Column(Boolean, default=True, nullable=False)
    max_clicks = Column(Integer, nullable=True)
    last_clicked_at = Column(DateTime, nullable=True)


class ClickEvent(Base):
    """Click telemetry event database model."""
    __tablename__ = "click_events"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    short_code = Column(String(16), ForeignKey("urls.short_code", ondelete="CASCADE"), index=True, nullable=False)
    clicked_at = Column(DateTime, default=lambda: datetime.now(timezone.utc), index=True, nullable=False)
    referrer = Column(String(512), default="Direct", nullable=False)
    device_type = Column(String(32), default="Desktop", nullable=False)
    browser = Column(String(64), default="Other", nullable=False)
    os_name = Column(String(64), default="Other", nullable=False)
    ip_hash = Column(String(64), nullable=True, index=True)
