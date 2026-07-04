import csv
import io
import time
import logging
from datetime import date
from typing import List, Dict, Any, Optional
import requests

logger = logging.getLogger(__name__)

# Standard browser headers to avoid 403 Forbidden responses
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Referer": "https://www.bseindia.com/"
}

# Mirrors the NSE downloader's throttling/retry approach so a transient block
# or network hiccup isn't mistaken for "no data today".
MIN_REQUEST_INTERVAL_SECONDS = 0.75
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0

def get_bse_bhavcopy_url(target_date: date) -> str:
    """
    Constructs the BSE bhavcopy URL.
    Format: https://www.bseindia.com/download/BhavCopy/Equity/EQDDMMYY.CSV
    E.g. for 2026-06-15: https://www.bseindia.com/download/BhavCopy/Equity/EQ150626.CSV
    """
    day = target_date.strftime("%d")
    month = target_date.strftime("%m")
    year_short = target_date.strftime("%y")  # 2 digit year
    return f"https://www.bseindia.com/download/BhavCopy/Equity/EQ{day}{month}{year_short}.CSV"

def download_bse_bhavcopy(target_date: date) -> str:
    """
    Downloads the BSE bhavcopy CSV for a given date.
    Retries transient failures (network errors, non-404 HTTP errors, and
    anti-bot blocks that return an HTML page instead of a CSV) with
    exponential backoff before giving up.
    """
    url = get_bse_bhavcopy_url(target_date)

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"Downloading BSE Bhavcopy from URL: {url} (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS)

        try:
            response = requests.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 404:
                raise FileNotFoundError(f"BSE Bhavcopy not found for {target_date} (likely a market holiday or weekend)")
            response.raise_for_status()

            text = response.text
            if text.strip().startswith("<!DOCTYPE") or "<html" in text.lower():
                raise ValueError(
                    f"BSE response for {target_date} was HTML instead of CSV - likely blocked by anti-bot protection"
                )

            return text
        except FileNotFoundError:
            raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    f"BSE Bhavcopy download attempt {attempt}/{MAX_RETRIES} failed for {target_date}: {e}. "
                    f"Retrying in {backoff:.1f}s..."
                )
                time.sleep(backoff)

    raise RuntimeError(
        f"BSE Bhavcopy download failed for {target_date} after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error

def parse_bse_bhavcopy_csv(csv_content: str, target_date: date) -> List[Dict[str, Any]]:
    """
    Parses BSE bhavcopy CSV data.
    Expected columns: SC_CODE, SC_NAME, SC_GROUP, SC_TYPE, OPEN, HIGH, LOW, CLOSE, NO_OF_SHRS (Volume)
    Filters for equity stocks.
    """
    # Sanity check: if it looks like HTML, it is not a valid CSV (BSE redirected/blocked request)
    if csv_content.strip().startswith("<!DOCTYPE") or "<html" in csv_content.lower():
        logger.warning(f"BSE downloaded content for {target_date} is HTML instead of CSV. This could be due to blocking or the file not existing on exchange servers.")
        return []

    records = []
    f = io.StringIO(csv_content)
    reader = csv.DictReader(f)
    
    # Clean headers (strip spaces and uppercase)
    if not reader.fieldnames:
        raise ValueError("Empty CSV file or invalid headers in BSE copy")
    
    reader.fieldnames = [field.strip().upper() for field in reader.fieldnames if field is not None]
    
    for row_idx, row in enumerate(reader):
        try:
            # Clean row values safely
            cleaned_row = {}
            for k, v in row.items():
                if k is not None:
                    cleaned_row[k.strip().upper()] = v.strip() if (v is not None and hasattr(v, 'strip')) else ""
            
            # Filter: BSE has various security groups/types. 
            # Usually SC_TYPE == 'Q' or we filter for standard equities groups (A, B, T, etc.)
            # If SC_GROUP is available, we check if it is part of standard equity groups.
            # To be simple and robust, we can exclude debt/derivative rows.
            # If SC_TYPE is present and is not 'Q' (Equity), we can skip. Or we can just check if OPEN/HIGH/LOW/CLOSE are > 0.
            sc_type = cleaned_row.get("SC_TYPE", "").upper()
            if sc_type and "Q" not in sc_type:  # Typically 'Q' or empty for equities
                continue
                
            symbol = cleaned_row.get("SC_NAME", "").strip()
            # If no SC_NAME, use SC_CODE
            if not symbol:
                symbol = cleaned_row.get("SC_CODE", "").strip()
                
            if not symbol:
                continue
                
            # Volume column could be NO_OF_SHRS or NO_SHRS or VOLUME
            vol_key = "NO_OF_SHRS" if "NO_OF_SHRS" in cleaned_row else ("NO_SHRS" if "NO_SHRS" in cleaned_row else "VOLUME")
            volume_str = cleaned_row.get(vol_key, "0")
            
            records.append({
                "symbol": symbol,
                "exchange": "BSE",
                "segment": "EQ",
                "market_timestamp": target_date,
                "open": float(cleaned_row["OPEN"]),
                "high": float(cleaned_row["HIGH"]),
                "low": float(cleaned_row["LOW"]),
                "close": float(cleaned_row["CLOSE"]),
                "volume": int(float(volume_str)),  # Convert to float first in case of decimal representation, then int
            })
        except (KeyError, ValueError) as e:
            # Log parsing errors but keep going
            logger.debug(f"Skipping BSE row {row_idx} due to parse error: {e}")
            continue
            
    return records

def generate_mock_bse_data(target_date: date) -> List[Dict[str, Any]]:
    """Generates realistic mock BSE bhavcopy records for testing and local dev."""
    logger.info(f"Generating mock BSE data for {target_date}")
    # BSE symbols often map to names of stocks, sometimes similar to NSE or numeric codes.
    # We will use some standard stock names for BSE.
    mock_symbols = ["500325", "500180", "500209", "532540", "532898", "500112", "500312", "532215", "500696", "500510"]
    records = []
    
    import random
    
    for symbol in mock_symbols:
        seed = int(target_date.strftime("%Y%m%d")) + sum(ord(c) for c in symbol) + 5  # offset seed from NSE
        rng = random.Random(seed)
        
        base_price = rng.uniform(50.0, 2000.0)
        pct_change = rng.uniform(-0.025, 0.025)
        open_p = base_price * (1 + rng.uniform(-0.008, 0.008))
        close_p = open_p * (1 + pct_change)
        high_p = max(open_p, close_p) * (1 + rng.uniform(0, 0.015))
        low_p = min(open_p, close_p) * (1 - rng.uniform(0, 0.015))
        vol = rng.randint(5000, 2000000)
        
        records.append({
            "symbol": symbol,
            "exchange": "BSE",
            "segment": "EQ",
            "market_timestamp": target_date,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": vol,
        })
    return records

def get_bse_data(target_date: date, use_mock: bool = False) -> List[Dict[str, Any]]:
    """Main entrypoint to get BSE records. Downloads or mocks them."""
    if use_mock:
        return generate_mock_bse_data(target_date)
        
    try:
        csv_text = download_bse_bhavcopy(target_date)
        return parse_bse_bhavcopy_csv(csv_text, target_date)
    except FileNotFoundError as e:
        logger.warning(str(e))
        return []
    except Exception as e:
        logger.error(f"Error fetching/parsing BSE Bhavcopy for {target_date}: {e}")
        raise e
