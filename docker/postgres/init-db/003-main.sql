CREATE EXTENSION pg_cron;

-- Add progress tracking and wallet columns to processing_gens (idempotent)
-- These will be created by SQLAlchemy's db.create_all() for new deployments,
-- but this ensures they exist for any manual database initialization.
DO $$
BEGIN
    -- Add progress_percent if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='progress_percent') THEN
        ALTER TABLE processing_gens ADD COLUMN progress_percent INTEGER NOT NULL DEFAULT 0;
    END IF;
    
    -- Add current_step if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='current_step') THEN
        ALTER TABLE processing_gens ADD COLUMN current_step INTEGER NOT NULL DEFAULT 0;
    END IF;
    
    -- Add total_steps if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='total_steps') THEN
        ALTER TABLE processing_gens ADD COLUMN total_steps INTEGER NOT NULL DEFAULT 0;
    END IF;
    
    -- Add progress_updated_at if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='progress_updated_at') THEN
        ALTER TABLE processing_gens ADD COLUMN progress_updated_at TIMESTAMP;
    END IF;
    
    -- Add wallet_id if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='wallet_id') THEN
        ALTER TABLE processing_gens ADD COLUMN wallet_id VARCHAR(42);
        CREATE INDEX IF NOT EXISTS ix_processing_gens_wallet_id ON processing_gens(wallet_id);
    END IF;
    
    -- Add wallet_id to waiting_prompts if not exists
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='waiting_prompts' AND column_name='wallet_id') THEN
        ALTER TABLE waiting_prompts ADD COLUMN wallet_id VARCHAR(42);
        CREATE INDEX IF NOT EXISTS ix_waiting_prompts_wallet_id ON waiting_prompts(wallet_id);
    END IF;
END $$;