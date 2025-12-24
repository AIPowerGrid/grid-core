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
    
    -- Add media_type to waiting_prompts for image vs video tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='waiting_prompts' AND column_name='media_type') THEN
        ALTER TABLE waiting_prompts ADD COLUMN media_type VARCHAR(10) NOT NULL DEFAULT 'image';
        CREATE INDEX IF NOT EXISTS ix_waiting_prompts_media_type ON waiting_prompts(media_type);
    END IF;
    
    -- Add media_type to processing_gens for job-level tracking
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='media_type') THEN
        ALTER TABLE processing_gens ADD COLUMN media_type VARCHAR(10) NOT NULL DEFAULT 'image';
        CREATE INDEX IF NOT EXISTS ix_processing_gens_media_type ON processing_gens(media_type);
    END IF;
    
    -- Add tags column for categorization
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='tags') THEN
        ALTER TABLE processing_gens ADD COLUMN tags JSONB;
    END IF;
    
    -- Add r2_download_url for direct file download links
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='r2_download_url') THEN
        ALTER TABLE processing_gens ADD COLUMN r2_download_url TEXT;
    END IF;
    
    -- Add file_size for tracking generated file sizes
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='processing_gens' AND column_name='file_size') THEN
        ALTER TABLE processing_gens ADD COLUMN file_size BIGINT;
        CREATE INDEX IF NOT EXISTS ix_processing_gens_file_size ON processing_gens(file_size);
    END IF;
    
    -- Add tags to waiting_prompts for user-provided categorization
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name='waiting_prompts' AND column_name='tags') THEN
        ALTER TABLE waiting_prompts ADD COLUMN tags JSONB;
    END IF;
END $$;