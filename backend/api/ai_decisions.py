"""
AI Decisions API

Endpoints for logging AI decisions and collecting human feedback.
Enables continuous improvement of AI agents through human oversight.
"""

import logging
from typing import Optional, List
from datetime import datetime
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from database.service import DatabaseService

logger = logging.getLogger(__name__)

router = APIRouter()


# ========== Request/Response Models ==========

class CreateAIDecisionRequest(BaseModel):
    """Request model for creating an AI decision log."""
    decision_id: str = Field(..., description="Unique decision identifier")
    agent_id: str = Field(..., description="ID of the agent making the decision")
    decision_type: str = Field(..., description="Type of decision (e.g., 'triage', 'escalate', 'isolate')")
    confidence_score: float = Field(..., ge=0, le=1, description="AI's confidence (0-1)")
    reasoning: str = Field(..., description="AI's reasoning for the decision")
    recommended_action: str = Field(..., description="Recommended action")
    finding_id: Optional[str] = Field(None, description="Associated finding ID")
    case_id: Optional[str] = Field(None, description="Associated case ID")
    workflow_id: Optional[str] = Field(None, description="Workflow ID")
    decision_metadata: Optional[dict] = Field(None, description="Additional metadata")


class SubmitFeedbackRequest(BaseModel):
    """Request model for submitting feedback on an AI decision."""
    human_reviewer: str = Field(..., description="Name/ID of reviewer")
    human_decision: str = Field(..., description="Human's decision: 'agree', 'disagree', or 'partial'")
    feedback_comment: Optional[str] = Field(None, description="Optional feedback comment")
    accuracy_grade: Optional[float] = Field(None, ge=0, le=1, description="Accuracy grade (0-1)")
    reasoning_grade: Optional[float] = Field(None, ge=0, le=1, description="Reasoning quality grade (0-1)")
    action_appropriateness: Optional[float] = Field(None, ge=0, le=1, description="Action appropriateness (0-1)")
    actual_outcome: Optional[str] = Field(None, description="Actual outcome: 'true_positive', 'false_positive', etc.")
    time_saved_minutes: Optional[int] = Field(None, description="Estimated time saved by AI")


class AIDecisionResponse(BaseModel):
    """Response model for AI decision."""
    id: int
    decision_id: str
    agent_id: str
    decision_type: str
    confidence_score: float
    reasoning: str
    recommended_action: str
    finding_id: Optional[str]
    case_id: Optional[str]
    workflow_id: Optional[str]
    decision_metadata: Optional[dict]
    human_reviewer: Optional[str]
    human_decision: Optional[str]
    feedback_comment: Optional[str]
    accuracy_grade: Optional[float]
    reasoning_grade: Optional[float]
    action_appropriateness: Optional[float]
    actual_outcome: Optional[str]
    time_saved_minutes: Optional[int]
    timestamp: str
    feedback_timestamp: Optional[str]


class AIDecisionStatsResponse(BaseModel):
    """Response model for AI decision statistics."""
    total_decisions: int
    total_with_feedback: int
    feedback_rate: float
    agreement_rate: float
    avg_accuracy_grade: float
    outcomes: dict
    total_time_saved_minutes: int
    total_time_saved_hours: float
    period_days: int


# ========== API Endpoints ==========

@router.post("/decisions", response_model=AIDecisionResponse, status_code=201)
async def create_ai_decision(request: CreateAIDecisionRequest):
    """
    Log an AI decision for tracking and feedback.
    
    This endpoint should be called by AI agents whenever they make a decision
    that could benefit from human review and feedback.
    """
    try:
        db_service = DatabaseService()
        
        decision = db_service.create_ai_decision(
            decision_id=request.decision_id,
            agent_id=request.agent_id,
            decision_type=request.decision_type,
            confidence_score=request.confidence_score,
            reasoning=request.reasoning,
            recommended_action=request.recommended_action,
            finding_id=request.finding_id,
            case_id=request.case_id,
            workflow_id=request.workflow_id,
            decision_metadata=request.decision_metadata
        )
        
        if not decision:
            raise HTTPException(status_code=500, detail="Failed to create AI decision log")
        
        return AIDecisionResponse(**decision.to_dict())
        
    except Exception as e:
        logger.error(f"Error creating AI decision: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/decisions/{decision_id}/feedback", response_model=AIDecisionResponse)
