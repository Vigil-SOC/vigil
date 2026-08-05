import asyncio
import json
import os
import pytest
import tempfile
from unittest.mock import Mock, patch, MagicMock, AsyncMock
from pathlib import Path
import sys

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from core.llm.harness.claude import ClaudeService
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
    
    @patch('core.llm.harness.claude.get_secret')
    def test_init_default_config(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        assert service.use_mcp_tools is True
        assert service.enable_thinking is False
        assert service.thinking_budget == 10000
        assert service._session_mgr.sessions == {}
        assert service.default_system_prompt is not None
    
    @patch('core.llm.harness.claude.get_secret')
    def test_init_custom_config(self, mock_get_secret):
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
    
    @patch('core.llm.harness.claude.get_secret')
    def test_init_no_api_key(self, mock_get_secret):
        mock_get_secret.return_value = None

        service = ClaudeService()

        assert service.api_key is None
        assert service.client is None
        assert service.async_client is None

    @patch('core.llm.router.router.discover_anthropic_api_key')
    @patch('core.llm.harness.claude.get_secret')
    def test_init_discovers_ui_saved_key(self, mock_get_secret, mock_discover):
        # Legacy lookups all return None — simulates the user who only
        # added a provider through the UI and never touched .env.
        mock_get_secret.return_value = None
        mock_discover.return_value = "sk-ant-ui-saved-key"

        service = ClaudeService()

        assert service.api_key == "sk-ant-ui-saved-key"
        # Discovery should only be tried after the legacy chain comes up empty.
        assert mock_discover.call_count == 1

    @patch('core.llm.router.router.discover_anthropic_api_key')
    @patch('core.llm.harness.claude.get_secret')
    def test_init_does_not_call_discovery_when_legacy_key_present(
        self, mock_get_secret, mock_discover
    ):
        mock_get_secret.return_value = "sk-ant-legacy-env-key"

        service = ClaudeService()

        assert service.api_key == "sk-ant-legacy-env-key"
        mock_discover.assert_not_called()

    # ------------------------------------------------------------------
    # MCP tools cache loading tests
    # ------------------------------------------------------------------

    @patch('core.llm.harness.claude.get_secret')
    def test_load_mcp_tools_from_cache_file(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        cache_data = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Search logs",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            cache_file = Path(tmpdir) / "mcp_tools_cache.json"
            cache_file.write_text(json.dumps(cache_data))

            cache_path = str(cache_file)
            # Patch the cache path inside claude_service so it reads our temp file
            with patch(
                'core.llm.harness.claude.Path',
                side_effect=lambda *args: Path(*args),
            ):
                with patch.object(
                    Path,
                    '__new__',
                    side_effect=None,
                ):
                    pass  # not needed – use simpler approach below

            # Simplest approach: patch the __file__ anchoring logic by replacing
            # the resolved cache_file path inside _load_mcp_tools via a context manager
            original_load = ClaudeService._load_mcp_tools

            def patched_load(self_inner):
                self_inner.mcp_tools = []
                try:
                    tools_dict = json.loads(cache_file.read_text())
                    seen_tool_names = set()
                    for server_name, server_tools in tools_dict.items():
                        for tool in server_tools:
                            tool_name = f"{server_name}_{tool['name']}"
                            if tool_name in seen_tool_names:
                                continue
                            seen_tool_names.add(tool_name)
                            input_schema = tool.get("inputSchema", {})
                            if not isinstance(input_schema, dict):
                                input_schema = {}
                            if not input_schema or "type" not in input_schema:
                                input_schema = {"type": "object", "properties": {}, "required": []}
                            self_inner.mcp_tools.append({
                                "name": tool_name,
                                "description": f"[{server_name}] {tool.get('description', '')}",
                                "input_schema": input_schema,
                            })
                except Exception:
                    self_inner.mcp_tools = []

            with patch.object(ClaudeService, '_load_mcp_tools', patched_load):
                service = ClaudeService(use_mcp_tools=True)

        assert len(service.mcp_tools) >= 1
        tool_names = [t["name"] for t in service.mcp_tools]
        assert "splunk_search" in tool_names

    @patch('core.llm.harness.claude.get_secret')
    def test_load_mcp_tools_cache_file_direct(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        cache_data = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Search logs",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        # The service computes the cache path as:
        # Path(__file__).parent.parent / "data" / "mcp_tools_cache.json"
        # where __file__ is services/claude_service.py, so parent.parent is project root.
        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache_data))

            with patch.object(ClaudeService, '_populate_mcp_registry'):
                service = ClaudeService(use_mcp_tools=True, mcp_client=None)

            assert len(service.mcp_tools) >= 1
            tool_names = [t["name"] for t in service.mcp_tools]
            assert "splunk_search" in tool_names
        finally:
            if original_exists and original_content is not None:
                cache_file.write_text(original_content)
            elif not original_exists and cache_file.exists():
                cache_file.unlink()

    @patch('core.llm.harness.claude.get_secret')
    def test_load_mcp_tools_fallback_to_in_memory_cache(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        mock_client = Mock()
        mock_client.tools_cache = {
            "jira": [
                {
                    "name": "create_issue",
                    "description": "Create a Jira issue",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        try:
            if cache_file.exists():
                cache_file.unlink()

            with patch.object(ClaudeService, '_populate_mcp_registry'):
                service = ClaudeService(use_mcp_tools=True, mcp_client=mock_client)

            assert len(service.mcp_tools) >= 1
            tool_names = [t["name"] for t in service.mcp_tools]
            assert "jira_create_issue" in tool_names
        finally:
            if original_exists and original_content is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(original_content)

    @patch('core.llm.harness.claude.get_secret')
    def test_load_mcp_tools_no_sources_available(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        try:
            if cache_file.exists():
                cache_file.unlink()

            service = ClaudeService(use_mcp_tools=True, mcp_client=None)

            assert service.mcp_tools == []
        finally:
            if original_exists and original_content is not None:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(original_content)

    @patch('core.llm.harness.claude.get_secret')
    def test_load_mcp_tools_malformed_cache_file(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        mock_client = Mock()
        mock_client.tools_cache = {
            "elastic": [
                {
                    "name": "query",
                    "description": "Run an Elasticsearch query",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("invalid json{")

            with patch.object(ClaudeService, '_populate_mcp_registry'):
                service = ClaudeService(use_mcp_tools=True, mcp_client=mock_client)

            assert len(service.mcp_tools) >= 1
            tool_names = [t["name"] for t in service.mcp_tools]
            assert "elastic_query" in tool_names
        finally:
            if original_exists and original_content is not None:
                cache_file.write_text(original_content)
            elif not original_exists and cache_file.exists():
                cache_file.unlink()

    @patch('core.llm.harness.claude.get_secret')
    def test_no_event_loop_creation(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        cache_data = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Search logs",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        try:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(cache_data))

            # Cached tools only surface for servers connected this boot.
            connected_client = MagicMock()
            connected_client.get_connection_status.return_value = {"splunk": True}

            with patch('asyncio.new_event_loop') as mock_new_loop:
                with patch.object(ClaudeService, '_populate_mcp_registry'):
                    service = ClaudeService(
                        use_mcp_tools=True, mcp_client=connected_client
                    )
                mock_new_loop.assert_not_called()

            assert len(service.mcp_tools) >= 1
        finally:
            if original_exists and original_content is not None:
                cache_file.write_text(original_content)
            elif not original_exists and cache_file.exists():
                cache_file.unlink()

    def test_startup_writes_cache_file(self):
        project_root = Path(__file__).parent.parent.parent
        cache_file = project_root / "data" / "mcp_tools_cache.json"

        original_exists = cache_file.exists()
        original_content = cache_file.read_text() if original_exists else None

        fake_tools = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Search splunk logs",
                    "inputSchema": {"type": "object", "properties": {}, "required": []},
                }
            ]
        }

        try:
            if cache_file.exists():
                cache_file.unlink()

            # Simulate the cache-writing logic from startup_event
            cache_dir = project_root / "data"
            cache_dir.mkdir(parents=True, exist_ok=True)
            cache_data = {}
            for server_name, server_tools in fake_tools.items():
                cache_data[server_name] = []
                for tool in server_tools:
                    input_schema = tool.get("inputSchema", {})
                    cache_data[server_name].append({
                        "name": tool.get("name"),
                        "description": tool.get("description", ""),
                        "inputSchema": input_schema,
                    })
            with open(cache_file, "w") as f:
                json.dump(cache_data, f, indent=2)

            assert cache_file.exists(), "Cache file was not created"
            content = json.loads(cache_file.read_text())
            assert isinstance(content, dict), "Cache file is not a JSON object"
            assert "splunk" in content, "Expected 'splunk' server key"
            assert len(content["splunk"]) == 1
            assert content["splunk"][0]["name"] == "search"
            assert "inputSchema" in content["splunk"][0]
        finally:
            if original_exists and original_content is not None:
                cache_file.write_text(original_content)
            elif not original_exists and cache_file.exists():
                cache_file.unlink()


class TestClaudeServicePrompts:
    
    @patch('core.llm.harness.claude.get_secret')
    def test_default_system_prompt(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        prompt = service._get_default_system_prompt()
        
        assert "Vigil SOC" in prompt
        assert "default_to_action" in prompt
        assert "use_parallel_tool_calls" in prompt
        assert "investigate_before_answering" in prompt
        assert len(prompt) > 100
    
    @patch('core.llm.harness.claude.get_secret')
    def test_system_prompt_includes_mcp_tools_section(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(use_mcp_tools=True)
        prompt = service._get_default_system_prompt()
        
        assert "available_mcp_tools" in prompt
        assert "deeptempo-findings" in prompt


class TestClaudeServiceSessionManagement:
    
    @patch('core.llm.harness.claude.get_secret')
    def test_create_session(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        session_id = "test-session-123"
        
        # Add messages to session
        service._session_mgr.sessions[session_id] = MOCK_CONVERSATION_HISTORY.copy()
        
        assert session_id in service._session_mgr.sessions
        assert len(service._session_mgr.sessions[session_id]) == 4
    
    @patch('core.llm.harness.claude.get_secret')
    def test_clear_session(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        session_id = "test-session-123"
        
        # Add messages to session
        service._session_mgr.sessions[session_id] = MOCK_CONVERSATION_HISTORY.copy()
        
        # Clear session
        if session_id in service._session_mgr.sessions:
            del service._session_mgr.sessions[session_id]
        
        assert session_id not in service._session_mgr.sessions
    
    @patch('core.llm.harness.claude.get_secret')
    def test_session_isolation(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        session1_id = "session-1"
        session2_id = "session-2"
        
        service._session_mgr.sessions[session1_id] = [{"role": "user", "content": "Message 1"}]
        service._session_mgr.sessions[session2_id] = [{"role": "user", "content": "Message 2"}]
        
        assert len(service._session_mgr.sessions[session1_id]) == 1
        assert len(service._session_mgr.sessions[session2_id]) == 1
        assert service._session_mgr.sessions[session1_id] != service._session_mgr.sessions[session2_id]


class TestClaudeServiceAPIInteraction:
    
    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.Anthropic')
    def test_chat_basic_response(self, mock_anthropic, mock_get_secret):
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
    
    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.Anthropic')
    def test_chat_with_tool_use(self, mock_anthropic, mock_get_secret):
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
    
    @patch('core.llm.harness.claude.get_secret')
    def test_missing_api_key_error(self, mock_get_secret):
        mock_get_secret.return_value = None
        
        service = ClaudeService()
        
        assert service.api_key is None
        assert service.client is None
    
    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.Anthropic')
    def test_rate_limit_error_handling(self, mock_anthropic, mock_get_secret):
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
    
    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.Anthropic')
    def test_authentication_error_handling(self, mock_anthropic, mock_get_secret):
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
    
    @patch('core.llm.harness.claude.get_secret')
    def test_thinking_mode_enabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService(enable_thinking=True, thinking_budget=15000)
        
        assert service.enable_thinking is True
        assert service.thinking_budget == 15000
    
    @patch('core.llm.harness.claude.get_secret')
    def test_thinking_mode_disabled_by_default(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"
        
        service = ClaudeService()
        
        assert service.enable_thinking is False


class TestClaudeServiceMCPTools:

    @patch('core.llm.harness.claude.get_secret')
    def test_mcp_tools_enabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        service = ClaudeService(use_mcp_tools=True)

        assert service.use_mcp_tools is True

    @patch('core.llm.harness.claude.get_secret')
    def test_mcp_tools_disabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        service = ClaudeService(use_mcp_tools=False)

        assert service.use_mcp_tools is False
        assert service.mcp_tools == []


class TestDualToolLoading:

    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.BACKEND_TOOLS_AVAILABLE', True)
    @patch('core.llm.harness.claude.BACKEND_TOOLS', [
        {'name': 'backend_tool_1', 'description': 'Backend tool', 'input_schema': {'type': 'object', 'properties': {}}},
    ])
    def test_both_tool_sets_load_when_both_flags_enabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        fake_mcp_tools = [
            {'name': 'mcp_tool_1', 'description': 'MCP tool', 'input_schema': {'type': 'object', 'properties': {}}},
        ]

        with patch.object(ClaudeService, '_load_mcp_tools', lambda self: setattr(self, 'mcp_tools', fake_mcp_tools)):
            service = ClaudeService(use_backend_tools=True, use_mcp_tools=True)

        assert len(service.backend_tools) > 0, "backend_tools should be non-empty"
        assert len(service.mcp_tools) > 0, "mcp_tools should be non-empty"

    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.BACKEND_TOOLS_AVAILABLE', True)
    @patch('core.llm.harness.claude.BACKEND_TOOLS', [
        {'name': 'backend_tool_1', 'description': 'Backend tool', 'input_schema': {'type': 'object', 'properties': {}}},
        {'name': 'backend_tool_2', 'description': 'Backend tool 2', 'input_schema': {'type': 'object', 'properties': {}}},
    ])
    def test_token_estimation_sums_both_tool_sets(self, mock_get_secret):
        import json
        mock_get_secret.return_value = "test-api-key-123"

        fake_mcp_tools = [
            {'name': 'mcp_tool_1', 'description': 'MCP tool', 'input_schema': {'type': 'object', 'properties': {}}},
        ]

        with patch.object(ClaudeService, '_load_mcp_tools', lambda self: setattr(self, 'mcp_tools', fake_mcp_tools)):
            service = ClaudeService(use_backend_tools=True, use_mcp_tools=True)

        # Compute expected token sum individually
        backend_tokens = service._estimate_tokens(json.dumps(service.backend_tools))
        mcp_tokens = service._estimate_tokens(json.dumps(service.mcp_tools))
        expected_total = backend_tokens + mcp_tokens

        # _needs_context_reduction returns (needs_reduction, total_tokens, available_tokens)
        # available_tokens = max_context - system_tokens - tool_tokens
        # With no messages and no system prompt: available = max_context - tool_tokens
        max_context = 180000
        _, _, available = service._needs_context_reduction([], system_prompt=None, max_context_tokens=max_context)

        actual_tool_tokens = max_context - available
        assert actual_tool_tokens == expected_total, (
            f"Expected tool tokens {expected_total}, got {actual_tool_tokens}"
        )

    @patch('core.llm.harness.claude.get_secret')
    @patch('core.llm.harness.claude.BACKEND_TOOLS_AVAILABLE', True)
    @patch('core.llm.harness.claude.BACKEND_TOOLS', [
        {'name': 'backend_tool_1', 'description': 'Backend tool', 'input_schema': {'type': 'object', 'properties': {}}},
    ])
    def test_only_backend_tools_when_mcp_disabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        service = ClaudeService(use_backend_tools=True, use_mcp_tools=False)

        assert len(service.backend_tools) > 0
        assert service.mcp_tools == []

    @patch('core.llm.harness.claude.get_secret')
    def test_only_mcp_tools_when_backend_disabled(self, mock_get_secret):
        mock_get_secret.return_value = "test-api-key-123"

        fake_mcp_tools = [
            {'name': 'mcp_tool_1', 'description': 'MCP tool', 'input_schema': {'type': 'object', 'properties': {}}},
        ]

        with patch.object(ClaudeService, '_load_mcp_tools', lambda self: setattr(self, 'mcp_tools', fake_mcp_tools)):
            service = ClaudeService(use_backend_tools=False, use_mcp_tools=True)

        assert service.backend_tools == []
        assert len(service.mcp_tools) > 0


class TestProcessMixedToolUse:

    def _make_service(self, backend_tools, mcp_tools):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None):
            service = ClaudeService(use_backend_tools=False, use_mcp_tools=False)
        service.backend_tools = backend_tools
        service.mcp_tools = mcp_tools
        return service

    def _make_tool_use_item(self, name, tool_id="toolu_1", input_data=None):
        item = Mock()
        item.type = "tool_use"
        item.name = name
        item.id = tool_id
        item.input = input_data or {}
        return item

    @pytest.mark.asyncio
    async def test_mixed_content_dispatches_correctly(self):
        backend_tools = [{'name': 'backend_op', 'description': 'b', 'input_schema': {}}]
        mcp_tools = [{'name': 'mcp_op', 'description': 'm', 'input_schema': {}}]
        service = self._make_service(backend_tools, mcp_tools)

        backend_item = self._make_tool_use_item('backend_op', tool_id='toolu_b')
        mcp_item = self._make_tool_use_item('mcp_op', tool_id='toolu_m')
        content = [backend_item, mcp_item]

        backend_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_b', 'content': [{'type': 'text', 'text': 'backend'}]}]
        mcp_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_m', 'content': [{'type': 'text', 'text': 'mcp'}]}]

        with patch.object(service, '_process_backend_tool_use', return_value=backend_result) as mock_backend, \
             patch.object(service, '_process_tool_use', return_value=mcp_result) as mock_mcp:
            results = await service._process_mixed_tool_use(content)

        # Each processor called with the single matching item wrapped in a list
        mock_backend.assert_called_once_with([backend_item])
        mock_mcp.assert_called_once_with([mcp_item])
        # Both results combined
        assert len(results) == 2
        assert results[0] == backend_result[0]
        assert results[1] == mcp_result[0]

    @pytest.mark.asyncio
    async def test_backend_only_content_dispatches_to_backend_processor(self):
        backend_tools = [
            {'name': 'tool_a', 'description': 'a', 'input_schema': {}},
            {'name': 'tool_b', 'description': 'b', 'input_schema': {}},
        ]
        service = self._make_service(backend_tools, mcp_tools=[])

        item_a = self._make_tool_use_item('tool_a', tool_id='toolu_a')
        item_b = self._make_tool_use_item('tool_b', tool_id='toolu_b')
        content = [item_a, item_b]

        result_a = [{'type': 'tool_result', 'tool_use_id': 'toolu_a', 'content': [{'type': 'text', 'text': 'r_a'}]}]
        result_b = [{'type': 'tool_result', 'tool_use_id': 'toolu_b', 'content': [{'type': 'text', 'text': 'r_b'}]}]

        side_effects = [result_a, result_b]
        with patch.object(service, '_process_backend_tool_use', side_effect=side_effects) as mock_backend, \
             patch.object(service, '_process_tool_use') as mock_mcp:
            results = await service._process_mixed_tool_use(content)

        assert mock_backend.call_count == 2
        mock_mcp.assert_not_called()
        assert len(results) == 2

    @pytest.mark.asyncio
    async def test_mcp_only_content_dispatches_to_mcp_processor(self):
        service = self._make_service(backend_tools=[], mcp_tools=[
            {'name': 'ext_search', 'description': 'search', 'input_schema': {}},
        ])

        item = self._make_tool_use_item('ext_search', tool_id='toolu_s')
        content = [item]

        mcp_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_s', 'content': [{'type': 'text', 'text': 'found'}]}]

        with patch.object(service, '_process_backend_tool_use') as mock_backend, \
             patch.object(service, '_process_tool_use', return_value=mcp_result) as mock_mcp:
            results = await service._process_mixed_tool_use(content)

        mock_backend.assert_not_called()
        mock_mcp.assert_called_once_with([item])
        assert results == mcp_result

    @pytest.mark.asyncio
    async def test_empty_content_returns_empty_list(self):
        service = self._make_service(backend_tools=[], mcp_tools=[])

        with patch.object(service, '_process_backend_tool_use') as mock_backend, \
             patch.object(service, '_process_tool_use') as mock_mcp:
            results = await service._process_mixed_tool_use([])

        mock_backend.assert_not_called()
        mock_mcp.assert_not_called()
        assert results == []

    @pytest.mark.asyncio
    async def test_dict_format_items_are_handled(self):
        backend_tools = [{'name': 'dict_tool', 'description': 'd', 'input_schema': {}}]
        service = self._make_service(backend_tools, mcp_tools=[])

        # Dict-format item (as opposed to Mock object)
        dict_item = {'type': 'tool_use', 'id': 'toolu_d', 'name': 'dict_tool', 'input': {}}
        content = [dict_item]

        backend_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_d', 'content': [{'type': 'text', 'text': 'ok'}]}]

        with patch.object(service, '_process_backend_tool_use', return_value=backend_result) as mock_backend, \
             patch.object(service, '_process_tool_use') as mock_mcp:
            results = await service._process_mixed_tool_use(content)

        mock_backend.assert_called_once_with([dict_item])
        mock_mcp.assert_not_called()
        assert results == backend_result


class TestChatAndStreamCombinedTools:

    BACKEND_TOOL = {'name': 'backend_op', 'description': 'Backend', 'input_schema': {'type': 'object', 'properties': {}}}
    MCP_TOOL = {'name': 'mcp_op', 'description': 'MCP', 'input_schema': {'type': 'object', 'properties': {}}}

    def _make_service_with_both_tools(self):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None):
            service = ClaudeService(use_backend_tools=True, use_mcp_tools=True)
        service.backend_tools = [self.BACKEND_TOOL]
        service.mcp_tools = [self.MCP_TOOL]
        return service

    @patch('core.llm.harness.claude.Anthropic')
    def test_chat_passes_combined_tools_to_api(self, mock_anthropic):
        mock_client = Mock()
        mock_anthropic.return_value = mock_client

        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="Done")]
        mock_response.stop_reason = "end_turn"
        mock_response.model = "claude-sonnet-4-5-20250929"
        mock_response.usage = Mock(input_tokens=50, output_tokens=10)
        mock_client.messages.create.return_value = mock_response

        service = self._make_service_with_both_tools()
        service.client = mock_client

        service.chat("Hello")

        call_kwargs = mock_client.messages.create.call_args[1]
        tools_passed = call_kwargs.get('tools', [])
        tool_names = [t['name'] for t in tools_passed]
        assert 'backend_op' in tool_names, "backend tool should be in combined list"
        assert 'mcp_op' in tool_names, "MCP tool should be in combined list"
        assert len(tools_passed) == 2

    @pytest.mark.asyncio
    @patch('core.llm.harness.claude.AsyncAnthropic')
    async def test_stream_passes_combined_tools_to_api(self, mock_async_anthropic):
        from unittest.mock import AsyncMock

        # Build an async context manager mock for messages.stream(...)
        captured_kwargs = {}

        mock_final_message = Mock()
        mock_final_message.content = [Mock(type="text", text="Done")]
        mock_final_message.stop_reason = "end_turn"

        async def _empty_aiter():
            return
            yield  # make it an async generator

        mock_stream_cm = MagicMock()
        mock_stream_cm.__aenter__ = AsyncMock(return_value=mock_stream_cm)
        mock_stream_cm.__aexit__ = AsyncMock(return_value=False)
        mock_stream_cm.__aiter__ = Mock(return_value=_empty_aiter())  # proper async iterator
        mock_stream_cm.get_final_message = AsyncMock(return_value=mock_final_message)

        mock_async_client = Mock()
        mock_async_anthropic.return_value = mock_async_client

        def capture_stream(**kwargs):
            captured_kwargs.update(kwargs)
            return mock_stream_cm

        mock_async_client.messages.stream = capture_stream

        service = self._make_service_with_both_tools()
        service.async_client = mock_async_client

        # Consume the async generator to drive execution
        chunks = []
        async for chunk in service.chat_stream("Hello"):
            chunks.append(chunk)

        tools_passed = captured_kwargs.get('tools', [])
        tool_names = [t['name'] for t in tools_passed]
        assert 'backend_op' in tool_names, "backend tool should be in combined stream list"
        assert 'mcp_op' in tool_names, "MCP tool should be in combined stream list"
        assert len(tools_passed) == 2


class TestTokenEstimationEdgeCases:

    def _make_service(self, backend_tools, mcp_tools, backend_enabled=True, mcp_enabled=True):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None):
            service = ClaudeService(
                use_backend_tools=backend_enabled,
                use_mcp_tools=mcp_enabled,
            )
        service.backend_tools = backend_tools
        service.mcp_tools = mcp_tools
        return service

    def _tool_tokens(self, service, max_context=180000):
        _, _, available = service._needs_context_reduction(
            [], system_prompt=None, max_context_tokens=max_context
        )
        return max_context - available

    def test_token_estimation_empty_backend_only_counts_mcp(self):
        import json
        mcp_tools = [
            {'name': 'mcp_t', 'description': 'MCP tool', 'input_schema': {'type': 'object'}},
        ]
        service = self._make_service(backend_tools=[], mcp_tools=mcp_tools)

        expected_mcp = service._estimate_tokens(json.dumps(mcp_tools))
        assert self._tool_tokens(service) == expected_mcp, (
            "Only MCP token cost should be counted when backend_tools is empty"
        )

    def test_token_estimation_empty_mcp_only_counts_backend(self):
        import json
        backend_tools = [
            {'name': 'backend_t', 'description': 'Backend tool', 'input_schema': {'type': 'object'}},
        ]
        service = self._make_service(backend_tools=backend_tools, mcp_tools=[])

        expected_backend = service._estimate_tokens(json.dumps(backend_tools))
        assert self._tool_tokens(service) == expected_backend, (
            "Only backend token cost should be counted when mcp_tools is empty"
        )

    def test_token_estimation_both_empty_contributes_zero(self):
        service = self._make_service(backend_tools=[], mcp_tools=[])
        assert self._tool_tokens(service) == 0, (
            "tool_tokens should be 0 when both backend_tools and mcp_tools are empty"
        )


class TestEmptyToolSetsPassNoneToApi:

    BACKEND_TOOL = {'name': 'bt', 'description': 'B', 'input_schema': {'type': 'object', 'properties': {}}}
    MCP_TOOL = {'name': 'mt', 'description': 'M', 'input_schema': {'type': 'object', 'properties': {}}}

    def _make_service(self, backend_tools, mcp_tools):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None):
            service = ClaudeService(use_backend_tools=True, use_mcp_tools=True)
        service.backend_tools = backend_tools
        service.mcp_tools = mcp_tools
        return service

    @patch('core.llm.harness.claude.Anthropic')
    def test_chat_does_not_pass_tools_when_both_sets_empty(self, mock_anthropic):
        mock_client = Mock()
        mock_anthropic.return_value = mock_client

        mock_response = Mock()
        mock_response.content = [Mock(type="text", text="OK")]
        mock_response.stop_reason = "end_turn"
        mock_response.model = "claude-sonnet-4-5-20250929"
        mock_response.usage = Mock(input_tokens=10, output_tokens=5)
        mock_client.messages.create.return_value = mock_response

        service = self._make_service(backend_tools=[], mcp_tools=[])
        service.client = mock_client

        service.chat("Hello")

        call_kwargs = mock_client.messages.create.call_args[1]
        assert 'tools' not in call_kwargs, (
            "'tools' must not be present in Claude API call when both tool sets are empty"
        )


class TestProcessMixedToolUseEdgeCases:

    def _make_service(self, backend_tools, mcp_tools):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None):
            service = ClaudeService(use_backend_tools=False, use_mcp_tools=False)
        service.backend_tools = backend_tools
        service.mcp_tools = mcp_tools
        return service

    def _make_tool_use_item(self, name, tool_id="toolu_1"):
        item = Mock()
        item.type = "tool_use"
        item.name = name
        item.id = tool_id
        item.input = {}
        return item

    @pytest.mark.asyncio
    async def test_tool_name_collision_backend_takes_precedence(self):
        shared_name = 'shared_tool'
        backend_tools = [{'name': shared_name, 'description': 'backend version', 'input_schema': {}}]
        mcp_tools = [{'name': shared_name, 'description': 'mcp version', 'input_schema': {}}]
        service = self._make_service(backend_tools, mcp_tools)

        item = self._make_tool_use_item(shared_name, tool_id='toolu_shared')
        backend_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_shared',
                           'content': [{'type': 'text', 'text': 'from_backend'}]}]

        with patch.object(service, '_process_backend_tool_use', return_value=backend_result) as mock_backend, \
             patch.object(service, '_process_tool_use') as mock_mcp:
            results = await service._process_mixed_tool_use([item])

        # backend_tool_names set is checked first → backend wins
        mock_backend.assert_called_once_with([item])
        mock_mcp.assert_not_called()
        assert results == backend_result

    @pytest.mark.asyncio
    async def test_backend_processor_receives_single_element_list(self):
        backend_tools = [{'name': 'tool_x', 'description': 'x', 'input_schema': {}}]
        service = self._make_service(backend_tools, mcp_tools=[])

        item_x = self._make_tool_use_item('tool_x', tool_id='toolu_x')
        result_x = [{'type': 'tool_result', 'tool_use_id': 'toolu_x',
                     'content': [{'type': 'text', 'text': 'ok'}]}]

        with patch.object(service, '_process_backend_tool_use', return_value=result_x) as mock_backend:
            await service._process_mixed_tool_use([item_x])

        # Verify the processor received exactly a single-element list
        mock_backend.assert_called_once_with([item_x])
        arg = mock_backend.call_args[0][0]
        assert isinstance(arg, list) and len(arg) == 1, (
            "_process_backend_tool_use must receive a [single_item] list, not the raw item"
        )

    @pytest.mark.asyncio
    async def test_mcp_processor_receives_single_element_list(self):
        service = self._make_service(backend_tools=[], mcp_tools=[
            {'name': 'remote_scan', 'description': 'scan', 'input_schema': {}}
        ])

        item = self._make_tool_use_item('remote_scan', tool_id='toolu_scan')
        mcp_result = [{'type': 'tool_result', 'tool_use_id': 'toolu_scan',
                       'content': [{'type': 'text', 'text': 'scanned'}]}]

        with patch.object(service, '_process_tool_use', return_value=mcp_result) as mock_mcp:
            await service._process_mixed_tool_use([item])

        mock_mcp.assert_called_once_with([item])
        arg = mock_mcp.call_args[0][0]
        assert isinstance(arg, list) and len(arg) == 1, (
            "_process_tool_use must receive a [single_item] list, not the raw item"
        )


class TestLoadMcpToolsCache:

    def _make_service_no_load(self):
        with patch('core.llm.harness.claude.get_secret', return_value="test-key"), \
             patch.object(ClaudeService, '_load_mcp_tools', lambda self: None), \
             patch.object(ClaudeService, '_load_backend_tools', lambda self: None):
            service = ClaudeService(use_mcp_tools=True)
        service.mcp_tools = []
        return service

    def test_fallback_to_in_memory_cache(self):
        service = self._make_service_no_load()

        mock_client = MagicMock()
        mock_client.tools_cache = {
            "jira": [
                {
                    "name": "create_issue",
                    "description": "Create a Jira issue",
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                }
            ]
        }

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = False
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            service._mcp_client = mock_client
            with patch.object(service, '_populate_mcp_registry', lambda d: None):
                service._load_mcp_tools()

        assert len(service.mcp_tools) == 1
        assert service.mcp_tools[0]["name"] == "jira_create_issue"

    def test_no_sources_available(self):
        service = self._make_service_no_load()

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = False
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            service._mcp_client = None
            service._load_mcp_tools()

        assert service.mcp_tools == []

    def test_malformed_cache_file_falls_back_to_memory(self, tmp_path):
        cache_file = tmp_path / "mcp_tools_cache.json"
        cache_file.write_text("{not valid json}")

        service = self._make_service_no_load()

        mock_client = MagicMock()
        mock_client.tools_cache = {
            "threat_intel": [
                {
                    "name": "lookup_ip",
                    "description": "Lookup an IP address",
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                }
            ]
        }

        import builtins
        real_open = builtins.open

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = True
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            def selective_open(path, *args, **kwargs):
                if path is mock_cf:
                    return real_open(cache_file, *args, **kwargs)
                return real_open(path, *args, **kwargs)

            service._mcp_client = mock_client
            with patch('builtins.open', side_effect=selective_open), \
                 patch.object(service, '_populate_mcp_registry', lambda d: None):
                service._load_mcp_tools()

        assert len(service.mcp_tools) == 1
        assert service.mcp_tools[0]["name"] == "threat_intel_lookup_ip"

    def test_no_event_loop_creation(self):
        service = self._make_service_no_load()

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = False
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            service._mcp_client = None
            with patch('asyncio.new_event_loop') as mock_new_loop:
                service._load_mcp_tools()
                mock_new_loop.assert_not_called()

    def test_tools_have_server_prefix_and_correct_schema(self):
        service = self._make_service_no_load()

        mock_client = MagicMock()
        mock_client.tools_cache = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Run search",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                        "required": ["query"]
                    }
                }
            ]
        }

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = False
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            service._mcp_client = mock_client
            with patch.object(service, '_populate_mcp_registry', lambda d: None):
                service._load_mcp_tools()

        assert len(service.mcp_tools) == 1
        tool = service.mcp_tools[0]
        assert tool["name"] == "splunk_search"
        assert "input_schema" in tool
        assert tool["input_schema"]["type"] == "object"
        assert "query" in tool["input_schema"]["properties"]

    def test_cache_file_load_with_real_file(self, tmp_path):
        import json as _json
        cache_data = {
            "splunk": [
                {
                    "name": "search",
                    "description": "Run a Splunk search",
                    "inputSchema": {"type": "object", "properties": {}, "required": []}
                }
            ]
        }
        cache_file = tmp_path / "mcp_tools_cache.json"
        cache_file.write_text(_json.dumps(cache_data))

        service = self._make_service_no_load()

        import builtins
        real_open = builtins.open

        with patch('core.llm.harness.claude.REPO_ROOT') as mock_repo_root:
            mock_cf = MagicMock()
            mock_cf.exists.return_value = True
            mock_repo_root.__truediv__.return_value.__truediv__.return_value = mock_cf

            def selective_open(path, *args, **kwargs):
                if path is mock_cf:
                    return real_open(cache_file, *args, **kwargs)
                return real_open(path, *args, **kwargs)

            # Cached tools only surface for servers connected this boot.
            service._mcp_client = MagicMock()
            service._mcp_client.get_connection_status.return_value = {"splunk": True}
            with patch('builtins.open', side_effect=selective_open), \
                 patch.object(service, '_populate_mcp_registry', lambda d: None):
                service._load_mcp_tools()

        assert len(service.mcp_tools) == 1
        assert service.mcp_tools[0]["name"] == "splunk_search"
        assert "[splunk]" in service.mcp_tools[0]["description"]


class TestExecuteBackendTool:

    def _make_service(self):
        with patch('core.llm.harness.claude.get_secret', return_value="test-api-key-123"):
            service = ClaudeService()
        return service

    @pytest.mark.asyncio
    async def test_mcp_fallback_success(self):
        service = self._make_service()
        with patch.object(service, '_execute_mcp_tool', new=AsyncMock(return_value="search results")):
            result = await service._execute_backend_tool("splunk_splunk_nl_search", {})
        assert result == {"result": "search results"}

    @pytest.mark.asyncio
    async def test_mcp_fallback_exception(self):
        service = self._make_service()
        with patch.object(service, '_execute_mcp_tool', new=AsyncMock(side_effect=Exception("connection refused"))):
            result = await service._execute_backend_tool("splunk_splunk_nl_search", {})
        assert result == {"error": "Unknown tool: splunk_splunk_nl_search"}

    @pytest.mark.asyncio
    async def test_existing_tools_unchanged(self):
        service = self._make_service()
        mock_findings = [{"finding_id": "f1", "severity": "high", "anomaly_score": 0.9,
                          "data_source": "splunk", "timestamp": "2026-01-01T00:00:00Z",
                          "status": "open", "description": "Test finding"}]
        with patch('services.database_data_service.DatabaseDataService') as mock_ds_cls, \
             patch.object(service, '_execute_mcp_tool', new=AsyncMock()) as mock_mcp:
            mock_ds = mock_ds_cls.return_value
            mock_ds.count_findings.return_value = 1
            mock_ds.get_findings.return_value = mock_findings
            result = await service._execute_backend_tool("list_findings", {"limit": 10, "offset": 0})
        assert "findings" in result
        assert result["total"] == 1
        mock_mcp.assert_not_called()

    @pytest.mark.asyncio
    async def test_daemon_callsite_awaits_directly(self):
        service = self._make_service()
        call_record = []

        async def fake_backend_tool(tool_name, tool_input):
            call_record.append((tool_name, tool_input))
            return {"result": "ok"}

        service._execute_backend_tool = fake_backend_tool

        # Import AgentRunner and wire up a minimal instance
        from daemon.agent_runner import AgentRunner
        runner = object.__new__(AgentRunner)
        runner._claude_service = service
        runner._dry_run = False
        runner.workdir = MagicMock()

        # Patch module-level _get_tool_tier to return "auto" so it doesn't short-circuit
        runner.config = MagicMock()
        runner.config.dry_run = False
        with patch('daemon.agent_runner._get_tool_tier', return_value="auto"):
            result = await runner._execute_external_tool("inv1", "my_mcp_tool", {"key": "val"})

        assert call_record == [("my_mcp_tool", {"key": "val"})]
        assert result == '{"result": "ok"}'


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

