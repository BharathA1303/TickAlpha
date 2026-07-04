import datetime
from sqlalchemy import Column, Integer, String, Date, Numeric, BigInteger, DateTime, Boolean, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import declarative_base
from sqlalchemy.sql import func

Base = declarative_base()

class PriceData(Base):
    """
    Table to store daily EOD OHLCV data for equities, F&O derivatives, and commodities.
    Compliance: All read queries to this table MUST pass through the delay gate.

    Versioning: exchanges occasionally re-issue a corrected bhavcopy for a date
    that was already ingested (or an admin re-runs ingestion for a bad day).
    Rather than overwriting OHLCV in place - which would silently change the
    numbers behind a tick replay that a client may have already started or
    cached - each correction is stored as a NEW row with an incremented
    `version`, and the previous row is marked `superseded_at` instead of being
    deleted. Exactly one row per (symbol, exchange, segment, expiry, strike,
    option_type, market_timestamp) has `superseded_at IS NULL` at any time -
    that is the "current" version read by default. Old versions remain
    queryable for audit via `?version=N`.
    """
    __tablename__ = "price_data"

    id = Column(Integer, primary_key=True, index=True)
    symbol = Column(String(50), nullable=False)
    exchange = Column(String(10), nullable=False)  # "NSE", "BSE", "MCX"
    segment = Column(String(10), nullable=False, default="EQ")  # "EQ", "FUT", "OPT"

    # Contract specs with non-null defaults to ensure PostgreSQL UniqueConstraint triggers on duplicates
    expiry = Column(Date, nullable=False, server_default='1970-01-01', default=datetime.date(1970, 1, 1))
    strike = Column(Numeric(15, 4), nullable=False, server_default='0.0', default=0.0)
    option_type = Column(String(10), nullable=False, server_default='XX', default='XX')  # "CE", "PE", "XX"
    open_interest = Column(BigInteger, nullable=False, server_default='0', default=0)

    market_timestamp = Column(Date, nullable=False)  # The actual trading date
    open = Column(Numeric(15, 4), nullable=False)
    high = Column(Numeric(15, 4), nullable=False)
    low = Column(Numeric(15, 4), nullable=False)
    close = Column(Numeric(15, 4), nullable=False)
    volume = Column(BigInteger, nullable=False)
    ingested_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    # Versioning fields (see class docstring)
    version = Column(Integer, nullable=False, server_default='1', default=1)
    superseded_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "symbol", "exchange", "segment", "expiry", "strike", "option_type", "market_timestamp", "version",
            name="uq_price_data_symbol_exchange_segment_date_version"
        ),
        Index("idx_price_data_symbol_date", "symbol", "market_timestamp"),
        Index("idx_price_data_lookup", "exchange", "segment", "symbol", "market_timestamp"),
        # Partial index (and de-facto "at most one current version per key"
        # guard) covering only the live row for each key - this is the index
        # every normal read hits, since normal reads filter on
        # superseded_at IS NULL. Dialect-specific `_where` kwargs are needed
        # per-backend (e.g. SQLite in tests): a bare `Index(..., unique=True)`
        # would silently drop the WHERE clause on dialects that don't get an
        # explicit kwarg, turning this into a plain (non-partial) unique
        # index that would then reject every correction's new version.
        Index(
            "idx_price_data_current_version",
            "symbol", "exchange", "segment", "expiry", "strike", "option_type", "market_timestamp",
            unique=True,
            postgresql_where=(superseded_at.is_(None)),
            sqlite_where=(superseded_at.is_(None)),
        ),
    )

    def to_dict(self):
        # Convert dummy values back to None/null for client serialization
        exp_val = self.expiry.isoformat() if self.expiry and self.expiry != datetime.date(1970, 1, 1) else None
        strike_val = float(self.strike) if self.strike is not None and float(self.strike) != 0.0 else None
        opt_val = self.option_type if self.option_type != "XX" else None
        oi_val = self.open_interest if self.open_interest != 0 else None

        return {
            "id": self.id,
            "symbol": self.symbol,
            "exchange": self.exchange,
            "segment": self.segment,
            "expiry": exp_val,
            "strike": strike_val,
            "option_type": opt_val,
            "open_interest": oi_val,
            "market_timestamp": self.market_timestamp.isoformat() if self.market_timestamp else None,
            "open": float(self.open) if self.open is not None else None,
            "high": float(self.high) if self.high is not None else None,
            "low": float(self.low) if self.low is not None else None,
            "close": float(self.close) if self.close is not None else None,
            "volume": self.volume,
            "ingested_at": self.ingested_at.isoformat() if self.ingested_at else None,
            "version": self.version,
            "is_current": self.superseded_at is None,
            "superseded_at": self.superseded_at.isoformat() if self.superseded_at else None,
        }


class APIKey(Base):
    """
    Table to store client credentials (hashed secrets) for JWT generation.
    """
    __tablename__ = "api_keys"

    id = Column(Integer, primary_key=True, index=True)
    client_id = Column(String(64), unique=True, nullable=False, index=True)  # Public API key ID
    secret_hash = Column(String(64), nullable=False)  # SHA-256 hash of client secret
    owner = Column(String(100), nullable=False)
    name = Column(String(100), nullable=False, server_default="", default="")
    scopes = Column(ARRAY(String), nullable=False, server_default="{}")  # e.g., ['nse:eq', 'nse:fo', 'mcx:com']
    # Symbols this key may access, format EXCHANGE:SEGMENT:SYMBOL. Empty list = all symbols allowed within its scopes.
    allowed_symbols = Column(ARRAY(String), nullable=False, server_default="{}")
    max_replay_speed = Column(Integer, nullable=False, server_default="60", default=60)
    rate_limit_per_min = Column(Integer, nullable=False, default=60)
    is_active = Column(Boolean, nullable=False, default=True)
    # One of: "active", "paused", "disabled", "deleted"
    status = Column(String(20), nullable=False, server_default="active", default="active")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "owner": self.owner,
            "name": self.name,
            "scopes": self.scopes,
            "allowed_symbols": self.allowed_symbols,
            "max_replay_speed": self.max_replay_speed,
            "rate_limit_per_min": self.rate_limit_per_min,
            "status": self.status,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class IngestionLog(Base):
    """
    Table to audit ingestion runs (NSE/BSE/MCX downloads/parsing).
    """
    __tablename__ = "ingestion_log"

    id = Column(Integer, primary_key=True, index=True)
    source = Column(String(50), nullable=False)  # e.g., "nse_bhavcopy", "bse_bhavcopy", "nse_fo", "mcx_eod"
    target_date = Column(Date, nullable=False)
    status = Column(String(20), nullable=False)  # "success" or "failed"
    rows_ingested = Column(Integer, nullable=False, default=0)
    error_message = Column(String, nullable=True)
    run_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "source": self.source,
            "target_date": self.target_date.isoformat() if self.target_date else None,
            "status": self.status,
            "rows_ingested": self.rows_ingested,
            "error_message": self.error_message,
            "run_at": self.run_at.isoformat() if self.run_at else None,
        }
