import asyncio
import json
import logging
import requests
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio
from core.config import get_integration_config

logger = logging.getLogger(__name__)
server = Server("jira")


def result(data):
    return [types.TextContent(type="text", text=json.dumps(data, indent=2))]


def get_config():
    return get_integration_config('jira')


@server.list_tools()
async def handle_list_tools():
    return [
        types.Tool(
            name="jira_create_ticket",
            description="Create Jira ticket for security incident",
            inputSchema={
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "description": {"type": "string"},
                    "project": {"type": "string"},
                    "issue_type": {"type": "string", "default": "Task"},
                    "priority": {"type": "string", "default": "Medium"}
                },
                "required": ["summary", "description", "project"]
            }
        ),
        types.Tool(
            name="jira_export_case_report",
            description="Export full case details to Jira with findings and resolution",
            inputSchema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "project": {"type": "string"},
                    "include_findings": {"type": "boolean", "default": True},
                    "include_timeline": {"type": "boolean", "default": True}
                },
                "required": ["case_id", "project"]
            }
        ),
        types.Tool(
            name="jira_export_remediation",
            description="Export remediation steps as Jira subtasks",
            inputSchema={
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "parent_issue_key": {"type": "string"},
                    "assign_to": {"type": "string", "default": None}
                },
                "required": ["case_id", "parent_issue_key"]
            }
        ),
        types.Tool(
            name="jira_search",
            description="Search Jira tickets",
            inputSchema={
                "type": "object",
                "properties": {
                    "jql": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10}
                },
                "required": ["jql"]
            }
        ),
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict | None):
    config = get_config()
    url = config.get('url')
    email = config.get('email')
    token = config.get('api_token')
    if not url or not email or not token:
        return result({"error": "Jira not configured"})
    
    args = arguments or {}
    auth = (email, token)
    headers = {"Content-Type": "application/json"}
    
    try:
        if name == "jira_create_ticket":
            summary = args.get("summary")
            desc = args.get("description")
            project = args.get("project")
            if not summary or not desc or not project:
                return result({"error": "summary, description, project required"})
            
            payload = {
                "fields": {
                    "project": {"key": project},
                    "summary": summary,
                    "description": desc,
                    "issuetype": {"name": args.get("issue_type", "Task")},
                    "priority": {"name": args.get("priority", "Medium")}
                }
            }
            resp = requests.post(f"{url}/rest/api/2/issue", auth=auth, headers=headers, json=payload, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            return result({"success": True, "key": data.get("key"), "id": data.get("id")})
        
        elif name == "jira_export_case_report":
            return await export_case_report(args, url, auth, headers)
        
        elif name == "jira_export_remediation":
            return await export_remediation(args, url, auth, headers)
        
        elif name == "jira_search":
            jql = args.get("jql")
            if not jql:
                return result({"error": "jql required"})
            resp = requests.get(f"{url}/rest/api/2/search", auth=auth, 
                params={"jql": jql, "maxResults": args.get("max_results", 10)}, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            issues = [{
                "key": i.get("key"), "summary": i.get("fields", {}).get("summary"),
                "status": i.get("fields", {}).get("status", {}).get("name")
            } for i in data.get("issues", [])]
            return result({"total": data.get("total"), "issues": issues})
        
        return result({"error": f"Unknown tool: {name}"})
    except Exception as e:
        return result({"error": str(e)})


async def export_case_report(args: dict, url: str, auth, headers) -> list:
    """Export full case details to Jira."""
    from database.connection import get_db_session
    from database.models import Case, Finding
    
    case_id = args.get("case_id")
    project = args.get("project")
    include_findings = args.get("include_findings", True)
    
    # Get case from database
    session = get_db_session()
    try:
        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            return result({"error": f"Case {case_id} not found"})
        
        # Build description
        description = f"*Case Report: {case.title}*\n\n"
        description += f"*Priority:* {case.priority}\n"
        description += f"*Status:* {case.status}\n"
        description += f"*Created:* {case.created_at.isoformat() if case.created_at else 'N/A'}\n\n"
        
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
        
        # Add full report link
        description += f"*Full report available in AI SOC system (Case ID: {case_id})*\n"
        
        # Map priority
        priority_map = {
            "critical": "Highest",
            "high": "High",
            "medium": "Medium",
            "low": "Low"
        }
        jira_priority = priority_map.get(case.priority.lower(), "Medium")
        
        # Create main issue
        issue_data = {
            "fields": {
                "project": {"key": project},
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
        if include_findings and findings:
            for finding in findings[:5]:  # Limit to 5 subtasks
                subtask_data = {
                    "fields": {
                        "project": {"key": project},
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
                except Exception:
                    pass  # Continue even if subtask creation fails
        
        return result({
            "success": True,
            "issue_key": issue_key,
            "subtasks_created": subtasks_created,
            "url": f"{url}/browse/{issue_key}"
        })
    
    except Exception as e:
        return result({"error": str(e)})
    
    finally:
        session.close()


async def export_remediation(args: dict, url: str, auth, headers) -> list:
    """Export remediation steps as Jira subtasks."""
    from database.connection import get_db_session
    from database.models import Case
    
    case_id = args.get("case_id")
    parent_issue_key = args.get("parent_issue_key")
    assign_to = args.get("assign_to")
    
    # Get case from database
    session = get_db_session()
    try:
        case = session.query(Case).filter(Case.case_id == case_id).first()
        if not case:
            return result({"error": f"Case {case_id} not found"})
        
        # Get resolution steps
        resolution_steps = case.metadata.get('resolution_steps', []) if case.metadata else []
        if not resolution_steps:
            return result({"error": f"No resolution steps found for case {case_id}"})
        
        # Get parent issue to determine project
        parent_response = requests.get(
            f"{url}/rest/api/2/issue/{parent_issue_key}",
            auth=auth,
            headers=headers,
            timeout=30
        )
        parent_response.raise_for_status()
        parent_data = parent_response.json()
        project_key = parent_data["fields"]["project"]["key"]
        
        # Create subtasks for each remediation step
        created_subtasks = []
        for i, step in enumerate(resolution_steps, 1):
            subtask_data = {
                "fields": {
                    "project": {"key": project_key},
                    "parent": {"key": parent_issue_key},
                    "summary": f"Remediation Step {i}: {step.get('description', 'N/A')[:80]}",
                    "description": f"*Action:* {step.get('action_taken', 'N/A')}\n\n*Result:* {step.get('result', 'N/A')}",
                    "issuetype": {"name": "Sub-task"}
                }
            }
            
            if assign_to:
                # Try to find user by email
                user_response = requests.get(
                    f"{url}/rest/api/2/user/search",
                    auth=auth,
                    headers=headers,
                    params={"username": assign_to},
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
                result_data = response.json()
                created_subtasks.append(result_data.get("key"))
            except Exception as e:
                logger.error(f"Failed to create subtask {i}: {e}")
                continue
        
        return result({
            "success": True,
            "subtasks_created": len(created_subtasks),
            "subtask_keys": created_subtasks
        })
    
    except Exception as e:
        return result({"error": str(e)})
    
    finally:
        session.close()


async def main():
    async with mcp.server.stdio.stdio_server() as (read, write):
        await server.run(read, write, InitializationOptions(
            server_name="jira", server_version="0.1.0",
            capabilities=server.get_capabilities(notification_options=NotificationOptions(), experimental_capabilities={})
        ))


if __name__ == "__main__":
    asyncio.run(main())
