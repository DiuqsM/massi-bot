-- Massi-Bot Full Database Schema
-- Run in: Supabase Dashboard → SQL Editor
-- Run this ONCE to create all tables from scratch.
-- Safe to re-run: uses IF NOT EXISTS throughout.

-- ═══════════════════════════════════════════
-- 1. MODELS (agency's creator accounts)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS models (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    telegram_id         BIGINT UNIQUE,
    fanvue_model_id     TEXT,
    stage_name          TEXT,
    profile_json        JSONB DEFAULT '{}',
    onboarding_complete BOOLEAN DEFAULT FALSE,
    notes               TEXT,
    created_at          TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_models_telegram_id ON models(telegram_id);
CREATE INDEX IF NOT EXISTS idx_models_fanvue_model_id ON models(fanvue_model_id);

-- ═══════════════════════════════════════════
-- 2. SUBSCRIBERS (fans on Fanvue / OnlyFans)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS subscribers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    platform            TEXT NOT NULL,           -- 'fanvue' or 'onlyfans'
    platform_user_id    TEXT NOT NULL,
    model_id            TEXT NOT NULL,
    username            TEXT DEFAULT '',
    display_name        TEXT DEFAULT '',
    state               TEXT DEFAULT 'new',      -- engine SubState enum value
    whale_score         INTEGER DEFAULT 0,
    total_spent         NUMERIC(10,2) DEFAULT 0,
    persona_id          TEXT DEFAULT '',
    current_script_id   TEXT,
    current_tier        INTEGER DEFAULT 0,
    loop_count          INTEGER DEFAULT 0,
    callback_references JSONB DEFAULT '[]',
    recent_messages     JSONB DEFAULT '[]',
    spending_history    JSONB DEFAULT '{}',
    qualifying_data     JSONB DEFAULT '{}',
    last_message_at     TIMESTAMPTZ,
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_subscriber UNIQUE (platform, platform_user_id, model_id)
);

CREATE INDEX IF NOT EXISTS idx_subscribers_model_state
    ON subscribers(model_id, platform, state);
CREATE INDEX IF NOT EXISTS idx_subscribers_whale
    ON subscribers(model_id, platform, whale_score DESC);
CREATE INDEX IF NOT EXISTS idx_subscribers_last_message
    ON subscribers(model_id, platform, last_message_at DESC);

-- ═══════════════════════════════════════════
-- 3. TRANSACTIONS (purchases, tips, subs)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS transactions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    subscriber_id   TEXT NOT NULL,               -- UUID from subscribers.id
    model_id        TEXT NOT NULL,
    type            TEXT NOT NULL,               -- 'ppv', 'tip', 'subscription', 'custom'
    amount          NUMERIC(10,2) NOT NULL,      -- in dollars
    platform        TEXT DEFAULT 'fanvue',
    content_ref     TEXT,                        -- bundle_id if PPV
    created_at      TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_transactions_model
    ON transactions(model_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_transactions_subscriber
    ON transactions(subscriber_id, created_at DESC);

-- ═══════════════════════════════════════════
-- 4. CONTENT CATALOG (content bundles)
-- ═══════════════════════════════════════════
CREATE TABLE IF NOT EXISTS content_catalog (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    model_id            TEXT NOT NULL,
    session_number      INTEGER NOT NULL,        -- 1-12
    tier                INTEGER NOT NULL,        -- 1-6
    bundle_id           TEXT NOT NULL,           -- unique bundle identifier
    fanvue_media_uuid   TEXT,                    -- Fanvue Vault media UUID (set after upload)
    of_media_id         TEXT,                    -- OnlyFans Vault media ID (set after upload)
    b2_key              TEXT,                    -- Backblaze B2 object key
    media_type          TEXT DEFAULT 'mixed',    -- 'image', 'video', 'mixed'
    price_cents         INTEGER NOT NULL,        -- price in cents (e.g. 2738 = $27.38)
    source              TEXT DEFAULT 'live' CHECK (source IN ('live', 'ai_generated')),
    created_at          TIMESTAMPTZ DEFAULT NOW(),

    CONSTRAINT uq_content UNIQUE (model_id, session_number, tier)
);

CREATE INDEX IF NOT EXISTS idx_content_catalog_model_tier
    ON content_catalog(model_id, tier, session_number);
CREATE INDEX IF NOT EXISTS idx_content_catalog_bundle_id
    ON content_catalog(bundle_id);
CREATE INDEX IF NOT EXISTS idx_content_catalog_of_media_id
    ON content_catalog(of_media_id)
    WHERE of_media_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_content_catalog_source
    ON content_catalog(source);

-- ═══════════════════════════════════════════
-- VERIFY
-- ═══════════════════════════════════════════
SELECT table_name
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name IN ('models', 'subscribers', 'transactions', 'content_catalog')
ORDER BY table_name;
