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
async def test_duplicate_ingestion_noop_when_unchanged(db_session: AsyncSession):
    """Re-ingesting identical OHLCV for an already-ingested day should not create a new version."""
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

    inserted = await save_records_to_db(db_session, records)
    assert inserted == 1
    await db_session.flush()
    db_session.expire_all()

    stmt = select(PriceData).where(PriceData.symbol == "RELIANCE")
    result = await db_session.execute(stmt)
    records_in_db = result.scalars().all()
    assert len(records_in_db) == 1
    assert records_in_db[0].version == 1
    assert records_in_db[0].superseded_at is None
    assert float(records_in_db[0].close) == 2420.0

    # Re-ingesting the SAME values (e.g. a routine re-run) should be a no-op:
    # no new version, no row change.
    reingested = await save_records_to_db(db_session, records)
    assert reingested == 0
    await db_session.flush()
    db_session.expire_all()

    stmt2 = select(PriceData).where(PriceData.symbol == "RELIANCE")
    records_in_db2 = (await db_session.execute(stmt2)).scalars().all()
    assert len(records_in_db2) == 1
    assert records_in_db2[0].version == 1


@pytest.mark.asyncio
async def test_eod_correction_creates_new_version_and_keeps_old(db_session: AsyncSession):
    """
    Verifies EOD correction semantics: when an already-ingested day's OHLCV
    changes (e.g. exchange issues a corrected bhavcopy), the old row is
    preserved (marked superseded) rather than overwritten, and a new
    versioned row becomes current. This matters because a tick replay
    session may have already generated/cached ticks from the old value -
    silently overwriting it would change that session's history out from
    under it (see brownian_bridge.tick_cache_key).
    """
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
    inserted = await save_records_to_db(db_session, records)
    assert inserted == 1
    await db_session.flush()
    db_session.expire_all()

    # A correction arrives with a different close/volume.
    corrected_records = [
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "segment": "EQ",
            "market_timestamp": target_date,
            "open": 2400.0,
            "high": 2450.0,
            "low": 2390.0,
            "close": 2430.0,  # corrected close
            "volume": 1200000  # corrected volume
        }
    ]
    corrected = await save_records_to_db(db_session, corrected_records)
    assert corrected == 1
    await db_session.flush()
    db_session.expire_all()

    # Both versions must now exist: the old one preserved (superseded), the
    # new one current. Nothing is overwritten in place.
    stmt_all = select(PriceData).where(PriceData.symbol == "RELIANCE").order_by(PriceData.version)
    all_versions = (await db_session.execute(stmt_all)).scalars().all()
    assert len(all_versions) == 2

    v1, v2 = all_versions
    assert v1.version == 1
    assert float(v1.close) == 2420.0
    assert v1.superseded_at is not None

    assert v2.version == 2
    assert float(v2.close) == 2430.0
    assert v2.volume == 1200000
    assert v2.superseded_at is None

    # Exactly one "current" (non-superseded) row for this key.
    stmt_current = select(PriceData).where(
        PriceData.symbol == "RELIANCE",
        PriceData.superseded_at.is_(None),
    )
    current_rows = (await db_session.execute(stmt_current)).scalars().all()
    assert len(current_rows) == 1
    assert current_rows[0].version == 2
