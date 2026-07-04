import datetime
from datetime import date, datetime as datetime_cls
from typing import List, Optional
import pytz
from sqlalchemy import select, and_, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import PriceData

def get_delay_cutoff(current_date: Optional[date] = None) -> date:
    """
    Compliance logic: returns the delay cutoff date based on settings.DELAY_DAYS (default 3).
    All market data with market_timestamp > cutoff is strictly restricted.
    Calculations are done in the Indian Standard Time (IST) timezone.
    """
    if current_date is not None:
        ref_date = current_date
    else:
        # Get current date in Indian Standard Time (IST)
        ist_tz = pytz.timezone("Asia/Kolkata")
        ref_date = datetime_cls.now(ist_tz).date()
    
    return ref_date - datetime.timedelta(days=settings.DELAY_DAYS)

async def get_eligible_data(
    db: AsyncSession,
    symbol: str,
    exchange: str,
    segment: str = "EQ",
    expiry: Optional[date] = None,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
    market_timestamp: Optional[date] = None,
    current_date: Optional[date] = None,
    version: Optional[int] = None
) -> Optional[PriceData]:
    """
    COMPLIANCE-CRITICAL FUNCTION:
    Enforces the N-day delay gate for a specific instrument query.
    Supports Cash Equities, F&O derivatives, and Commodities.

    By default returns only the current (non-superseded) version of a record,
    i.e. the latest EOD value after any corrections. Pass `version` to
    retrieve a specific historical version instead (e.g. for audit, to see
    what a tick replay session saw before a correction was applied) - this
    bypasses the "current only" filter but still enforces the delay gate.
    """
    cutoff = get_delay_cutoff(current_date)

    # 1. Enforce strict compliance check on requested timestamp
    if market_timestamp is not None:
        if market_timestamp > cutoff:
            raise ValueError(
                f"Requested market_timestamp {market_timestamp} falls within the restricted {settings.DELAY_DAYS}-day window (cutoff: {cutoff})"
            )

    # 2. Build filter conditions
    conditions = [
        PriceData.symbol == symbol.upper(),
        PriceData.exchange == exchange.upper(),
        PriceData.segment == segment.upper()
    ]

    if expiry is not None:
        conditions.append(PriceData.expiry == expiry)
    if strike is not None:
        conditions.append(PriceData.strike == strike)
    if option_type is not None:
        conditions.append(PriceData.option_type == option_type.upper())

    if version is not None:
        conditions.append(PriceData.version == version)
    else:
        conditions.append(PriceData.superseded_at.is_(None))

    if market_timestamp is not None:
        conditions.append(PriceData.market_timestamp == market_timestamp)
        stmt = select(PriceData).where(and_(*conditions))
    else:
        # Find the latest eligible price point (<= cutoff)
        conditions.append(PriceData.market_timestamp <= cutoff)
        stmt = (
            select(PriceData)
            .where(and_(*conditions))
            .order_by(desc(PriceData.market_timestamp))
            .limit(1)
        )

    result = await db.execute(stmt)
    return result.scalars().first()

async def get_eligible_range(
    db: AsyncSession,
    symbol: str,
    exchange: str,
    start_date: date,
    end_date: date,
    segment: str = "EQ",
    expiry: Optional[date] = None,
    strike: Optional[float] = None,
    option_type: Optional[str] = None,
    current_date: Optional[date] = None
) -> List[PriceData]:
    """
    COMPLIANCE-CRITICAL FUNCTION FOR RANGE QUERIES:
    Gates, validates, and clips range queries to the delay cutoff.
    """
    cutoff = get_delay_cutoff(current_date)
    
    if start_date > cutoff:
        raise ValueError(
            f"Requested start_date {start_date} falls within the restricted {settings.DELAY_DAYS}-day window (cutoff: {cutoff})"
        )
        
    # Clip the end date to the compliance cutoff boundary
    actual_end_date = min(end_date, cutoff)
    
    conditions = [
        PriceData.symbol == symbol.upper(),
        PriceData.exchange == exchange.upper(),
        PriceData.segment == segment.upper(),
        PriceData.market_timestamp >= start_date,
        PriceData.market_timestamp <= actual_end_date,
        # Only the current (non-superseded) version of each day - a range
        # query should reflect the latest known-correct history, not a mix
        # of stale and corrected values for different days in the range.
        PriceData.superseded_at.is_(None),
    ]

    if expiry is not None:
        conditions.append(PriceData.expiry == expiry)
    if strike is not None:
        conditions.append(PriceData.strike == strike)
    if option_type is not None:
        conditions.append(PriceData.option_type == option_type.upper())

    stmt = (
        select(PriceData)
        .where(and_(*conditions))
        .order_by(PriceData.market_timestamp)
    )

    result = await db.execute(stmt)
    return list(result.scalars().all())

async def get_eligible_symbols(
    db: AsyncSession,
    exchange: str,
    segment: str = "EQ",
    current_date: Optional[date] = None
) -> List[str]:
    """
    COMPLIANCE-CRITICAL FUNCTION FOR SYMBOLS LIST:
    """
    cutoff = get_delay_cutoff(current_date)
    stmt = (
        select(PriceData.symbol)
        .where(
            and_(
                PriceData.exchange == exchange.upper(),
                PriceData.segment == segment.upper(),
                PriceData.market_timestamp <= cutoff,
                PriceData.superseded_at.is_(None),
            )
        )
        .distinct()
        .order_by(PriceData.symbol)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())
