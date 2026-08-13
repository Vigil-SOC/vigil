"""Graph API endpoints for visualizing entity relationships and attack paths."""

from typing import List, Optional, Dict, Any
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
import logging

from core.storage.database_data_service import DatabaseDataService
from core.routing import Auth, RouterMeta

from core.findings.graph_builder_service import GraphBuilderService

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/graph",
    tags=["graph"],
    auth=Auth.REQUIRED,
)
logger = logging.getLogger(__name__)


class GraphNode(BaseModel):
    """Graph node model."""
    id: str
    label: str
    type: str  # ip, hostname, user, domain, port, cluster
    severity: Optional[str] = None
    findingCount: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None


class GraphLink(BaseModel):
    """Graph link model."""
    source: str
    target: str
    value: Optional[int] = None
    label: Optional[str] = None
    techniques: Optional[List[str]] = None


class GraphData(BaseModel):
    """Graph data model."""
    nodes: List[GraphNode]
    links: List[GraphLink]
    metadata: Optional[Dict[str, Any]] = None


@router.get("/entities", response_model=GraphData)
async def get_entity_graph(
    finding_ids: Optional[str] = Query(None, description="Comma-separated finding IDs"),
    case_id: Optional[str] = Query(None, description="Case ID"),
    cluster_id: Optional[str] = Query(None, description="Cluster ID"),
    limit: int = Query(default=100, ge=1, le=1000)
):
    """
    Get entity relationship graph.
    
    Builds a graph of entities (IPs, hosts, users, domains) and their relationships
    based on findings.
    
    Args:
        finding_ids: Comma-separated list of finding IDs
        case_id: Case ID to get entities from
        cluster_id: Cluster ID to get entities from
        limit: Maximum number of findings to process
        
    Returns:
        Graph data with nodes and links
    """
    data_service = DatabaseDataService()
    findings = []
    
    # Get findings based on filters
    if finding_ids:
        ids = [fid.strip() for fid in finding_ids.split(',')]
        for fid in ids:
            finding = data_service.get_finding(fid)
            if finding:
                findings.append(finding)
    elif case_id:
        findings = data_service.get_findings_by_case(case_id)
    elif cluster_id:
        all_findings = data_service.get_findings(limit=limit * 2)
        findings = [f for f in all_findings if f.get('cluster_id') == cluster_id][:limit]
    else:
        # Get recent findings
        findings = data_service.get_findings(limit=limit)
    
    if not findings:
        return GraphData(nodes=[], links=[], metadata={"message": "No findings found"})
    
    # Build graph from findings
    graph_builder = GraphBuilderService()
    graph_data = graph_builder.build_entity_graph(findings)
    
    return GraphData(
        nodes=[GraphNode(**node) for node in graph_data['nodes']],
        links=[GraphLink(**link) for link in graph_data['links']],
        metadata=graph_data.get('metadata', {})
    )


@router.get("/attack-path/{case_id}", response_model=GraphData)
async def get_attack_path(case_id: str):
    """
    Get attack path visualization for a case.
    
    Shows the progression of an attack across systems, highlighting
    lateral movement and compromise chains.
    
    Args:
        case_id: Case identifier
        
    Returns:
        Graph data showing attack progression
    """
    data_service = DatabaseDataService()
    case = data_service.get_case(case_id)
    
    if not case:
        raise HTTPException(status_code=404, detail="Case not found")
    
    # Get findings for the case
    findings = data_service.get_findings_by_case(case_id)
    
    if not findings:
        return GraphData(nodes=[], links=[], metadata={"message": "No findings in case"})
    
    # Build attack path graph
    graph_builder = GraphBuilderService()
    graph_data = graph_builder.build_attack_path(findings, case)
    
    return GraphData(
        nodes=[GraphNode(**node) for node in graph_data['nodes']],
        links=[GraphLink(**link) for link in graph_data['links']],
        metadata=graph_data.get('metadata', {})
    )


@router.get("/cluster/{cluster_id}", response_model=GraphData)
async def get_cluster_graph(cluster_id: str):
    """
    Get graph visualization for a cluster of findings.
    
    Shows how findings in a cluster are related through shared entities.
    
    Args:
        cluster_id: Cluster identifier
        
    Returns:
        Graph data for the cluster
    """
    data_service = DatabaseDataService()
    
    # Get findings in cluster
    all_findings = data_service.get_findings(limit=10000)
    findings = [f for f in all_findings if f.get('cluster_id') == cluster_id]
    
    if not findings:
        raise HTTPException(status_code=404, detail="Cluster not found or has no findings")
    
    # Build cluster graph
    graph_builder = GraphBuilderService()
    graph_data = graph_builder.build_cluster_graph(findings, cluster_id)
    
    return GraphData(
        nodes=[GraphNode(**node) for node in graph_data['nodes']],
        links=[GraphLink(**link) for link in graph_data['links']],
        metadata=graph_data.get('metadata', {})
    )


@router.get("/technique/{technique_id}", response_model=GraphData)
async def get_technique_graph(
    technique_id: str,
    limit: int = Query(default=100, ge=1, le=500)
):
    """
    Get graph of entities involved in a specific MITRE ATT&CK technique.
    
    Args:
        technique_id: MITRE ATT&CK technique ID (e.g., T1071.001)
        limit: Maximum number of findings to process
        
    Returns:
        Graph data for the technique
    """
    data_service = DatabaseDataService()
    
    # Get findings with this technique
    all_findings = data_service.get_findings(limit=limit * 2)
    findings = []
    
    for finding in all_findings:
        mitre_predictions = finding.get('mitre_predictions', {})
        if technique_id in mitre_predictions:
            findings.append(finding)
            if len(findings) >= limit:
                break
    
    if not findings:
        return GraphData(
            nodes=[],
            links=[],
            metadata={"message": f"No findings with technique {technique_id}"}
        )
    
    # Build technique graph
    graph_builder = GraphBuilderService()
    graph_data = graph_builder.build_technique_graph(findings, technique_id)
    
    return GraphData(
        nodes=[GraphNode(**node) for node in graph_data['nodes']],
        links=[GraphLink(**link) for link in graph_data['links']],
        metadata=graph_data.get('metadata', {})
    )


@router.get("/summary", response_model=Dict[str, Any])
async def get_graph_summary(
    limit: int = Query(default=100, ge=1, le=1000)
):
    """
    Get summary statistics about the entity graph.
    
    Args:
        limit: Maximum number of findings to analyze
        
    Returns:
        Summary statistics
    """
    data_service = DatabaseDataService()
    findings = data_service.get_findings(limit=limit)
    
    # Build graph and calculate metrics
    graph_builder = GraphBuilderService()
    graph_data = graph_builder.build_entity_graph(findings)
    
    # Calculate summary metrics
    nodes_by_type = {}
    for node in graph_data['nodes']:
        node_type = node['type']
        nodes_by_type[node_type] = nodes_by_type.get(node_type, 0) + 1
    
    # Find most connected nodes
    node_connections = {}
    for link in graph_data['links']:
        node_connections[link['source']] = node_connections.get(link['source'], 0) + 1
        node_connections[link['target']] = node_connections.get(link['target'], 0) + 1
    
    top_nodes = sorted(node_connections.items(), key=lambda x: x[1], reverse=True)[:10]
    
    return {
        "total_nodes": len(graph_data['nodes']),
        "total_links": len(graph_data['links']),
        "nodes_by_type": nodes_by_type,
        "top_connected_nodes": [
            {"id": node_id, "connections": count}
            for node_id, count in top_nodes
        ],
        "findings_analyzed": len(findings)
    }

