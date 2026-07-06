import asyncio
import json
import logging
import time as time_module
from datetime import date, datetime, time, timedelta
from typing import Dict, List, Optional, Set
import redis.asyncio as aioredis
from sqlalchemy import select

from app.config import settings
from app.core import cache as cache_module
from app.core.cache import get_cached_response, set_cached_response
from app.core.delay_gate import get_delay_cutoff
from app.db.session import AsyncSessionLocal
from app.db.models import PriceData
from app.ingestion.run_ingestion import is_trading_day
from app.simulator.brownian_bridge import ensure_ticks_cached, tick_cache_key, TOTAL_SECONDS, START_TIME_STR

logger = logging.getLogger(__name__)

def time_to_seconds(t_str: str) -> int:
    """Converts 'HH:MM:SS' to seconds since midnight."""
    h, m, s = map(int, t_str.split(":"))
    return h * 3600 + m * 60 + s

def seconds_to_time_str(secs: int) -> str:
    """Converts seconds since midnight to 'HH:MM:SS' format."""
    return str(timedelta(seconds=secs)).zfill(8)

def date_str_to_date(date_str: str) -> date:
    """Converts 'YYYY-MM-DD' to a date object."""
    return date.fromisoformat(date_str)

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

        # Max sessions advanced concurrently per 1s tick (load protection).
        self.MAX_CONCURRENT_SESSIONS = 50

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

    async def load_ticks_for_symbol(self, exchange: str, segment: str, symbol: str, date_str: str, version: int = 1) -> List[dict]:
        """
        Loads ticks from cache (Redis or local memory) or returns empty list if not found.
        `version` pins the lookup to a specific price_data version (see
        brownian_bridge.tick_cache_key) so an in-progress session keeps
        reading the same tick path even if the underlying EOD data is later
        corrected to a new version.
        """
        cache_key = tick_cache_key(exchange, segment, symbol, date_str_to_date(date_str), version)

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

        # 3. If missing, attempt to regenerate using the EOD data pinned to
        # this exact version (not just whatever is "current" now).
        async with AsyncSessionLocal() as db:
            try:
                target_date = date_str_to_date(date_str)
                stmt = select(PriceData).where(
                    PriceData.exchange == exchange.upper(),
                    PriceData.segment == segment.upper(),
                    PriceData.symbol == symbol.upper(),
                    PriceData.market_timestamp == target_date,
                    PriceData.version == version,
                )
                result = await db.execute(stmt)
                eod_data = result.scalars().first()
                success = await ensure_ticks_cached(db, exchange, segment, symbol, target_date, eod_data=eod_data)
                if success:
                    raw = await get_cached_response(cache_key)
                    if raw:
                        ticks = json.loads(raw)
                        self.tick_data_cache[cache_key] = ticks
                        return ticks
            except Exception as e:
                logger.error(f"Error pre-generating ticks for background manager: {e}")

        return []

    async def find_next_replay_date(self, exchange: str, segment: str, symbol: str, after_date: date) -> Optional[date]:
        """
        Finds the next trading day strictly after `after_date` that has EOD
        data for this symbol and is within the compliance delay-gate cutoff.
        If none exists (we've walked past the cutoff), wraps around to the
        EARLIEST available trading day for this symbol instead of stalling -
        this is a dev/testing convenience so a long-running replay session
        never goes idle waiting for new data, it just keeps cycling through
        whatever historical days are available.
        """
        cutoff = get_delay_cutoff()
        candidate = after_date
        # Bounded walk forward (cutoff - after_date is at most a handful of
        # days in the normal case) rather than an unbounded loop.
        for _ in range(400):
            candidate = candidate + timedelta(days=1)
            if candidate > cutoff:
                break
            if not is_trading_day(candidate):
                continue
            stmt = select(PriceData.id).where(
                PriceData.exchange == exchange.upper(),
                PriceData.segment == segment.upper(),
                PriceData.symbol == symbol.upper(),
                PriceData.market_timestamp == candidate,
                PriceData.superseded_at.is_(None),
            ).limit(1)
            async with AsyncSessionLocal() as db:
                result = await db.execute(stmt)
                if result.scalars().first() is not None:
                    return candidate

        # Walked past the cutoff with no later day found - wrap to the
        # earliest available trading day for this symbol so the session
        # keeps cycling instead of going idle.
        stmt_earliest = select(PriceData.market_timestamp).where(
            PriceData.exchange == exchange.upper(),
            PriceData.segment == segment.upper(),
            PriceData.symbol == symbol.upper(),
            PriceData.superseded_at.is_(None),
        ).order_by(PriceData.market_timestamp).limit(1)
        async with AsyncSessionLocal() as db:
            result = await db.execute(stmt_earliest)
            earliest = result.scalars().first()
            return earliest

    async def publish_to_session(self, session_id: str, message: dict):
        """Broadcasts messages to Redis Pub/Sub and/or registered local in-memory listeners."""
        # 1. Redis Pub/Sub
        if cache_module.redis_client:
            try:
                await cache_module.redis_client.publish(
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
                except asyncio.QueueFull:
                    # Load protection / backpressure: this client is consuming
                    # slower than we produce. Rather than blocking every other
                    # client (or growing memory unbounded), drop this client's
                    # oldest buffered batch and enqueue the newest one so it
                    # keeps receiving current ticks and self-recovers.
                    try:
                        q.get_nowait()
                        q.task_done()
                    except Exception:
                        pass
                    try:
                        q.put_nowait(message)
                    except Exception:
                        dead_queues.add(q)
                except Exception:
                    dead_queues.add(q)
            if dead_queues:
                self.listeners[session_id] -= dead_queues

    async def process_active_session(self, session_id: str):
        """
        Advances the virtual clock and publishes the ticks for one active session.

        Sessions never auto-complete: reaching virtual 15:30 (market close)
        rolls the session over to the next available trading day and resets
        the clock to 09:15, rather than marking the session "completed" and
        stopping tick delivery. This is a deliberate dev/testing convenience
        so a session can be left running indefinitely (24/7, independent of
        real-world market hours) without needing to be manually recreated
        every simulated trading day.
        """
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
        # Which price_data version each subscription was pinned to at
        # subscribe-time (see routes_sessions.subscribe_symbols), so a
        # correction landing mid-replay doesn't change this session's ticks.
        subscription_versions = dict(state.get("subscription_versions", {}))

        curr_secs = time_to_seconds(v_time_str)
        next_secs = curr_secs + speed

        rolled_over = False
        if next_secs >= self.market_close_secs:
            rolled_over = True

        if not rolled_over:
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

                # Fall back to version 1 for sessions/state persisted before this
                # field existed (e.g. across a deploy) rather than erroring.
                pinned_version = subscription_versions.get(spec, 1)
                ticks = await self.load_ticks_for_symbol(exchange, segment, symbol, date_str, pinned_version)
                if ticks:
                    # Slice ticks within the time window (start_idx exclusive, end_idx inclusive)
                    sliced_ticks = ticks[max(0, start_idx + 1):min(len(ticks), end_idx + 1)]
                    if sliced_ticks:
                        tick_payload[spec] = sliced_ticks

            # 4. Broadcast ticks if we have data to send
            if tick_payload:
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
            return

        # --- Roll over to the next trading day ---
        old_date = date_str_to_date(date_str)
        new_date, new_subscription_versions = await self._roll_over_to_next_day(old_date, subscriptions)

        state["date"] = new_date.isoformat()
        state["virtual_time"] = START_TIME_STR
        state["subscription_versions"] = new_subscription_versions
        await set_cached_response(f"session:{session_id}", json.dumps(state), ttl=86400)

        logger.info(
            f"Session {session_id} rolled over from {old_date} to {new_date} "
            f"(virtual clock reset to {START_TIME_STR}, session stays active)."
        )
        message = {
            "type": "day_rollover",
            "session_id": session_id,
            "previous_date": old_date.isoformat(),
            "date": new_date.isoformat(),
            "virtual_time": START_TIME_STR,
            "status": "active",
            "ticks": {}
        }
        await self.publish_to_session(session_id, message)

    async def _roll_over_to_next_day(self, old_date: date, subscriptions: List[str]) -> tuple:
        """
        Resolves the next replay date (or wraps to the earliest available
        one, see find_next_replay_date) independently per subscribed symbol,
        then picks the day most subscriptions agree on so the whole session
        moves together. Returns (new_date, new_subscription_versions).
        """
        candidate_dates: Dict[date, int] = {}
        specs_parsed = []
        for spec in subscriptions:
            parts = spec.split(":")
            if len(parts) != 3:
                continue
            exchange, segment, symbol = parts
            specs_parsed.append((spec, exchange, segment, symbol))
            next_date = await self.find_next_replay_date(exchange, segment, symbol, old_date)
            if next_date is not None:
                candidate_dates[next_date] = candidate_dates.get(next_date, 0) + 1

        if not candidate_dates:
            # No subscribed symbol has any other eligible day with data -
            # keep replaying the same day rather than getting stuck with an
            # undefined date.
            new_date = old_date
        else:
            # Pick the date most subscriptions agree on (ties broken by
            # earliest date) so the session's symbols stay in sync.
            new_date = sorted(candidate_dates.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]

        new_subscription_versions: Dict[str, int] = {}
        async with AsyncSessionLocal() as db:
            for spec, exchange, segment, symbol in specs_parsed:
                stmt = select(PriceData).where(
                    PriceData.exchange == exchange.upper(),
                    PriceData.segment == segment.upper(),
                    PriceData.symbol == symbol.upper(),
                    PriceData.market_timestamp == new_date,
                    PriceData.superseded_at.is_(None),
                )
                result = await db.execute(stmt)
                eod_data = result.scalars().first()
                if eod_data is None:
                    continue
                resolved_eod = await ensure_ticks_cached(
                    db, exchange, segment, symbol, new_date, eod_data=eod_data
                )
                if resolved_eod is not None:
                    new_subscription_versions[spec] = resolved_eod.version

        return new_date, new_subscription_versions

    async def run_loop(self):
        """Infinite loop running every 1 second, advancing virtual clocks."""
        while self.is_running:
            start_time = time_module.monotonic()
            
            try:
                # Get list of active session IDs
                active_ids: Set[str] = set()
                if cache_module.redis_client:
                    try:
                        active_ids = await cache_module.redis_client.smembers("active_sessions")
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
                
                # Advance and publish ticks for each active session.
                # Bound concurrency so a spike in active sessions can't launch
                # an unbounded number of heavy coroutines at once and starve /
                # crash the event loop. Sessions beyond the limit are processed
                # in the next batch — the virtual clock catches up naturally.
                if active_ids:
                    sem = asyncio.Semaphore(self.MAX_CONCURRENT_SESSIONS)

                    async def _guarded(sid: str):
                        async with sem:
                            await self.process_active_session(sid)

                    tasks = [_guarded(sid) for sid in active_ids]
                    await asyncio.gather(*tasks, return_exceptions=True)

            except Exception as e:
                logger.error(f"Error in simulation loop: {e}")

            # Keep loop at ~1-second ticks. If a heavy batch overruns the
            # budget, log it (a signal the server is under-provisioned for the
            # current load) and continue without stacking work.
            elapsed = time_module.monotonic() - start_time
            if elapsed > 1.0:
                logger.warning(
                    f"Simulation loop overran budget: {elapsed:.2f}s for "
                    f"{len(active_ids)} active session(s)"
                )
            sleep_time = max(0.01, 1.0 - elapsed)
            await asyncio.sleep(sleep_time)

# Global singleton manager instance
simulator_manager = SimulatorManager()
