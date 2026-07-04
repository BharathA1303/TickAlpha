import csv
import io
import time
import zipfile
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
    "Referer": "https://www.nseindia.com/"
}

# NSE's anti-bot protection tolerates roughly 3 req/sec from a given client;
# a short pre-request delay plus retry/backoff keeps us well under that and
# avoids transient 403s being mistaken for "no data today".
MIN_REQUEST_INTERVAL_SECONDS = 0.75
MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0

def get_nse_bhavcopy_url(target_date: date) -> str:
    """
    Constructs the NSE bhavcopy URL using the new UDiFF format.
    Format: https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_YYYYMMDD_F_0000.csv.zip
    """
    date_str = target_date.strftime("%Y%m%d")
    return f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{date_str}_F_0000.csv.zip"

def download_nse_bhavcopy(target_date: date) -> bytes:
    """
    Downloads the zipped NSE bhavcopy for a given date.
    Retries transient failures (network errors, non-404 HTTP errors, and
    anti-bot blocks that return HTML instead of a zip) with exponential
    backoff, since a single flaky request should not be mistaken for a
    genuine "no data today" (404) result.
    """
    url = get_nse_bhavcopy_url(target_date)

    last_error: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        logger.info(f"Downloading NSE Bhavcopy from URL: {url} (attempt {attempt}/{MAX_RETRIES})")
        time.sleep(MIN_REQUEST_INTERVAL_SECONDS)

        session = requests.Session()
        try:
            # Hit NSE India home page first to initialize session cookies
            try:
                session.get("https://www.nseindia.com", headers=HEADERS, timeout=10)
            except Exception as e:
                logger.warning(f"Failed to initialize NSE session cookies: {e}")

            response = session.get(url, headers=HEADERS, timeout=15)
            if response.status_code == 404:
                raise FileNotFoundError(f"NSE Bhavcopy not found for {target_date} (likely a market holiday or weekend)")
            response.raise_for_status()

            content_type = response.headers.get("content-type", "")
            if "zip" not in content_type and response.content[:2] != b"PK":
                # NSE's anti-bot layer often responds 200 with an HTML challenge
                # page instead of a real zip. Treat that as a retryable failure.
                raise ValueError(
                    f"NSE response for {target_date} did not look like a zip file "
                    f"(content-type={content_type!r}) - likely blocked by anti-bot protection"
                )

            return response.content
        except FileNotFoundError:
            raise
        except Exception as e:
            last_error = e
            if attempt < MAX_RETRIES:
                backoff = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                logger.warning(
                    f"NSE Bhavcopy download attempt {attempt}/{MAX_RETRIES} failed for {target_date}: {e}. "
                    f"Retrying in {backoff:.1f}s..."
                )
                time.sleep(backoff)
        finally:
            session.close()

    raise RuntimeError(
        f"NSE Bhavcopy download failed for {target_date} after {MAX_RETRIES} attempts: {last_error}"
    ) from last_error

