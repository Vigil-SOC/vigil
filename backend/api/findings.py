"""Findings API endpoints."""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import logging

from services.database_data_service import DatabaseDataService

router = APIRouter()
logger = logging.getLogger(__name__)
# Use DatabaseDataService which automatically uses PostgreSQL if available, falls back to JSON
data_service = DatabaseDataService()


class FindingFilter(BaseModel):
    """Filter parameters for findings."""
    severity: Optional[str] = None
    data_source: Optional[str] = None
    cluster_id: Optional[int] = None
    min_anomaly_score: Optional[float] = None
    limit: Optional[int] = 100


@router.get("/")
async def get_findings(
    severity: Optional[str] = Query(None),
    data_source: Optional[str] = Query(None),
    cluster_id: Optional[int] = Query(None),
    min_anomaly_score: Optional[float] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Text search across finding IDs, descriptions, entity context"),
    offset: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    sort_by: str = Query("timestamp"),
    sort_order: str = Query("desc"),
    force_refresh: bool = Query(False)
):
    """
    Get findings with optional filters, search, and server-side pagination.
    
    Returns:
        Paginated list of findings with total count and has_more flag.
    """
    if force_refresh and data_service.is_s3_configured():
        logger.info("Force refresh triggered - syncing from S3")
        success, message, stats = data_service.sync_from_s3()
        if success:
            logger.info(f"S3 sync completed: {message}")
        else:
            logger.warning(f"S3 sync failed or partial: {message}")
    
    cluster_id_str = str(cluster_id) if cluster_id is not None else None
    
    total = data_service.count_findings(
        severity=severity, data_source=data_source,
        cluster_id=cluster_id_str, min_anomaly_score=min_anomaly_score,
        status=status, search_query=search,
    )
    findings = data_service.get_findings(
        limit=limit, offset=offset,
        severity=severity, data_source=data_source,
        cluster_id=cluster_id_str, min_anomaly_score=min_anomaly_score,
        status=status, search_query=search,
        sort_by=sort_by, sort_order=sort_order,
    )
    
    return {
        "findings": findings,
        "total": total,
        "offset": offset,
        "limit": limit,
        "has_more": (offset + limit) < total,
    }


@router.get("/{finding_id}")
async def get_finding(finding_id: str):
    """
    Get a specific finding by ID.
    
    Args:
        finding_id: The finding ID
    
    Returns:
        Finding details
    """
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    return finding


@router.get("/stats/summary")
async def get_findings_summary():
    """
    Get summary statistics for findings.
    
    Returns:
        Summary statistics
    """
    findings = data_service.get_findings()
    
    # Calculate statistics
    severity_counts = {}
    data_source_counts = {}
    total_count = len(findings)
    
    for finding in findings:
        severity = finding.get('severity', 'unknown')
        severity_counts[severity] = severity_counts.get(severity, 0) + 1
        
        data_source = finding.get('data_source', 'unknown')
        data_source_counts[data_source] = data_source_counts.get(data_source, 0) + 1
    
    return {
        "total": total_count,
        "by_severity": severity_counts,
        "by_data_source": data_source_counts
    }


@router.post("/export")
async def export_findings(output_format: str = "json"):
    """
    Export findings to a file.
    
    Args:
        output_format: Export format (json or jsonl)
    
    Returns:
        Export result
    """
    from pathlib import Path
    from datetime import datetime
    
    output_dir = Path.home() / ".deeptempo" / "exports"
    output_dir.mkdir(parents=True, exist_ok=True)
    
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = output_dir / f"findings_export_{timestamp}.{output_format}"
    
    success = data_service.export_findings(output_path, format=output_format)
    
    if success:
        return {"success": True, "file_path": str(output_path)}
    else:
        raise HTTPException(status_code=500, detail="Export failed")


class FindingUpdate(BaseModel):
    """Schema for updating a finding."""
    mitre_predictions: Optional[Dict[str, float]] = None
    predicted_techniques: Optional[List[Dict[str, Any]]] = None
    severity: Optional[str] = None
    status: Optional[str] = None
    anomaly_score: Optional[float] = None
    cluster_id: Optional[str] = None
    evidence_links: Optional[List[str]] = None
    # Canonical entity fields
    src_ip: Optional[str] = None
    dst_ip: Optional[str] = None
    hostname: Optional[str] = None
    username: Optional[str] = None
    process_name: Optional[str] = None
    file_hash: Optional[str] = None
    alert_category: Optional[str] = None
    raw_fields: Optional[Dict[str, Any]] = None
    # Legacy blob (still accepted for backward compat)
    entity_context: Optional[Dict[str, Any]] = None


