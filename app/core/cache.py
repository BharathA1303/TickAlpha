import time
import logging
from typing import Optional, Dict, Tuple
import redis.asyncio as aioredis
from app.config import settings

logger = logging.getLogger(__name__)

# Redis client
redis_client: Optional[aioredis.Redis] = None

# In-memory fallbacks for caching and rate limiting
_in_memory_cache: Dict[str, Tuple[str, float]] = {}  # key -> (value, expiry_timestamp)
_in_memory_rate_limit: Dict[str, Tuple[int, float]] = {}  # key -> (count, expiry_timestamp)

async def init_redis():
    global redis_client
    try:
        redis_client = aioredis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2.0
        )
        # Test connection
        await redis_client.ping()
        logger.info("Connected to Redis successfully")
    except Exception as e:
        logger.warning(f"Failed to connect to Redis: {e}. Falling back to in-memory cache/rate limiter.")
        redis_client = None

async def close_redis():
    global redis_client
    if redis_client:
        await redis_client.close()
        logger.info("Closed Redis connection")

# --- Cache Interfaces ---

async def get_cached_response(key: str) -> Optional[str]:
    """Retrieve response from cache (Redis or in-memory)."""
    global redis_client
    if redis_client:
        try:
            return await redis_client.get(key)
        except Exception as e:
            logger.error(f"Redis get error for key {key}: {e}")
    
    # In-memory fallback
    if key in _in_memory_cache:
        val, expiry = _in_memory_cache[key]
        if time.time() < expiry:
            return val
        else:
            del _in_memory_cache[key]
    return None

async def set_cached_response(key: str, value: str, ttl: int = 60) -> None:
    """Set response in cache (Redis or in-memory) with a TTL in seconds."""
    global redis_client
    if redis_client:
        try:
            await redis_client.set(key, value, ex=ttl)
            return
        except Exception as e:
            logger.error(f"Redis set error for key {key}: {e}")
    
    # In-memory fallback
    _in_memory_cache[key] = (value, time.time() + ttl)

# --- Rate Limit Interfaces ---

async def check_and_increment_rate_limit(key: str, limit: int, ttl: int = 60) -> bool:
    """
    Increments request count for `key` and checks if it exceeds `limit`.
    Returns True if allowed, False if rate limited.
    """
    global redis_client
    if redis_client:
        try:
            # Atomic transaction using pipelining or a simple multi
            pipe = redis_client.pipeline()
            # Try to increment
            pipe.incr(key)
            # Set TTL on the key
            pipe.expire(key, ttl, nx=True) # Only set expire if it doesn't already have one
            results = await pipe.execute()
            count = results[0]
            return count <= limit
        except Exception as e:
            logger.error(f"Redis rate limit error for key {key}: {e}")
            # Fall through to in-memory check if Redis fails mid-operation
    
    # In-memory fallback
    now = time.time()
    if key in _in_memory_rate_limit:
        count, expiry = _in_memory_rate_limit[key]
        if now < expiry:
            new_count = count + 1
            _in_memory_rate_limit[key] = (new_count, expiry)
            return new_count <= limit
        else:
            # Expired
            _in_memory_rate_limit[key] = (1, now + ttl)
            return True
    else:
        _in_memory_rate_limit[key] = (1, now + ttl)
        return True

async def clear_cache() -> None:
    """Helper to clear both local and Redis caches (useful in tests)."""
    global redis_client
    _in_memory_cache.clear()
    _in_memory_rate_limit.clear()
    if redis_client:
        try:
            await redis_client.flushdb()
        except Exception as e:
            logger.error(f"Redis flushdb error: {e}")
