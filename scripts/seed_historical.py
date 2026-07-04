import os
import sys
import csv
import logging
from datetime import date, datetime, timedelta
from pathlib import Path
import yfinance as yf
import numpy as np
from sqlalchemy import select, and_, update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.sql import func

# Add project root to path
sys.path.append(str(Path(__file__).resolve().parent.parent))

from app.db.session import get_sync_db, sync_engine
from app.db.models import PriceData, Base, IngestionLog

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("seed_historical")

# Define target symbols and their yfinance equivalents
NSE_TICKERS = {
    "RELIANCE": "RELIANCE.NS",
    "TCS": "TCS.NS",
    "INFY": "INFY.NS",
    "SBIN": "SBIN.NS",
    "HDFCBANK": "HDFCBANK.NS"
}

BSE_TICKERS = {
    "500325": "RELIANCE.NS",   # Reliance on BSE
    "532540": "TCS.NS",        # TCS on BSE
    "500209": "INFY.NS",       # Infosys on BSE
    "500112": "SBIN.NS",       # SBI on BSE
    "500180": "HDFCBANK.NS"    # HDFC Bank on BSE
}

MCX_TICKERS = {
    "GOLD": "GC=F",
    "SILVER": "SI=F"
}

# Date utility: next month's last Thursday for derivatives expiry
def get_next_expiry_date(ref_date: date) -> date:
    year = ref_date.year
    month = ref_date.month + 1
    if month > 12:
        month = 1
        year += 1
        
    if month == 12:
        last_day = date(year, 12, 31)
    else:
        last_day = date(year, month + 1, 1) - timedelta(days=1)
        
    while last_day.weekday() != 3:  # 3 = Thursday
        last_day -= timedelta(days=1)
    return last_day

