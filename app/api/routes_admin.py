import logging
import secrets
from typing import List
from fastapi import APIRouter, Depends, Query, status, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select, desc
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import APIKey, IngestionLog
from app.core.auth import verify_jwt_token, hash_secret

logger = logging.getLogger(__name__)

# Existing router for status
router = APIRouter(prefix="/v1/ingestion-status", tags=["Admin Ingestion Status"])

# New router for administrative key management
admin_keys_router = APIRouter(prefix="/v1/admin", tags=["Admin Credentials Manager"])

# Pydantic models for key generation
class KeyGenerateRequest(BaseModel):
    owner: str = Field(..., description="Name of the API client owner (e.g., 'alphasync-website')")
    scopes: List[str] = Field(
        default=["nse:eq", "bse:eq", "nse:fut", "nse:opt", "mcx:fut"],
        description="List of scopes to grant (e.g., 'nse:eq', 'bse:eq')"
    )
    rate_limit_per_min: int = Field(60, ge=1, le=1000, description="Rate limit in requests per minute")

class KeyGenerateResponse(BaseModel):
    client_id: str
    client_secret: str
    owner: str
    scopes: List[str]
    rate_limit_per_min: int

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

@admin_keys_router.post("/keys", response_model=KeyGenerateResponse, status_code=status.HTTP_201_CREATED)
async def generate_client_credentials(
    req: KeyGenerateRequest,
    db: AsyncSession = Depends(get_db),
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Generates new Client ID and Client Secret, saving the hashed secret in PostgreSQL.
    Requires caller to have 'admin' scope.
    """
    if "admin" not in client.scopes:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin privileges required to generate client credentials"
        )
        
    client_id = f"client_{secrets.token_hex(12)}"
    client_secret = f"secret_{secrets.token_urlsafe(32)}"
    secret_hash = hash_secret(client_secret)
    
    # Store credentials in DB
    api_key_obj = APIKey(
        client_id=client_id,
        secret_hash=secret_hash,
        owner=req.owner,
        scopes=req.scopes,
        rate_limit_per_min=req.rate_limit_per_min,
        is_active=True
    )
    db.add(api_key_obj)
    
    # Return plaintext details so developer can copy it
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "owner": req.owner,
        "scopes": req.scopes,
        "rate_limit_per_min": req.rate_limit_per_min
    }
