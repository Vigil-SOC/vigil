"""
JIRA Export API - Export cases and remediation to JIRA.

Provides endpoints to export case reports and remediation steps to JIRA.
"""

import logging
from typing import Optional
from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.middleware.auth import get_current_user
from backend.services.auth_service import AuthService
from database.models import User, Case, Finding
from database.connection import get_db, get_db_session
from core.config import get_integration_config
import requests

logger = logging.getLogger(__name__)

router = APIRouter()


# Request/Response Models
class JiraExportRequest(BaseModel):
    """JIRA export request."""
    project_key: str
    include_findings: bool = True
    include_timeline: bool = True


class JiraRemediationExportRequest(BaseModel):
    """JIRA remediation export request."""
    parent_issue_key: str
    assign_to: Optional[str] = None


class JiraExportResponse(BaseModel):
    """JIRA export response."""
    success: bool
    issue_key: Optional[str] = None
    subtasks_created: int = 0
    url: Optional[str] = None
    error: Optional[str] = None


@router.post("/cases/{case_id}/export/jira", response_model=JiraExportResponse)
async def export_case_to_jira(
    case_id: str,
    request: JiraExportRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """
    Export a case to JIRA as a ticket with findings as subtasks.
    
    Args:
        case_id: Case ID to export
        request: Export configuration
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Export result with JIRA issue key
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "cases.read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: cases.read required"
        )
    
    # Get JIRA config
    jira_config = get_integration_config('jira')
    url = jira_config.get('url')
    email = jira_config.get('email')
    token = jira_config.get('api_token')
    
    if not all([url, email, token]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JIRA not configured. Set JIRA_URL, JIRA_EMAIL, JIRA_API_TOKEN in environment."
        )
    
    try:
        # Get case
        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Case {case_id} not found"
            )
        
        # Build description
        description = f"*Case Report: {case.title}*\n\n"
        description += f"*Priority:* {case.priority}\n"
        description += f"*Status:* {case.status}\n"
        description += f"*Created:* {case.created_at.isoformat() if case.created_at else 'N/A'}\n"
        description += f"*Assigned To:* {case.assignee or 'Unassigned'}\n\n"
        
        if case.description:
            description += f"*Description:*\n{case.description}\n\n"
        
        # Get findings
        findings = session.query(Finding).filter(Finding.case_id == case_id).all()
        if findings:
            description += f"*Findings ({len(findings)}):*\n"
            for f in findings[:10]:  # Limit to 10
                description += f"- [{f.severity.upper()}] {f.title}\n"
            if len(findings) > 10:
                description += f"- ... and {len(findings) - 10} more\n"
            description += "\n"
        
        # Get resolution steps
        if case.metadata and case.metadata.get('resolution_steps'):
            description += "*Resolution Steps:*\n"
            for i, step in enumerate(case.metadata['resolution_steps'][:5], 1):
                description += f"{i}. {step.get('description', 'N/A')}\n"
            description += "\n"
        
        # Add link
        description += f"*Full report available in AI SOC (Case ID: {case_id})*\n"
        description += f"*Exported by: {current_user.full_name} ({current_user.email})*\n"
        
        # Map priority
        priority_map = {
            "critical": "Highest",
            "high": "High",
            "medium": "Medium",
            "low": "Low"
        }
        jira_priority = priority_map.get(case.priority.lower(), "Medium")
        
        # Create main issue
        auth = (email, token)
        headers = {"Content-Type": "application/json"}
        
        issue_data = {
            "fields": {
                "project": {"key": request.project_key},
                "summary": f"[Security Case] {case.title}",
                "description": description,
                "issuetype": {"name": "Task"},
                "priority": {"name": jira_priority},
                "labels": ["security", "ai-soc", f"case-{case_id}"]
            }
        }
        
        response = requests.post(
            f"{url}/rest/api/2/issue",
            auth=auth,
            headers=headers,
            json=issue_data,
            timeout=30
        )
        response.raise_for_status()
        result_data = response.json()
        issue_key = result_data.get("key")
        
        # Create subtasks for findings if requested
        subtasks_created = 0
        if request.include_findings and findings:
            for finding in findings[:5]:  # Limit to 5 subtasks
                subtask_data = {
                    "fields": {
                        "project": {"key": request.project_key},
                        "parent": {"key": issue_key},
                        "summary": f"[{finding.severity.upper()}] {finding.title}",
                        "description": finding.description[:500] if finding.description else "See parent issue",
                        "issuetype": {"name": "Sub-task"}
                    }
                }
                
                try:
                    sub_response = requests.post(
                        f"{url}/rest/api/2/issue",
                        auth=auth,
                        headers=headers,
                        json=subtask_data,
                        timeout=30
                    )
                    sub_response.raise_for_status()
                    subtasks_created += 1
                except Exception as e:
                    logger.warning(f"Failed to create subtask for finding {finding.finding_id}: {e}")
                    continue
        
        logger.info(f"Case {case_id} exported to JIRA: {issue_key} by {current_user.username}")
        
        return JiraExportResponse(
            success=True,
            issue_key=issue_key,
            subtasks_created=subtasks_created,
            url=f"{url}/browse/{issue_key}"
        )
    
    except HTTPException:
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"JIRA API error: {e}")
        return JiraExportResponse(
            success=False,
            error=f"JIRA API error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Export error: {e}")
        return JiraExportResponse(
            success=False,
            error=str(e)
        )


@router.post("/cases/{case_id}/remediation/jira", response_model=JiraExportResponse)
async def export_remediation_to_jira(
    case_id: str,
    request: JiraRemediationExportRequest,
    current_user: User = Depends(get_current_user),
    session: Session = Depends(get_db)
):
    """
    Export remediation steps to JIRA as subtasks.
    
    Args:
        case_id: Case ID
        request: Export configuration with parent issue key
        current_user: Current authenticated user
        session: Database session
    
    Returns:
        Export result with created subtask keys
    """
    # Check permission
    if not AuthService.check_permission(current_user.user_id, "cases.read"):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Permission denied: cases.read required"
        )
    
    # Get JIRA config
    jira_config = get_integration_config('jira')
    url = jira_config.get('url')
    email = jira_config.get('email')
    token = jira_config.get('api_token')
    
    if not all([url, email, token]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="JIRA not configured"
        )
    
    try:
        # Get case
        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Case {case_id} not found"
            )
        
        # Get resolution steps
        resolution_steps = case.metadata.get('resolution_steps', []) if case.metadata else []
        if not resolution_steps:
            return JiraExportResponse(
                success=False,
                error=f"No resolution steps found for case {case_id}"
            )
        
        # Get parent issue to determine project
        auth = (email, token)
        headers = {"Content-Type": "application/json"}
        
        parent_response = requests.get(
            f"{url}/rest/api/2/issue/{request.parent_issue_key}",
            auth=auth,
            headers=headers,
            timeout=30
        )
        parent_response.raise_for_status()
        parent_data = parent_response.json()
        project_key = parent_data["fields"]["project"]["key"]
        
        # Create subtasks for each remediation step
        created_subtasks = 0
        for i, step in enumerate(resolution_steps, 1):
            subtask_data = {
                "fields": {
                    "project": {"key": project_key},
                    "parent": {"key": request.parent_issue_key},
                    "summary": f"Remediation Step {i}: {step.get('description', 'N/A')[:80]}",
                    "description": f"*Action:* {step.get('action_taken', 'N/A')}\n\n*Result:* {step.get('result', 'N/A')}",
                    "issuetype": {"name": "Sub-task"}
                }
            }
            
            if request.assign_to:
                # Try to find user by email
                user_response = requests.get(
                    f"{url}/rest/api/2/user/search",
                    auth=auth,
                    headers=headers,
                    params={"username": request.assign_to},
                    timeout=30
                )
                if user_response.ok and user_response.json():
                    subtask_data["fields"]["assignee"] = {"name": user_response.json()[0]["name"]}
            
            try:
                response = requests.post(
                    f"{url}/rest/api/2/issue",
                    auth=auth,
                    headers=headers,
                    json=subtask_data,
                    timeout=30
                )
                response.raise_for_status()
                created_subtasks += 1
            except Exception as e:
                logger.warning(f"Failed to create remediation subtask {i}: {e}")
                continue
        
        logger.info(f"Exported {created_subtasks} remediation steps for case {case_id} by {current_user.username}")
        
        return JiraExportResponse(
            success=True,
            subtasks_created=created_subtasks,
            url=f"{url}/browse/{request.parent_issue_key}"
        )
    
    except HTTPException:
        raise
    except requests.exceptions.RequestException as e:
        logger.error(f"JIRA API error: {e}")
        return JiraExportResponse(
            success=False,
            error=f"JIRA API error: {str(e)}"
        )
    except Exception as e:
        logger.error(f"Export error: {e}")
        return JiraExportResponse(
            success=False,
            error=str(e)
        )

