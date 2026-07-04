-- Migration: add EOD price correction/versioning support to price_data
-- Safe to run multiple times (IF NOT EXISTS / IF EXISTS guards throughout).
-- Run against the alphasync_data database on the server BEFORE deploying
-- app code that depends on the `version`/`superseded_at` columns.
--
-- Why: exchanges occasionally re-issue a corrected bhavcopy for a date that
-- was already ingested. The old schema's unique constraint on
-- (symbol, exchange, segment, expiry, strike, option_type, market_timestamp)
-- forced every correction to silently overwrite the prior row in place,
-- which could change tick-replay history out from under a client mid-session.
-- This migration adds a `version` column to the uniqueness key (so
-- corrections insert a new row instead of overwriting) and a
-- `superseded_at` marker so exactly one version per key is "current".

ALTER TABLE price_data ADD COLUMN IF NOT EXISTS version INTEGER NOT NULL DEFAULT 1;
ALTER TABLE price_data ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ;

-- Drop the old unique constraint that didn't include `version` (if present).
ALTER TABLE price_data DROP CONSTRAINT IF EXISTS uq_price_data_symbol_exchange_segment_date;

-- Recreate it including `version`, so multiple historical versions of the
-- same (symbol, exchange, segment, expiry, strike, option_type, date) can
-- coexist as distinct rows.
ALTER TABLE price_data
    ADD CONSTRAINT uq_price_data_symbol_exchange_segment_date_version
    UNIQUE (symbol, exchange, segment, expiry, strike, option_type, market_timestamp, version);

-- Partial unique index enforcing "at most one current (non-superseded)
-- version per key" at the database level - this is also the index that
-- every normal (non-audit) read query hits.
CREATE UNIQUE INDEX IF NOT EXISTS idx_price_data_current_version
    ON price_data (symbol, exchange, segment, expiry, strike, option_type, market_timestamp)
    WHERE superseded_at IS NULL;

-- Existing rows (all ingested under the old overwrite-in-place model) are
-- all version 1 and all current by definition - the column defaults above
-- already give every pre-existing row version=1, superseded_at=NULL, so no
-- backfill UPDATE is required.
