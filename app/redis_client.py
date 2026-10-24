import json
import time
import logging
from typing import Optional, Dict, Tuple
import redis
from app.config import settings

logger = logging.getLogger(__name__)

_in_memory_cache: Dict[str, Tuple[str, float]] = {}
_in_memory_rate_limits: Dict[str, list] = {}
_in_memory_clicks: Dict[str, int] = {}
_cache_hits: int = 0
_cache_misses: int = 0

# How long to wait before retrying a Redis server that was unreachable.
_RECONNECT_INTERVAL_SECONDS = 15.0

redis_client = redis.Redis.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    socket_connect_timeout=0.5,
    socket_timeout=0.5
)
_redis_available = False
_next_reconnect_at = 0.0


def _redis() -> Optional[redis.Redis]:
    """
    Return the Redis client if the server is reachable, else None so callers use the
    in-memory fallback. An unreachable server is re-probed every few seconds, so the
    app recovers when Redis comes up after the app or returns from an outage.
    """
    global _redis_available, _next_reconnect_at
    if _redis_available:
        return redis_client
    now = time.monotonic()
    if now < _next_reconnect_at:
        return None
    try:
        redis_client.ping()
    except redis.exceptions.RedisError:
        _next_reconnect_at = now + _RECONNECT_INTERVAL_SECONDS
        return None
    _redis_available = True
    logger.info("Connected to Redis server successfully.")
    return redis_client


def _mark_redis_down(action: str, error: Exception) -> None:
    """Record a Redis failure and switch to the in-memory fallback until the next probe."""
    global _redis_available, _next_reconnect_at
    if _redis_available:
        logger.warning(f"Redis {action} failed, falling back to in-memory: {error}")
    _redis_available = False
    _next_reconnect_at = time.monotonic() + _RECONNECT_INTERVAL_SECONDS


if _redis() is None:
    logger.warning("Redis server is not reachable. Falling back to in-memory caching and rate limiting.")


def ping_redis() -> bool:
    """Actively check Redis connectivity (used by the health endpoint)."""
    r = _redis()
    if r is None:
        return False
    try:
        return bool(r.ping())
    except redis.exceptions.RedisError as e:
        _mark_redis_down("ping", e)
        return False


def _record_cache_lookup(hit: bool) -> None:
    global _cache_hits, _cache_misses
    stat_key = "stats:cache_hits" if hit else "stats:cache_misses"
    if hit:
        _cache_hits += 1
    else:
        _cache_misses += 1
    r = _redis()
    if r:
        try:
            r.incr(stat_key)
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"incr {stat_key}", e)


def get_cached_url(short_code: str) -> Optional[dict]:
    """
    Retrieve a cached link record {"long_url", "expires_at", "max_clicks"} from Redis or the
    fallback cache, with hit/miss tracking. Entries in an older format count as misses.
    """
    key = f"url:{short_code}"
    raw = None
    r = _redis()
    if r:
        try:
            raw = r.get(key)
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"get {key}", e)
    if raw is None:
        entry = _in_memory_cache.get(key)
        if entry is not None:
            if entry[1] > time.monotonic():
                raw = entry[0]
            else:
                _in_memory_cache.pop(key, None)

    record = None
    if raw is not None:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict) and "long_url" in parsed:
                record = parsed
        except ValueError:
            pass

    _record_cache_lookup(record is not None)
    return record


def set_cached_url(
    short_code: str,
    long_url: str,
    expires_at: Optional[str] = None,
    max_clicks: Optional[int] = None,
    ttl: int = settings.CACHE_TTL_SECONDS
) -> bool:
    """Store a link record in Redis or the fallback cache with expiration."""
    key = f"url:{short_code}"
    payload = json.dumps({"long_url": long_url, "expires_at": expires_at, "max_clicks": max_clicks})
    r = _redis()
    if r:
        try:
            return bool(r.set(key, payload, ex=ttl))
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"set {key}", e)
    _in_memory_cache[key] = (payload, time.monotonic() + ttl)
    return True


def delete_cached_url(short_code: str) -> bool:
    """Invalidate cache entry for short code."""
    key = f"url:{short_code}"
    r = _redis()
    if r:
        try:
            r.delete(key)
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"delete {key}", e)
    _in_memory_cache.pop(key, None)
    return True


