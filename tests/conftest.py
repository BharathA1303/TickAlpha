import asyncio
import pytest
import pytest_asyncio
from typing import AsyncGenerator
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from httpx import AsyncClient, ASGITransport

from app.config import settings
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.core.cache import clear_cache

from sqlalchemy.pool import NullPool

# Create async engine for testing using NullPool to avoid event loop mismatch errors
test_engine = create_async_engine(
    settings.database_url_async,
    echo=False,
    poolclass=NullPool
)

TestSessionLocal = async_sessionmaker(
    bind=test_engine,
    class_=AsyncSession,
    expire_on_commit=False
)

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for the test session."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_test_db():
    """Initializes the database schema before running tests, and drops it afterwards."""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await test_engine.dispose()

@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """Yields a database session nested inside a transaction, which rolls back at the end."""
    async with TestSessionLocal() as session:
        # Start a transaction block
        await session.begin()
        try:
            yield session
        finally:
            # Always roll back after each test to keep DB clean
            await session.rollback()

@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """Yields an HTTP client connected to the app, with DB dependency overridden."""
    # Override get_db dependency to use the test transaction database session
    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    
    # Initialize cache / clear mock cache
    await clear_cache()
    
    # Setup AsyncClient using ASGITransport
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
        
    app.dependency_overrides.clear()
