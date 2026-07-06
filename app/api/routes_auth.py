import hmac
import uuid
import logging
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.config import settings
from app.db.session import get_db
from app.db.models import APIKey
from app.core.auth import hash_secret, create_access_token, verify_jwt_token, ADMIN_SUBJECT
from app.core.cache import set_cached_response, redis_client

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["Authentication"])

class TokenRequest(BaseModel):
    client_id: str = Field(..., description="API client identifier")
    client_secret: str = Field(..., description="API client secret key")

class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int = 3600

class AdminLoginRequest(BaseModel):
    username: str = Field(..., description="Admin console username")
    password: str = Field(..., description="Admin console password")

@router.post("/admin-login", response_model=TokenResponse)
async def admin_login(req: AdminLoginRequest):
    """
    Authenticates the admin console user and issues an admin-scoped JWT.
    """
    username_ok = hmac.compare_digest(req.username, settings.ADMIN_USERNAME)
    password_ok = hmac.compare_digest(req.password, settings.ADMIN_PASSWORD)
    if not (username_ok and password_ok):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid admin username or password",
        )

    access_token = create_access_token(
        client_id=ADMIN_SUBJECT,
        scopes=["admin"],
        expires_in_seconds=3600
    )

    return TokenResponse(
        access_token=access_token,
        expires_in=3600
    )

class FeedTokenRequest(BaseModel):
    session_id: str = Field(..., description="The ID of the active replay session")

class FeedTokenResponse(BaseModel):
    feed_token: str
    expires_in: int = 60

@router.post("/token", response_model=TokenResponse)
async def generate_token(req: TokenRequest, db: AsyncSession = Depends(get_db)):
    """
    Exchanges Client ID and Client Secret for a JWT access token.
    The client credentials must be pre-registered in the database.
    """
    logger.info(f"Authenticating client: {req.client_id}")
    
    # 1. Query client credentials in database
    stmt = select(APIKey).where(APIKey.client_id == req.client_id)
    result = await db.execute(stmt)
    db_client = result.scalars().first()
    
    # 2. Verify hashed secret
    if not db_client or db_client.secret_hash != hash_secret(req.client_secret):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Client ID or Client Secret"
        )
        
    if not db_client.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Client is deactivated"
        )
        
    # 3. Generate JWT access token
    access_token = create_access_token(
        client_id=db_client.client_id,
        scopes=db_client.scopes,
        expires_in_seconds=3600
    )
    
    return TokenResponse(
        access_token=access_token,
        expires_in=3600
    )

@router.post("/feed-token", response_model=FeedTokenResponse)
async def generate_feed_token(
    req: FeedTokenRequest,
    client: APIKey = Depends(verify_jwt_token)
):
    """
    Generates a short-lived (60 seconds) single-use token to authenticate a WebSocket connection.
    Access is protected by the standard JWT Bearer token.
    """
    feed_token = f"feed_token_{uuid.uuid4().hex}"
    
    # Store feed token mapping in Redis: feed_token -> client_id:session_id
    # Valid for 60 seconds. Uses set_cached_response helper which falls back to in-memory cache if Redis is down
    token_value = f"{client.client_id}:{req.session_id}"
    await set_cached_response(f"feed_token:{feed_token}", token_value, ttl=60)
    
    logger.info(f"Generated WebSocket feed token for client {client.client_id}, session {req.session_id}")
    return FeedTokenResponse(feed_token=feed_token, expires_in=60)
