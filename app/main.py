import time
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, Dict

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    status,
    Request,
    BackgroundTasks,
    Response,
    Query
)
from fastapi.responses import RedirectResponse, HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text, func
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import engine, Base, SessionLocal, get_db
from app.models import URLItem, ClickEvent
from app.schemas import (
    URLCreate,
    URLUpdate,
    URLResponse,
    URLBatchCreate,
    URLBatchResponse,
    URLListResponse,
    URLStatsResponse,
    HealthResponse,
    MetricsResponse
)
from app.utils import (
    generate_base62_short_code,
    is_reserved_short_code,
    is_self_referential_url,
    hash_client_ip,
    parse_user_agent,
    normalize_referrer,
    is_url_expired
)
from app.qr import generate_qr_svg, generate_qr_png
from app.redis_client import (
    get_cached_url,
    set_cached_url,
    delete_cached_url,
    is_rate_limited,
    increment_url_clicks,
    get_buffered_clicks,
    flush_buffered_clicks,
    get_cache_stats
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Ensure database tables exist on startup."""
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(
    title=settings.APP_NAME,
    description="A high-performance URL Shortener REST API backed by PostgreSQL, Redis, and FastAPI using Base62 collision-safe generation.",
    version=settings.APP_VERSION,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

STATIC_DIR = Path(__file__).resolve().parent / "static"
if STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    """Record request latency and attach rate limit headers if applicable."""
    start_time = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
    response.headers["X-Process-Time-Ms"] = f"{elapsed_ms:.2f}"

    rate_headers = getattr(request.state, "rate_limit_headers", None)
    if rate_headers:
        for header_name, header_val in rate_headers.items():
            response.headers[header_name] = str(header_val)

    return response


def rate_limiter(
    limit: int = settings.RATE_LIMIT_REQUESTS,
    window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS
):
    """Rate limiting dependency per client IP."""
    def dependency(request: Request):
        forwarded = request.headers.get("X-Forwarded-For")
        client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "unknown")
        endpoint = request.url.path
        rate_key = f"{client_ip}:{endpoint}"

        limited, current_count, retry_after = is_rate_limited(
            key=rate_key,
            limit=limit,
            window_seconds=window_seconds
        )
        rate_headers = {
            "X-RateLimit-Limit": str(limit),
            "X-RateLimit-Remaining": str(max(0, limit - current_count)),
            "X-RateLimit-Reset": str(retry_after)
        }
        request.state.rate_limit_headers = rate_headers

        if limited:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Rate limit exceeded. Retry in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after), **rate_headers}
            )
    return dependency


def persist_clicks_to_db(
    short_code: str,
    user_agent: Optional[str] = None,
    referrer: Optional[str] = None,
    client_ip: Optional[str] = None
):
    """Flush click counters to database and record telemetry event."""
    clicks_to_add = flush_buffered_clicks(short_code)
    db = SessionLocal()
    try:
        now_utc = datetime.now(timezone.utc)
        if clicks_to_add > 0:
            db.query(URLItem).filter(URLItem.short_code == short_code).update(
                {
                    URLItem.click_count: URLItem.click_count + clicks_to_add,
                    URLItem.last_clicked_at: now_utc
                }
            )

        ua_info = parse_user_agent(user_agent)
        norm_ref = normalize_referrer(referrer)
        hashed_ip = hash_client_ip(client_ip)

        event = ClickEvent(
            short_code=short_code,
            clicked_at=now_utc,
            referrer=norm_ref,
            device_type=ua_info["device_type"],
            browser=ua_info["browser"],
            os_name=ua_info["os_name"],
            ip_hash=hashed_ip or None
        )
        db.add(event)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist background clicks/events for '{short_code}': {e}")
    finally:
        db.close()


def build_url_response(url_item: URLItem, request: Request) -> URLResponse:
    """Construct a standardized URLResponse schema from a database model."""
    base_url = str(request.base_url).rstrip("/")
    current_clicks = (url_item.click_count or 0) + get_buffered_clicks(url_item.short_code)
    return URLResponse(
        short_code=url_item.short_code,
        short_url=f"{base_url}/{url_item.short_code}",
        long_url=url_item.long_url,
        created_at=url_item.created_at,
        expires_at=url_item.expires_at,
        is_active=url_item.is_active,
        max_clicks=url_item.max_clicks,
        click_count=current_clicks,
        qr_code_url=f"{base_url}/api/v1/urls/{url_item.short_code}/qr",
    )


def _create_single_short_url(payload: URLCreate, request: Request, db: Session) -> URLResponse:
    """Shorten a single URL and persist mapping."""
    long_url_str = str(payload.long_url)
    if is_self_referential_url(long_url_str, str(request.base_url)):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot shorten a URL pointing to this shortener service."
        )

    expires_at = payload.expires_at
    if expires_at is None and payload.expires_in_seconds is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=payload.expires_in_seconds)

    if expires_at is not None and is_url_expired(expires_at):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Expiration time cannot be in the past."
        )

    if payload.custom_code:
        custom_code = payload.custom_code.strip()
        if is_reserved_short_code(custom_code):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Custom code '{custom_code}' is a reserved system keyword."
            )
        existing = db.query(URLItem).filter(URLItem.short_code == custom_code).first()
        if existing:
            if existing.long_url == long_url_str:
                updated = False
                if expires_at is not None:
                    existing.expires_at = expires_at
                    updated = True
                if payload.max_clicks is not None:
                    existing.max_clicks = payload.max_clicks
                    updated = True
                if not existing.is_active:
                    existing.is_active = True
                    updated = True
                if updated:
                    db.commit()
                    db.refresh(existing)
                set_cached_url(existing.short_code, existing.long_url)
                return build_url_response(existing, request)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Custom short code '{custom_code}' is already in use."
            )
        short_code = custom_code
    else:
        max_retries = 10
        short_code = None
        for attempt in range(max_retries):
            salt = "" if attempt == 0 else f"_salt_{attempt}"
            candidate = generate_base62_short_code(url=long_url_str, salt=salt, length=7)
            if is_reserved_short_code(candidate):
                continue

            existing = db.query(URLItem).filter(URLItem.short_code == candidate).first()
            if not existing:
                short_code = candidate
                break
            elif existing.long_url == long_url_str:
                if is_url_expired(existing.expires_at) or not existing.is_active:
                    existing.is_active = True
                    existing.expires_at = expires_at
                    existing.max_clicks = payload.max_clicks
                    db.commit()
                    db.refresh(existing)
                elif expires_at is not None or payload.max_clicks is not None:
                    if expires_at is not None:
                        existing.expires_at = expires_at
                    if payload.max_clicks is not None:
                        existing.max_clicks = payload.max_clicks
                    db.commit()
                    db.refresh(existing)
                set_cached_url(existing.short_code, existing.long_url)
                return build_url_response(existing, request)
            else:
                logger.info(f"Collision detected for Base62 code '{candidate}' (attempt {attempt + 1}). Retrying with salt...")

        if not short_code:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to generate unique short code. Please try again."
            )

    url_item = URLItem(
        short_code=short_code,
        long_url=long_url_str,
        expires_at=expires_at,
        max_clicks=payload.max_clicks,
        is_active=True
    )
    try:
        db.add(url_item)
        db.commit()
        db.refresh(url_item)
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A conflict occurred while creating the short URL."
        )

    set_cached_url(url_item.short_code, url_item.long_url)
    return build_url_response(url_item, request)


@app.get("/", tags=["Health"])
def root():
    """Root health check and API discovery endpoint."""
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs_url": "/docs",
        "version": settings.APP_VERSION,
        "algorithm": "Base62 with SHA-256 and automated collision resolution",
        "rate_limiting": {
            "max_requests": settings.RATE_LIMIT_REQUESTS,
            "window_seconds": settings.RATE_LIMIT_WINDOW_SECONDS
        },
        "dashboard_url": "/dashboard",
        "health_url": "/health",
        "metrics_url": "/api/v1/metrics",
    }


@app.get(
    "/health",
    response_model=HealthResponse,
    tags=["Health"],
    summary="Health check endpoint"
)
def health_check(db: Session = Depends(get_db)):
    """Health check validating database and cache connectivity."""
    db_status = "healthy"
    try:
        db.execute(text("SELECT 1"))
    except Exception as e:
        logger.error(f"Database health check failed: {e}")
        db_status = "unhealthy"

    cache_backend = get_cache_stats()["backend"]
    overall_status = "healthy" if db_status == "healthy" else "degraded"

    return HealthResponse(
        status=overall_status,
        version=settings.APP_VERSION,
        database=db_status,
        cache_backend=cache_backend,
        timestamp=datetime.now(timezone.utc)
    )


@app.get(
    "/dashboard",
    response_class=HTMLResponse,
    tags=["Dashboard"],
    include_in_schema=False,
    summary="Interactive URL Shortener Dashboard"
)
def dashboard():
    """Serve the single-page application dashboard."""
    index_path = STATIC_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(str(index_path))
    return HTMLResponse("<h1>Dashboard</h1>", status_code=200)


@app.get(
    "/api/v1/metrics",
    response_model=MetricsResponse,
    tags=["Observability"],
    summary="Application metrics and telemetry"
)
def get_metrics(db: Session = Depends(get_db)):
    """Application observability metrics including URL counts, clicks, and cache stats."""
    now_utc = datetime.now(timezone.utc)
    total_urls = db.query(URLItem).count()
    active_urls = db.query(URLItem).filter(
        URLItem.is_active.is_(True),
        (URLItem.expires_at.is_(None)) | (URLItem.expires_at > now_utc)
    ).count()
    total_clicks_res = db.query(func.coalesce(func.sum(URLItem.click_count), 0)).scalar()
    total_clicks = int(total_clicks_res or 0)

    cache_stats = get_cache_stats()

    return MetricsResponse(
        total_urls=total_urls,
        active_urls=active_urls,
        total_clicks=total_clicks,
        cache_hits=cache_stats["hits"],
        cache_misses=cache_stats["misses"],
        cache_hit_ratio=cache_stats["hit_ratio"],
        cache_backend=cache_stats["backend"]
    )


@app.post(
    "/api/v1/urls",
    response_model=URLResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(limit=settings.RATE_LIMIT_REQUESTS, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS))],
    tags=["URLs"],
    summary="Create a short URL"
)
def create_short_url(
    payload: URLCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Create a shortened URL."""
    return _create_single_short_url(payload, request, db)


@app.post(
    "/api/v1/urls/batch",
    response_model=URLBatchResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(limit=settings.RATE_LIMIT_REQUESTS, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS))],
    tags=["URLs"],
    summary="Batch create short URLs"
)
def create_batch_short_urls(
    payload: URLBatchCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Batch shorten multiple long URLs with rate limiting and validation."""
    if len(payload.urls) > settings.MAX_BATCH_SIZE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Batch size exceeds maximum limit of {settings.MAX_BATCH_SIZE} URLs."
        )
    items = [_create_single_short_url(item, request, db) for item in payload.urls]
    return URLBatchResponse(items=items, count=len(items))


@app.get(
    "/api/v1/urls",
    response_model=URLListResponse,
    tags=["URLs"],
    summary="List and search shortened URLs"
)
def list_urls(
    request: Request,
    skip: int = Query(0, ge=0),
    limit: int = Query(settings.DEFAULT_PAGE_LIMIT, ge=1, le=100),
    search: Optional[str] = Query(None, description="Search term for short code or long URL"),
    active_only: bool = Query(False, description="Filter for active, non-expired URLs only"),
    db: Session = Depends(get_db)
):
    """Retrieve a paginated list of short URLs with optional search and active-only filtering."""
    query = db.query(URLItem)

    if search:
        search_term = f"%{search.strip()}%"
        query = query.filter(
            (URLItem.short_code.ilike(search_term)) | (URLItem.long_url.ilike(search_term))
        )

    if active_only:
        now_utc = datetime.now(timezone.utc)
        query = query.filter(
            URLItem.is_active.is_(True),
            (URLItem.expires_at.is_(None)) | (URLItem.expires_at > now_utc)
        )

    total = query.count()
    items_db = query.order_by(URLItem.created_at.desc()).offset(skip).limit(limit).all()
    items = [build_url_response(item, request) for item in items_db]

    return URLListResponse(
        items=items,
        total=total,
        skip=skip,
        limit=limit
    )


@app.patch(
    "/api/v1/urls/{short_code}",
    response_model=URLResponse,
    tags=["URLs"],
    summary="Update an existing short URL"
)
def update_short_url(
    short_code: str,
    payload: URLUpdate,
    request: Request,
    db: Session = Depends(get_db)
):
    """Update metadata, destination URL, or active state for a short URL."""
    url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()
    if not url_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL '{short_code}' not found."
        )

    if "long_url" in payload.model_fields_set and payload.long_url is not None:
        long_url_str = str(payload.long_url)
        if is_self_referential_url(long_url_str, str(request.base_url)):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot shorten a URL pointing to this shortener service."
            )
        url_item.long_url = long_url_str

    if "is_active" in payload.model_fields_set:
        url_item.is_active = payload.is_active

    if "expires_at" in payload.model_fields_set:
        url_item.expires_at = payload.expires_at

    if "max_clicks" in payload.model_fields_set:
        url_item.max_clicks = payload.max_clicks

    db.commit()
    db.refresh(url_item)

    if url_item.is_active and not is_url_expired(url_item.expires_at):
        set_cached_url(url_item.short_code, url_item.long_url)
    else:
        delete_cached_url(url_item.short_code)

    return build_url_response(url_item, request)


@app.delete(
    "/api/v1/urls/{short_code}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["URLs"],
    summary="Delete a short URL and its analytics"
)
def delete_short_url(
    short_code: str,
    db: Session = Depends(get_db)
):
    """Delete a shortened URL and all associated telemetry events."""
    url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()
    if not url_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL '{short_code}' not found."
        )

    db.query(ClickEvent).filter(ClickEvent.short_code == short_code).delete()
    db.delete(url_item)
    db.commit()

    delete_cached_url(short_code)
    flush_buffered_clicks(short_code)

    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get(
    "/api/v1/urls/{short_code}/qr",
    tags=["QR Code"],
    summary="Generate QR code for a short URL"
)
def get_url_qr_code(
    short_code: str,
    request: Request,
    format: str = Query("svg", pattern="^(svg|png)$"),
    scale: int = Query(8, ge=2, le=20),
    db: Session = Depends(get_db)
):
    """Generate and return an SVG or PNG QR code for the short URL."""
    url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()
    if not url_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL '{short_code}' not found."
        )

    base_url = str(request.base_url).rstrip("/")
    short_url = f"{base_url}/{url_item.short_code}"

    if format == "png":
        img_bytes = generate_qr_png(data=short_url, scale=scale)
        return Response(content=img_bytes, media_type="image/png")
    else:
        svg_content = generate_qr_svg(data=short_url, scale=scale)
        return Response(content=svg_content, media_type="image/svg+xml")


@app.get(
    "/api/v1/urls/{short_code}/stats",
    response_model=URLStatsResponse,
    tags=["Analytics"],
    summary="Get URL analytics & click statistics"
)
def get_url_stats(
    short_code: str,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Retrieve visit analytics (click count) and metadata for a shortened URL.
    Combines persisted database counts with real-time Redis click buffers.
    """
    url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()
    if not url_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL with code '{short_code}' not found."
        )

    real_time_clicks = (url_item.click_count or 0) + get_buffered_clicks(short_code)

    events = db.query(ClickEvent).filter(ClickEvent.short_code == short_code).all()

    unique_ips = {e.ip_hash for e in events if e.ip_hash}
    unique_visitors = len(unique_ips)

    clicks_by_date: Dict[str, int] = {}
    top_referrers: Dict[str, int] = {}
    device_breakdown: Dict[str, int] = {}
    browser_breakdown: Dict[str, int] = {}
    os_breakdown: Dict[str, int] = {}

    for e in events:
        if e.clicked_at:
            date_str = e.clicked_at.strftime("%Y-%m-%d")
            clicks_by_date[date_str] = clicks_by_date.get(date_str, 0) + 1

        ref = e.referrer or "Direct"
        top_referrers[ref] = top_referrers.get(ref, 0) + 1

        dev = e.device_type or "Desktop"
        device_breakdown[dev] = device_breakdown.get(dev, 0) + 1

        browser = e.browser or "Other"
        browser_breakdown[browser] = browser_breakdown.get(browser, 0) + 1

        os_name = e.os_name or "Other"
        os_breakdown[os_name] = os_breakdown.get(os_name, 0) + 1

    sorted_dates = dict(sorted(clicks_by_date.items()))
    sorted_referrers = dict(sorted(top_referrers.items(), key=lambda x: x[1], reverse=True))
    sorted_devices = dict(sorted(device_breakdown.items(), key=lambda x: x[1], reverse=True))
    sorted_browsers = dict(sorted(browser_breakdown.items(), key=lambda x: x[1], reverse=True))
    sorted_os = dict(sorted(os_breakdown.items(), key=lambda x: x[1], reverse=True))

    base_url = str(request.base_url).rstrip("/")
    short_url = f"{base_url}/{url_item.short_code}"

    return URLStatsResponse(
        short_code=url_item.short_code,
        short_url=short_url,
        long_url=url_item.long_url,
        click_count=real_time_clicks,
        created_at=url_item.created_at,
        is_active=url_item.is_active,
        is_expired=is_url_expired(url_item.expires_at),
        expires_at=url_item.expires_at,
        max_clicks=url_item.max_clicks,
        last_clicked_at=url_item.last_clicked_at,
        unique_visitors=unique_visitors,
        clicks_by_date=sorted_dates,
        top_referrers=sorted_referrers,
        device_breakdown=sorted_devices,
        browser_breakdown=sorted_browsers,
        os_breakdown=sorted_os,
    )