async def submit_feedback(decision_id: str, request: SubmitFeedbackRequest):
    """
    Submit human feedback on an AI decision.
    
    This allows SOC analysts to grade AI decisions and provide feedback
    for continuous improvement.
    """
    try:
        db_service = DatabaseService()
        
        decision = db_service.submit_ai_decision_feedback(
            decision_id=decision_id,
            human_reviewer=request.human_reviewer,
            human_decision=request.human_decision,
            feedback_comment=request.feedback_comment,
            accuracy_grade=request.accuracy_grade,
            reasoning_grade=request.reasoning_grade,
            action_appropriateness=request.action_appropriateness,
            actual_outcome=request.actual_outcome,
            time_saved_minutes=request.time_saved_minutes
        )
        
        if not decision:
            raise HTTPException(status_code=404, detail=f"AI decision not found: {decision_id}")
        
        return AIDecisionResponse(**decision.to_dict())
        
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error submitting feedback: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/decisions/stats", response_model=AIDecisionStatsResponse)
async def get_ai_decision_stats(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    days: int = Query(30, ge=1, le=365, description="Number of days to analyze")
):
    """
    Get statistics on AI decisions and feedback.

    Provides metrics on AI accuracy, agreement rates, and time saved.
    """
    try:
        db_service = DatabaseService()
        stats = db_service.get_ai_decision_stats(agent_id=agent_id, days=days)

        return AIDecisionStatsResponse(**stats)

    except Exception as e:
        logger.error(f"Error getting AI decision stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/decisions/pending-feedback")
async def get_pending_feedback_decisions(
    limit: int = Query(50, ge=1, le=100)
):
    """
    Get decisions that are awaiting human feedback.

    Returns decisions ordered by confidence (lowest first) since
    low-confidence decisions benefit most from feedback.
    """
    try:
        db_service = DatabaseService()

        decisions = db_service.list_ai_decisions(
            has_feedback=False,
            limit=limit
        )

        # Sort by confidence score (lowest first)
        decisions_sorted = sorted(decisions, key=lambda d: d.confidence_score)

        return [AIDecisionResponse(**d.to_dict()) for d in decisions_sorted]

    except Exception as e:
        logger.error(f"Error getting pending feedback decisions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/decisions", response_model=List[AIDecisionResponse])
async def list_ai_decisions(
    agent_id: Optional[str] = Query(None, description="Filter by agent ID"),
    finding_id: Optional[str] = Query(None, description="Filter by finding ID"),
    case_id: Optional[str] = Query(None, description="Filter by case ID"),
    workflow_id: Optional[str] = Query(None, description="Filter by workflow ID"),
    has_feedback: Optional[bool] = Query(None, description="Filter by feedback status"),
    limit: int = Query(100, ge=1, le=500, description="Maximum results"),
    offset: int = Query(0, ge=0, description="Offset for pagination")
):
    """
    List AI decisions with optional filters.

    Use this to review decisions that need feedback or analyze past decisions.
    """
    try:
        db_service = DatabaseService()

        decisions = db_service.list_ai_decisions(
            agent_id=agent_id,
            finding_id=finding_id,
            case_id=case_id,
            has_feedback=has_feedback,
            limit=limit,
            offset=offset
        )

        if workflow_id:
            decisions = [d for d in decisions if d.workflow_id == workflow_id]

        return [AIDecisionResponse(**d.to_dict()) for d in decisions]

    except Exception as e:
        logger.error(f"Error listing AI decisions: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/decisions/{decision_id}", response_model=AIDecisionResponse)
async def get_ai_decision(decision_id: str):
    """Get a specific AI decision by ID."""
    try:
        db_service = DatabaseService()
        decision = db_service.get_ai_decision(decision_id)

        if not decision:
            raise HTTPException(status_code=404, detail=f"AI decision not found: {decision_id}")

        return AIDecisionResponse(**decision.to_dict())

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting AI decision: {e}")
        raise HTTPException(status_code=500, detail=str(e))

