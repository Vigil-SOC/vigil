-- Vigil SOC Database Initialization
-- This script is automatically run when the PostgreSQL container first starts

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Create indexes for better performance (SQLAlchemy will create tables)
-- These will be created if they don't already exist

-- Set timezone to UTC
SET timezone = 'UTC';

-- Log initialization
DO $$
BEGIN
    RAISE NOTICE 'Vigil SOC database initialized successfully';
END $$;

