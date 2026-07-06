import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional
from fastapi import Depends, HTTPException, status, Header, Security
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import jwt

from app.config import settings
from app.db.session import get_db
from app.db.models import APIKey
from app.core.cache import check_and_increment_rate_limit

logger = logging.getLogger(__name__)

# HTTP Bearer token extractor
security_scheme = HTTPBearer(auto_error=False)

def hash_secret(secret: str) -> str:
    """Hashes a client secret using SHA-256."""
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()

def create_access_token(client_id: str, scopes: List[str], expires_in_seconds: int = 3600) -> str:
    """Generates a signed JWT access token for a client."""
    now = datetime.now(timezone.utc)
    expire = now + timedelta(seconds=expires_in_seconds)
    payload = {
        "sub": client_id,
        "scopes": scopes,
        "iat": now,
        "exp": expire
    }
    token = jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)
    return token

def decode_access_token(token: str) -> Dict[str, Any]:
    """
    Decodes and validates a JWT access token.
    Raises jwt.PyJWTError if token is invalid or expired.
    """
    return jwt.decode(
        token,
        settings.JWT_SECRET,
        algorithms=[settings.JWT_ALGORITHM]
    )

async def verify_jwt_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
    db: AsyncSession = Depends(get_db)
) -> APIKey:
    """
    FastAPI dependency that:
    1. Extracts and decodes the JWT access token from the Authorization header.
    2. Retrieves the client (APIKey model) from the database.
    3. Validates that the client is active.
    4. Applies rate limiting.
    """
    if not credentials or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid access token (use Bearer <token>)",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = credentials.credentials
    try:
        payload = decode_access_token(token)
        client_id = payload.get("sub")
        if not client_id:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid token payload: missing sub claim",
            )
    except jwt.ExpiredSignatureError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Access token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.PyJWTError as e:
        logger.warning(f"JWT decode error: {e}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid access token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Lookup client in the database
    stmt = select(APIKey).where(APIKey.client_id == client_id)
    result = await db.execute(stmt)
    db_client = result.scalars().first()

    if not db_client:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client not found or registered",
        )

    if not db_client.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client account is deactivated",
        )

    # Rate Limiting check
    current_minute = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")
    rate_limit_key = f"rate_limit:{db_client.id}:{current_minute}"
    
    is_allowed = await check_and_increment_rate_limit(
        key=rate_limit_key,
        limit=db_client.rate_limit_per_min,
        ttl=60
    )
    
    if not is_allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Max {db_client.rate_limit_per_min} requests/min allowed.",
        )

    return db_client


ADMIN_SUBJECT = "admin"

async def require_admin(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security_scheme),
) -> str:
    """
    Admin gate intentionally disabled (open by request) - always passes,
    regardless of whether a token is present or what it contains.
    """
    return ADMIN_SUBJECT


class VerifyScopes:
    """
    Dependency factory to check if client token contains necessary scopes.
    Usage:
        @router.get("/nse/data", dependencies=[Depends(VerifyScopes("nse:eq"))])
    """
    def __init__(self, required_scope: str):
        self.required_scope = required_scope

    async def __call__(self, client: APIKey = Depends(verify_jwt_token)) -> APIKey:
        if self.required_scope not in client.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing required scope: {self.required_scope}",
            )
        return client
