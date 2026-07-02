import json
import logging
import uuid
from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import APIKey, PriceData
from app.core.auth import verify_jwt_token
from app.core.delay_gate import get_delay_cutoff
from app.core.cache import get_cached_response, set_cached_response, redis_client
from app.simulator.brownian_bridge import ensure_ticks_cached

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/sessions", tags=["Replay Sessions"])

import datetime

# Pydantic models
class SessionCreate(BaseModel):
    date: datetime.date = Field(..., description="Target trading date to replay (YYYY-MM-DD)")
    replay_speed: int = Field(1, ge=1, le=60, description="Replay speed multiplier (1x to 60x)")

class SubscriptionRequest(BaseModel):
    symbols: List[str] = Field(
        ...,
        description="List of symbols to watch in format EXCHANGE:SEGMENT:SYMBOL (e.g. NSE:EQ:RELIANCE)"
    )

class SessionResponse(BaseModel):
    session_id: str
    date: str
    replay_speed: int
    virtual_time: str
    status: str
    subscriptions: List[str]
    created_by: str

async def save_session_state(session_id: str, state: dict):
    """Saves session state dictionary to Redis (or fallback)."""
    # Use Redis client directly if available for hash/json operations, or fallback to cache helper
    await set_cached_response(f"session:{session_id}", json.dumps(state), ttl=86400) # 24h TTL
    
    # Add to active set if status is active
    if redis_client:
        try:
            if state["status"] == "active":
                await redis_client.sadd("active_sessions", session_id)
            else:
                await redis_client.srem("active_sessions", session_id)
        except Exception as e:
            logger.error(f"Redis set operation failed: {e}")

async def get_session_state(session_id: str) -> Optional[dict]:
    """Retrieves session state from Redis (or fallback)."""
    raw = await get_cached_response(f"session:{session_id}")
    return json.loads(raw) if raw else None

@router.post("", response_model=SessionResponse)
async def create_session(req: SessionCreate, client: APIKey = Depends(verify_jwt_token)):
    """
    Creates a new replay session.
    Verifies that the target date complies with the 3-day rolling delay gate.
    """
    # 1. Enforce delay gate
    cutoff = get_delay_cutoff()
    if req.date > cutoff:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Requested date {req.date} is restricted. Maximum allowed date is {cutoff} (3-day delay)."
        )
        
    session_id = f"sess_{uuid.uuid4().hex[:16]}"
    session_state = {
        "session_id": session_id,
        "date": req.date.isoformat(),
        "replay_speed": req.replay_speed,
        "virtual_time": "09:15:00",
        "status": "paused",
        "subscriptions": [],
        "created_by": client.client_id
    }
    
    await save_session_state(session_id, session_state)
    logger.info(f"Created session {session_id} for client {client.client_id} (Date: {req.date}, Speed: {req.replay_speed}x)")
    return session_state

