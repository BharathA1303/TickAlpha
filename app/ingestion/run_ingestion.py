import argparse
import asyncio
import logging
import sys
import os
from datetime import date, timedelta
from pathlib import Path
from typing import List, Dict, Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent.parent))

from app.config import settings
from app.db.session import AsyncSessionLocal
from app.db.models import PriceData, IngestionLog
from app.ingestion.nse_bhavcopy import get_nse_data
from app.ingestion.bse_bhavcopy import get_bse_data

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("run_ingestion")

def is_trading_day(target_date: date) -> bool:
    """Checks if the date is a weekday (Monday to Friday)."""
    return target_date.weekday() < 5

def archive_raw_data(exchange: str, target_date: date, filename: str, content: Any):
    """Backup raw CSV or ZIP data to data/archive/{exchange}/{date}/ before loading."""
    archive_dir = Path("data") / "archive" / exchange.upper() / target_date.isoformat()
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = archive_dir / filename
    
    mode = "wb" if isinstance(content, bytes) else "w"
    encoding = None if isinstance(content, bytes) else "utf-8"
    
    with open(file_path, mode, encoding=encoding) as f:
        f.write(content)
        
    logger.info(f"Archived raw file to: {file_path}")

async def save_records_to_db(db, records: List[Dict[str, Any]]) -> int:
    """Upserts list of records into PostgreSQL using native ON CONFLICT."""
    if not records:
        return 0
        
    # Ensure all dictionaries have all keys and resolve NULL unique constraint issue
    for r in records:
        if r.get("expiry") is None:
            r["expiry"] = date(1970, 1, 1)
        if r.get("strike") is None:
            r["strike"] = 0.0
        if r.get("option_type") is None:
            r["option_type"] = "XX"
        if r.get("open_interest") is None:
            r["open_interest"] = 0
        
    # Bulk upsert using pg_insert
    stmt = pg_insert(PriceData).values(records)
    
    # Define update mapping on conflict (preserving existing derivatives identifiers)
    update_dict = {
        "open": stmt.excluded.open,
        "high": stmt.excluded.high,
        "low": stmt.excluded.low,
        "close": stmt.excluded.close,
        "volume": stmt.excluded.volume,
        "open_interest": stmt.excluded.open_interest,
        "ingested_at": func.now()
    }
    
    upsert_stmt = stmt.on_conflict_do_update(
        constraint="uq_price_data_symbol_exchange_segment_date",
        set_=update_dict
    )
    
    await db.execute(upsert_stmt)
    return len(records)

async def ingest_date_for_exchange(db, target_date: date, exchange: str, use_mock: bool) -> int:
    """Downloads/generates and saves records for a specific exchange and date."""
    exchange = exchange.upper()
    logger.info(f"Starting ingestion for {exchange} on {target_date} (Mock: {use_mock})")
    
    try:
        # Mock ingestion bypasses downloads and archives mock CSV string
        if use_mock:
            if exchange == "NSE":
                records = get_nse_data(target_date, use_mock=True)
                mock_csv_content = "symbol,segment,date,open,high,low,close,volume\n" + \
                    "\n".join([f"{r['symbol']},{r['segment']},{target_date.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}" for r in records])
                archive_raw_data("NSE", target_date, "mock_nse_bhavcopy.csv", mock_csv_content)
            elif exchange == "BSE":
                records = get_bse_data(target_date, use_mock=True)
                mock_csv_content = "symbol,segment,date,open,high,low,close,volume\n" + \
                    "\n".join([f"{r['symbol']},{r['segment']},{target_date.isoformat()},{r['open']},{r['high']},{r['low']},{r['close']},{r['volume']}" for r in records])
                archive_raw_data("BSE", target_date, "mock_bse_bhavcopy.csv", mock_csv_content)
            else:
                records = []
        else:
            # Real Ingestion (in production, would download files from exchanges and archive raw bytes)
            if exchange == "NSE":
                # In real life we'd download the zip bytes, save to archive, and then parse
                from app.ingestion.nse_bhavcopy import download_nse_bhavcopy, parse_nse_bhavcopy_csv, zipfile, io
                try:
                    zip_bytes = download_nse_bhavcopy(target_date)
                    # Archive raw ZIP file
                    archive_raw_data("NSE", target_date, "cmbhav.csv.zip", zip_bytes)
                    
                    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
                        csv_filenames = [name for name in z.namelist() if name.endswith(".csv")]
                        if csv_filenames:
                            with z.open(csv_filenames[0]) as f:
                                csv_content = f.read().decode("utf-8", errors="ignore")
                                records = parse_nse_bhavcopy_csv(csv_content, target_date)
                        else:
                            records = []
                except FileNotFoundError:
                    logger.warning(f"No NSE bhavcopy found for {target_date} on official servers.")
                    records = []
            elif exchange == "BSE":
                from app.ingestion.bse_bhavcopy import download_bse_bhavcopy, parse_bse_bhavcopy_csv
                try:
                    csv_text = download_bse_bhavcopy(target_date)
                    # Archive raw CSV file
                    archive_raw_data("BSE", target_date, "eqbhav.csv", csv_text)
                    records = parse_bse_bhavcopy_csv(csv_text, target_date)
                except FileNotFoundError:
                    logger.warning(f"No BSE bhavcopy found for {target_date} on official servers.")
                    records = []
            else:
                raise ValueError(f"Unknown exchange: {exchange}")
                
        # Inject optional derivative default fields for cash equities
        for record in records:
            if "expiry" not in record:
                record["expiry"] = None
            if "strike" not in record:
                record["strike"] = None
            if "option_type" not in record:
                record["option_type"] = None
            if "open_interest" not in record:
                record["open_interest"] = None
                
        rows_ingested = await save_records_to_db(db, records)
        
        # Log success in ingestion_log
        log_entry = IngestionLog(
            source=f"{exchange.lower()}_bhavcopy",
            target_date=target_date,
            status="success" if rows_ingested > 0 else "skipped",
            rows_ingested=rows_ingested,
            error_message=None
        )
        db.add(log_entry)
        logger.info(f"Ingested {rows_ingested} rows for {exchange} on {target_date}")
        return rows_ingested
        
    except Exception as e:
        error_msg = str(e)
        logger.error(f"Failed ingestion for {exchange} on {target_date}: {error_msg}")
        
        # Log failure in ingestion_log
        log_entry = IngestionLog(
            source=f"{exchange.lower()}_bhavcopy",
            target_date=target_date,
            status="failed",
            rows_ingested=0,
            error_message=error_msg[:1000]
        )
        db.add(log_entry)
        return 0

