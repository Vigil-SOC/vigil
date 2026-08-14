"""
Claude API Tool Schemas
Defines all tools available to Claude via function calling
"""

# Security-Detections Tools (Core functionality)
SECURITY_DETECTION_TOOLS = [
    {
        "name": "analyze_coverage",
        "description": "Analyze detection coverage for MITRE ATT&CK techniques. Returns count and list of detections covering each technique across Sigma, Splunk, Elastic, and KQL formats. Use this to understand what detections exist for specific techniques.",
        "input_schema": {
            "type": "object",
            "properties": {
                "techniques": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "List of MITRE technique IDs (e.g., ['T1059.001', 'T1071.001'])",
                }
            },
            "required": ["techniques"],
        },
    },
    {
        "name": "search_detections",
        "description": "Search across 7,200+ detection rules (Sigma, Splunk, Elastic, KQL) using keywords. Returns matching detection rules with metadata. Use this to find relevant detections for specific attack patterns or tools.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query - keywords or phrases (e.g., 'powershell base64', 'lateral movement', 'mimikatz')",
                },
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql"],
                    "description": "Optional: Filter by detection format. Omit to search all formats.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of results to return (default: 20)",
                    "default": 20,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "identify_gaps",
        "description": "Identify detection gaps for a given context such as threat actor, attack type, or campaign. Analyzes which MITRE techniques have insufficient detection coverage. Use this for gap analysis and prioritization.",
        "input_schema": {
            "type": "object",
            "properties": {
                "context": {
                    "type": "string",
                    "description": "Context for gap analysis (e.g., 'ransomware', 'APT29', 'initial access', 'lateral movement')",
                }
            },
            "required": ["context"],
        },
    },
    {
        "name": "get_coverage_stats",
        "description": "Get overall detection coverage statistics including total detections, techniques covered, and breakdown by source format. Use this for high-level coverage overview.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql"],
                    "description": "Optional: Get stats for specific source format only",
                }
            },
        },
    },
    {
        "name": "get_detection_count",
        "description": "Get count of detection rules, optionally filtered by source format.",
        "input_schema": {
            "type": "object",
            "properties": {
                "source_type": {
                    "type": "string",
                    "enum": ["sigma", "splunk", "elastic", "kql"],
                    "description": "Optional: Count for specific source format",
                }
            },
        },
    },
]

# DeepTempo Findings Tools (Already implemented in backend)
DEEPTEMPO_FINDING_TOOLS = [
    {
        "name": "list_findings",
        "description": "List security findings with server-side pagination and compact summaries. Returns compact finding summaries (use get_finding for full details). Supports filtering by severity, data_source, status, and pagination via offset/limit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity level",
                },
                "data_source": {
                    "type": "string",
                    "description": "Filter by data source (e.g., 'sysmon', 'cloudtrail')",
                },
                "status": {
                    "type": "string",
                    "description": "Filter by status (e.g., 'new', 'investigating', 'resolved')",
                },
                "sort_by": {
                    "type": "string",
                    "enum": ["timestamp", "anomaly_score", "severity"],
                    "description": "Column to sort by",
                    "default": "timestamp",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "description": "Sort direction",
                    "default": "desc",
                },
                "offset": {
                    "type": "integer",
                    "description": "Pagination offset (0-based)",
                    "default": 0,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of findings to return",
                    "default": 20,
                },
            },
        },
    },
    {
        "name": "search_findings",
        "description": "Search findings by text query across finding IDs, descriptions, and entity context. Returns compact summaries with pagination. Use get_finding for full details on specific results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text search query (searches finding IDs, descriptions, entity context)",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity level",
                },
                "data_source": {
                    "type": "string",
                    "description": "Filter by data source",
                },
                "status": {"type": "string", "description": "Filter by status"},
                "sort_by": {
                    "type": "string",
                    "enum": ["timestamp", "anomaly_score", "severity"],
                    "default": "anomaly_score",
                },
                "sort_order": {
                    "type": "string",
                    "enum": ["asc", "desc"],
                    "default": "desc",
                },
                "offset": {"type": "integer", "default": 0},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_findings_stats",
        "description": "Get aggregate statistics about all findings without returning individual finding data. Returns counts by severity, data source, status, and top MITRE techniques. Use this to get an overview before drilling into specific findings.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_finding",
        "description": "Get detailed information about a specific finding by ID. Returns full finding details including predicted techniques, embeddings, and related context.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "description": "The finding ID (e.g., 'f-20260209-001')",
                }
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "nearest_neighbors",
        "description": "Find similar findings using embedding-based similarity search. Use this to identify related incidents or patterns.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {
                    "type": "string",
                    "description": "Reference finding ID to find neighbors for",
                },
                "limit": {
                    "type": "integer",
                    "description": "Number of similar findings to return",
                    "default": 10,
                },
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "list_cases",
        "description": "List investigation cases with optional filters. Returns active and closed cases.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["open", "in_progress", "closed"],
                    "description": "Filter by case status",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Filter by severity",
                },
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "get_case",
        "description": "Get detailed information about a specific case including all findings, timeline, activities, and MITRE techniques.",
        "input_schema": {
            "type": "object",
            "properties": {"case_id": {"type": "string", "description": "The case ID"}},
            "required": ["case_id"],
        },
    },
    {
        "name": "create_case",
        "description": "Create a new investigation case. Use this to organize related findings into a case for tracking and investigation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "title": {"type": "string", "description": "Case title/summary"},
                "description": {
                    "type": "string",
                    "description": "Detailed case description",
                },
                "severity": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "description": "Case severity",
                },
                "finding_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Optional: Initial findings to add to case",
                },
            },
            "required": ["title", "severity"],
        },
    },
    {
        "name": "add_finding_to_case",
        "description": "Add a finding to an existing case.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string"},
                "finding_id": {"type": "string"},
            },
            "required": ["case_id", "finding_id"],
        },
    },
    {
        "name": "update_case",
        "description": "Update an existing case's title, description, status, or priority.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The case ID to update"},
                "title": {"type": "string", "description": "New title"},
                "description": {
                    "type": "string",
                    "description": "New description or executive summary",
                },
                "status": {
                    "type": "string",
                    "enum": ["open", "investigating", "resolved", "closed"],
                },
                "priority": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                },
            },
            "required": ["case_id"],
        },
    },
    {
        "name": "add_resolution_step",
        "description": "Add a resolution step to a case documenting a containment, eradication, or recovery action taken or recommended.",
        "input_schema": {
            "type": "object",
            "properties": {
                "case_id": {"type": "string", "description": "The case ID"},
                "description": {
                    "type": "string",
                    "description": "What needs to be done or was done",
                },
                "action_taken": {
                    "type": "string",
                    "description": "The specific action taken or recommended",
                },
                "result": {
                    "type": "string",
                    "description": "Outcome or expected outcome of the action",
                },
            },
            "required": ["case_id", "description", "action_taken"],
        },
    },
]

