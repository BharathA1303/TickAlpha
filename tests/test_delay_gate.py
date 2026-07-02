import pytest
from datetime import date
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import PriceData
from app.core.delay_gate import get_eligible_data, get_eligible_range, get_eligible_symbols

@pytest.mark.asyncio
async def test_delay_gate_logic(db_session: AsyncSession):
    # Reference Date for compliance cutoff: 2026-07-01.
    # Cutoff date (3 days ago): 2026-06-28.
    current_date = date(2026, 7, 1)
    
    # 1. Seed test data
    data_points = [
        # RELIANCE on NSE (various dates)
        PriceData(symbol="RELIANCE", exchange="NSE", segment="EQ", market_timestamp=date(2026, 6, 25), open=100, high=105, low=98, close=102, volume=1000),  # 6 days (Eligible)
        PriceData(symbol="RELIANCE", exchange="NSE", segment="EQ", market_timestamp=date(2026, 6, 28), open=102, high=107, low=101, close=105, volume=1200),  # 3 days (Eligible - Boundary)
        PriceData(symbol="RELIANCE", exchange="NSE", segment="EQ", market_timestamp=date(2026, 6, 29), open=105, high=110, low=104, close=108, volume=1100),  # 2 days (Restricted)
        PriceData(symbol="RELIANCE", exchange="NSE", segment="EQ", market_timestamp=date(2026, 6, 30), open=108, high=112, low=107, close=110, volume=900),   # 1 day (Restricted)
        PriceData(symbol="RELIANCE", exchange="NSE", segment="EQ", market_timestamp=date(2026, 7, 1), open=110, high=115, low=109, close=112, volume=1300),    # 0 days (Restricted - Today)
        
        # TCS on NSE - Only restricted data
        PriceData(symbol="TCS", exchange="NSE", segment="EQ", market_timestamp=date(2026, 6, 30), open=3000, high=3050, low=2990, close=3020, volume=500),      # 1 day (Restricted)
    ]
    
    for dp in data_points:
        db_session.add(dp)
    await db_session.flush()

    # --- Test 1: Retrieve latest price (market_timestamp=None) ---
    # Should return the 3-day cutoff row (2026-06-28), NOT the 2 days or later rows.
    latest = await get_eligible_data(db_session, "RELIANCE", "NSE", segment="EQ", market_timestamp=None, current_date=current_date)
    assert latest is not None
    assert latest.market_timestamp == date(2026, 6, 28)
    assert float(latest.close) == 105.0

    # --- Test 2: Specific Date Queries ---
    # A. Eligible: 6 days ago (2026-06-25)
    p_6 = await get_eligible_data(db_session, "RELIANCE", "NSE", segment="EQ", market_timestamp=date(2026, 6, 25), current_date=current_date)
    assert p_6 is not None
    assert p_6.market_timestamp == date(2026, 6, 25)
    
    # B. Eligible Boundary: 3 days ago (2026-06-28)
    p_3 = await get_eligible_data(db_session, "RELIANCE", "NSE", segment="EQ", market_timestamp=date(2026, 6, 28), current_date=current_date)
    assert p_3 is not None
    assert p_3.market_timestamp == date(2026, 6, 28)

    # C. Restricted: 2 days ago (2026-06-29) -> Should raise ValueError
    with pytest.raises(ValueError, match="restricted"):
        await get_eligible_data(db_session, "RELIANCE", "NSE", segment="EQ", market_timestamp=date(2026, 6, 29), current_date=current_date)
        
    # D. Restricted: Today (2026-07-01) -> Should raise ValueError
    with pytest.raises(ValueError, match="restricted"):
        await get_eligible_data(db_session, "RELIANCE", "NSE", segment="EQ", market_timestamp=date(2026, 7, 1), current_date=current_date)

    # --- Test 3: Range Queries ---
    # A. Range starts before cutoff, ends after cutoff -> Should clip end date to cutoff (2026-06-28)
    range_data = await get_eligible_range(
        db_session, "RELIANCE", "NSE", 
        start_date=date(2026, 6, 1), end_date=date(2026, 6, 30), 
        segment="EQ", current_date=current_date
    )
    # Should only return 2026-06-25 and 2026-06-28
    assert len(range_data) == 2
    assert range_data[0].market_timestamp == date(2026, 6, 25)
    assert range_data[1].market_timestamp == date(2026, 6, 28)

    # B. Range entirely within restricted window -> Should raise ValueError
    with pytest.raises(ValueError, match="restricted"):
        await get_eligible_range(
            db_session, "RELIANCE", "NSE", 
            start_date=date(2026, 6, 29), end_date=date(2026, 6, 30), 
            segment="EQ", current_date=current_date
        )

    # --- Test 4: Symbols Listing ---
    # RELIANCE has eligible data. TCS only has restricted data.
    # Therefore, only RELIANCE should show up in the eligible symbols list.
    symbols = await get_eligible_symbols(db_session, "NSE", segment="EQ", current_date=current_date)
    assert "RELIANCE" in symbols
    assert "TCS" not in symbols
