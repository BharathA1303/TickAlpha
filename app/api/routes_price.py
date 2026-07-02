import json
import logging
from datetime import date
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import APIKey
from app.core.auth import verify_jwt_token
from app.core.delay_gate import get_eligible_data, get_eligible_range
from app.core.cache import get_cached_response, set_cached_response

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/price", tags=["Price Data"])


def _check_symbol_allowed(client: APIKey, exchange: str, segment: str, symbol: str):
    """Enforces the admin-controlled per-key symbol allowlist. Empty list = all symbols allowed."""
    if not client.allowed_symbols:
        return
    spec = f"{exchange}:{segment}:{symbol}".upper()
    if spec not in {s.upper() for s in client.allowed_symbols}:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Client's API key is not permitted to access symbol '{spec}'"
        )

@router.get("/{exchange}/{symbol}")
async def get_latest_price(
    exchange: str,
    symbol: str,
    segment: str = Query("EQ", description="Segment (EQ, FUT, OPT)"),
    expiry: Optional[date] = Query(None, description="Expiry date for derivatives (YYYY-MM-DD)"),
    strike: Optional[float] = Query(None, description="Strike price for options"),
    option_type: Optional[str] = Query(None, description="Option type (CE, PE)"),
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Returns the latest eligible (3-day delayed) price for a symbol.
    Protected by JWT token verification and scope checking.
    """
    exchange_upper = exchange.upper()
    symbol_upper = symbol.upper()
    segment_upper = segment.upper()
    
    # 1. Enforce exchange scope compliance (e.g. nse:eq, nse:fo, mcx:com)
    required_scope = f"{exchange_upper.lower()}:{segment_upper.lower()}"
    if required_scope not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access token missing required scope: {required_scope}",
        )
    _check_symbol_allowed(client, exchange_upper, segment_upper, symbol_upper)

    # 2. Check Cache
    cache_key = f"price:latest:{exchange_upper}:{segment_upper}:{symbol_upper}:{expiry}:{strike}:{option_type}"
    cached_val = await get_cached_response(cache_key)
    if cached_val:
        logger.debug(f"Cache hit for latest price: {cache_key}")
        return json.loads(cached_val)
        
    # 3. Call Delay Gate (Compliance Gated Database Query)
    try:
        price_record = await get_eligible_data(
            db=db,
            symbol=symbol_upper,
            exchange=exchange_upper,
            segment=segment_upper,
            expiry=expiry,
            strike=strike,
            option_type=option_type
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    if not price_record:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No eligible EOD price data found for {exchange_upper}:{segment_upper}:{symbol_upper}"
        )
        
    # 4. Serialize and Cache Gated Result
    result_dict = price_record.to_dict()
    await set_cached_response(cache_key, json.dumps(result_dict), ttl=60)
    
    return result_dict


@router.get("/{exchange}/{symbol}/range")
async def get_price_range(
    exchange: str,
    symbol: str,
    start: date = Query(..., description="Start date (YYYY-MM-DD)"),
    end: date = Query(..., description="End date (YYYY-MM-DD)"),
    segment: str = Query("EQ", description="Segment (EQ, FUT, OPT)"),
    expiry: Optional[date] = Query(None, description="Expiry date for derivatives (YYYY-MM-DD)"),
    strike: Optional[float] = Query(None, description="Strike price for options"),
    option_type: Optional[str] = Query(None, description="Option type (CE, PE)"),
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Returns a series of eligible EOD price records for charting or replaying.
    Clips or rejects any requested range overlapping the restricted 3-day compliance window.
    Protected by JWT token verification and scope checking.
    """
    exchange_upper = exchange.upper()
    symbol_upper = symbol.upper()
    segment_upper = segment.upper()
    
    # 1. Enforce exchange scope compliance
    required_scope = f"{exchange_upper.lower()}:{segment_upper.lower()}"
    if required_scope not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Access token missing required scope: {required_scope}",
        )
    _check_symbol_allowed(client, exchange_upper, segment_upper, symbol_upper)

    # 2. Check Cache
    cache_key = f"price:range:{exchange_upper}:{segment_upper}:{symbol_upper}:{start.isoformat()}:{end.isoformat()}:{expiry}:{strike}:{option_type}"
    cached_val = await get_cached_response(cache_key)
    if cached_val:
        logger.debug(f"Cache hit for price range: {cache_key}")
        return json.loads(cached_val)
        
    # 3. Call Delay Gate (Compliance Gated Database Query)
    try:
        price_records = await get_eligible_range(
            db=db,
            symbol=symbol_upper,
            exchange=exchange_upper,
            start_date=start,
            end_date=end,
            segment=segment_upper,
            expiry=expiry,
            strike=strike,
            option_type=option_type
        )
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
        
    # 4. Serialize and Cache Gated Results
    results_list = [record.to_dict() for record in price_records]
    await set_cached_response(cache_key, json.dumps(results_list), ttl=60)
    
    return results_list
