"""
Unit tests for MCP (Model Context Protocol) tool integration.
Tests tool discovery, execution, parameter validation, and error handling.
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
import json

# Skip all tests until MCPService API is documented
pytestmark = pytest.mark.skip(reason="MCPService methods don't exist - needs rewrite for current MCP integration")

# MCPService doesn't exist - MCP integration is handled differently
# from core.integrations.mcp.service import MCPService


class TestMCPToolDiscovery:
    """Test MCP tool discovery and listing."""
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_list_available_tools(self, mock_client):
        """Test listing available MCP tools."""
        mock_client.return_value.list_tools.return_value = [
            {"name": "virustotal", "description": "VirusTotal lookups"},
            {"name": "shodan", "description": "Shodan IP lookups"},
            {"name": "misp", "description": "MISP threat intelligence"}
        ]
        
        service = MCPService()
        tools = service.list_tools()
        
        assert len(tools) == 3
        assert any(t["name"] == "virustotal" for t in tools)
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_get_tool_info(self, mock_client):
        """Test getting detailed tool information."""
        mock_client.return_value.get_tool.return_value = {
            "name": "virustotal",
            "description": "Query VirusTotal for file/URL/IP analysis",
            "parameters": {
                "type": {"type": "string", "enum": ["file", "url", "ip"]},
                "value": {"type": "string", "description": "Hash, URL, or IP to check"}
            },
            "required": ["type", "value"]
        }
        
        service = MCPService()
        tool_info = service.get_tool_info("virustotal")
        
        assert tool_info["name"] == "virustotal"
        assert "parameters" in tool_info
        assert "type" in tool_info["parameters"]
    
    def test_filter_tools_by_category(self):
        """Test filtering tools by category."""
        service = MCPService()
        
        # Mock tools with categories
        service._tools = [
            {"name": "virustotal", "category": "threat_intel"},
            {"name": "splunk", "category": "siem"},
            {"name": "shodan", "category": "threat_intel"},
            {"name": "crowdstrike", "category": "edr"}
        ]
        
        threat_intel_tools = service.filter_tools(category="threat_intel")
        
        assert len(threat_intel_tools) == 2
        assert all(t["category"] == "threat_intel" for t in threat_intel_tools)


class TestToolExecution:
    """Test MCP tool execution."""
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_execute_tool(self, mock_client):
        """Test executing an MCP tool."""
        mock_client.return_value.call_tool.return_value = {
            "result": {
                "positives": 5,
                "total": 70,
                "permalink": "https://virustotal.com/..."
            }
        }
        
        service = MCPService()
        
        result = service.execute_tool(
            tool_name="virustotal",
            parameters={
                "type": "ip",
                "value": "185.220.101.5"
            }
        )
        
        assert result["result"]["positives"] == 5
        mock_client.return_value.call_tool.assert_called_once()
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_execute_tool_with_timeout(self, mock_client):
        """Test tool execution with timeout."""
        mock_client.return_value.call_tool.return_value = {"result": "success"}
        
        service = MCPService()
        
        result = service.execute_tool(
            tool_name="shodan",
            parameters={"ip": "1.2.3.4"},
            timeout=30
        )
        
        assert result["result"] == "success"
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_execute_tool_error(self, mock_client):
        """Test handling tool execution errors."""
        mock_client.return_value.call_tool.side_effect = Exception("API Error")
        
        service = MCPService()
        
        with pytest.raises(Exception) as exc_info:
            service.execute_tool(
                tool_name="virustotal",
                parameters={"type": "ip", "value": "1.2.3.4"}
            )
        
        assert "API Error" in str(exc_info.value)


class TestParameterValidation:
    """Test tool parameter validation."""
    
    def test_validate_required_parameters(self):
        """Test validation of required parameters."""
        service = MCPService()
        
        tool_schema = {
            "name": "virustotal",
            "parameters": {
                "type": {"type": "string"},
                "value": {"type": "string"}
            },
            "required": ["type", "value"]
        }
        
        # Valid parameters
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"type": "ip", "value": "1.2.3.4"}
        )
        assert is_valid is True
        assert len(errors) == 0
        
        # Missing required parameter
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"type": "ip"}  # Missing 'value'
        )
        assert is_valid is False
        assert "value" in str(errors).lower()
    
    def test_validate_parameter_types(self):
        """Test validation of parameter types."""
        service = MCPService()
        
        tool_schema = {
            "name": "test_tool",
            "parameters": {
                "count": {"type": "integer"},
                "enabled": {"type": "boolean"}
            }
        }
        
        # Correct types
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"count": 5, "enabled": True}
        )
        assert is_valid is True
        
        # Wrong type
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"count": "five", "enabled": True}  # String instead of int
        )
        assert is_valid is False
    
    def test_validate_enum_values(self):
        """Test validation of enum parameter values."""
        service = MCPService()
        
        tool_schema = {
            "name": "test_tool",
            "parameters": {
                "type": {"type": "string", "enum": ["file", "url", "ip"]}
            }
        }
        
        # Valid enum value
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"type": "ip"}
        )
        assert is_valid is True
        
        # Invalid enum value
        is_valid, errors = service.validate_parameters(
            tool_schema,
            {"type": "domain"}  # Not in enum
        )
        assert is_valid is False


class TestToolChaining:
    """Test chaining multiple MCP tools."""
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_chain_tools(self, mock_client):
        """Test chaining tool executions."""
        # First tool returns IOCs
        mock_client.return_value.call_tool.side_effect = [
            {"result": {"iocs": ["185.220.101.5", "evil.com"]}},
            {"result": {"positives": 5, "total": 70}},  # VT for IP
            {"result": {"positives": 3, "total": 70}}   # VT for domain
        ]
        
        service = MCPService()
        
        # Extract IOCs
        ioc_result = service.execute_tool("extract_iocs", {"text": "..."})
        iocs = ioc_result["result"]["iocs"]
        
        # Enrich each IOC
        enriched = []
        for ioc in iocs:
            result = service.execute_tool("virustotal", {"type": "auto", "value": ioc})
            enriched.append(result)
        
        assert len(enriched) == 2
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_conditional_tool_execution(self, mock_client):
        """Test conditional tool execution based on results."""
        mock_client.return_value.call_tool.side_effect = [
            {"result": {"threat_score": 85}},  # High score
            {"result": {"isolated": True}}      # Execute isolation
        ]
        
        service = MCPService()
        
        # Analyze threat
        analysis = service.execute_tool("analyze_threat", {"finding": "..."})
        
        # If high score, isolate host
        if analysis["result"]["threat_score"] > 80:
            isolation = service.execute_tool("crowdstrike_isolate", {"host": "ws-042"})
            assert isolation["result"]["isolated"] is True


class TestAsyncToolExecution:
    """Test asynchronous tool execution."""
    
    @pytest.mark.asyncio
    @patch('core.integrations.mcp.service.MCPClient')
    async def test_execute_tool_async(self, mock_client):
        """Test async tool execution."""
        mock_client.return_value.call_tool_async.return_value = {
            "result": "success"
        }
        
        service = MCPService()
        
        result = await service.execute_tool_async(
            tool_name="shodan",
            parameters={"ip": "1.2.3.4"}
        )
        
        assert result["result"] == "success"
    
    @pytest.mark.asyncio
    @patch('core.integrations.mcp.service.MCPClient')
    async def test_parallel_tool_execution(self, mock_client):
        """Test executing multiple tools in parallel."""
        mock_client.return_value.call_tool_async.side_effect = [
            {"result": {"vt_score": 5}},
            {"result": {"shodan_tags": ["malware"]}},
            {"result": {"misp_events": 2}}
        ]
        
        service = MCPService()
        
        # Execute multiple tools in parallel
        results = await service.execute_tools_parallel([
            {"tool": "virustotal", "params": {"type": "ip", "value": "1.2.3.4"}},
            {"tool": "shodan", "params": {"ip": "1.2.3.4"}},
            {"tool": "misp", "params": {"ip": "1.2.3.4"}}
        ])
        
        assert len(results) == 3


class TestToolConfiguration:
    """Test MCP tool configuration."""
    
    def test_load_tool_config(self):
        """Test loading tool configuration."""
        service = MCPService(config_path="mcp-config.json")
        
        # Mock config loading
        with patch('builtins.open', create=True) as mock_open:
            mock_open.return_value.__enter__.return_value.read.return_value = json.dumps({
                "servers": {
                    "virustotal": {
                        "url": "http://localhost:8000",
                        "api_key": "test-key"
                    }
                }
            })
            
            config = service.load_config()
            
            assert "servers" in config
            assert "virustotal" in config["servers"]
    
    def test_tool_server_connection(self):
        """Test connecting to MCP tool servers."""
        service = MCPService()
        
        server_config = {
            "url": "http://localhost:8000",
            "api_key": "test-key"
        }
        
        with patch('core.integrations.mcp.service.MCPClient') as mock_client:
            mock_client.return_value.connect.return_value = True
            
            connected = service.connect_to_server("virustotal", server_config)
            
            assert connected is True


class TestToolResults:
    """Test handling and parsing tool results."""
    
    def test_parse_virustotal_result(self):
        """Test parsing VirusTotal results."""
        service = MCPService()
        
        raw_result = {
            "data": {
                "attributes": {
                    "last_analysis_stats": {
                        "malicious": 5,
                        "suspicious": 2,
                        "undetected": 63,
                        "timeout": 0
                    }
                }
            }
        }
        
        parsed = service.parse_tool_result("virustotal", raw_result)
        
        assert parsed["malicious"] == 5
        assert parsed["total_engines"] == 70
    
    def test_normalize_tool_output(self):
        """Test normalizing output from different tools."""
        service = MCPService()
        
        # Different tools, same data type (IP reputation)
        vt_result = {"positives": 5, "total": 70}
        shodan_result = {"tags": ["malware"], "vulns": ["CVE-2021-1234"]}
        
        normalized_vt = service.normalize_output("virustotal", vt_result)
        normalized_shodan = service.normalize_output("shodan", shodan_result)
        
        # Both should have standardized fields
        assert "threat_score" in normalized_vt
        assert "threat_score" in normalized_shodan


class TestErrorHandling:
    """Test error handling in MCP tool execution."""
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_handle_tool_not_found(self, mock_client):
        """Test handling tool not found error."""
        mock_client.return_value.call_tool.side_effect = Exception("Tool not found")
        
        service = MCPService()
        
        with pytest.raises(Exception) as exc_info:
            service.execute_tool("nonexistent_tool", {})
        
        assert "not found" in str(exc_info.value).lower()
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_handle_api_rate_limit(self, mock_client):
        """Test handling API rate limit errors."""
        mock_client.return_value.call_tool.side_effect = Exception("Rate limit exceeded")
        
        service = MCPService()
        
        result = service.execute_tool_with_retry(
            tool_name="virustotal",
            parameters={"type": "ip", "value": "1.2.3.4"},
            max_retries=3,
            backoff_factor=2
        )
        
        # Should eventually fail after retries
        assert result["status"] == "error"
        assert "rate limit" in result["error"].lower()
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_handle_timeout(self, mock_client):
        """Test handling tool execution timeout."""
        import asyncio
        mock_client.return_value.call_tool.side_effect = asyncio.TimeoutError()
        
        service = MCPService()
        
        result = service.execute_tool(
            tool_name="slow_tool",
            parameters={},
            timeout=5
        )
        
        assert result["status"] == "timeout"


class TestToolCaching:
    """Test caching of tool results."""
    
    def test_cache_tool_result(self):
        """Test caching tool results."""
        service = MCPService(enable_cache=True)
        
        tool_name = "virustotal"
        params = {"type": "ip", "value": "1.2.3.4"}
        result = {"positives": 5, "total": 70}
        
        # Cache result
        service.cache_result(tool_name, params, result)
        
        # Retrieve from cache
        cached = service.get_cached_result(tool_name, params)
        
        assert cached == result
    
    def test_cache_expiration(self):
        """Test cache expiration."""
        service = MCPService(enable_cache=True, cache_ttl=60)
        
        tool_name = "shodan"
        params = {"ip": "1.2.3.4"}
        result = {"tags": ["malware"]}
        
        # Cache result with timestamp
        service.cache_result(tool_name, params, result)
        
        # Simulate time passing
        with patch('time.time') as mock_time:
            mock_time.return_value += 120  # 2 minutes later
            
            cached = service.get_cached_result(tool_name, params)
            
            # Should be expired
            assert cached is None
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_use_cache_when_available(self, mock_client):
        """Test using cache instead of calling tool."""
        service = MCPService(enable_cache=True)
        
        tool_name = "virustotal"
        params = {"type": "ip", "value": "1.2.3.4"}
        
        # First call - not cached
        mock_client.return_value.call_tool.return_value = {"result": "fresh"}
        result1 = service.execute_tool(tool_name, params)
        assert mock_client.return_value.call_tool.call_count == 1
        
        # Second call - should use cache
        result2 = service.execute_tool(tool_name, params)
        assert mock_client.return_value.call_tool.call_count == 1  # Not called again
        assert result2 == result1


class TestToolIntegrationWithAI:
    """Test integration of MCP tools with AI analysis."""
    
    @patch('core.integrations.mcp.service.ClaudeService')
    @patch('core.integrations.mcp.service.MCPClient')
    def test_ai_tool_selection(self, mock_mcp, mock_claude):
        """Test AI selecting appropriate tools for analysis."""
        mock_claude.return_value.select_tools.return_value = [
            "virustotal",
            "shodan",
            "misp"
        ]
        
        service = MCPService()
        
        finding = {
            "title": "Suspicious IP Communication",
            "iocs": ["185.220.101.5"]
        }
        
        selected_tools = service.ai_select_tools(finding)
        
        assert "virustotal" in selected_tools
        assert "shodan" in selected_tools
    
    @patch('core.integrations.mcp.service.ClaudeService')
    @patch('core.integrations.mcp.service.MCPClient')
    def test_ai_interprets_tool_results(self, mock_mcp, mock_claude):
        """Test AI interpreting tool results."""
        tool_results = {
            "virustotal": {"positives": 8, "total": 70},
            "shodan": {"tags": ["malware", "botnet"]},
            "misp": {"events": 3}
        }
        
        mock_claude.return_value.interpret_results.return_value = {
            "summary": "High confidence malicious activity",
            "risk_score": 85,
            "recommended_actions": ["Block IP", "Isolate affected hosts"]
        }
        
        service = MCPService()
        
        interpretation = service.ai_interpret_results(tool_results)
        
        assert interpretation["risk_score"] == 85
        assert "Block IP" in interpretation["recommended_actions"]


@pytest.mark.integration
class TestMCPEndToEnd:
    """End-to-end MCP integration tests."""
    
    @patch('core.integrations.mcp.service.MCPClient')
    def test_full_enrichment_workflow(self, mock_client):
        """Test complete enrichment workflow using MCP tools."""
        # Setup mock responses
        mock_client.return_value.call_tool.side_effect = [
            {"result": {"positives": 8, "total": 70}},      # VT
            {"result": {"tags": ["malware"]}},              # Shodan
            {"result": {"events": 2}}                       # MISP
        ]
        
        service = MCPService()
        
        finding = {
            "id": "f1",
            "title": "Suspicious IP",
            "iocs": ["185.220.101.5"]
        }
        
        # Enrich using multiple tools
        enrichment = service.enrich_finding(finding)
        
        assert "virustotal" in enrichment
        assert "shodan" in enrichment
        assert "misp" in enrichment
        assert enrichment["virustotal"]["positives"] == 8

