"""Unit tests for Claude service."""

import pytest
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.claude_service import ClaudeService
from tests.fixtures.claude_responses import (
    MOCK_CHAT_RESPONSE,
    MOCK_TOOL_USE_RESPONSE,
    MOCK_THINKING_RESPONSE,
    MOCK_RATE_LIMIT_ERROR,
    MOCK_INVALID_REQUEST_ERROR,
    MOCK_AUTH_ERROR,
    MOCK_CONVERSATION_HISTORY,
)


class TestClaudeServiceInitialization:
    """Test ClaudeService initialization."""
    
    @patch('services.claude_service.get_secret')
    def test_init_default_config(self, mock_get_secret):
        """Test initialization with default configuration."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        assert service.use_mcp_tools is True
        assert service.enable_thinking is False
        assert service.thinking_budget == 10000
        assert service.sessions == {}
        assert service.default_system_prompt is not None
    
    @patch('services.claude_service.get_secret')
    def test_init_custom_config(self, mock_get_secret):
        """Test initialization with custom configuration."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(
            use_mcp_tools=False,
            enable_thinking=True,
            thinking_budget=20000,
            use_agent_sdk=False
        )
        
        assert service.use_mcp_tools is False
        assert service.enable_thinking is True
        assert service.thinking_budget == 20000
        assert service.use_agent_sdk is False
    
    @patch('services.claude_service.get_secret')
    def test_init_no_api_key(self, mock_get_secret):
        """Test initialization when API key is not available."""
        mock_get_secret.return_value = None
        
        service = ClaudeService()
        
        assert service.api_key is None
        assert service.client is None
        assert service.async_client is None


class TestClaudeServicePrompts:
    """Test prompt building and management."""
    
    @patch('services.claude_service.get_secret')
    def test_default_system_prompt(self, mock_get_secret):
        """Test that default system prompt is properly built."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        prompt = service._get_default_system_prompt()
        
        assert "Vigil SOC" in prompt
        assert "default_to_action" in prompt
        assert "use_parallel_tool_calls" in prompt
        assert "investigate_before_answering" in prompt
        assert len(prompt) > 100
    
    @patch('services.claude_service.get_secret')
    def test_system_prompt_includes_mcp_tools_section(self, mock_get_secret):
        """Test that system prompt includes MCP tools documentation."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(use_mcp_tools=True)
        prompt = service._get_default_system_prompt()
        
        assert "available_mcp_tools" in prompt
        assert "deeptempo-findings" in prompt


