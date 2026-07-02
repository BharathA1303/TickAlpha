import asyncio
import json
import logging
import time as time_module
from datetime import datetime, time, timedelta
from typing import Dict, List, Optional, Set
import redis.asyncio as aioredis

from app.config import settings
from app.core.cache import get_cached_response, set_cached_response, redis_client
from app.db.session import AsyncSessionLocal
from app.simulator.brownian_bridge import ensure_ticks_cached, TOTAL_SECONDS, START_TIME_STR

logger = logging.getLogger(__name__)

def time_to_seconds(t_str: str) -> int:
    """Converts 'HH:MM:SS' to seconds since midnight."""
    h, m, s = map(int, t_str.split(":"))
    return h * 3600 + m * 60 + s

def seconds_to_time_str(secs: int) -> str:
    """Converts seconds since midnight to 'HH:MM:SS' format."""
    return str(timedelta(seconds=secs)).zfill(8)

class SimulatorManager:
    def __init__(self):
        self.is_running = False
        self.task: Optional[asyncio.Task] = None
        # In-memory cache of full tick paths to avoid querying Redis every single second
        # Key: ticks:EXCHANGE:SEGMENT:SYMBOL:YYYY-MM-DD -> List of ticks
        self.tick_data_cache: Dict[str, List[dict]] = {}
        
        # Local WebSocket queue listeners (session_id -> Set of asyncio.Queues)
        self.listeners: Dict[str, Set[asyncio.Queue]] = {}
        
        # Start and close times in seconds from midnight
        self.market_start_secs = time_to_seconds(START_TIME_STR)
        self.market_close_secs = time_to_seconds("15:30:00")

    async def start(self):
        """Starts the background simulation loop."""
        if self.is_running:
            return
        self.is_running = True
        self.task = asyncio.create_task(self.run_loop())
        logger.info("SimulatorManager background loop started.")

    async def stop(self):
        """Stops the background simulation loop."""
        self.is_running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        self.tick_data_cache.clear()
        self.listeners.clear()
        logger.info("SimulatorManager background loop stopped.")

    async def load_ticks_for_symbol(self, exchange: str, segment: str, symbol: str, date_str: str) -> List[dict]:
        """Loads ticks from cache (Redis or local memory) or returns empty list if not found."""
        cache_key = f"ticks:{exchange.upper()}:{segment.upper()}:{symbol.upper()}:{date_str}"
        
        # 1. Check local memory cache
        if cache_key in self.tick_data_cache:
            return self.tick_data_cache[cache_key]
            
        # 2. Check Redis
        raw = await get_cached_response(cache_key)
        if raw:
            ticks = json.loads(raw)
            # Store in local memory cache
            self.tick_data_cache[cache_key] = ticks
            return ticks
            
        # 3. If missing, attempt to generate using a short-lived DB session
        async with AsyncSessionLocal() as db:
            try:
                from datetime import date
                target_date = date.fromisoformat(date_str)
                success = await ensure_ticks_cached(db, exchange, segment, symbol, target_date)
                if success:
                    raw = await get_cached_response(cache_key)
                    if raw:
                        ticks = json.loads(raw)
                        self.tick_data_cache[cache_key] = ticks
                        return ticks
            except Exception as e:
                logger.error(f"Error pre-generating ticks for background manager: {e}")
                
        return []

    async def publish_to_session(self, session_id: str, message: dict):
        """Broadcasts messages to Redis Pub/Sub and/or registered local in-memory listeners."""
        # 1. Redis Pub/Sub
        if redis_client:
            try:
                await redis_client.publish(
                    f"session_channel:{session_id}",
                    json.dumps(message)
                )
            except Exception as e:
                logger.error(f"Redis publish error for session {session_id}: {e}")
                
        # 2. Local in-memory queues (fallback/parallel delivery)
        if session_id in self.listeners:
            dead_queues = set()
            for q in self.listeners[session_id]:
                try:
                    q.put_nowait(message)
                except Exception:
                    dead_queues.add(q)
            if dead_queues:
                self.listeners[session_id] -= dead_queues

    async def process_active_session(self, session_id: str):
        """Advances the virtual clock and publishes the ticks for one active session."""
        # 1. Fetch current session state
        raw_state = await get_cached_response(f"session:{session_id}")
        if not raw_state:
            return
            
        state = json.loads(raw_state)
        if state.get("status") != "active":
            return
            
        # 2. Calculate virtual times
        v_time_str = state["virtual_time"]
        speed = state["replay_speed"]
        date_str = state["date"]
        subscriptions = state["subscriptions"]
        
        curr_secs = time_to_seconds(v_time_str)
        next_secs = curr_secs + speed
        
        completed = False
        if next_secs >= self.market_close_secs:
            next_secs = self.market_close_secs
            completed = True
            state["status"] = "completed"
            
        next_v_time_str = seconds_to_time_str(next_secs)
        state["virtual_time"] = next_v_time_str
        
        # Calculate indices (0-indexed based on market_start_secs)
        start_idx = curr_secs - self.market_start_secs
        end_idx = next_secs - self.market_start_secs
        
        # 3. Slices ticks for each subscribed symbol
        tick_payload = {}
        for spec in subscriptions:
            parts = spec.split(":")
            if len(parts) != 3:
                continue
            exchange, segment, symbol = parts
            
            ticks = await self.load_ticks_for_symbol(exchange, segment, symbol, date_str)
            if ticks:
                # Slice ticks within the time window (start_idx exclusive, end_idx inclusive)
                sliced_ticks = ticks[max(0, start_idx + 1):min(len(ticks), end_idx + 1)]
                if sliced_ticks:
                    tick_payload[spec] = sliced_ticks
                    
        # 4. Broadcast ticks if we have data to send or if completing
        if tick_payload or completed:
            message = {
                "type": "tick_update",
                "session_id": session_id,
                "virtual_time": next_v_time_str,
                "status": state["status"],
                "ticks": tick_payload
            }
            await self.publish_to_session(session_id, message)

        # 5. Save updated state back to cache
        await set_cached_response(f"session:{session_id}", json.dumps(state), ttl=86400)
        
        if completed:
            logger.info(f"Session {session_id} has completed replay.")
            if redis_client:
                try:
                    await redis_client.srem("active_sessions", session_id)
                except Exception as e:
                    logger.error(f"Redis srem error: {e}")

    async def run_loop(self):
        """Infinite loop running every 1 second, advancing virtual clocks."""
        while self.is_running:
            start_time = time_module.monotonic()
            
            try:
                # Get list of active session IDs
                active_ids: Set[str] = set()
                if redis_client:
                    try:
                        active_ids = await redis_client.smembers("active_sessions")
                    except Exception as e:
                        logger.error(f"Redis smembers error: {e}")
                else:
                    # In-memory fallback: scan cache dictionary for active sessions
                    from app.core.cache import _in_memory_cache
                    for key, (val, _) in list(_in_memory_cache.items()):
                        if key.startswith("session:"):
                            try:
                                state = json.loads(val)
                                if state.get("status") == "active":
                                    active_ids.add(state["session_id"])
                            except Exception:
                                pass
                
                # Advance and publish ticks for each active session
                if active_ids:
                    tasks = [self.process_active_session(sid) for sid in active_ids]
                    await asyncio.gather(*tasks, return_exceptions=True)
                    
            except Exception as e:
                logger.error(f"Error in simulation loop: {e}")
                
            # Keep loop exactly at 1-second ticks
            elapsed = time_module.monotonic() - start_time
            sleep_time = max(0.01, 1.0 - elapsed)
            await asyncio.sleep(sleep_time)

# Global singleton manager instance
simulator_manager = SimulatorManager()