def save_csv_archive(exchange: str, target_date: date, filename: str, rows: list, headers: list):
    """Saves raw data to local archive folder: data/archive/{exchange}/{date}/"""
    archive_dir = Path(f"data/archive/{exchange}/{target_date.isoformat()}")
    archive_dir.mkdir(parents=True, exist_ok=True)
    
    file_path = archive_dir / filename
    with open(file_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows(rows)
    logger.info(f"Saved raw archive backup to: {file_path}")

def _record_key(r) -> tuple:
    """Mirrors app.ingestion.run_ingestion._record_key: the identity tuple
    that - together with `version` - makes up price_data's versioned unique
    constraint. Works on either a plain dict or a PriceData ORM instance."""
    get = r.get if isinstance(r, dict) else (lambda k: getattr(r, k))
    return (
        get("symbol"), get("exchange"), get("segment"),
        get("expiry"), float(get("strike")), get("option_type"), get("market_timestamp"),
    )


def _ohlcv_changed(existing: PriceData, new: dict) -> bool:
    return (
        float(existing.open) != float(new["open"])
        or float(existing.high) != float(new["high"])
        or float(existing.low) != float(new["low"])
        or float(existing.close) != float(new["close"])
        or int(existing.volume) != int(new["volume"])
        or int(existing.open_interest) != int(new["open_interest"])
    )


def upsert_records(db, records: list) -> int:
    """
    Inserts/corrects EOD records using the same versioned-upsert semantics as
    app.ingestion.run_ingestion.save_records_to_db (sync equivalent, since
    this seeder uses the sync DB session): never overwrites price_data rows
    in place. Unchanged re-seeding of the same date is a no-op; a changed
    value inserts a new version and marks the old one superseded. See
    save_records_to_db's docstring in app/ingestion/run_ingestion.py for the
    full rationale (tick replay history must not change silently).
    """
    if not records:
        return 0

    # Normalize default values for non-null constraint safety
    for r in records:
        if r.get("expiry") is None:
            r["expiry"] = date(1970, 1, 1)
        if r.get("strike") is None:
            r["strike"] = 0.0
        if r.get("option_type") is None:
            r["option_type"] = "XX"
        if r.get("open_interest") is None:
            r["open_interest"] = 0

    symbols = list({r["symbol"] for r in records})
    exchanges = list({r["exchange"] for r in records})
    segments = list({r["segment"] for r in records})
    dates = list({r["market_timestamp"] for r in records})

    stmt = select(PriceData).where(
        and_(
            PriceData.symbol.in_(symbols),
            PriceData.exchange.in_(exchanges),
            PriceData.segment.in_(segments),
            PriceData.market_timestamp.in_(dates),
            PriceData.superseded_at.is_(None),
        )
    )
    current_by_key = {_record_key(row): row for row in db.execute(stmt).scalars().all()}

    to_insert = []
    to_supersede_ids = []
    written_count = 0

    for r in records:
        key = _record_key(r)
        existing = current_by_key.get(key)

        if existing is None:
            new_record = dict(r)
            new_record["version"] = 1
            to_insert.append(new_record)
            written_count += 1
        elif _ohlcv_changed(existing, r):
            to_supersede_ids.append(existing.id)
            new_record = dict(r)
            new_record["version"] = existing.version + 1
            to_insert.append(new_record)
            written_count += 1
        # else: unchanged, no-op.

    if to_supersede_ids:
        db.execute(
            sa_update(PriceData)
            .where(PriceData.id.in_(to_supersede_ids))
            .values(superseded_at=func.now())
        )

    if to_insert:
        db.execute(pg_insert(PriceData).values(to_insert))

    return written_count

def main():
    # Make sure tables exist
    Base.metadata.create_all(bind=sync_engine)
    
    # Target range: past 40 calendar days
    end_date = date.today()
    start_date = end_date - timedelta(days=40)
    
    logger.info(f"Seeding historical data from {start_date} to {end_date}...")
    
    all_tickers = list(NSE_TICKERS.values()) + list(MCX_TICKERS.values())
    
    # Download data from yfinance in bulk
    data_df = yf.download(all_tickers, start=start_date.isoformat(), end=end_date.isoformat())
    
    if data_df.empty:
        logger.error("Failed to download any data from yfinance.")
        return
        
    with get_sync_db() as db:
        # We iterate day by day
        for dt_index in data_df.index:
            target_date = dt_index.date()
            logger.info(f"Processing date: {target_date}")
            
            # Lists to store records for this day
            nse_csv_rows = []
            bse_csv_rows = []
            fo_csv_rows = []
            mcx_csv_rows = []
            
            db_records = []
            
            # 1. Process NSE & BSE Equities
            for symbol, yf_ticker in NSE_TICKERS.items():
                try:
                    open_val = data_df["Open"][yf_ticker].loc[dt_index]
                    high_val = data_df["High"][yf_ticker].loc[dt_index]
                    low_val = data_df["Low"][yf_ticker].loc[dt_index]
                    close_val = data_df["Close"][yf_ticker].loc[dt_index]
                    vol_val = data_df["Volume"][yf_ticker].loc[dt_index]
                    
                    # Handle nan check safely
                    if any(map(lambda v: v is None or np.isnan(v), [open_val, high_val, low_val, close_val])):
                        continue
                        
                    open_val, high_val, low_val, close_val = float(open_val), float(high_val), float(low_val), float(close_val)
                    vol_val = int(vol_val)
                    
                    if open_val <= 0 or vol_val < 0:
                        continue
                    
                    # NSE EQ
                    db_records.append({
                        "symbol": symbol,
                        "exchange": "NSE",
                        "segment": "EQ",
                        "expiry": None,
                        "strike": None,
                        "option_type": None,
                        "open_interest": None,
                        "market_timestamp": target_date,
                        "open": round(open_val, 2),
                        "high": round(high_val, 2),
                        "low": round(low_val, 2),
                        "close": round(close_val, 2),
                        "volume": vol_val
                    })
                    nse_csv_rows.append([symbol, "EQ", target_date, open_val, high_val, low_val, close_val, vol_val])
                    
                    # BSE EQ
                    bse_symbol = [b for b, y in BSE_TICKERS.items() if y == yf_ticker][0]
                    db_records.append({
                        "symbol": bse_symbol,
                        "exchange": "BSE",
                        "segment": "EQ",
                        "expiry": None,
                        "strike": None,
                        "option_type": None,
                        "open_interest": None,
                        "market_timestamp": target_date,
                        "open": round(open_val * 0.999, 2),
                        "high": round(high_val * 0.999, 2),
                        "low": round(low_val * 0.999, 2),
                        "close": round(close_val * 0.999, 2),
                        "volume": int(vol_val * 0.08)
                    })
                    bse_csv_rows.append([bse_symbol, "EQ", target_date, open_val * 0.999, high_val * 0.999, low_val * 0.999, close_val * 0.999, int(vol_val * 0.08)])
                    
                    # 2. Synthesize NSE Derivatives (Futures & Options)
                    expiry_dt = get_next_expiry_date(target_date)
                    
                    # Futures contract
                    fut_price = close_val * 1.0015
                    db_records.append({
                        "symbol": symbol,
                        "exchange": "NSE",
                        "segment": "FUT",
                        "expiry": expiry_dt,
                        "strike": None,
                        "option_type": "XX",
                        "open_interest": int(vol_val * 0.12),
                        "market_timestamp": target_date,
                        "open": round(open_val * 1.0015, 2),
                        "high": round(high_val * 1.002, 2),
                        "low": round(low_val * 1.001, 2),
                        "close": round(fut_price, 2),
                        "volume": int(vol_val * 0.25)
                    })
                    fo_csv_rows.append([symbol, "FUT", expiry_dt, "", "XX", int(vol_val * 0.12), target_date, open_val * 1.0015, high_val * 1.002, low_val * 1.001, fut_price, int(vol_val * 0.25)])
                    
                    # Call Option
                    strike_price = round(close_val / 50.0) * 50.0
                    opt_open = close_val * 0.025
                    opt_close = close_val * 0.022
                    opt_high = max(opt_open, opt_close) * 1.2
                    opt_low = min(opt_open, opt_close) * 0.8
                    db_records.append({
                        "symbol": symbol,
                        "exchange": "NSE",
                        "segment": "OPT",
                        "expiry": expiry_dt,
                        "strike": strike_price,
                        "option_type": "CE",
                        "open_interest": int(vol_val * 0.08),
                        "market_timestamp": target_date,
                        "open": round(opt_open, 2),
                        "high": round(opt_high, 2),
                        "low": round(opt_low, 2),
                        "close": round(opt_close, 2),
                        "volume": int(vol_val * 0.15)
                    })
                    fo_csv_rows.append([symbol, "OPT", expiry_dt, strike_price, "CE", int(vol_val * 0.08), target_date, opt_open, opt_high, opt_low, opt_close, int(vol_val * 0.15)])
                    
                    # Put Option
                    opt_open_p = close_val * 0.018
                    opt_close_p = close_val * 0.021
                    opt_high_p = max(opt_open_p, opt_close_p) * 1.3
                    opt_low_p = min(opt_open_p, opt_close_p) * 0.7
                    db_records.append({
                        "symbol": symbol,
                        "exchange": "NSE",
                        "segment": "OPT",
                        "expiry": expiry_dt,
                        "strike": strike_price,
                        "option_type": "PE",
                        "open_interest": int(vol_val * 0.06),
                        "market_timestamp": target_date,
                        "open": round(opt_open_p, 2),
                        "high": round(opt_high_p, 2),
                        "low": round(opt_low_p, 2),
                        "close": round(opt_close_p, 2),
                        "volume": int(vol_val * 0.12)
                    })
                    fo_csv_rows.append([symbol, "OPT", expiry_dt, strike_price, "PE", int(vol_val * 0.06), target_date, opt_open_p, opt_high_p, opt_low_p, opt_close_p, int(vol_val * 0.12)])
                    
                except Exception as e:
                    logger.error(f"Error processing equity/F&O {symbol} on {target_date}: {e}")
                    
            # 3. Process MCX Commodities
            for symbol, yf_ticker in MCX_TICKERS.items():
                try:
                    open_val = data_df["Open"][yf_ticker].loc[dt_index]
                    high_val = data_df["High"][yf_ticker].loc[dt_index]
                    low_val = data_df["Low"][yf_ticker].loc[dt_index]
                    close_val = data_df["Close"][yf_ticker].loc[dt_index]
                    vol_val = data_df["Volume"][yf_ticker].loc[dt_index]
                    
                    if any(map(lambda v: v is None or np.isnan(v), [open_val, high_val, low_val, close_val])):
                        continue
                        
                    open_val, high_val, low_val, close_val = float(open_val), float(high_val), float(low_val), float(close_val)
                    vol_val = int(vol_val) if not np.isnan(vol_val) else 1000
                    
                    # Convert USD price to Indian market rate scale
                    multiplier = 70.0 if symbol == "GOLD" else 80.0
                    expiry_dt = get_next_expiry_date(target_date)
                    
                    db_records.append({
                        "symbol": symbol,
                        "exchange": "MCX",
                        "segment": "FUT",
                        "expiry": expiry_dt,
                        "strike": None,
                        "option_type": "XX",
                        "open_interest": int(vol_val * 1.5),
                        "market_timestamp": target_date,
                        "open": round(open_val * multiplier, 2),
                        "high": round(high_val * multiplier, 2),
                        "low": round(low_val * multiplier, 2),
                        "close": round(close_val * multiplier, 2),
                        "volume": vol_val
                    })
                    mcx_csv_rows.append([symbol, "FUT", expiry_dt, "XX", int(vol_val * 1.5), target_date, open_val * multiplier, high_val * multiplier, low_val * multiplier, close_val * multiplier, vol_val])
                except Exception as e:
                    logger.error(f"Error processing MCX commodity {symbol} on {target_date}: {e}")
                    
            # 4. Save CSV archives
            headers_eq = ["symbol", "segment", "date", "open", "high", "low", "close", "volume"]
            headers_fo = ["symbol", "segment", "expiry", "strike", "option_type", "open_interest", "date", "open", "high", "low", "close", "volume"]
            headers_mcx = ["symbol", "segment", "expiry", "option_type", "open_interest", "date", "open", "high", "low", "close", "volume"]
            
            if nse_csv_rows:
                save_csv_archive("NSE", target_date, "bhavcopy.csv", nse_csv_rows, headers_eq)
            if bse_csv_rows:
                save_csv_archive("BSE", target_date, "bhavcopy.csv", bse_csv_rows, headers_eq)
            if fo_csv_rows:
                save_csv_archive("NSE_FO", target_date, "bhavcopy_fo.csv", fo_csv_rows, headers_fo)
            if mcx_csv_rows:
                save_csv_archive("MCX", target_date, "mcx_eod.csv", mcx_csv_rows, headers_mcx)
                
            # 5. Insert in Database
            if db_records:
                rows = upsert_records(db, db_records)
                logger.info(f"Ingested and upserted {rows} records in database for {target_date}.")
                
                # Log success
                log_entry = IngestionLog(
                    source="seeder_yfinance",
                    target_date=target_date,
                    status="success",
                    rows_ingested=rows,
                    error_message=None
                )
                db.add(log_entry)

    logger.info("Database historical seeding successfully finished!")

if __name__ == "__main__":
    main()
