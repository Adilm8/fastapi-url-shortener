import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, status, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError

from app.config import settings
from app.database import engine, Base, SessionLocal, get_db
from app.models import URLItem
from app.schemas import URLCreate, URLResponse, URLStatsResponse
from app.utils import generate_base62_short_code
from app.redis_client import (
    get_cached_url,
    set_cached_url,
    is_rate_limited,
    increment_url_clicks,
    get_buffered_clicks,
    flush_buffered_clicks
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


def rate_limiter(
    limit: int = settings.RATE_LIMIT_REQUESTS,
    window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS
):
    """FastAPI dependency to enforce Redis-backed rate limiting per client IP."""
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
        if limited:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Too many requests. Rate limit exceeded. Retry in {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)}
            )
    return dependency


def persist_clicks_to_db(short_code: str):
    """
    Background worker task to flush buffered clicks from Redis to the database.
    Decouples redirect response time from database disk writes.
    """
    clicks_to_add = flush_buffered_clicks(short_code)
    if clicks_to_add <= 0:
        return

    db = SessionLocal()
    try:
        db.query(URLItem).filter(URLItem.short_code == short_code).update(
            {URLItem.click_count: URLItem.click_count + clicks_to_add}
        )
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error(f"Failed to persist background clicks for '{short_code}': {e}")
    finally:
        db.close()


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
        }
    }


@app.post(
    "/api/v1/urls",
    response_model=URLResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limiter(limit=settings.RATE_LIMIT_REQUESTS, window_seconds=settings.RATE_LIMIT_WINDOW_SECONDS))],
    tags=["URLs"],
    summary="Create a short URL (Base62 + Collision Detection)"
)
def create_short_url(
    payload: URLCreate,
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Shorten a given long URL using Base62 encoding and automated collision detection.
    - If `custom_code` is provided, attempts to assign it.
    - Otherwise, computes a 7-character Base62 hash with automated collision detection & salting.
    - Stores mapping in PostgreSQL and pre-populates Redis cache.
    """
    long_url_str = str(payload.long_url)

    if payload.custom_code:
        existing = db.query(URLItem).filter(URLItem.short_code == payload.custom_code).first()
        if existing:
            if existing.long_url == long_url_str:
                base_url = str(request.base_url).rstrip("/")
                return URLResponse(
                    short_code=existing.short_code,
                    short_url=f"{base_url}/{existing.short_code}",
                    long_url=existing.long_url,
                    created_at=existing.created_at
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Custom short code '{payload.custom_code}' is already in use."
            )
        short_code = payload.custom_code
    else:
        max_retries = 10
        short_code = None
        for attempt in range(max_retries):
            salt = "" if attempt == 0 else f"_salt_{attempt}"
            candidate = generate_base62_short_code(url=long_url_str, salt=salt, length=7)
            
            existing = db.query(URLItem).filter(URLItem.short_code == candidate).first()
            if not existing:
                short_code = candidate
                break
            elif existing.long_url == long_url_str:
                set_cached_url(existing.short_code, existing.long_url)
                base_url = str(request.base_url).rstrip("/")
                return URLResponse(
                    short_code=existing.short_code,
                    short_url=f"{base_url}/{existing.short_code}",
                    long_url=existing.long_url,
                    created_at=existing.created_at
                )
            else:
                logger.info(f"Collision detected for Base62 code '{candidate}' (attempt {attempt + 1}). Retrying with salt...")

        if not short_code:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Unable to generate unique short code. Please try again."
            )

    url_item = URLItem(
        short_code=short_code,
        long_url=long_url_str
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

    base_url = str(request.base_url).rstrip("/")
    return URLResponse(
        short_code=url_item.short_code,
        short_url=f"{base_url}/{url_item.short_code}",
        long_url=url_item.long_url,
        created_at=url_item.created_at
    )


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

    real_time_clicks = url_item.click_count + get_buffered_clicks(short_code)

    base_url = str(request.base_url).rstrip("/")
    return URLStatsResponse(
        short_code=url_item.short_code,
        short_url=f"{base_url}/{url_item.short_code}",
        long_url=url_item.long_url,
        click_count=real_time_clicks,
        created_at=url_item.created_at
    )


@app.get(
    "/{short_code}",
    response_class=RedirectResponse,
    status_code=status.HTTP_307_TEMPORARY_REDIRECT,
    tags=["Redirect"],
    summary="Redirect to original long URL (with Background Analytics)"
)
def redirect_to_long_url(
    short_code: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """
    Redirect to the original target URL.
    - Resolves via Redis cache-aside (sub-millisecond).
    - Increments Redis atomic click counter.
    - Enqueues background persistence to PostgreSQL (non-blocking).
    """
    target_url = get_cached_url(short_code)

    if not target_url:
        url_item = db.query(URLItem).filter(URLItem.short_code == short_code).first()
        if not url_item:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Short URL with code '{short_code}' not found."
            )
        target_url = url_item.long_url
        set_cached_url(short_code, target_url)

    increment_url_clicks(short_code)
    background_tasks.add_task(persist_clicks_to_db, short_code)

    return RedirectResponse(
        url=target_url,
        status_code=status.HTTP_307_TEMPORARY_REDIRECT
    )
