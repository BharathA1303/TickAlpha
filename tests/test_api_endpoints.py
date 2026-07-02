import hashlib
import pytest
from datetime import date
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.models import APIKey, PriceData, IngestionLog
from app.core.cache import clear_cache
from app.core.auth import hash_secret, create_access_token

import pytest_asyncio

@pytest_asyncio.fixture
async def seed_data(db_session: AsyncSession):
    """Fixture to seed database with standard test keys and price data."""
    # 1. Create API Keys (client ID + secret hash)
    # Key 1: NSE scope + admin scope
    client_nse = "client_test_nse"
    secret_nse = "secret_test_nse"
    api_key_nse = APIKey(
        client_id=client_nse,
        secret_hash=hash_secret(secret_nse),
        owner="test-nse-client",
        scopes=["nse:eq", "admin"],
        rate_limit_per_min=60,
        is_active=True
    )
    
    # Key 2: BSE scope only
    client_bse = "client_test_bse"
    secret_bse = "secret_test_bse"
    api_key_bse = APIKey(
        client_id=client_bse,
        secret_hash=hash_secret(secret_bse),
        owner="test-bse-client",
        scopes=["bse:eq"],
        rate_limit_per_min=60,
        is_active=True
    )
    
    # Key 3: Inactive key
    client_inactive = "client_test_inactive"
    secret_inactive = "secret_test_inactive"
    api_key_inactive = APIKey(
        client_id=client_inactive,
        secret_hash=hash_secret(secret_inactive),
        owner="test-inactive-client",
        scopes=["nse:eq", "bse:eq"],
        rate_limit_per_min=60,
        is_active=False
    )
    
    # Key 4: Key with rate limit of 1
    client_rate_limited = "client_test_limited"
    secret_rate_limited = "secret_test_limited"
    api_key_limited = APIKey(
        client_id=client_rate_limited,
        secret_hash=hash_secret(secret_rate_limited),
        owner="test-limited-client",
        scopes=["nse:eq"],
        rate_limit_per_min=1,
        is_active=True
    )

    db_session.add(api_key_nse)
    db_session.add(api_key_bse)
    db_session.add(api_key_inactive)
    db_session.add(api_key_limited)

    # 2. Create Price Data
    # Dates are set to 2020-01-01 which is older than 3 days and always eligible
    price_nse = PriceData(
        symbol="RELIANCE",
        exchange="NSE",
        segment="EQ",
        market_timestamp=date(2020, 1, 1),
        open=1500.0,
        high=1520.0,
        low=1490.0,
        close=1510.0,
        volume=500000
    )
    
    price_bse = PriceData(
        symbol="RELIANCE",
        exchange="BSE",
        segment="EQ",
        market_timestamp=date(2020, 1, 1),
        open=1501.0,
        high=1521.0,
        low=1491.0,
        close=1511.0,
        volume=10000
    )
    
    db_session.add(price_nse)
    db_session.add(price_bse)

    # 3. Create Ingestion Log
    log = IngestionLog(
        source="nse_bhavcopy",
        target_date=date(2020, 1, 1),
        status="success",
        rows_ingested=100
    )
    db_session.add(log)

    await db_session.flush()
    
    return {
        "client_nse": client_nse, "secret_nse": secret_nse,
        "client_bse": client_bse, "secret_bse": secret_bse,
        "client_inactive": client_inactive, "secret_inactive": secret_inactive,
        "client_limited": client_rate_limited, "secret_limited": secret_rate_limited
    }

