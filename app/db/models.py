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

    __table_args__ = (
        UniqueConstraint(
            "symbol", "exchange", "segment", "expiry", "strike", "option_type", "market_timestamp",
            name="uq_price_data_symbol_exchange_segment_date"
        ),
        Index("idx_price_data_symbol_date", "symbol", "market_timestamp"),
        Index("idx_price_data_lookup", "exchange", "segment", "symbol", "market_timestamp"),
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
    scopes = Column(ARRAY(String), nullable=False, server_default="{}")  # e.g., ['nse:eq', 'nse:fo', 'mcx:com']
    rate_limit_per_min = Column(Integer, nullable=False, default=60)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    def to_dict(self):
        return {
            "id": self.id,
            "client_id": self.client_id,
            "owner": self.owner,
            "scopes": self.scopes,
            "rate_limit_per_min": self.rate_limit_per_min,
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