@app.get(
    "/{short_code}",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    tags=["Redirect"],
    summary="Redirect to original long URL"
)
def redirect_to_long_url(
    short_code: str,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """Redirect to original long URL and schedule analytics persistence."""
    target_url = get_cached_url(short_code)
    url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()

    if not target_url and not url_item:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Short URL with code '{short_code}' not found."
        )

    if url_item:
        current_total_clicks = (url_item.click_count or 0) + get_buffered_clicks(short_code)
        if (
            not url_item.is_active
            or is_url_expired(url_item.expires_at)
            or (url_item.max_clicks is not None and current_total_clicks >= url_item.max_clicks)
        ):
            delete_cached_url(short_code)
            raise HTTPException(
                status_code=status.HTTP_410_GONE,
                detail=f"Short URL '{short_code}' has expired, reached its click limit, or been deactivated."
            )
        if not target_url:
            target_url = url_item.long_url
            set_cached_url(short_code, target_url)

    forwarded = request.headers.get("X-Forwarded-For")
    client_ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else None)
    user_agent = request.headers.get("User-Agent")
    referrer = request.headers.get("Referer")

    increment_url_clicks(short_code)
    background_tasks.add_task(persist_clicks_to_db, short_code, user_agent, referrer, client_ip)

    return RedirectResponse(
        url=target_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