# Attack Layer Tools
ATTACK_LAYER_TOOLS = [
    {
        "name": "get_attack_layer",
        "description": "Get MITRE ATT&CK Navigator layer JSON showing coverage of techniques. Use this to visualize detection coverage in ATT&CK Navigator.",
        "input_schema": {
            "type": "object",
            "properties": {
                "layer_type": {
                    "type": "string",
                    "enum": ["coverage", "findings", "detections"],
                    "description": "Type of layer to generate",
                    "default": "coverage",
                }
            },
        },
    },
    {
        "name": "get_technique_rollup",
        "description": "Get rollup statistics for MITRE techniques showing finding counts and severity distribution.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tactic": {
                    "type": "string",
                    "description": "Optional: Filter by MITRE tactic (e.g., 'initial-access', 'execution')",
                }
            },
        },
    },
]

# Approval Tools
APPROVAL_TOOLS = [
    {
        "name": "list_pending_approvals",
        "description": "List pending actions awaiting approval. Returns actions that require analyst approval before execution (e.g., host isolation, IP blocking).",
        "input_schema": {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of pending actions to return",
                    "default": 50,
                }
            },
        },
    },
    {
        "name": "get_approval_action",
        "description": "Get detailed information about a specific pending action by ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID (e.g., 'action-20260209-001')",
                }
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "approve_action",
        "description": "Approve a pending action for execution. The action will be executed automatically after approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID to approve",
                },
                "approved_by": {
                    "type": "string",
                    "description": "Name of approver (e.g., 'analyst_name', 'auto_approved')",
                    "default": "analyst",
                },
            },
            "required": ["action_id"],
        },
    },
    {
        "name": "reject_action",
        "description": "Reject a pending action. The action will not be executed and will be marked as rejected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "action_id": {
                    "type": "string",
                    "description": "The action ID to reject",
                },
                "reason": {"type": "string", "description": "Reason for rejection"},
                "rejected_by": {
                    "type": "string",
                    "description": "Name of person rejecting",
                    "default": "analyst",
                },
            },
            "required": ["action_id", "reason"],
        },
    },
    {
        "name": "get_approval_stats",
        "description": "Get statistics about approval actions including total, pending, approved, rejected, and executed counts.",
        "input_schema": {"type": "object", "properties": {}},
    },
]

# Combine all tools
ALL_TOOLS = (
    SECURITY_DETECTION_TOOLS
    + DEEPTEMPO_FINDING_TOOLS
    + ATTACK_LAYER_TOOLS
    + APPROVAL_TOOLS
)
