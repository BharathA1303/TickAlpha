import logging
import secrets
from datetime import date, timedelta
from typing import List, Optional
from fastapi import APIRouter, Depends, Query, status, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import APIKey, IngestionLog
from app.core.auth import verify_jwt_token, require_admin, hash_secret
from app.ingestion.run_ingestion import is_trading_day

logger = logging.getLogger(__name__)

# Existing router for status
router = APIRouter(prefix="/v1/ingestion-status", tags=["Admin Ingestion Status"])

# Router for administrative key management (admin console login required)
admin_keys_router = APIRouter(prefix="/v1/admin", tags=["Admin Credentials Manager"])

VALID_SEGMENT_SCOPES = {"nse:eq", "bse:eq", "nse:fut", "nse:opt", "mcx:fut", "mcx:opt", "cds:fut", "cds:opt", "admin"}

# Pydantic models for key generation
class KeyGenerateRequest(BaseModel):
    owner: str = Field(..., description="Name of the API client owner (e.g., 'alphasync-website')")
    name: str = Field("", description="Human-readable label for this key")
    scopes: List[str] = Field(
        default=["nse:eq", "bse:eq", "nse:fut", "nse:opt", "mcx:fut"],
        description="List of scopes to grant (e.g., 'nse:eq', 'bse:eq')"
    )
    allowed_symbols: List[str] = Field(
        default_factory=list,
        description="Symbols this key may access, format EXCHANGE:SEGMENT:SYMBOL. Empty = all symbols within its scopes."
    )
    max_replay_speed: int = Field(60, ge=1, le=60, description="Maximum tick replay speed multiplier (1x-60x) this key may request")
    rate_limit_per_min: int = Field(60, ge=1, le=1000, description="Rate limit in requests per minute")

class KeyGenerateResponse(BaseModel):
    client_id: str
    client_secret: str
    owner: str
    name: str
    scopes: List[str]
    allowed_symbols: List[str]
    max_replay_speed: int
    rate_limit_per_min: int

class KeyUpdateRequest(BaseModel):
    name: Optional[str] = None
    scopes: Optional[List[str]] = None
    allowed_symbols: Optional[List[str]] = None
    max_replay_speed: Optional[int] = Field(None, ge=1, le=60)
    rate_limit_per_min: Optional[int] = Field(None, ge=1, le=1000)


async def _get_key_or_404(db: AsyncSession, client_id: str) -> APIKey:
    stmt = select(APIKey).where(APIKey.client_id == client_id)
    result = await db.execute(stmt)
    key = result.scalars().first()
    if not key or key.status == "deleted":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return key


@router.get("")
async def get_ingestion_status(
    limit: int = Query(20, ge=1, le=100, description="Number of log records to return"),
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Returns recent entries from the ingestion log.
    Protected by JWT verification and requires 'admin' scope.
    """
    if "admin" not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to view ingestion logs"
        )

    stmt = select(IngestionLog).order_by(desc(IngestionLog.run_at)).limit(limit)
    result = await db.execute(stmt)
    logs = result.scalars().all()

    return [log.to_dict() for log in logs]


@router.get("/health")
async def get_ingestion_health(
    lookback_days: int = Query(7, ge=1, le=30, description="How many trailing trading days to check for gaps"),
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Reports whether nightly ingestion is up to date, for use by external
    monitoring/alerting (or the admin console) rather than having to grep logs.
    Flags any trading day in the lookback window where both NSE and BSE
    ingestion logged zero rows (a strong signal of a blocked/broken source),
    and reports how many days have passed since the last successful run.
    """
    if "admin" not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to view ingestion health"
        )

    today = date.today()
    problem_dates: List[str] = []
    last_success_date: Optional[date] = None

    for i in range(lookback_days):
        check_date = today - timedelta(days=i)
        if not is_trading_day(check_date):
            continue

        stmt = select(IngestionLog).where(IngestionLog.target_date == check_date)
        result = await db.execute(stmt)
        day_logs = result.scalars().all()

        rows_by_source = {log.source: log.rows_ingested for log in day_logs}
        total_rows = sum(rows_by_source.values())

        if not day_logs:
            problem_dates.append(check_date.isoformat())
        elif total_rows == 0:
            problem_dates.append(check_date.isoformat())
        elif last_success_date is None:
            last_success_date = check_date

    is_healthy = len(problem_dates) == 0
    days_since_success = (today - last_success_date).days if last_success_date else None

    return {
        "status": "healthy" if is_healthy else "degraded",
        "lookback_days": lookback_days,
        "last_successful_ingestion_date": last_success_date.isoformat() if last_success_date else None,
        "days_since_last_success": days_since_success,
        "problem_trading_days": problem_dates,
        "message": (
            "Ingestion is up to date."
            if is_healthy
            else f"{len(problem_dates)} trading day(s) in the lookback window have missing or zero-row ingestion. "
                 f"Check ingestion_log for error_message details (likely NSE/BSE source blocking)."
        )
    }