@router.get("/{session_id}", response_model=SessionResponse)
async def get_session(session_id: str, client: APIKey = Depends(verify_jwt_token)):
    """Retrieves the current state of a replay session."""
    state = await get_session_state(session_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
    return state

@router.post("/{session_id}/start", response_model=SessionResponse)
async def start_session(session_id: str, client: APIKey = Depends(verify_jwt_token)):
    """Resumes or starts streaming ticks for the session."""
    state = await get_session_state(session_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
        
    if state["status"] == "completed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Session has already completed replaying"
        )
        
    state["status"] = "active"
    await save_session_state(session_id, state)
    logger.info(f"Started session {session_id}")
    return state

@router.post("/{session_id}/pause", response_model=SessionResponse)
async def pause_session(session_id: str, client: APIKey = Depends(verify_jwt_token)):
    """Pauses tick streaming for the session."""
    state = await get_session_state(session_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
        
    if state["status"] == "active":
        state["status"] = "paused"
        await save_session_state(session_id, state)
        logger.info(f"Paused session {session_id}")
    return state

@router.post("/{session_id}/subscribe", response_model=SessionResponse)
async def subscribe_symbols(
    session_id: str,
    req: SubscriptionRequest,
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Subscribes to a list of symbols for the replay session.
    Supports wildcards like 'ALL' or 'EXCHANGE:SEGMENT:ALL'.
    Parses specifications and pre-caches the simulated tick data.
    """
    state = await get_session_state(session_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
        
    target_date = date.fromisoformat(state["date"])
    
    current_subs = set(state["subscriptions"])
    
    # Preload all EOD records for this date in a single batch query to avoid N+1 database queries
    stmt = select(PriceData).where(PriceData.market_timestamp == target_date)
    res = await db.execute(stmt)
    all_eod_rows = res.scalars().all()
    eod_map = {
        (row.exchange.upper(), row.segment.upper(), row.symbol.upper()): row
        for row in all_eod_rows
    }

    for spec in req.symbols:
        spec_upper = spec.strip().upper()
        
        # 1. Resolve symbols (handle wildcards or literal symbol spec) using the preloaded map
        resolved_specs = []
        if spec_upper == "ALL":
            resolved_specs = [f"{ex}:{seg}:{sym}" for ex, seg, sym in eod_map.keys()]
        elif spec_upper.endswith(":ALL"):
            parts = spec_upper.split(":")
            if len(parts) == 3:
                ex_filter, seg_filter = parts[0], parts[1]
                resolved_specs = [
                    f"{ex}:{seg}:{sym}"
                    for ex, seg, sym in eod_map.keys()
                    if ex == ex_filter and seg == seg_filter
                ]
            else:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid wildcard subscription format '{spec}'. Must be EXCHANGE:SEGMENT:ALL"
                )
        else:
            # Standard single symbol check
            parts = spec.split(":")
            if len(parts) != 3:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"Invalid symbol format '{spec}'. Must be EXCHANGE:SEGMENT:SYMBOL"
                )
            resolved_specs = [spec]

        if not resolved_specs:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"No EOD data found matching target '{spec}' on date {target_date}."
            )
            
        # 2. Process and cache each resolved symbol
        for resolved_spec in resolved_specs:
            parts = resolved_spec.split(":")
            exchange, segment, symbol = parts[0].upper(), parts[1].upper(), parts[2].upper()
            
            # Verify client scope for this specific asset
            required_scope = f"{exchange.lower()}:{segment.lower()}"
            if required_scope not in client.scopes:
                if spec_upper == "ALL" or spec_upper.endswith(":ALL"):
                    # Silently skip matching assets that the client is not scoped for
                    continue
                else:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Client missing scope '{required_scope}' required for symbol '{resolved_spec}'"
                    )
            
            # Trigger tick cache generation (Brownian Bridge generation) using preloaded EOD data
            eod_obj = eod_map.get((exchange, segment, symbol))
            success = await ensure_ticks_cached(
                db=db,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                target_date=target_date,
                eod_data=eod_obj
            )
            if not success:
                if spec_upper == "ALL" or spec_upper.endswith(":ALL"):
                    continue
                else:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"No EOD price data found for '{resolved_spec}' on date {target_date} to simulate ticks."
                    )
            
            current_subs.add(resolved_spec)
        
    state["subscriptions"] = list(current_subs)
    await save_session_state(session_id, state)
    logger.info(f"Session {session_id} subscribed in bulk to: {req.symbols} -> Resolved size: {len(current_subs)}")
    return state

@router.post("/{session_id}/unsubscribe", response_model=SessionResponse)
async def unsubscribe_symbols(
    session_id: str,
    req: SubscriptionRequest,
    client: APIKey = Depends(verify_jwt_token)
):
    """Unsubscribes from a list of symbols."""
    state = await get_session_state(session_id)
    if not state:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Session not found"
        )
        
    current_subs = set(state["subscriptions"])
    for spec in req.symbols:
        current_subs.discard(spec)
        
    state["subscriptions"] = list(current_subs)
    await save_session_state(session_id, state)
    logger.info(f"Session {session_id} unsubscribed from: {req.symbols}")
    return state
