"""Service for generating custom integrations using Claude AI."""

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Dict, Any, Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class CustomIntegrationService:
    """Service for AI-powered custom integration generation."""
    
    def __init__(self):
        """Initialize the custom integration service."""
        self.custom_integrations_dir = Path.home() / '.deeptempo' / 'custom_integrations'
        self.custom_integrations_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file = self.custom_integrations_dir / 'metadata.json'
        
    async def generate_integration(
        self,
        documentation: str,
        integration_name: Optional[str] = None,
        category: str = "Custom",
        conversation_history: Optional[List[Dict[str, str]]] = None
    ) -> Dict[str, Any]:
        """
        Generate a custom integration from API documentation using Claude.
        
        Args:
            documentation: API or MCP documentation text
            integration_name: Optional custom name for the integration
            category: Integration category
            conversation_history: Optional list of previous messages for interactive mode
        
        Returns:
            Dictionary with integration metadata and server code
        """
        try:
            # Import Claude service
            from services.claude_service import ClaudeService
            
            # Initialize Claude
            claude = ClaudeService(use_mcp_tools=False)
            
            # Check if Claude is configured
            if not claude.api_key or not claude.client:
                return {
                    "success": False,
                    "error": "Claude API is not configured. Please configure it in Settings."
                }
            
            # Create prompt for Claude to analyze the documentation
            system_prompt = "You are an expert at analyzing API documentation and generating Python MCP (Model Context Protocol) server code for security integrations."
            
            if conversation_history:
                # Interactive mode - continue the conversation
                # Extract the last user message
                last_message = conversation_history[-1]["content"] if conversation_history else ""
                context = conversation_history[:-1] if len(conversation_history) > 1 else None
                
                response = claude.chat(
                    message=last_message,
                    context=context,
                    system_prompt=system_prompt,
                    model="claude-sonnet-4-20250514"
                )
            else:
                # Initial generation - create the analysis prompt
                prompt = self._create_analysis_prompt(documentation, integration_name, category)
                
                response = claude.chat(
                    message=prompt,
                    system_prompt=system_prompt,
                    model="claude-sonnet-4-20250514"
                )
            
            # Check if Claude is asking questions or ready to generate
            if self._is_asking_questions(response):
                # Claude needs more information
                # Build the full conversation history
                if conversation_history:
                    full_history = conversation_history + [{"role": "assistant", "content": response}]
                else:
                    full_history = [
                        {"role": "user", "content": self._create_analysis_prompt(documentation, integration_name, category)},
                        {"role": "assistant", "content": response}
                    ]
                
                return {
                    "success": True,
                    "needs_clarification": True,
                    "message": response,
                    "conversation_history": full_history
                }
            
            # Parse Claude's response
            integration_data = self._parse_claude_response(response, category)
            
            if not integration_data:
                return {
                    "success": False,
                    "error": "Failed to parse Claude's response. The documentation may be unclear or incomplete."
                }
            
            # Generate integration ID
            integration_id = integration_data.get("id", self._generate_id(integration_data["name"]))
            
            return {
                "success": True,
                "needs_clarification": False,
                "integration_id": integration_id,
                "integration_name": integration_data["name"],
                "metadata": integration_data["metadata"],
                "server_code": integration_data["server_code"],
                "message": f"Successfully generated custom integration '{integration_data['name']}'"
            }
        
        except Exception as e:
            logger.error(f"Error generating custom integration: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def _create_analysis_prompt(self, documentation: str, integration_name: Optional[str], category: str) -> str:
        """Create a prompt for Claude to analyze the documentation."""
        
        name_hint = f"The integration should be named '{integration_name}'." if integration_name else ""
        
        return f"""I need you to analyze the following API documentation and generate a complete MCP (Model Context Protocol) server integration for a security operations platform.

{name_hint}
Category: {category}

**Documentation:**
```
{documentation}
```

Please analyze this documentation and generate:

1. **Integration Metadata** (JSON format):
   - id: A unique identifier (lowercase, hyphens, e.g., "my-custom-api")
   - name: Human-readable name
   - category: "{category}"
   - description: Brief description (1-2 sentences)
   - functionality_type: One of: "Data Enrichment", "Detection", "Response", "Case Management", "Communication", "Utilities"
   - fields: Array of configuration fields needed (api_key, url, etc.)
     - Each field should have: name, label, type, required, placeholder, helpText
     - Types: text, password, url, number, boolean, select
   - docs_url: Official documentation URL (if available)

2. **MCP Server Code** (Python):
   - Complete, production-ready MCP server implementation
   - Follow the Vigil SOC patterns
   - Include proper error handling
   - Use config_utils.py for configuration loading
   - Define tools based on the API's capabilities
   - Each tool should have a clear name, description, and input schema

**Important Guidelines:**
- Extract the most useful API endpoints and create MCP tools for them
- Focus on security-relevant operations (search, analyze, query, enrich, etc.)
- Use descriptive tool names (e.g., "search_threats", "get_ip_info", "analyze_file")
- Include proper authentication handling
- Add comprehensive error messages
- Follow Python best practices and type hints

**Response Format:**
Please respond with a JSON object containing:
```json
{{
  "id": "integration-id",
  "name": "Integration Name",
  "metadata": {{
    "id": "integration-id",
    "name": "Integration Name",
    "category": "{category}",
    "description": "Description",
    "functionality_type": "Data Enrichment",
    "fields": [
      {{
        "name": "api_key",
        "label": "API Key",
        "type": "password",
        "required": true,
        "placeholder": "Your API key",
        "helpText": "Get your API key from..."
      }}
    ],
    "docs_url": "https://..."
  }},
  "server_code": "# Complete Python MCP server code here..."
}}
```

Make sure the server_code is a complete, runnable Python file that follows this structure:

```python
\"\"\"MCP Server for [Integration Name] integration.\"\"\"

import asyncio
import logging
import json
from typing import Any, Optional
from mcp.server.models import InitializationOptions
import mcp.types as types
from mcp.server import NotificationOptions, Server
import mcp.server.stdio

# Add any necessary imports for the API client

logger = logging.getLogger(__name__)

def get_config():
    \"\"\"Get integration configuration.\"\"\"
    try:
        from core.config import get_integration_config
        config = get_integration_config('integration-id')
        return config
    except Exception as e:
        logger.error(f"Error loading config: {{e}}")
        return {{}}

server = Server("integration-id-server")

@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    \"\"\"List available tools.\"\"\"
    return [
        types.Tool(
            name="tool_name",
            description="Tool description",
            inputSchema={{
                "type": "object",
                "properties": {{
                    "param": {{
                        "type": "string",
                        "description": "Parameter description"
                    }}
                }},
                "required": ["param"]
            }}
        )
    ]

@server.call_tool()
async def handle_call_tool(
    name: str, arguments: dict | None
) -> list[types.TextContent | types.ImageContent | types.EmbeddedResource]:
    \"\"\"Handle tool execution requests.\"\"\"
    
    config = get_config()
    
    if not config:
        return [types.TextContent(
            type="text",
            text=json.dumps({{
                "error": "Integration not configured",
                "message": "Please configure in Settings > Integrations"
            }}, indent=2)
        )]
    
    try:
        if name == "tool_name":
            # Implementation here
            result = {{"result": "data"}}
            return [types.TextContent(
                type="text",
                text=json.dumps(result, indent=2)
            )]
        
        raise ValueError(f"Unknown tool: {{name}}")
    
    except Exception as e:
        logger.error(f"Error in tool {{name}}: {{e}}")
        return [types.TextContent(
            type="text",
            text=json.dumps({{
                "error": str(e),
                "tool": name
            }}, indent=2)
        )]

async def main():
    \"\"\"Run the MCP server.\"\"\"
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="integration-id-server",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={{}},
                ),
            ),
        )

if __name__ == "__main__":
    asyncio.run(main())
```

Analyze the documentation carefully and generate the most useful integration possible.

**IMPORTANT**: If the documentation is unclear, incomplete, or you need clarification on:
- Which endpoints to prioritize
- Authentication details
- Parameter meanings
- Response formats
- Error handling requirements

Ask me specific questions BEFORE generating the code. Start your response with "I have some questions:" and list your questions.

Only generate the JSON response when you have enough information to create a complete, production-ready integration."""
    
    def _is_asking_questions(self, response: str) -> bool:
        """Check if Claude is asking for clarification instead of generating code."""
        question_indicators = [
            "I have some questions:",
            "Could you clarify",
            "I need more information",
            "Can you provide",
            "Which endpoints",
            "What authentication",
            "Before I generate"
        ]
        
        # If response contains question indicators and doesn't contain JSON, it's asking questions
        has_questions = any(indicator.lower() in response.lower() for indicator in question_indicators)
        has_json = "```json" in response or '"metadata"' in response
        
        return has_questions and not has_json
    
    def _parse_claude_response(self, response: str, category: str) -> Optional[Dict[str, Any]]:
        """Parse Claude's response to extract integration data."""
        try:
            # Try to find JSON in the response
            # Look for code blocks first
            json_match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', response, re.DOTALL)
            
            if json_match:
                json_str = json_match.group(1)
            else:
                # Try to find raw JSON
                json_match = re.search(r'\{.*"metadata".*"server_code".*\}', response, re.DOTALL)
                if json_match:
                    json_str = json_match.group(0)
                else:
                    logger.error("Could not find JSON in Claude's response")
                    return None
            
            # Parse JSON
            data = json.loads(json_str)
            
            # Validate structure
            if not all(key in data for key in ["id", "name", "metadata", "server_code"]):
                logger.error("Missing required fields in Claude's response")
                return None
            
            # Ensure metadata has the category
            data["metadata"]["category"] = category
            
            return data
        
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON from Claude's response: {e}")
            return None
        except Exception as e:
            logger.error(f"Error parsing Claude's response: {e}", exc_info=True)
            return None
    
    def _generate_id(self, name: str) -> str:
        """Generate an integration ID from a name."""
        # Convert to lowercase, replace spaces with hyphens
        integration_id = name.lower().replace(' ', '-')
        # Remove non-alphanumeric characters (except hyphens)
        integration_id = re.sub(r'[^a-z0-9-]', '', integration_id)
        # Remove consecutive hyphens
        integration_id = re.sub(r'-+', '-', integration_id)
        # Strip leading/trailing hyphens
        integration_id = integration_id.strip('-')
        
        # Add custom prefix
        return f"custom-{integration_id}"
    
    async def save_integration(
        self,
        integration_id: str,
        metadata: Dict[str, Any],
        server_code: str
    ) -> Dict[str, Any]:
        """
        Save a generated custom integration.
        
        Args:
            integration_id: Unique integration identifier
            metadata: Integration metadata
            server_code: MCP server Python code
        
        Returns:
            Success status
        """
        try:
            # Save metadata
            all_metadata = self._load_metadata()
            all_metadata[integration_id] = {
                **metadata,
                "created_at": datetime.now().isoformat(),
                "is_custom": True
            }
            self._save_metadata(all_metadata)
            
            # Save server code
            server_file = self.custom_integrations_dir / f"{integration_id}_server.py"
            server_file.write_text(server_code)
            
            # Create __init__.py if it doesn't exist
            init_file = self.custom_integrations_dir / "__init__.py"
            if not init_file.exists():
                init_file.write_text('"""Custom integrations directory."""\n')
            
            logger.info(f"Saved custom integration '{integration_id}'")
            
            return {
                "success": True,
                "message": f"Custom integration '{integration_id}' saved successfully",
                "server_path": str(server_file)
            }
        
        except Exception as e:
            logger.error(f"Error saving custom integration: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    def list_custom_integrations(self) -> List[Dict[str, Any]]:
        """
        List all custom integrations.
        
        Returns:
            List of custom integration metadata
        """
        try:
            all_metadata = self._load_metadata()
            return list(all_metadata.values())
        except Exception as e:
            logger.error(f"Error listing custom integrations: {e}")
            return []
    
    async def delete_integration(self, integration_id: str) -> Dict[str, Any]:
        """
        Delete a custom integration.
        
        Args:
            integration_id: Integration identifier
        
        Returns:
            Success status
        """
        try:
            # Remove from metadata
            all_metadata = self._load_metadata()
            
            if integration_id not in all_metadata:
                return {
                    "success": False,
                    "error": f"Integration '{integration_id}' not found"
                }
            
            del all_metadata[integration_id]
            self._save_metadata(all_metadata)
            
            # Remove server file
            server_file = self.custom_integrations_dir / f"{integration_id}_server.py"
            if server_file.exists():
                server_file.unlink()
            
            logger.info(f"Deleted custom integration '{integration_id}'")
            
            return {
                "success": True,
                "message": f"Custom integration '{integration_id}' deleted successfully"
            }
        
        except Exception as e:
            logger.error(f"Error deleting custom integration: {e}", exc_info=True)
            return {
                "success": False,
                "error": str(e)
            }
    
    async def validate_integration(self, integration_id: str) -> Dict[str, Any]:
        """
        Validate a custom integration's server code.
        
        Args:
            integration_id: Integration identifier
        
        Returns:
            Validation results
        """
        try:
            server_file = self.custom_integrations_dir / f"{integration_id}_server.py"
            
            if not server_file.exists():
                return {
                    "success": False,
                    "valid": False,
                    "error": "Server file not found"
                }
            
            # Try to compile the code
            code = server_file.read_text()
            try:
                compile(code, str(server_file), 'exec')
                syntax_valid = True
                syntax_error = None
            except SyntaxError as e:
                syntax_valid = False
                syntax_error = str(e)
            
            # Check for required components
            has_server_init = 'Server(' in code
            has_list_tools = '@server.list_tools()' in code
            has_call_tool = '@server.call_tool()' in code
            has_main = 'async def main():' in code
            
            validation_checks = {
                "syntax_valid": syntax_valid,
                "has_server_init": has_server_init,
                "has_list_tools": has_list_tools,
                "has_call_tool": has_call_tool,
                "has_main": has_main
            }
            
            all_valid = all(validation_checks.values())
            
            return {
                "success": True,
                "valid": all_valid,
                "checks": validation_checks,
                "syntax_error": syntax_error
            }
        
        except Exception as e:
            logger.error(f"Error validating integration: {e}", exc_info=True)
            return {
                "success": False,
                "valid": False,
                "error": str(e)
            }
    
    def _load_metadata(self) -> Dict[str, Any]:
        """Load custom integrations metadata."""
        if not self.metadata_file.exists():
            return {}
        
        try:
            with open(self.metadata_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading metadata: {e}")
            return {}
    
    def _save_metadata(self, metadata: Dict[str, Any]) -> None:
        """Save custom integrations metadata."""
        try:
            with open(self.metadata_file, 'w') as f:
                json.dump(metadata, f, indent=2)
        except Exception as e:
            logger.error(f"Error saving metadata: {e}")
            raise