class BulkEnrichmentRequest(BaseModel):
    """Schema for bulk enrichment request."""
    finding_ids: List[str]
    enrichment_data: Dict[str, FindingUpdate]


@router.patch("/{finding_id}")
async def update_finding(finding_id: str, update: FindingUpdate):
    """
    Update/enrich an existing finding.
    
    This endpoint allows you to add or update information on a finding,
    including MITRE ATT&CK technique mappings, severity, and other metadata.
    
    Args:
        finding_id: The finding ID to update
        update: Fields to update
    
    Returns:
        Updated finding
    
    Example:
        PATCH /api/findings/f-20260114-abc123
        {
            "mitre_predictions": {"T1071.001": 0.85, "T1048.003": 0.72},
            "predicted_techniques": [
                {"technique_id": "T1071.001", "confidence": 0.85},
                {"technique_id": "T1048.003", "confidence": 0.72}
            ],
            "severity": "high"
        }
    """
    # Get existing finding
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    
    # Prepare updates (exclude None values)
    updates = {}
    for key, value in update.model_dump(exclude_none=True).items():
        updates[key] = value
    
    if not updates:
        raise HTTPException(status_code=400, detail="No updates provided")
    
    # Update the finding
    success = data_service.update_finding(finding_id, **updates)
    
    if not success:
        raise HTTPException(status_code=500, detail="Failed to update finding")
    
    # Return updated finding
    updated_finding = data_service.get_finding(finding_id)
    logger.info(f"Updated finding {finding_id} with {len(updates)} fields")
    
    return {
        "success": True,
        "finding": updated_finding,
        "updated_fields": list(updates.keys())
    }


@router.post("/bulk-enrich")
async def bulk_enrich_findings(request: BulkEnrichmentRequest):
    """
    Bulk enrich multiple findings with MITRE ATT&CK and other data.
    
    This endpoint allows you to enrich multiple findings at once,
    useful for batch processing or adding threat intelligence data.
    
    Args:
        request: Bulk enrichment request with finding IDs and enrichment data
    
    Returns:
        Summary of enrichment results
    
    Example:
        POST /api/findings/bulk-enrich
        {
            "finding_ids": ["f-001", "f-002"],
            "enrichment_data": {
                "f-001": {
                    "mitre_predictions": {"T1071.001": 0.85},
                    "severity": "high"
                },
                "f-002": {
                    "mitre_predictions": {"T1059.001": 0.92},
                    "severity": "critical"
                }
            }
        }
    """
    results = {
        "total": len(request.finding_ids),
        "updated": 0,
        "failed": 0,
        "not_found": 0,
        "errors": []
    }
    
    for finding_id in request.finding_ids:
        try:
            # Check if finding exists
            finding = data_service.get_finding(finding_id)
            if not finding:
                results["not_found"] += 1
                results["errors"].append(f"{finding_id}: Not found")
                continue
            
            # Get enrichment data for this finding
            enrichment = request.enrichment_data.get(finding_id)
            if not enrichment:
                continue
            
            # Prepare updates
            updates = enrichment.model_dump(exclude_none=True)
            if not updates:
                continue
            
            # Update the finding
            success = data_service.update_finding(finding_id, **updates)
            
            if success:
                results["updated"] += 1
                logger.info(f"Enriched finding {finding_id}")
            else:
                results["failed"] += 1
                results["errors"].append(f"{finding_id}: Update failed")
                
        except Exception as e:
            results["failed"] += 1
            results["errors"].append(f"{finding_id}: {str(e)}")
            logger.error(f"Error enriching finding {finding_id}: {e}")
    
    return {
        "success": results["updated"] > 0,
        "message": f"Updated {results['updated']} of {results['total']} findings",
        "results": results
    }


