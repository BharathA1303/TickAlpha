import json
import logging
import uuid
from datetime import date
from typing import Dict, List, Optional
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

# Sessions never auto-complete: once the virtual clock reaches market close
# (15:30), SimulatorManager rolls the session over to the next available
# trading day and resets the clock to 09:15 rather than stopping (see
# SimulatorManager.process_active_session). This lets a session run
# indefinitely - a dev/testing convenience so tick delivery isn't tied to
# real-world market hours and a session doesn't need to be recreated every
# simulated day. "completed" is retained as a legacy status value but is no
# longer set anywhere in the active code path.

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


def is_symbol_allowed(client: APIKey, resolved_spec: str) -> bool:
    """
    Checks whether a client's key permits access to a given EXCHANGE:SEGMENT:SYMBOL.
    An empty allowed_symbols list means all symbols within the client's scopes are permitted.
    """
    if not client.allowed_symbols:
        return True
    return resolved_spec.upper() in {s.upper() for s in client.allowed_symbols}


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

    # 2. Enforce per-key maximum replay speed cap
    max_speed = getattr(client, "max_replay_speed", 60) or 60
    if req.replay_speed > max_speed:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Requested replay speed {req.replay_speed}x exceeds this key's maximum allowed speed of {max_speed}x."
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
    # Maps resolved_spec ("EXCHANGE:SEGMENT:SYMBOL") -> the price_data.version
    # this session subscribed against, so the clock loop reads the exact
    # same tick path for the rest of this session's life even if a
    # correction lands (a new version) after the session started. Persisted
    # into session state below as "subscription_versions".
    subscription_versions = dict(state.get("subscription_versions", {}))

    # Preload only the CURRENT (non-superseded) EOD records for this date in
    # a single batch query to avoid N+1 database queries. Filtering to the
    # current version avoids accidentally picking up a stale superseded row
    # for symbols that have been corrected.
    #
    # NOTE: F&O symbols (segment FUT/OPT) can have MULTIPLE rows sharing the
    # same (exchange, segment, symbol) - one per strike/expiry/option_type
    # (e.g. NSE:OPT:RELIANCE has a separate row for every strike and CE/PE).
    # eod_by_spec below intentionally keeps only ONE representative row per
    # (exchange, segment, symbol) for resolving bare symbol specs like
    # "NSE:FUT:RELIANCE" or wildcard/ALL expansion - this matches how equities
    # already worked (one row per symbol) and extends it to "one representative
    # contract per underlying" for F&O wildcard resolution, rather than trying
    # to enumerate every strike. A client that wants a SPECIFIC option contract
    # should use the dedicated price endpoints with expiry/strike/option_type
    # (see api_integration_guide.md Section 1b) rather than session wildcards.
    stmt = select(PriceData).where(
        PriceData.market_timestamp == target_date,
        PriceData.superseded_at.is_(None),
    )
    res = await db.execute(stmt)
    all_eod_rows = res.scalars().all()
    eod_by_spec: Dict[tuple, PriceData] = {}
    for row in all_eod_rows:
        key = (row.exchange.upper(), row.segment.upper(), row.symbol.upper())
        # Keep the first row seen per key as the "representative" contract;
        # deterministic instead of "whichever happens to be last in the
        # query result", though which specific contract wins is inherently
        # arbitrary for multi-contract symbols (see note above).
        eod_by_spec.setdefault(key, row)

    for spec in req.symbols:
        spec_upper = spec.strip().upper()

        # 1. Resolve symbols (handle wildcards or literal symbol spec) using the preloaded map
        resolved_specs = []
        if spec_upper == "ALL":
            resolved_specs = [f"{ex}:{seg}:{sym}" for ex, seg, sym in eod_by_spec.keys()]
        elif spec_upper.endswith(":ALL"):
            parts = spec_upper.split(":")
            if len(parts) == 3:
                ex_filter, seg_filter = parts[0], parts[1]
                resolved_specs = [
                    f"{ex}:{seg}:{sym}"
                    for ex, seg, sym in eod_by_spec.keys()
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

            # Verify client's symbol allowlist (admin-controlled per-key restriction)
            if not is_symbol_allowed(client, resolved_spec):
                if spec_upper == "ALL" or spec_upper.endswith(":ALL"):
                    continue
                else:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=f"Client's API key is not permitted to access symbol '{resolved_spec}'"
                    )

            # Resolve the EOD row for this spec. eod_obj can legitimately be
            # None here (e.g. a literal spec the preload didn't cover), in
            # which case ensure_ticks_cached falls back to querying it fresh
            # internally and returns the row IT resolved - so we read the
            # version from that return value, never from the possibly-None
            # eod_obj local (that mismatch was the source of a 500 crash:
            # ensure_ticks_cached could succeed via its own internal lookup
            # while eod_obj stayed None, and `eod_obj.version` would then
            # raise AttributeError on a None).
            eod_obj = eod_by_spec.get((exchange, segment, symbol))
            resolved_eod = await ensure_ticks_cached(
                db=db,
                exchange=exchange,
                segment=segment,
                symbol=symbol,
                target_date=target_date,
                eod_data=eod_obj,
            )
            if resolved_eod is None:
                if spec_upper == "ALL" or spec_upper.endswith(":ALL"):
                    continue
                else:
                    raise HTTPException(
                        status_code=status.HTTP_404_NOT_FOUND,
                        detail=f"No EOD price data found for '{resolved_spec}' on date {target_date} to simulate ticks."
                    )

            current_subs.add(resolved_spec)
            # Pin this subscription to the version that was current at
            # subscribe-time, so a later correction doesn't change the ticks
            # this session streams mid-replay.
            subscription_versions[resolved_spec] = resolved_eod.version

    state["subscriptions"] = list(current_subs)
    state["subscription_versions"] = subscription_versions
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
