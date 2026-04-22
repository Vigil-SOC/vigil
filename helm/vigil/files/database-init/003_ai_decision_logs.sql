-- Migration: Add AI Decision Logs Table
-- Purpose: Track AI decisions for human feedback and continuous improvement
-- Date: 2026-01-15

-- Create AI decision logs table
CREATE TABLE IF NOT EXISTS ai_decision_logs (
    id SERIAL PRIMARY KEY,
    decision_id VARCHAR(50) UNIQUE NOT NULL,
    
    -- Decision context
    agent_id VARCHAR(50) NOT NULL,
    workflow_id VARCHAR(50),
    finding_id VARCHAR(50) REFERENCES findings(finding_id) ON DELETE CASCADE,
    case_id VARCHAR(50) REFERENCES cases(case_id) ON DELETE CASCADE,
    
    -- AI's decision
    decision_type VARCHAR(50) NOT NULL,
    confidence_score FLOAT NOT NULL CHECK (confidence_score >= 0 AND confidence_score <= 1),
    reasoning TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    decision_metadata JSONB,
    
    -- Human feedback
    human_reviewer VARCHAR(100),
    human_decision VARCHAR(50),
    feedback_comment TEXT,
    
    -- Grading (0-1 scale)
    accuracy_grade FLOAT CHECK (accuracy_grade IS NULL OR (accuracy_grade >= 0 AND accuracy_grade <= 1)),
    reasoning_grade FLOAT CHECK (reasoning_grade IS NULL OR (reasoning_grade >= 0 AND reasoning_grade <= 1)),
    action_appropriateness FLOAT CHECK (action_appropriateness IS NULL OR (action_appropriateness >= 0 AND action_appropriateness <= 1)),
    
    -- Outcome tracking
    actual_outcome VARCHAR(50),
    time_saved_minutes INTEGER,
    
    -- Timestamps
    timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
    feedback_timestamp TIMESTAMP
);

-- Create indexes for common queries
CREATE INDEX idx_ai_decision_agent_id ON ai_decision_logs(agent_id);
CREATE INDEX idx_ai_decision_finding_id ON ai_decision_logs(finding_id);
CREATE INDEX idx_ai_decision_case_id ON ai_decision_logs(case_id);
CREATE INDEX idx_ai_decision_timestamp ON ai_decision_logs(timestamp);
CREATE INDEX idx_ai_decision_human_decision ON ai_decision_logs(human_decision);
CREATE INDEX idx_ai_decision_actual_outcome ON ai_decision_logs(actual_outcome);

-- Add comment to table
COMMENT ON TABLE ai_decision_logs IS 'Tracks AI agent decisions for human feedback and continuous improvement';

-- Add comments to key columns
COMMENT ON COLUMN ai_decision_logs.decision_id IS 'Unique identifier for the decision';
COMMENT ON COLUMN ai_decision_logs.agent_id IS 'ID of the agent that made the decision (e.g., triage, auto_responder)';
COMMENT ON COLUMN ai_decision_logs.confidence_score IS 'AI confidence in the decision (0-1 scale)';
COMMENT ON COLUMN ai_decision_logs.human_decision IS 'Human feedback: agree, disagree, or partial';
COMMENT ON COLUMN ai_decision_logs.accuracy_grade IS 'Human grading of accuracy (0-1 scale)';
COMMENT ON COLUMN ai_decision_logs.actual_outcome IS 'Actual outcome: true_positive, false_positive, true_negative, false_negative';
COMMENT ON COLUMN ai_decision_logs.time_saved_minutes IS 'Estimated time saved by AI handling this decision';

-- Insert sample data for testing
INSERT INTO ai_decision_logs (
    decision_id, agent_id, decision_type, confidence_score, reasoning, 
    recommended_action, timestamp
) VALUES (
    'dec-sample-001',
    'triage',
    'escalate',
    0.82,
    'High anomaly score (0.89) combined with critical MITRE technique T1486 (Ransomware) detected. Entity context shows lateral movement patterns across 3 hosts.',
    'Escalate to Investigation Agent for deep analysis. Consider immediate containment.',
    NOW() - INTERVAL '2 hours'
);

-- Log migration completion
DO $$
BEGIN
    RAISE NOTICE 'Migration 003_ai_decision_logs.sql completed successfully';
END $$;

