import os
from pathlib import Path
from typing import Optional
from pydantic_settings import BaseSettings, SettingsConfigDict

from urllib.parse import quote_plus

# Base directory
BASE_DIR = Path(__file__).resolve().parent.parent

class Settings(BaseSettings):
    # Database
    POSTGRES_USER: str = "alphasync_user"
    POSTGRES_PASSWORD: str = "alphasync_password"
    POSTGRES_DB: str = "alphasync_data"
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432

    # Redis
    REDIS_HOST: str = "localhost"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0

    # Application
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    LOG_LEVEL: str = "info"

    # Compliance
    DELAY_DAYS: int = 3

    # Security
    JWT_SECRET: str = "5a585e3b348f0e560880a24de2e734f63a16d2f7ead55cc55f46c60ff34aee2c"
    JWT_ALGORITHM: str = "HS256"


    model_config = SettingsConfigDict(
        env_file=str(BASE_DIR / ".env"),
        env_file_encoding="utf-8",
        extra="ignore"
    )

    @property
    def database_url_async(self) -> str:
        """Returns the PostgreSQL connection URL for asyncpg."""
        escaped_password = quote_plus(self.POSTGRES_PASSWORD)
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{escaped_password}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def database_url_sync(self) -> str:
        """Returns the PostgreSQL connection URL for standard sync psycopg2."""
        escaped_password = quote_plus(self.POSTGRES_PASSWORD)
        return f"postgresql://{self.POSTGRES_USER}:{escaped_password}@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"

    @property
    def redis_url(self) -> str:
        """Returns the Redis connection URL."""
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

settings = Settings()
