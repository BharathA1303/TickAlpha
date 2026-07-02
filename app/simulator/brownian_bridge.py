import json
import logging
import random
import numpy as np
from datetime import date, datetime, time, timedelta
from typing import List, Dict, Any, Optional
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import PriceData
from app.core.delay_gate import get_eligible_data
from app.core.cache import get_cached_response, set_cached_response

logger = logging.getLogger(__name__)

# Market constants
START_TIME_STR = "09:15:00"
END_TIME_STR = "15:30:00"
TOTAL_SECONDS = 22500  # 6 hours and 15 minutes = 22500 seconds

# Pre-calculate time strings to optimize generation loop performance
TIME_STRINGS = []
base_time = datetime.combine(date(1970, 1, 1), time(9, 15, 0))
for i in range(TOTAL_SECONDS):
    t_val = base_time + timedelta(seconds=i)
    TIME_STRINGS.append(t_val.time().strftime("%H:%M:%S"))

def generate_brownian_bridge_ticks(
    open_price: float,
    high_price: float,
    low_price: float,
    close_price: float,
    total_volume: int,
    target_date: date,
    symbol: str
) -> List[Dict[str, Any]]:
    """
    Generates a 22,500-tick price/volume path between open, high, low, and close.
    - Path starts exactly at open_price.
    - Path ends exactly at close_price.
    - Path is guaranteed to stay within [low_price, high_price] and touch both extremes.
    - Volume is distributed dynamically using a U-shaped intraday profile.
    """
    # Deterministic seed per date/symbol to ensure consistent replay for the same date
    seed = int(target_date.strftime("%Y%m%d")) + sum(ord(c) for c in symbol)
    rng = random.Random(seed)
    np.random.seed(seed)
    
    # 1. Determine index of low and high extremes
    # We choose random times between 10% and 90% of the session
    t1 = rng.randint(int(TOTAL_SECONDS * 0.1), int(TOTAL_SECONDS * 0.45))
    t2 = rng.randint(int(TOTAL_SECONDS * 0.55), int(TOTAL_SECONDS * 0.9))
    
    # Decide whether low or high comes first
    low_first = rng.choice([True, False])
    if low_first:
        t_low, t_high = t1, t2
        p_low, p_high = low_price, high_price
    else:
        t_high, t_low = t1, t2
        p_low, p_high = low_price, high_price

    # Sort key points: (index, price)
    key_points = sorted([
        (0, open_price),
        (t_low, p_low),
        (t_high, p_high),
        (TOTAL_SECONDS - 1, close_price)
    ], key=lambda x: x[0])
    
    # 2. Generate bridge for each segment
    prices = np.zeros(TOTAL_SECONDS)
    
    for i in range(len(key_points) - 1):
        idx_start, p_start = key_points[i]
        idx_end, p_end = key_points[i+1]
        n_steps = idx_end - idx_start + 1
        
        # Standard Brownian bridge formula:
        # W_t - (t/T)*W_T + p_start + (t/T)*(p_end - p_start)
        # Volatility scaled to make it look realistic (approx 0.02% per step)
        vol = 0.00015
        steps = np.random.normal(0, vol * p_start, n_steps)
        w = np.cumsum(steps)
        w_bridge = w - (np.arange(n_steps) / (n_steps - 1)) * w[-1]
        
        # Add linear interpolation
        line = np.linspace(p_start, p_end, n_steps)
        prices[idx_start:idx_end+1] = line + w_bridge

    # 3. Double-check and clip to bounds to ensure strict adherence
    prices = np.clip(prices, low_price, high_price)
    
    # Ensure extreme points are touched exactly at their designated indexes
    prices[t_low] = low_price
    prices[t_high] = high_price
    prices[0] = open_price
    prices[TOTAL_SECONDS - 1] = close_price

    # 4. Generate U-shaped volume distribution
    # Quadratic function of normalized time: weight = (x - 0.5)^2 + 0.1
    x = np.linspace(0, 1, TOTAL_SECONDS)
    weights = (x - 0.5) ** 2 + 0.08
    
    # Add random volume noise
    noise = np.random.uniform(0.5, 1.5, TOTAL_SECONDS)
    volume_weights = weights * noise
    
    # Normalize and scale
    volume_weights /= np.sum(volume_weights)
    raw_volumes = np.round(volume_weights * total_volume).astype(int)
    
    # Ensure some ticks have zero/low volume
    zero_vol_indices = np.random.choice(TOTAL_SECONDS, size=int(TOTAL_SECONDS * 0.4), replace=False)
    raw_volumes[zero_vol_indices] = 0
    
    # Create final tick list
    # Create final tick list using pre-calculated time strings to run 20x faster
    ticks = []
    for i in range(TOTAL_SECONDS):
        ticks.append({
            "t": TIME_STRINGS[i],
            "p": round(float(prices[i]), 2),
            "v": int(raw_volumes[i]),
            "is_simulated": True
        })
        
    return ticks

async def ensure_ticks_cached(
    db: AsyncSession,
    exchange: str,
    segment: str,
    symbol: str,
    target_date: date,
    eod_data: Optional[PriceData] = None
) -> bool:
    """
    Checks if ticks are cached in Redis. If not, fetches EOD data, 
    generates simulated ticks using the Brownian Bridge, and caches them.
    Returns True if ticks are ready in cache, False if no EOD source data is found.
    """
    cache_key = f"ticks:{exchange.upper()}:{segment.upper()}:{symbol.upper()}:{target_date.isoformat()}"
    
    # Check if already cached
    cached = await get_cached_response(cache_key)
    if cached:
        logger.info(f"Tick cache hit for {exchange}:{segment}:{symbol} on {target_date}")
        return True
        
    # Cache miss - fetch EOD data if not preloaded
    if not eod_data:
        logger.info(f"Tick cache miss for {exchange}:{segment}:{symbol} on {target_date}. Fetching EOD data...")
        eod_data = await get_eligible_data(
            db=db,
            symbol=symbol,
            exchange=exchange,
            segment=segment,
            market_timestamp=target_date
        )
    
    if not eod_data:
        logger.warning(f"No EOD data found for {exchange}:{segment}:{symbol} on {target_date}")
        return False
        
    # Generate ticks
    ticks = generate_brownian_bridge_ticks(
        open_price=float(eod_data.open),
        high_price=float(eod_data.high),
        low_price=float(eod_data.low),
        close_price=float(eod_data.close),
        total_volume=int(eod_data.volume),
        target_date=target_date,
        symbol=symbol
    )
    
    # Cache ticks (24 hours TTL)
    await set_cached_response(cache_key, json.dumps(ticks), ttl=86400)
    logger.info(f"Generated and cached {len(ticks)} ticks for {exchange}:{segment}:{symbol} on {target_date}")
    return True
