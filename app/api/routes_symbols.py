import json
import logging
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import APIKey
from app.core.auth import verify_jwt_token
from app.core.delay_gate import get_eligible_symbols
from app.core.cache import get_cached_response, set_cached_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/symbols", tags=["Symbols"])

@router.get("")
async def list_symbols(
    exchange: str = Query(..., description="Exchange (NSE, BSE, or MCX)"),
    segment: str = Query("EQ", description="Segment (EQ, FUT, OPT)"),
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Returns list of available symbols in the database for the given exchange and segment.
    Strictly gates symbols using the 3-day delay compliance logic.
    Protected by JWT token verification and scope checking.
    """
    exchange_upper = exchange.upper()
    segment_upper = segment.upper()
    
    if exchange_upper not in ["NSE", "BSE", "MCX"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Exchange must be 'NSE', 'BSE', or 'MCX'"
        )

    # 1. Enforce exchange and segment scope compliance
    required_scope = f"{exchange_upper.lower()}:{segment_upper.lower()}"
    if required_scope not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access token missing required scope: {required_scope}",
        )

    # 2. Check Cache
    cache_key = f"symbols:list:{exchange_upper}:{segment_upper}"
    cached_val = await get_cached_response(cache_key)
    if cached_val:
        logger.debug(f"Cache hit for symbols list: {cache_key}")
        return json.loads(cached_val)

    # 3. Call Delay Gate Gated Query
    symbols = await get_eligible_symbols(db, exchange_upper, segment_upper)

    # 4. Cache and Return Gated Result
    await set_cached_response(cache_key, json.dumps(symbols), ttl=60)
    return symbols
