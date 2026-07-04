import logging
from contextlib import asynccontextmanager
from datetime import date
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sqlalchemy import select

from app.config import settings
from app.db.models import Base, PriceData
from app.db.session import async_engine, AsyncSessionLocal
from app.core.cache import init_redis, close_redis
from app.api import routes_price, routes_symbols, routes_admin, routes_auth, routes_sessions, routes_feed
from app.simulator.simulator_manager import simulator_manager

# Scheduler import
from datetime import timedelta
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from app.ingestion.run_ingestion import ingest_date, is_trading_day

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("alphasync-data-layer")

# Background Scheduler for nightly EOD updates
scheduler = AsyncIOScheduler()

# How many trailing trading days the nightly job re-checks for gaps
# (e.g. missed runs during downtime, or a source that was unreachable earlier).
GAP_CHECK_LOOKBACK_DAYS = 7

async def scheduled_nightly_ingestion():
    """Runs ingestion for today's data, then self-heals any gaps in the last
    GAP_CHECK_LOOKBACK_DAYS trading days (e.g. missed runs, prior source outages)."""
    today_date = date.today()
    logger.info(f"Triggering scheduled nightly ingestion for {today_date}")
    results = await ingest_date(today_date, use_mock=False)
    logger.info(f"Nightly ingestion results: {results}")

    async with AsyncSessionLocal() as db:
        for i in range(1, GAP_CHECK_LOOKBACK_DAYS + 1):
            check_date = today_date - timedelta(days=i)
            if not is_trading_day(check_date):
                continue
            existing = await db.execute(
                select(PriceData.id).where(PriceData.market_timestamp == check_date).limit(1)
            )
            if existing.first() is None:
                logger.warning(f"Gap detected: no data for {check_date}. Backfilling now.")
                gap_results = await ingest_date(check_date, use_mock=False)
                logger.info(f"Gap-fill results for {check_date}: {gap_results}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- Startup ---
    logger.info("Initializing alphasync-data-layer...")
    
    # 1. Initialize Database Tables
    logger.info("Creating database tables if they do not exist...")
    async with async_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    logger.info("Database tables initialized successfully.")
    
    # 2. Initialize Redis Cache / Rate Limiter
    await init_redis()

    # 3. Start Tick Simulation Engine
    # This background loop is a process-wide singleton: if multiple API
    # replicas each ran it, every replica would independently advance the
    # same session clocks and broadcast duplicate ticks. When scaling out
    # (multiple uvicorn/gunicorn workers or replicas), set
    # ENABLE_SIMULATOR_LOOP=false on all but one dedicated process (see
    # docker-compose.yml's `scheduler` service, which sets it True while the
    # `api` service sets it False).
    if settings.ENABLE_SIMULATOR_LOOP:
        logger.info("Starting tick simulation engine background runner...")
        await simulator_manager.start()
    else:
        logger.info("ENABLE_SIMULATOR_LOOP=false — skipping simulator clock loop on this process (handled elsewhere).")

    # 4. Initialize & Start APScheduler for Nightly Ingestion
    # Same singleton concern as above: only one process should own the cron
    # job, otherwise nightly ingestion would run once per replica.
    if settings.ENABLE_SCHEDULER:
        logger.info("Starting background scheduler...")
        # Schedule nightly ingestion run at 19:00 (7:00 PM) everyday
        scheduler.add_job(
            scheduled_nightly_ingestion,
            trigger="cron",
            hour=19,
            minute=0,
            id="nightly_ingestion",
            replace_existing=True
        )
        scheduler.start()
        logger.info("Scheduler started successfully.")
    else:
        logger.info("ENABLE_SCHEDULER=false — skipping nightly ingestion scheduler on this process.")

    yield

    # --- Shutdown ---
    logger.info("Shutting down alphasync-data-layer...")

    # 1. Shutdown scheduler
    if settings.ENABLE_SCHEDULER:
        scheduler.shutdown()
        logger.info("Scheduler shut down.")

    # 2. Stop Tick Simulation Engine
    if settings.ENABLE_SIMULATOR_LOOP:
        await simulator_manager.stop()
        logger.info("Tick simulation engine stopped.")

    # 3. Close Redis Connection
    await close_redis()

# Create FastAPI app
app = FastAPI(
    title="AlphaSync Dedicated Delayed-Data Layer",
    description=(
        "Compliance-enforced data layer service serving delayed exchange data "
        "and animating it into simulated tick-by-tick real-time updates."
    ),
    version="1.0.0",
    lifespan=lifespan
)

# Enable CORS for the developer portal frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Adjust this in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Register routers
app.include_router(routes_auth.router)
app.include_router(routes_sessions.router)
app.include_router(routes_feed.router)
app.include_router(routes_price.router)
app.include_router(routes_symbols.router)
app.include_router(routes_admin.router)
app.include_router(routes_admin.admin_keys_router)

from fastapi.staticfiles import StaticFiles

@app.get("/health", tags=["System"])
async def health_check():
    """Liveness check, no authentication required."""
    return {
        "status": "healthy",
        "service": "alphasync-data-layer",
        "compliance_rules": f"{settings.DELAY_DAYS}_day_delay_enforced"
    }

# Serve developer portal dashboard statically
app.mount("/", StaticFiles(directory="frontend", html=True), name="frontend")