def is_rate_limited(
    key: str,
    limit: int = settings.RATE_LIMIT_REQUESTS,
    window_seconds: int = settings.RATE_LIMIT_WINDOW_SECONDS
) -> Tuple[bool, int, int]:
    """
    Check if a key (e.g. client IP + action) exceeds the allowed rate limit.
    Returns:
        (is_limited: bool, current_count: int, retry_after: int)
    """
    redis_key = f"rate_limit:{key}"
    r = _redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            current_count, ttl = pipe.execute()
            if ttl < 0:
                r.expire(redis_key, window_seconds)
                ttl = window_seconds
            retry_after = max(ttl, 1)
            return (current_count > limit, current_count, retry_after)
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"rate limit for '{key}'", e)

    now = time.time()
    timestamps = _in_memory_rate_limits.get(key, [])
    timestamps = [t for t in timestamps if now - t < window_seconds]
    timestamps.append(now)
    _in_memory_rate_limits[key] = timestamps

    current_count = len(timestamps)
    oldest = timestamps[0]
    retry_after = max(int(window_seconds - (now - oldest)), 1)
    return (current_count > limit, current_count, retry_after)


def increment_url_clicks(short_code: str) -> int:
    """
    Atomically increment the click counter in Redis.
    Allows sub-millisecond redirect without waiting for SQL disk write.
    """
    key = f"clicks:{short_code}"
    r = _redis()
    if r:
        try:
            return r.incr(key)
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"incr {key}", e)

    _in_memory_clicks[key] = _in_memory_clicks.get(key, 0) + 1
    return _in_memory_clicks[key]


def get_buffered_clicks(short_code: str) -> int:
    """Retrieve unpersisted click count from Redis."""
    key = f"clicks:{short_code}"
    r = _redis()
    if r:
        try:
            val = r.get(key)
            return int(val) if val else 0
        except ValueError as e:
            logger.warning(f"Invalid buffered click count for '{short_code}': {e}")
            return 0
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"get {key}", e)
    return _in_memory_clicks.get(key, 0)


def flush_buffered_clicks(short_code: str) -> int:
    """
    Atomically fetch and reset the buffered clicks in Redis for persistence to DB.
    """
    key = f"clicks:{short_code}"
    r = _redis()
    if r:
        try:
            pipe = r.pipeline()
            pipe.get(key)
            pipe.delete(key)
            val, _ = pipe.execute()
            return int(val) if val else 0
        except ValueError as e:
            logger.warning(f"Invalid buffered click count for '{short_code}': {e}")
            return 0
        except redis.exceptions.RedisError as e:
            _mark_redis_down(f"flush {key}", e)

    val = _in_memory_clicks.pop(key, 0)
    return val


def get_cache_stats() -> dict:
    """
    Retrieve cache telemetry including total hits, misses, hit ratio,
    and active cache backend type.
    """
    hits = _cache_hits
    misses = _cache_misses

    r = _redis()
    if r:
        try:
            h, m = r.mget("stats:cache_hits", "stats:cache_misses")
            hits = int(h) if h else 0
            misses = int(m) if m else 0
        except ValueError as e:
            logger.warning(f"Invalid cache stats in Redis: {e}")
        except redis.exceptions.RedisError as e:
            _mark_redis_down("get cache stats", e)

    total = hits + misses
    hit_ratio = round(hits / total, 4) if total > 0 else 0.0
    backend = "redis" if _redis_available else "in-memory"

    return {
        "hits": hits,
        "misses": misses,
        "hit_ratio": hit_ratio,
        "backend": backend
    }


def clear_all_redis_keys():
    """Reset all in-memory and Redis keys (used for test isolation)."""
    global _cache_hits, _cache_misses
    _in_memory_cache.clear()
    _in_memory_rate_limits.clear()
    _in_memory_clicks.clear()
    _cache_hits = 0
    _cache_misses = 0
    r = _redis()
    if r:
        try:
            keys = (
                r.keys("rate_limit:*")
                + r.keys("clicks:*")
                + r.keys("url:*")
                + ["stats:cache_hits", "stats:cache_misses"]
            )
            existing_keys = [k for k in keys if k]
            if existing_keys:
                r.delete(*existing_keys)
        except Exception as e:
            logger.warning(f"Error clearing Redis test keys: {e}")