@router.post("/{finding_id}/enrich")
async def get_or_generate_enrichment(finding_id: str, force_regenerate: bool = Query(False)):
    """
    Get or generate AI enrichment for a finding.
    
    This endpoint checks if AI enrichment already exists for the finding.
    If it exists, returns the cached enrichment immediately.
    If not, generates new enrichment using Claude AI, caches it, and returns it.
    
    Args:
        finding_id: The finding ID to enrich
        force_regenerate: Force regeneration even if enrichment exists
    
    Returns:
        AI enrichment data with threat analysis, impact, recommendations, etc.
    
    Example Response:
        {
            "finding_id": "f-20260114-001",
            "cached": false,
            "enrichment": {
                "threat_summary": "...",
                "potential_impact": "...",
                "recommended_actions": [...],
                "related_techniques": [...],
                "indicators": {...},
                "confidence_score": 0.85
            }
        }
    """
    import asyncio
    from datetime import datetime
    
    # Get the finding
    finding = data_service.get_finding(finding_id)
    if not finding:
        raise HTTPException(status_code=404, detail="Finding not found")
    
    # Check if enrichment already exists
    existing_enrichment = finding.get('ai_enrichment')
    if existing_enrichment and not force_regenerate:
        logger.info(f"Returning cached enrichment for {finding_id}")
        return {
            "finding_id": finding_id,
            "cached": True,
            "enrichment": existing_enrichment
        }
    
    # Generate new enrichment using Claude
    try:
        from services.claude_service import ClaudeService
        
        claude_service = ClaudeService(use_backend_tools=True, use_mcp_tools=False)
        
        # Check if API key is configured
        if not claude_service.has_api_key():
            raise HTTPException(
                status_code=503, 
                detail="Claude API not configured. AI enrichment requires Claude API key."
            )
        
        # Extract finding details (use `or` to guard against keys present with None values)
        severity = finding.get('severity') or 'unknown'
        data_source = finding.get('data_source') or 'unknown'
        timestamp = finding.get('timestamp') or ''
        description = finding.get('description') or ''
        entity_context = finding.get('entity_context') or {}
        mitre_predictions = finding.get('mitre_predictions') or {}
        predicted_techniques = finding.get('predicted_techniques') or []
        anomaly_score = float(finding.get('anomaly_score') or 0)
        
        # Build entity context string from canonical fields (with entity_context fallback)
        entity_str = ""
        src_ip   = finding.get('src_ip') or (entity_context.get('src_ip') or entity_context.get('src_ips', [None])[0] if entity_context else None)
        dst_ip   = finding.get('dst_ip') or (entity_context.get('dst_ip') or entity_context.get('dst_ips', [None])[0] if entity_context else None)
        hostname = finding.get('hostname') or (entity_context.get('hostname') if entity_context else None)
        username = finding.get('username') or (entity_context.get('username') or entity_context.get('user') if entity_context else None)
        if src_ip:
            entity_str += f"Source IP: {src_ip}\n"
        if dst_ip:
            entity_str += f"Destination IP: {dst_ip}\n"
        if hostname:
            entity_str += f"Hostname: {hostname}\n"
        if username:
            entity_str += f"User: {username}\n"
        if finding.get('process_name'):
            entity_str += f"Process: {finding['process_name']}\n"
        if finding.get('file_hash'):
            entity_str += f"File Hash: {finding['file_hash']}\n"
        if finding.get('alert_category'):
            entity_str += f"Alert Category: {finding['alert_category']}\n"
        
        # Build MITRE techniques string
        techniques_str = ""
        if predicted_techniques:
            techniques_list = [
                f"{t.get('technique_id', 'Unknown')} (confidence: {float(t.get('confidence') or 0):.2f})"
                for t in predicted_techniques[:5]
            ]
            techniques_str = "\n".join(techniques_list)
        elif mitre_predictions:
            techniques_list = [
                f"{tech_id} (confidence: {float(conf or 0):.2f})"
                for tech_id, conf in sorted(mitre_predictions.items(), key=lambda x: float(x[1] or 0), reverse=True)[:5]
            ]
            techniques_str = "\n".join(techniques_list)
        
        # Construct comprehensive analysis prompt
        prompt = f"""You are a cybersecurity analyst reviewing a security finding. Provide a comprehensive, structured analysis.

FINDING DETAILS:
=================
Finding ID: {finding_id}
Severity: {severity}
Data Source: {data_source}
Timestamp: {timestamp}
Anomaly Score: {anomaly_score:.2f}

Description:
{description if description else 'No description available'}

{f'''Entity Context:
{entity_str}''' if entity_str else ''}

{f'''MITRE ATT&CK Techniques:
{techniques_str}''' if techniques_str else 'No MITRE techniques predicted'}

ANALYSIS REQUIREMENTS:
=======================
Please provide a detailed analysis in the following JSON structure:

{{
    "threat_summary": "A clear, concise summary (2-3 sentences) of what this finding represents and why it matters",
    "threat_type": "Classification of threat (e.g., 'Data Exfiltration', 'Lateral Movement', 'Command & Control', 'Malware', etc.)",
    "potential_impact": "Detailed explanation of potential impact on the organization (3-4 sentences)",
    "risk_level": "Overall risk assessment: 'Critical', 'High', 'Medium', or 'Low'",
    "recommended_actions": [
        "Immediate action item 1",
        "Immediate action item 2",
        "Additional investigation step 1",
        "Additional investigation step 2"
    ],
    "investigation_questions": [
        "Key question to investigate 1?",
        "Key question to investigate 2?",
        "Key question to investigate 3?"
    ],
    "indicators": {{
        "malicious_ips": ["list any suspicious IPs mentioned"],
        "suspicious_domains": ["list any suspicious domains"],
        "suspicious_users": ["list any suspicious user accounts"],
        "suspicious_processes": ["list any suspicious processes or commands"]
    }},
    "related_techniques": [
        {{
            "technique_id": "T####.###",
            "technique_name": "Technique name",
            "relevance": "Why this technique is relevant"
        }}
    ],
    "timeline_context": "Brief explanation of what likely happened and in what order",
    "business_context": "How this finding relates to typical business operations and what makes it anomalous",
    "confidence_score": 0.85,
    "analysis_notes": "Any additional context, caveats, or recommendations for the analyst"
}}

Respond ONLY with valid JSON. Be specific and actionable. Focus on helping a SOC analyst make quick, informed decisions."""

        # Generate enrichment
        logger.info(f"Generating AI enrichment for {finding_id}")
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: claude_service.chat(
                message=prompt,
                model="claude-sonnet-4-20250514",
                max_tokens=4096
            )
        )
        
        # Parse JSON response
        import json
        import re
        
        if not response:
            raise ValueError("Claude API returned an empty response")
        
        # Try to extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'```json\s*(\{.*?\})\s*```', response, re.DOTALL)
        if json_match:
            json_str = json_match.group(1)
        else:
            # Try to find raw JSON
            json_match = re.search(r'\{.*\}', response, re.DOTALL)
            if json_match:
                json_str = json_match.group(0)
            else:
                json_str = response
        
        try:
            enrichment = json.loads(json_str)
        except json.JSONDecodeError:
            # If JSON parsing fails, create structured enrichment from text response
            enrichment = {
                "threat_summary": "AI analysis completed - see full analysis below",
                "threat_type": "Security Finding",
                "potential_impact": "Requires manual review",
                "risk_level": severity.title() if severity else "Medium",
                "recommended_actions": ["Review the detailed analysis", "Investigate related entities"],
                "investigation_questions": ["What is the root cause?", "Are there related events?"],
                "indicators": {},
                "related_techniques": [],
                "timeline_context": "Analysis in progress",
                "business_context": "Requires additional context",
                "confidence_score": 0.7,
                "analysis_notes": response[:1000],  # Store first 1000 chars as notes
                "raw_response": response  # Store full response
            }
        
        # Add metadata
        enrichment['generated_at'] = datetime.utcnow().isoformat() + 'Z'
        enrichment['model'] = 'claude-sonnet-4-20250514'
        
        # Save enrichment to database
        success = data_service.update_finding(finding_id, ai_enrichment=enrichment)
        
        if not success:
            logger.error(f"Failed to save enrichment for {finding_id}")
            # Still return the enrichment even if saving fails
        else:
            logger.info(f"Successfully generated and cached enrichment for {finding_id}")
        
        return {
            "finding_id": finding_id,
            "cached": False,
            "enrichment": enrichment
        }
    
    except Exception as e:
        logger.error(f"Error generating enrichment for {finding_id}: {e}")
        raise HTTPException(
            status_code=500, 
            detail=f"Failed to generate enrichment: {str(e)}"
        )


@router.delete("/all")
async def clear_all_findings():
    """Delete all findings from the database."""
    try:
        from database.connection import get_session
        from database.models import Finding

        with get_session() as session:
            count = session.query(Finding).count()
            session.query(Finding).delete()
            session.commit()

        logger.info(f"Cleared {count} findings")
        return {"success": True, "deleted": count, "message": f"Deleted {count} findings"}
    except Exception as e:
        logger.error(f"Error clearing findings: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to clear findings: {str(e)}")

