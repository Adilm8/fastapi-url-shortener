import time
import logging
from typing import Optional, Dict, Tuple
import redis
from app.config import settings

logger = logging.getLogger(__name__)

_in_memory_cache: Dict[str, str] = {}
_in_memory_rate_limits: Dict[str, list] = {}
_in_memory_clicks: Dict[str, int] = {}
_cache_hits: int = 0
_cache_misses: int = 0
_redis_available = False

try:
    redis_client = redis.Redis.from_url(
        settings.REDIS_URL,
        decode_responses=True,
        socket_connect_timeout=0.2,
        socket_timeout=0.2
    )
    redis_client.ping()
    _redis_available = True
    logger.info("Connected to Redis server successfully.")
except Exception:
    logger.warning("Redis server is not reachable. Falling back to in-memory caching and rate limiting.")
    redis_client = None
    _redis_available = False


def get_cached_url(short_code: str) -> Optional[str]:
    """Retrieve long URL from Redis cache or fallback cache with hit/miss tracking."""
    global _cache_hits, _cache_misses
    key = f"url:{short_code}"
    val = None
    if _redis_available and redis_client:
        try:
            val = redis_client.get(key)
        except redis.exceptions.RedisError as e:
            logger.warning(f"Redis get error for code '{short_code}': {e}")
    if val is None:
        val = _in_memory_cache.get(key)

    if val is not None:
        _cache_hits += 1
        if _redis_available and redis_client:
            try:
                redis_client.incr("stats:cache_hits")
            except redis.exceptions.RedisError as e:
                logger.warning(f"Redis incr error for stats:cache_hits: {e}")
    else:
        _cache_misses += 1
        if _redis_available and redis_client:
            try:
                redis_client.incr("stats:cache_misses")
            except redis.exceptions.RedisError as e:
                logger.warning(f"Redis incr error for stats:cache_misses: {e}")

    return val


def set_cached_url(short_code: str, long_url: str, ttl: int = settings.CACHE_TTL_SECONDS) -> bool:
    """Store long URL mapping in Redis cache or fallback cache with expiration."""
    key = f"url:{short_code}"
    if _redis_available and redis_client:
        try:
            return bool(redis_client.set(key, long_url, ex=ttl))
        except redis.exceptions.RedisError as e:
            logger.warning(f"Redis set error for code '{short_code}': {e}")
    _in_memory_cache[key] = long_url
    return True


def delete_cached_url(short_code: str) -> bool:
    """Invalidate cache entry for short code."""
    key = f"url:{short_code}"
    if _redis_available and redis_client:
        try:
            redis_client.delete(key)
        except redis.exceptions.RedisError as e:
            logger.warning(f"Redis delete error for code '{short_code}': {e}")
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
    if _redis_available and redis_client:
        try:
            pipe = redis_client.pipeline()
            pipe.incr(redis_key)
            pipe.ttl(redis_key)
            results = pipe.execute()
            current_count = results[0]
            ttl = results[1]

            if ttl == -1 or current_count == 1:
                redis_client.expire(redis_key, window_seconds)
                ttl = window_seconds

            retry_after = max(ttl, 1)
            return (current_count > limit, current_count, retry_after)
        except redis.exceptions.RedisError as e:
            logger.warning(f"Redis rate limit error for '{key}': {e}")

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
    if _redis_available and redis_client:
        try:
            return redis_client.incr(key)
        except redis.exceptions.RedisError as e:
            logger.warning(f"Redis incr error for code '{short_code}': {e}")

    _in_memory_clicks[key] = _in_memory_clicks.get(key, 0) + 1
    return _in_memory_clicks[key]


def get_buffered_clicks(short_code: str) -> int:
    """Retrieve unpersisted click count from Redis."""
    key = f"clicks:{short_code}"
    if _redis_available and redis_client:
        try:
            val = redis_client.get(key)
            return int(val) if val else 0
        except (redis.exceptions.RedisError, ValueError) as e:
            logger.warning(f"Redis get clicks error for '{short_code}': {e}")
    return _in_memory_clicks.get(key, 0)


def flush_buffered_clicks(short_code: str) -> int:
    """
    Atomically fetch and reset the buffered clicks in Redis for persistence to DB.
    """
    key = f"clicks:{short_code}"
    if _redis_available and redis_client:
        try:
            pipe = redis_client.pipeline()
            pipe.get(key)
            pipe.delete(key)
            results = pipe.execute()
            val = results[0]
            return int(val) if val else 0
        except (redis.exceptions.RedisError, ValueError) as e:
            logger.warning(f"Redis flush clicks error for '{short_code}': {e}")

    val = _in_memory_clicks.pop(key, 0)
    return val


def get_cache_stats() -> dict:
    """
    Retrieve cache telemetry including total hits, misses, hit ratio,
    and active cache backend type.
    """
    hits = _cache_hits
    misses = _cache_misses

    if _redis_available and redis_client:
        try:
            h = redis_client.get("stats:cache_hits")
            m = redis_client.get("stats:cache_misses")
            hits = int(h) if h else 0
            misses = int(m) if m else 0
        except (redis.exceptions.RedisError, ValueError) as e:
            logger.warning(f"Redis get cache stats error: {e}")

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
    if _redis_available and redis_client:
        try:
            keys = (
                redis_client.keys("rate_limit:*")
                + redis_client.keys("clicks:*")
                + redis_client.keys("url:*")
                + ["stats:cache_hits", "stats:cache_misses"]
            )
            existing_keys = [k for k in keys if k]
            if existing_keys:
                redis_client.delete(*existing_keys)
        except Exception as e:
            logger.warning(f"Error clearing Redis test keys: {e}")
