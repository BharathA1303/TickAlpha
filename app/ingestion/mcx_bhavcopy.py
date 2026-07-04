"""
MCX (Multi Commodity Exchange) bhavcopy ingestion - NOT YET IMPLEMENTED.

Unlike NSE (cash equities and F&O), MCX is a separate exchange with its own
bhavcopy distribution. Its public download page
(https://www.mcxindia.com/market-data/bhavcopy) is JS-driven with no
confirmed stable static CSV/zip URL, so a scraper built by guessing at a URL
would be unreliable in a way that's hard to detect (it could silently return
zero rows indefinitely rather than failing clearly).

Real commodities data is intentionally left unimplemented here rather than
shipped with an unverified guess. Until this is built, commodities
(exchange="MCX") are only available via the synthetic sandbox seeder
(scripts/seed_historical.py), clearly as simulated/dev data - not real
historical MCX prices.

TODO: implement real MCX ingestion once a verified bhavcopy endpoint (or a
paid commercial feed, as already recommended in the README for production
NSE/BSE data) is confirmed. The functions below intentionally mirror the
nse_bhavcopy.py / nse_fo_bhavcopy.py interface (get_<x>_data(target_date,
use_mock)) so wiring this in later is a drop-in change in run_ingestion.py.
"""
import logging
from datetime import date
from typing import List, Dict, Any

logger = logging.getLogger(__name__)


def download_mcx_bhavcopy(target_date: date) -> bytes:
    raise NotImplementedError(
        "Real MCX bhavcopy ingestion is not implemented - no verified stable "
        "download URL. See module docstring in app/ingestion/mcx_bhavcopy.py."
    )


def parse_mcx_bhavcopy_csv(csv_content: str, target_date: date) -> List[Dict[str, Any]]:
    raise NotImplementedError(
        "Real MCX bhavcopy parsing is not implemented - no verified column "
        "layout. See module docstring in app/ingestion/mcx_bhavcopy.py."
    )


def generate_mock_mcx_data(target_date: date) -> List[Dict[str, Any]]:
    """
    Generates realistic mock MCX commodity futures records for testing and
    local dev, matching the shape real MCX ingestion would eventually
    produce. Prefer scripts/seed_historical.py for sandbox seeding today,
    which already synthesizes MCX GOLD/SILVER futures from yfinance
    commodity futures prices - this function exists so mock-mode ingestion
    (run_ingestion.py --mock) has an MCX branch to call symmetrically with
    NSE/BSE, rather than because it's meant to be the primary source of mock
    commodities data.
    """
    logger.info(f"Generating mock MCX data for {target_date}")
    import random
    from datetime import timedelta

    mock_commodities = {"GOLD": 65000.0, "SILVER": 78000.0, "CRUDEOIL": 6500.0}
    records = []

    # Commodity futures typically expire mid-month; approximate with the
    # 20th of next month for mock purposes.
    year, month = target_date.year, target_date.month + 1
    if month > 12:
        month = 1
        year += 1
    expiry = date(year, month, 20)

    for symbol, base_price in mock_commodities.items():
        seed = int(target_date.strftime("%Y%m%d")) + sum(ord(c) for c in symbol)
        rng = random.Random(seed)
        pct_change = rng.uniform(-0.02, 0.02)
        open_p = base_price * (1 + rng.uniform(-0.01, 0.01))
        close_p = open_p * (1 + pct_change)
        high_p = max(open_p, close_p) * (1 + rng.uniform(0, 0.015))
        low_p = min(open_p, close_p) * (1 - rng.uniform(0, 0.015))

        records.append({
            "symbol": symbol, "exchange": "MCX", "segment": "FUT",
            "expiry": expiry, "strike": None, "option_type": None,
            "open_interest": rng.randint(10000, 100000),
            "market_timestamp": target_date,
            "open": round(open_p, 2), "high": round(high_p, 2),
            "low": round(low_p, 2), "close": round(close_p, 2),
            "volume": rng.randint(5000, 80000),
        })

    return records


def get_mcx_data(target_date: date, use_mock: bool = False) -> List[Dict[str, Any]]:
    """Main entrypoint to get MCX records. Only mock mode is currently supported."""
    if use_mock:
        return generate_mock_mcx_data(target_date)

    raise NotImplementedError(
        "Real MCX ingestion is not implemented yet. Use --mock for sandbox/dev "
        "data, or scripts/seed_historical.py for a richer synthetic dataset. "
        "See module docstring in app/ingestion/mcx_bhavcopy.py for what's needed "
        "to add a real downloader."
    )