@pytest.mark.asyncio
async def test_health_endpoint(client: AsyncClient):
    """GET /health should be open and require no auth."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "healthy"

@pytest.mark.asyncio
async def test_token_auth_endpoint(client: AsyncClient, seed_data: dict):
    """Verify that posting valid credentials returns a signed JWT access token."""
    # 1. Valid credentials
    response = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    assert response.status_code == 200
    data = response.json()
    assert "access_token" in data
    assert data["token_type"] == "bearer"

    # 2. Invalid credentials (401)
    response_fail = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": "wrong_secret"}
    )
    assert response_fail.status_code == 401
    assert "Invalid Client ID" in response_fail.json()["detail"]

@pytest.mark.asyncio
async def test_authentication_failures(client: AsyncClient, seed_data: dict):
    """Verify various JWT header authorization rejection paths."""
    # 1. Missing header (401)
    res_missing = await client.get("/v1/price/NSE/RELIANCE")
    assert res_missing.status_code == 401
    
    # 2. Invalid JWT format (401)
    res_invalid = await client.get("/v1/price/NSE/RELIANCE", headers={"Authorization": "Bearer invalid_token"})
    assert res_invalid.status_code == 401
    assert "Invalid access token" in res_invalid.json()["detail"]

@pytest.mark.asyncio
async def test_scope_authorization(client: AsyncClient, seed_data: dict):
    """Verify that scopes are checked properly for specific exchanges/segments."""
    # Log in NSE client
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Calls NSE -> Allowed (200)
    res_nse_ok = await client.get("/v1/price/NSE/RELIANCE", headers=headers)
    assert res_nse_ok.status_code == 200
    assert res_nse_ok.json()["symbol"] == "RELIANCE"
    
    # 2. Calls BSE -> Rejected (403 due to missing scope)
    res_bse_fail = await client.get("/v1/price/BSE/RELIANCE", headers=headers)
    assert res_bse_fail.status_code == 403
    assert "scope" in res_bse_fail.json()["detail"].lower()

@pytest.mark.asyncio
async def test_rate_limiting(client: AsyncClient, seed_data: dict):
    """Verify rate limit rules are enforced."""
    # Log in rate limited client
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_limited"], "client_secret": seed_data["secret_limited"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    await clear_cache() # Clear cache to ensure rate limits are reset
    
    # First request: Allowed (200)
    res1 = await client.get("/v1/price/NSE/RELIANCE", headers=headers)
    assert res1.status_code == 200
    
    # Second request in the same minute: Rate Limited (429)
    res2 = await client.get("/v1/price/NSE/RELIANCE", headers=headers)
    assert res2.status_code == 429
    assert "Rate limit exceeded" in res2.json()["detail"]

@pytest.mark.asyncio
async def test_price_range_endpoint(client: AsyncClient, seed_data: dict):
    """Verify price range endpoint and compliance checks."""
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Query within eligible historical window: 2020-01-01 to 2020-01-02 -> Allowed (200)
    response = await client.get(
        "/v1/price/NSE/RELIANCE/range?start=2020-01-01&end=2020-01-02",
        headers=headers
    )
    assert response.status_code == 200
    data = response.json()
    assert len(data) == 1
    assert data[0]["symbol"] == "RELIANCE"
    assert data[0]["market_timestamp"] == "2020-01-01"

    # Query range entirely inside restricted window (e.g. today) -> Rejected (400 due to ValueError from delay gate)
    today_str = date.today().isoformat()
    response_restricted = await client.get(
        f"/v1/price/NSE/RELIANCE/range?start={today_str}&end={today_str}",
        headers=headers
    )
    assert response_restricted.status_code == 400
    assert "restricted" in response_restricted.json()["detail"]

@pytest.mark.asyncio
async def test_symbols_endpoint(client: AsyncClient, seed_data: dict):
    """Verify listing symbols for exchange."""
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    response = await client.get("/v1/symbols?exchange=NSE", headers=headers)
    assert response.status_code == 200
    symbols = response.json()
    assert "RELIANCE" in symbols

@pytest.mark.asyncio
async def test_ingestion_status_endpoint(client: AsyncClient, seed_data: dict):
    """Verify reading ingestion status logs with admin scope."""
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    response = await client.get("/v1/ingestion-status?limit=5", headers=headers)
    assert response.status_code == 200
    logs = response.json()
    assert len(logs) == 1
    assert logs[0]["source"] == "nse_bhavcopy"
    assert logs[0]["status"] == "success"

@pytest.mark.asyncio
async def test_admin_generate_keys(client: AsyncClient, seed_data: dict):
    """Verify that only an authenticated admin console session can manage API keys."""
    login_res = await client.post(
        "/v1/auth/admin-login",
        json={"username": "admin1", "password": "pass001"}
    )
    assert login_res.status_code == 200
    admin_token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {admin_token}"}

    response = await client.post(
        "/v1/admin/keys",
        headers=headers,
        json={
            "owner": "new-test-owner",
            "scopes": ["nse:eq"],
            "allowed_symbols": ["NSE:EQ:RELIANCE"],
            "max_replay_speed": 10,
            "rate_limit_per_min": 100,
        }
    )
    assert response.status_code == 201
    data = response.json()
    assert "client_id" in data
    assert "client_secret" in data
    assert data["owner"] == "new-test-owner"
    assert data["scopes"] == ["nse:eq"]
    assert data["allowed_symbols"] == ["NSE:EQ:RELIANCE"]
    assert data["max_replay_speed"] == 10

    new_client_id = data["client_id"]

    # List keys
    list_res = await client.get("/v1/admin/keys", headers=headers)
    assert list_res.status_code == 200
    assert any(k["client_id"] == new_client_id for k in list_res.json())

    # Pause the key
    pause_res = await client.post(f"/v1/admin/keys/{new_client_id}/pause", headers=headers)
    assert pause_res.status_code == 200
    assert pause_res.json()["status"] == "paused"
    assert pause_res.json()["is_active"] is False

    # Resume the key
    resume_res = await client.post(f"/v1/admin/keys/{new_client_id}/resume", headers=headers)
    assert resume_res.status_code == 200
    assert resume_res.json()["status"] == "active"

    # Disable the key
    disable_res = await client.post(f"/v1/admin/keys/{new_client_id}/disable", headers=headers)
    assert disable_res.status_code == 200
    assert disable_res.json()["status"] == "disabled"

    # Delete (soft) the key
    delete_res = await client.delete(f"/v1/admin/keys/{new_client_id}", headers=headers)
    assert delete_res.status_code == 200
    assert delete_res.json()["status"] == "deleted"

    # Deleted key no longer appears in listings
    list_res_after = await client.get("/v1/admin/keys", headers=headers)
    assert not any(k["client_id"] == new_client_id for k in list_res_after.json())

    # Verify that a regular API-key JWT (even with 'admin' scope) cannot manage keys (403)
    login_res_nse = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token_nse = login_res_nse.json()["access_token"]
    headers_nse = {"Authorization": f"Bearer {token_nse}"}

    response_fail = await client.post(
        "/v1/admin/keys",
        headers=headers_nse,
        json={"owner": "hacky-owner"}
    )
    assert response_fail.status_code == 403

    # Verify wrong admin credentials are rejected
    bad_login = await client.post(
        "/v1/auth/admin-login",
        json={"username": "admin1", "password": "wrong-password"}
    )
    assert bad_login.status_code == 401

@pytest.mark.asyncio
async def test_bulk_subscription_wildcards(client: AsyncClient, seed_data: dict):
    """Verify that wildcard subscriptions resolve multiple EOD symbols."""
    login_res = await client.post(
        "/v1/auth/token",
        json={"client_id": seed_data["client_nse"], "client_secret": seed_data["secret_nse"]}
    )
    token = login_res.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # 1. Create session (valid past date: 2020-01-01)
    sess_res = await client.post(
        "/v1/sessions",
        headers=headers,
        json={"date": "2020-01-01", "replay_speed": 1}
    )
    assert sess_res.status_code == 200
    session_id = sess_res.json()["session_id"]
    
    # 2. Subscribe using 'ALL' wildcard
    sub_res = await client.post(
        f"/v1/sessions/{session_id}/subscribe",
        headers=headers,
        json={"symbols": ["ALL"]}
    )
    assert sub_res.status_code == 200
    data = sub_res.json()
    # It should have subscribed to RELIANCE under NSE (but not BSE because client lacks scope)
    assert "NSE:EQ:RELIANCE" in data["subscriptions"]
    assert "BSE:EQ:RELIANCE" not in data["subscriptions"]