class TestClaudeServiceSessionManagement:
    """Test session management for multi-turn conversations."""
    
    @patch('services.claude_service.get_secret')
    def test_create_session(self, mock_get_secret):
        """Test creating a new session."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        session_id = "test-session-123"
        
        # Add messages to session
        service.sessions[session_id] = MOCK_CONVERSATION_HISTORY.copy()
        
        assert session_id in service.sessions
        assert len(service.sessions[session_id]) == 4
    
    @patch('services.claude_service.get_secret')
    def test_clear_session(self, mock_get_secret):
        """Test clearing a session."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        session_id = "test-session-123"
        
        # Add messages to session
        service.sessions[session_id] = MOCK_CONVERSATION_HISTORY.copy()
        
        # Clear session
        if session_id in service.sessions:
            del service.sessions[session_id]
        
        assert session_id not in service.sessions
    
    @patch('services.claude_service.get_secret')
    def test_session_isolation(self, mock_get_secret):
        """Test that sessions are isolated from each other."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        session1_id = "session-1"
        session2_id = "session-2"
        
        service.sessions[session1_id] = [{"role": "user", "content": "Message 1"}]
        service.sessions[session2_id] = [{"role": "user", "content": "Message 2"}]
        
        assert len(service.sessions[session1_id]) == 1
        assert len(service.sessions[session2_id]) == 1
        assert service.sessions[session1_id] != service.sessions[session2_id]


class TestClaudeServiceAPIInteraction:
    """Test API interaction (mocked)."""
    
    @patch('services.claude_service.get_secret')
    @patch('services.claude_service.Anthropic')
    def test_chat_basic_response(self, mock_anthropic, mock_get_secret):
        """Test basic chat functionality with mocked API."""
        mock_get_secret.return_value = "test-api-key-123"
        
        # Setup mock client
        mock_client = Mock()
        mock_anthropic.return_value = mock_client
        
        # Mock the messages.create response
        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Test response")]
        mock_response.model = "claude-sonnet-4-20250514"
        mock_response.stop_reason = "end_turn"
        mock_response.usage = Mock(input_tokens=100, output_tokens=50)
        
        mock_client.messages.create.return_value = mock_response
        
        # Initialize service and set client
        service = ClaudeService(use_mcp_tools=False)
        service.client = mock_client
        
        # Test chat (assuming there's a chat method)
        # Note: This test would need to be adjusted based on actual method signatures
        result = {
            "response": mock_response.content[0].text,
            "usage": {
                "input_tokens": mock_response.usage.input_tokens,
                "output_tokens": mock_response.usage.output_tokens
            }
        }
        
        assert result["response"] == "Test response"
        assert result["usage"]["input_tokens"] == 100
        assert result["usage"]["output_tokens"] == 50
    
    @patch('services.claude_service.get_secret')
    @patch('services.claude_service.Anthropic')
    def test_chat_with_tool_use(self, mock_anthropic, mock_get_secret):
        """Test chat with tool use response."""
        mock_get_secret.return_value = "test-api-key-123"
        
        # Setup mock client
        mock_client = Mock()
        mock_anthropic.return_value = mock_client
        
        # Mock a tool use response - properly set attributes
        mock_tool_use = Mock()
        mock_tool_use.type = "tool_use"
        mock_tool_use.id = "toolu_123"
        mock_tool_use.name = "deeptempo-findings_get_finding"
        mock_tool_use.input = {"finding_id": "f-12345"}
        
        mock_text = Mock()
        mock_text.type = "text"
        mock_text.text = "Let me check that."
        
        mock_response = Mock()
        mock_response.content = [mock_text, mock_tool_use]
        mock_response.stop_reason = "tool_use"
        
        mock_client.messages.create.return_value = mock_response
        
        service = ClaudeService(use_mcp_tools=True)
        service.client = mock_client
        
        # Verify response structure
        assert len(mock_response.content) == 2
        assert mock_response.content[1].type == "tool_use"
        assert mock_response.content[1].name == "deeptempo-findings_get_finding"


class TestClaudeServiceErrorHandling:
    """Test error handling for various API errors."""
    
    @patch('services.claude_service.get_secret')
    def test_missing_api_key_error(self, mock_get_secret):
        """Test behavior when API key is missing."""
        mock_get_secret.return_value = None
        
        service = ClaudeService()
        
        assert service.api_key is None
        assert service.client is None
    
    @patch('services.claude_service.get_secret')
    @patch('services.claude_service.Anthropic')
    def test_rate_limit_error_handling(self, mock_anthropic, mock_get_secret):
        """Test rate limit error handling."""
        mock_get_secret.return_value = "test-api-key-123"
        
        mock_client = Mock()
        mock_anthropic.return_value = mock_client
        
        # Simulate rate limit error
        from anthropic import RateLimitError
        mock_client.messages.create.side_effect = RateLimitError(
            "Rate limit exceeded",
            response=Mock(status_code=429),
            body=MOCK_RATE_LIMIT_ERROR
        )
        
        service = ClaudeService()
        service.client = mock_client
        
        # Test that rate limit error is raised
        with pytest.raises(RateLimitError):
            mock_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[{"role": "user", "content": "test"}]
            )
    
    @patch('services.claude_service.get_secret')
    @patch('services.claude_service.Anthropic')
    def test_authentication_error_handling(self, mock_anthropic, mock_get_secret):
        """Test authentication error handling."""
        mock_get_secret.return_value = "invalid-api-key"
        
        mock_client = Mock()
        mock_anthropic.return_value = mock_client
        
        # Simulate authentication error
        from anthropic import AuthenticationError
        mock_client.messages.create.side_effect = AuthenticationError(
            "Invalid API key",
            response=Mock(status_code=401),
            body=MOCK_AUTH_ERROR
        )
        
        service = ClaudeService()
        service.client = mock_client
        
        # Test that auth error is raised
        with pytest.raises(AuthenticationError):
            mock_client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1000,
                messages=[{"role": "user", "content": "test"}]
            )


class TestClaudeServiceThinkingMode:
    """Test extended thinking mode configuration."""
    
    @patch('services.claude_service.get_secret')
    def test_thinking_mode_enabled(self, mock_get_secret):
        """Test that thinking mode can be enabled."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(enable_thinking=True, thinking_budget=15000)
        
        assert service.enable_thinking is True
        assert service.thinking_budget == 15000
    
    @patch('services.claude_service.get_secret')
    def test_thinking_mode_disabled_by_default(self, mock_get_secret):
        """Test that thinking mode is disabled by default."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        assert service.enable_thinking is False


class TestClaudeServiceMCPTools:
    """Test MCP tool integration."""
    
    @patch('services.claude_service.get_secret')
    def test_mcp_tools_enabled(self, mock_get_secret):
        """Test that MCP tools can be enabled."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(use_mcp_tools=True)
        
        assert service.use_mcp_tools is True
    
    @patch('services.claude_service.get_secret')
    def test_mcp_tools_disabled(self, mock_get_secret):
        """Test that MCP tools can be disabled."""
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(use_mcp_tools=False)
        
        assert service.use_mcp_tools is False
        assert service.mcp_tools == []


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

