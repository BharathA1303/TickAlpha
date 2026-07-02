import pytest
from datetime import date
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PriceData
from app.ingestion.nse_bhavcopy import parse_nse_bhavcopy_csv
from app.ingestion.bse_bhavcopy import parse_bse_bhavcopy_csv
from app.ingestion.run_ingestion import save_records_to_db

def test_parse_nse_bhavcopy():
    # Mock CSV data for NSE
    csv_content = (
        "SYMBOL,SERIES,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,TOTTRDQTY,TOTTRDVAL,TIMESTAMP,TOTALTRADES,ISIN\n"
        "RELIANCE,EQ,2400.00,2450.00,2390.00,2420.00,2422.00,2395.00,1500000,3630000000,15-JUN-2026,50000,INE002A01018\n"
        "TCS,EQ,3200.0,3230.0,3180.0,3210.0,3208.0,3190.0,500000,1600000000,15-JUN-2026,30000,INE467B01029\n"
        "NIFTY,AM,,,,,18000.0,,,,15-JUN-2026,,\n"  # Non-EQ series should be skipped
    )
    
    target_date = date(2026, 6, 15)
    records = parse_nse_bhavcopy_csv(csv_content, target_date)
    
    assert len(records) == 2
    
    # Check RELIANCE
    reliance = next(r for r in records if r["symbol"] == "RELIANCE")
    assert reliance["exchange"] == "NSE"
    assert reliance["segment"] == "EQ"
    assert reliance["market_timestamp"] == target_date
    assert reliance["open"] == 2400.0
    assert reliance["high"] == 2450.0
    assert reliance["low"] == 2390.0
    assert reliance["close"] == 2420.0
    assert reliance["volume"] == 1500000

    # Check TCS
    tcs = next(r for r in records if r["symbol"] == "TCS")
    assert tcs["close"] == 3210.0
    assert tcs["volume"] == 500000


def test_parse_bse_bhavcopy():
    # Mock CSV data for BSE
    csv_content = (
        "SC_CODE,SC_NAME,SC_GROUP,SC_TYPE,OPEN,HIGH,LOW,CLOSE,LAST,PREVCLOSE,NO_TRADES,NO_OF_SHRS,NET_TURNOV,TDCLO_DELV\n"
        "500325,RELIANCE,A,Q,2401.00,2451.00,2391.00,2421.00,2422.00,2396.00,4000,12000,29000000.0,6000\n"
        "500180,HDFC,A,Q,1600.00,1620.00,1590.00,1610.00,1608.00,1595.00,3000,15000,24000000.0,7500\n"
    )
    
    target_date = date(2026, 6, 15)
    records = parse_bse_bhavcopy_csv(csv_content, target_date)
    
    assert len(records) == 2
    
    reliance = next(r for r in records if r["symbol"] == "RELIANCE")
    assert reliance["exchange"] == "BSE"
    assert reliance["segment"] == "EQ"
    assert reliance["market_timestamp"] == target_date
    assert reliance["open"] == 2401.0
    assert reliance["high"] == 2451.0
    assert reliance["low"] == 2391.0
    assert reliance["close"] == 2421.0
    assert reliance["volume"] == 12000


@pytest.mark.asyncio
async def test_duplicate_ingestion_upsert(db_session: AsyncSession):
    """Verifies that inserting duplicate keys updates rows instead of raising errors."""
    target_date = date(2026, 6, 15)
    
    records = [
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "segment": "EQ",
            "market_timestamp": target_date,
            "open": 2400.0,
            "high": 2450.0,
            "low": 2390.0,
            "close": 2420.0,
            "volume": 1000000
        }
    ]
    
    # 1. First ingestion run
    inserted = await save_records_to_db(db_session, records)
    assert inserted == 1
    await db_session.flush()
    db_session.expire_all()
    
    # Verify DB state
    stmt = select(PriceData).where(PriceData.symbol == "RELIANCE")
    result = await db_session.execute(stmt)
    records_in_db = result.scalars().all()
    assert len(records_in_db) == 1
    assert float(records_in_db[0].close) == 2420.0
    
    # 2. Second ingestion run with updated values (e.g. close changes to 2430.0, volume changes)
    updated_records = [
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "segment": "EQ",
            "market_timestamp": target_date,
            "open": 2400.0,
            "high": 2450.0,
            "low": 2390.0,
            "close": 2430.0,  # updated close
            "volume": 1200000  # updated volume
        }
    ]
    
    upserted = await save_records_to_db(db_session, updated_records)
    assert upserted == 1
    await db_session.flush()
    db_session.expire_all()
    
    # Verify DB state - should still only have 1 row, but with updated values!
    stmt2 = select(PriceData).where(PriceData.symbol == "RELIANCE")
    result2 = await db_session.execute(stmt2)
    records_in_db2 = result2.scalars().all()
    assert len(records_in_db2) == 1
    assert float(records_in_db2[0].close) == 2430.0
    assert records_in_db2[0].volume == 1200000