def parse_nse_bhavcopy_csv(csv_content: str, target_date: date) -> List[Dict[str, Any]]:
    """
    Parses NSE bhavcopy CSV data supporting both UDiFF (new) and Legacy column layouts.
    """
    # Sanity check: if it looks like HTML, it is not a valid CSV (NSE redirected/blocked request)
    if csv_content.strip().startswith("<!DOCTYPE") or "<html" in csv_content.lower():
        logger.warning(f"NSE downloaded content for {target_date} is HTML instead of CSV.")
        return []

    records = []
    f = io.StringIO(csv_content)
    reader = csv.DictReader(f)
    
    # Clean headers (strip spaces)
    if not reader.fieldnames:
        raise ValueError("Empty CSV file or invalid headers")
    
    reader.fieldnames = [field.strip() for field in reader.fieldnames if field is not None]
    
    # Detect format version (UDiFF vs Legacy)
    is_udiff = "TckrSymb" in reader.fieldnames
    
    # Set mappings based on format
    if is_udiff:
        col_symbol = "TckrSymb"
        col_series = "SctySrs"
        col_open = "OpnPric"
        col_high = "HghPric"
        col_low = "LwPric"
        col_close = "ClsPric"
        col_volume = "TtlTradgVol"
    else:
        # Legacy format (usually uppercase)
        reader.fieldnames = [field.upper() for field in reader.fieldnames]
        col_symbol = "SYMBOL"
        col_series = "SERIES"
        col_open = "OPEN"
        col_high = "HIGH"
        col_low = "LOW"
        col_close = "CLOSE"
        col_volume = "TOTTRDQTY"
    
    for row_idx, row in enumerate(reader):
        try:
            # Clean row values safely
            cleaned_row = {}
            for k, v in row.items():
                if k is not None:
                    k_clean = k.strip().upper() if not is_udiff else k.strip()
                    cleaned_row[k_clean] = v.strip() if (v is not None and hasattr(v, 'strip')) else ""
            
            series = cleaned_row.get(col_series, "")
            if series != "EQ":
                continue  # Only ingest Cash Equities
            
            symbol = cleaned_row.get(col_symbol, "")
            if not symbol:
                continue
            
            records.append({
                "symbol": symbol,
                "exchange": "NSE",
                "segment": "EQ",
                "market_timestamp": target_date,
                "open": float(cleaned_row[col_open]),
                "high": float(cleaned_row[col_high]),
                "low": float(cleaned_row[col_low]),
                "close": float(cleaned_row[col_close]),
                "volume": int(float(cleaned_row[col_volume])),
            })
        except (KeyError, ValueError) as e:
            logger.debug(f"Skipping row {row_idx} due to parse error: {e}")
            continue
            
    return records

def generate_mock_nse_data(target_date: date) -> List[Dict[str, Any]]:
    """Generates realistic mock NSE bhavcopy records for testing and local dev."""
    logger.info(f"Generating mock NSE data for {target_date}")
    mock_symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "ICICIBANK", "SBI", "BHARTIARTL", "ITC", "HINDUNILVR", "L&T"]
    records = []
    
    # Use deterministic seed based on date and symbol for repeatable test values
    import random
    
    for symbol in mock_symbols:
        seed = int(target_date.strftime("%Y%m%d")) + sum(ord(c) for c in symbol)
        rng = random.Random(seed)
        
        base_price = rng.uniform(100.0, 3000.0)
        pct_change = rng.uniform(-0.03, 0.03)
        open_p = base_price * (1 + rng.uniform(-0.01, 0.01))
        close_p = open_p * (1 + pct_change)
        high_p = max(open_p, close_p) * (1 + rng.uniform(0, 0.02))
        low_p = min(open_p, close_p) * (1 - rng.uniform(0, 0.02))
        vol = rng.randint(10000, 5000000)
        
        records.append({
            "symbol": symbol,
            "exchange": "NSE",
            "segment": "EQ",
            "market_timestamp": target_date,
            "open": round(open_p, 2),
            "high": round(high_p, 2),
            "low": round(low_p, 2),
            "close": round(close_p, 2),
            "volume": vol,
        })
    return records

def get_nse_data(target_date: date, use_mock: bool = False) -> List[Dict[str, Any]]:
    """Main entrypoint to get NSE records. Downloads or mocks them."""
    if use_mock:
        return generate_mock_nse_data(target_date)
        
    try:
        zip_bytes = download_nse_bhavcopy(target_date)
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as z:
            # Locate the csv inside the zip
            csv_filenames = [name for name in z.namelist() if name.endswith(".csv")]
            if not csv_filenames:
                raise ValueError("No CSV file found inside NSE bhavcopy zip")
            
            with z.open(csv_filenames[0]) as f:
                csv_content = f.read().decode("utf-8", errors="ignore")
                
        return parse_nse_bhavcopy_csv(csv_content, target_date)
    except FileNotFoundError as e:
        logger.warning(str(e))
        return []
    except Exception as e:
        logger.error(f"Error fetching/parsing NSE Bhavcopy for {target_date}: {e}")
        # Return empty list so we can gracefully log failure in ingestion_log
        raise e