@admin_keys_router.post("/keys", response_model=KeyGenerateResponse, status_code=status.HTTP_201_CREATED)
async def generate_client_credentials(
    req: KeyGenerateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """
    Generates new Client ID and Client Secret, saving the hashed secret in PostgreSQL.
    Requires an authenticated admin console session.
    """
    client_id = f"client_{secrets.token_hex(12)}"
    client_secret = f"secret_{secrets.token_urlsafe(32)}"
    secret_hash = hash_secret(client_secret)

    # Store credentials in DB
    api_key_obj = APIKey(
        client_id=client_id,
        secret_hash=secret_hash,
        owner=req.owner,
        name=req.name,
        scopes=req.scopes,
        allowed_symbols=req.allowed_symbols,
        max_replay_speed=req.max_replay_speed,
        rate_limit_per_min=req.rate_limit_per_min,
        is_active=True,
        status="active",
    )
    db.add(api_key_obj)

    # Return plaintext details so developer can copy it
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "owner": req.owner,
        "name": req.name,
        "scopes": req.scopes,
        "allowed_symbols": req.allowed_symbols,
        "max_replay_speed": req.max_replay_speed,
        "rate_limit_per_min": req.rate_limit_per_min
    }


@admin_keys_router.get("/keys")
async def list_client_credentials(
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Lists all API keys (excluding soft-deleted ones), most recent first."""
    stmt = select(APIKey).where(APIKey.status != "deleted").order_by(desc(APIKey.created_at))
    result = await db.execute(stmt)
    keys = result.scalars().all()
    return [key.to_dict() for key in keys]


@admin_keys_router.get("/keys/{client_id}")
async def get_client_credentials(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Retrieves a single API key's details."""
    key = await _get_key_or_404(db, client_id)
    return key.to_dict()


@admin_keys_router.patch("/keys/{client_id}")
async def update_client_credentials(
    client_id: str,
    req: KeyUpdateRequest,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Updates a key's name, scopes, allowed symbols, max replay speed, or rate limit."""
    key = await _get_key_or_404(db, client_id)

    if req.name is not None:
        key.name = req.name
    if req.scopes is not None:
        key.scopes = req.scopes
    if req.allowed_symbols is not None:
        key.allowed_symbols = req.allowed_symbols
    if req.max_replay_speed is not None:
        key.max_replay_speed = req.max_replay_speed
    if req.rate_limit_per_min is not None:
        key.rate_limit_per_min = req.rate_limit_per_min

    await db.flush()
    return key.to_dict()


@admin_keys_router.post("/keys/{client_id}/regenerate-secret", response_model=KeyGenerateResponse)
async def regenerate_client_secret(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """
    Issues a brand new client secret for an existing key, invalidating the old one.
    The plaintext secret is returned once and never stored or retrievable again.
    """
    key = await _get_key_or_404(db, client_id)

    new_secret = f"secret_{secrets.token_urlsafe(32)}"
    key.secret_hash = hash_secret(new_secret)
    await db.flush()

    return {
        "client_id": key.client_id,
        "client_secret": new_secret,
        "owner": key.owner,
        "name": key.name,
        "scopes": key.scopes,
        "allowed_symbols": key.allowed_symbols,
        "max_replay_speed": key.max_replay_speed,
        "rate_limit_per_min": key.rate_limit_per_min,
    }


@admin_keys_router.post("/keys/{client_id}/pause")
async def pause_client_key(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Temporarily blocks data/feed access for this key. Key can be resumed later."""
    key = await _get_key_or_404(db, client_id)
    key.status = "paused"
    key.is_active = False
    await db.flush()
    return key.to_dict()


@admin_keys_router.post("/keys/{client_id}/resume")
async def resume_client_key(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Resumes a paused or disabled key, restoring active access."""
    key = await _get_key_or_404(db, client_id)
    key.status = "active"
    key.is_active = True
    await db.flush()
    return key.to_dict()


@admin_keys_router.post("/keys/{client_id}/disable")
async def disable_client_key(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Explicitly disables a key long-term. Distinct from pause, but also reversible via resume."""
    key = await _get_key_or_404(db, client_id)
    key.status = "disabled"
    key.is_active = False
    await db.flush()
    return key.to_dict()


@admin_keys_router.delete("/keys/{client_id}", status_code=status.HTTP_200_OK)
async def delete_client_key(
    client_id: str,
    db: AsyncSession = Depends(get_db),
    _admin: str = Depends(require_admin)
):
    """Soft-deletes a key: hidden from listings and unusable, but retained for audit history."""
    key = await _get_key_or_404(db, client_id)
    key.status = "deleted"
    key.is_active = False
    await db.flush()
    return {"client_id": client_id, "status": "deleted"}