async def ingest_date(target_date: date, use_mock: bool) -> Dict[str, int]:
    """Ingests both NSE and BSE bhavcopies for a given date."""
    if not is_trading_day(target_date):
        logger.info(f"Skipping {target_date} - it is a weekend.")
        return {"NSE": 0, "BSE": 0}
        
    async with AsyncSessionLocal() as db:
        try:
            nse_rows = await ingest_date_for_exchange(db, target_date, "NSE", use_mock)
            bse_rows = await ingest_date_for_exchange(db, target_date, "BSE", use_mock)
            await db.commit()
            return {"NSE": nse_rows, "BSE": bse_rows}
        except Exception as e:
            logger.error(f"Database error during ingestion commit for {target_date}: {e}")
            await db.rollback()
            return {"NSE": 0, "BSE": 0}

async def run_backfill(days_to_backfill: int, use_mock: bool):
    """Backfills the database with historical data for the last N calendar days."""
    today_date = date.today()
    logger.info(f"Running backfill for the last {days_to_backfill} days starting from yesterday...")
    
    total_nse = 0
    total_bse = 0
    
    for i in range(1, days_to_backfill + 1):
        target_date = today_date - timedelta(days=i)
        if not is_trading_day(target_date):
            logger.info(f"Skipping date {target_date} (weekend)")
            continue
            
        logger.info(f"Backfilling day {i}/{days_to_backfill}: {target_date}")
        counts = await ingest_date(target_date, use_mock)
        total_nse += counts["NSE"]
        total_bse += counts["BSE"]
        
        if not use_mock:
            await asyncio.sleep(1.0)
            
    logger.info(f"Backfill complete. Total NSE records: {total_nse}, Total BSE records: {total_bse}")

async def main():
    parser = argparse.ArgumentParser(description="Ingest NSE/BSE Equity Bhavcopies.")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--date", help="Target date to ingest (YYYY-MM-DD)")
    group.add_argument("--backfill-days", type=int, help="Number of trading days to backfill backward from yesterday")
    parser.add_argument("--mock", action="store_true", help="Generate mock data instead of fetching from exchanges")
    
    args = parser.parse_args()
    
    if args.date:
        try:
            target_date = date.fromisoformat(args.date)
        except ValueError:
            logger.error("Invalid date format. Use YYYY-MM-DD")
            sys.exit(1)
        await ingest_date(target_date, args.mock)
    elif args.backfill_days:
        await run_backfill(args.backfill_days, args.mock)
    else:
        yesterday = date.today() - timedelta(days=1)
        await ingest_date(yesterday, args.mock)

if __name__ == "__main__":
    asyncio.run(main())
