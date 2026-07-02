-- Migration: add admin-console fields to api_keys
-- Safe to run multiple times (IF NOT EXISTS guards).
-- Run against the alphasync_data database on the server.

ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS name VARCHAR(100) NOT NULL DEFAULT '';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS allowed_symbols VARCHAR[] NOT NULL DEFAULT '{}';
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS max_replay_speed INTEGER NOT NULL DEFAULT 60;
ALTER TABLE api_keys ADD COLUMN IF NOT EXISTS status VARCHAR(20) NOT NULL DEFAULT 'active';

-- Backfill status for any pre-existing rows based on their current is_active flag
UPDATE api_keys SET status = 'disabled' WHERE is_active = FALSE AND status = 'active';
