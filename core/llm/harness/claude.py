"""Claude API service for Anthropic integration."""

import json
import logging
import time
import uuid
from pathlib import Path  # noqa: F401 — patched as ``claude.Path`` in tests
from typing import Any, Dict, List, Optional, Union

from core.llm.defaults import DEFAULT_MODEL
from core.secrets import get_secret

# Import backend tool support
try:
    from core.llm.tool_schemas import ALL_TOOLS as BACKEND_TOOLS

    BACKEND_TOOLS_AVAILABLE = True
except ImportError as e:
    logging.warning(f"Backend tools not available: {e}")
    BACKEND_TOOLS = []
    BACKEND_TOOLS_AVAILABLE = False

try:
    # Anthropic imports are retained for type references and the Bifrost-routed
    # client helpers imported just below. Direct construction happens through
    # `create_anthropic_client` / `create_async_anthropic_client` in
    # core.llm.providers.clients so every Anthropic call flows through Bifrost (GH #84).
    from anthropic import Anthropic, AsyncAnthropic  # noqa: F401

    from core.llm.providers.clients import (
        create_anthropic_client,
        create_async_anthropic_client,
    )

    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

# OTEL instrumentation (lazy to avoid hard dependency)
try:
    from core.telemetry import create_genai_metrics, get_meter, get_tracer

    _cs_tracer = get_tracer("vigil.services.claude")
    _cs_meter = get_meter("vigil.services.claude")
    _cs_genai_metrics = create_genai_metrics(_cs_meter)
    _OTEL_CS_AVAILABLE = True
except Exception:
    _OTEL_CS_AVAILABLE = False

logger = logging.getLogger(__name__)

from core.detections.detection_rules_service import DetectionRulesService  # noqa: E402
from core.integrations.mcp.client import process_mcp_client  # noqa: E402
from core.integrations.mcp.registry import MCPRegistry  # noqa: E402


class ClaudeService:
    """Service for interacting with Claude API."""

    SERVICE_NAME = "deeptempo-ai-soc"
    API_KEY_NAME = "claude_api_key"

    def __init__(
        self,
        enable_thinking: bool = False,
        thinking_budget: int = 10000,
        provider_api_key_ref: Optional[str] = None,
        mcp_client=None,
        mcp_registry: Optional[MCPRegistry] = None,
        detection_rules: Optional[DetectionRulesService] = None,
    ):
        """One completion at a time. Tools and the loop live in the agent layer.

        Args:
            enable_thinking: Retained for callers; the one-shot sends no thinking.
            thinking_budget: Likewise.
            provider_api_key_ref: Optional secret-manager key for a non-default
                Anthropic provider row (GH #88). When set, _load_api_key reads
                this secret first before the legacy CLAUDE_API_KEY fallback chain.
        """
        self._mcp_client = (
            mcp_client if mcp_client is not None else process_mcp_client()
        )
        self._mcp_registry = mcp_registry or MCPRegistry()
        self._detection_rules = detection_rules or DetectionRulesService()

        self.client: Optional[Anthropic] = None
        self.async_client: Optional[AsyncAnthropic] = None
        self.api_key: Optional[str] = None
        self.provider_api_key_ref = provider_api_key_ref
        self.enable_thinking = enable_thinking
        self.thinking_budget = thinking_budget

        self.default_system_prompt = self._get_default_system_prompt()
        self._load_api_key()

    def _get_default_system_prompt(self) -> str:
        """Get default system prompt with Claude 4.5 best practices."""
        return """You are Claude, an AI assistant for security operations and analysis in the Vigil SOC platform.

<default_to_action>
By default, implement changes rather than only suggesting them. If the user's intent is unclear, infer the most useful likely action and proceed, using tools to discover any missing details instead of guessing. Try to infer the user's intent about whether a tool call (e.g., file edit or read) is intended or not, and act accordingly.
</default_to_action>

<use_parallel_tool_calls>
If you intend to call multiple tools and there are no dependencies between the tool calls, make all of the independent tool calls in parallel. Prioritize calling tools simultaneously whenever the actions can be done in parallel rather than sequentially. For example, when reading 3 files, run 3 tool calls in parallel to read all 3 files into context at the same time. Maximize use of parallel tool calls where possible to increase speed and efficiency. However, if some tool calls depend on previous calls to inform dependent values like the parameters, do NOT call these tools in parallel and instead call them sequentially. Never use placeholders or guess missing parameters in tool calls.
</use_parallel_tool_calls>

<investigate_before_answering>
Never speculate about data you have not retrieved. If the user references a specific finding, case, or other security entity, you MUST use the appropriate MCP tool to fetch it before answering. Make sure to investigate and retrieve relevant data BEFORE answering questions. Never make any claims about security data before investigating - give grounded and hallucination-free answers.
</investigate_before_answering>

<available_mcp_tools>
You have access to MCP (Model Context Protocol) tools that connect to various security platforms and data sources. The tools are prefixed with the server name (e.g., "deeptempo-findings_get_finding"). Use these tools to:

1. **Findings & Cases**: Retrieve and analyze security findings and cases from DeepTempo
   - Finding IDs start with "f-" (e.g., "f-20260109-40d9379b")
   - Case IDs start with "case-" (e.g., "case-20260114-a1b2c3d4")
   - Use deeptempo-findings server tools: list_findings, get_finding, list_cases, get_case, create_case, update_case, list_completed_hunts

2. **Security Integrations**: Query data from various security platforms
   - The available integrations are dynamically loaded based on what's configured
   - Tools are named with the pattern: {integration-name}_{tool-name}
   - Check your available tools to see which integrations are active

3. **Threat Intelligence**: Analyze indicators, URLs, files, etc.
   - Use tools for VirusTotal, Shodan, AnyRun, Hybrid Analysis, etc. (if available)
   - These help enrich findings with external context

4. **Investigation Workflows**: Execute predefined investigation workflows
   - Automate common SOC investigation patterns
   - Use tempo_flow_server tools for workflows

5. **MITRE ATT&CK Analysis**: Analyze and visualize attack techniques
   - Use attack-layer server tools: get_technique_rollup, get_findings_by_technique, create_attack_layer
   - Generate ATT&CK Navigator layers for visualization

When a user mentions an ID or entity (finding, case, IP, hash, domain), ALWAYS use the appropriate MCP tool to retrieve it first. Never try to access these as files - they are stored in databases and accessed via MCP tools.
</available_mcp_tools>

<recognizing_security_entities>
Common patterns you should recognize and how to handle them:

- Finding IDs: "f-YYYYMMDD-XXXXXXXX" → Use deeptempo-findings_get_finding tool
- Case IDs: "case-YYYYMMDD-XXXXXXXX" → Use deeptempo-findings_get_case tool
- IP addresses: X.X.X.X → Consider using IP geolocation or threat intel tools
- Domain names: example.com → Consider using URL analysis or threat intel tools
- File hashes: MD5/SHA1/SHA256 → Consider using malware analysis tools
- URLs: http(s)://... → Consider using URL analysis tools

IMPORTANT: When a user says "analyze [ID]", "check [ID]", "investigate [ID]", etc., your FIRST action should ALWAYS be to use the appropriate MCP tool to fetch that entity's data.
</recognizing_security_entities>

<security_analysis_workflow>
When analyzing security findings and cases:
1. **Retrieve**: Use MCP tools to fetch the finding/case data first
2. **Understand**: Parse the severity, data source, MITRE techniques, and context
3. **Correlate**: Look for related findings or patterns using similarity/correlation tools
4. **Enrich**: Use threat intelligence tools to add external context
5. **Analyze**: Provide clear assessment of the threat, impact, and recommended actions
6. **Act**: Be thorough but efficient - prioritize actionable insights
</security_analysis_workflow>

<case_management_capabilities>
You have comprehensive tools to manage ALL aspects of cases during investigations:

**1. FINDINGS MANAGEMENT**
- Add single/multiple findings to cases
- Remove findings from cases
- Track why findings were added

**2. ACTIVITIES & NOTES**
- Log investigation activities automatically
- Activity types: note, action_taken, investigation_step, analysis, communication, task_update
- Track all investigation actions

**3. TIMELINE & KILL CHAIN**
- Build chronological attack timelines
- Tag MITRE ATT&CK techniques
- Document attack progression stages
- Create structured kill chain cases

**4. COMMENTS & COLLABORATION**
- Add comments to cases (threaded discussions)
- Get all comments for review
- Support team collaboration on investigations
- Use: `add_case_comment(case_id, author, content)`

**5. EVIDENCE MANAGEMENT**
- Add evidence/artifacts with chain of custody
- Types: file, log, network_capture, memory_dump, screenshot
- Track who collected what and when
- Use: `add_case_evidence(case_id, evidence_type, name, collected_by, ...)`

**6. IOCs (Indicators of Compromise)**
- Add IOCs: IP addresses, domains, hashes, URLs, emails, file names
- Bulk add multiple IOCs at once
- Track threat level and confidence
- Get all IOCs for a case
- Use: `add_case_ioc(case_id, ioc_type, value, threat_level, ...)` or `bulk_add_iocs(case_id, iocs)`

**7. TASK MANAGEMENT**
- Create investigation tasks
- Assign tasks to team members
- Update task status (pending, in_progress, completed, cancelled)
- Track task completion
- Use: `add_case_task(case_id, title, ...)` and `update_case_task(task_id, status, ...)`

**8. CASE RELATIONSHIPS**
- Link related cases (duplicate, related, parent, child, blocks, blocked_by)
- Track case relationships
- Build case hierarchies
- Use: `link_related_cases(case_id, related_case_id, relationship_type, created_by, ...)`

**9. ESCALATIONS**
- Escalate cases to higher tiers or management
- Track escalation reasons and urgency
- Auto-update priority for critical escalations
- Use: `escalate_case(case_id, escalated_from, escalated_to, reason, urgency_level)`

**10. CASE CLOSURE**
- Properly close cases with full metadata
- Categories: resolved, false_positive, duplicate, unable_to_resolve
- Document root cause, lessons learned, recommendations
- Include executive summary
- Use: `close_case(case_id, closure_category, closed_by, root_cause, lessons_learned, false_positive_reason, closure_notes, ...)`

**11. RESOLUTION STEPS**
- Document remediation actions taken
- Track results of each action
- Build comprehensive resolution timeline

**WHEN THE USER SAYS:**
- "Add this to case-123" → Add finding automatically
- "Comment that this is suspicious" → Add comment to case
- "Log evidence from the firewall" → Add evidence to case
- "Add IOC 192.168.1.5 as malicious IP" → Add IOC with threat level
- "Create a task to analyze the malware" → Add task to case
- "This is related to case-456" → Link cases as related
- "Escalate this to the SOC manager" → Escalate case
- "Close this case - it was a false positive" → Close case with category
- "Add these 5 IPs as IOCs" → Bulk add IOCs

**BE COMPREHENSIVE AND PROACTIVE:**
- Add IOCs as you discover them
- Create tasks for follow-up work
- Add evidence as it's collected
- Link related cases when patterns emerge
- Escalate when appropriate
- Document everything in comments and activities
- Close cases properly with full metadata

**NO PERMISSION NEEDED**: Just do it and confirm what you did. The user expects you to manage cases completely.
</case_management_capabilities>

Your goal is to help SOC analysts work more efficiently by leveraging all available tools and integrations to provide comprehensive, accurate, and actionable security analysis. When investigating, you should automatically build out cases with all relevant findings, activities, timeline entries, and MITRE mappings as the investigation progresses."""

    def _load_api_key(self) -> bool:
        """Load API key from secure storage.

        Resolution order:

        1. ``provider_api_key_ref`` when explicitly passed at init (GH #88).
        2. Legacy ``CLAUDE_API_KEY`` / ``ANTHROPIC_API_KEY`` env / secret names.
        3. UI-saved Anthropic provider rows in ``llm_provider_configs``.

        Step 3 was the missing piece behind the "Claude API not configured"
        chat-drawer error reported when users configured Anthropic only
        through Settings → AI / LLM Providers: that path writes the key to
        ``llm_provider_<id>_api_key`` (see ``services/api/routers/llm_providers.py``)
        — not to the legacy names this method used to check.
        """
        try:
            # Use secrets manager with fallback to legacy names
            provider_key = (
                get_secret(self.provider_api_key_ref)
                if self.provider_api_key_ref
                else None
            )
            self.api_key = (
                provider_key
                or get_secret("CLAUDE_API_KEY")
                or get_secret("ANTHROPIC_API_KEY")
                or get_secret("claude_api_key")
                or get_secret("anthropic_api_key")
            )

            # Fallback: pick up keys saved by the LLM Providers UI. Lazy
            # import keeps the legacy/no-DB code path (and the unit tests
            # that pre-date this fallback) working when core.storage.connection
            # isn't importable.
            if not self.api_key:
                try:
                    from core.llm.router.router import discover_anthropic_api_key

                    self.api_key = discover_anthropic_api_key()
                except Exception as exc:  # noqa: BLE001
                    logger.debug("UI-provider key discovery skipped: %s", exc)

            if self.api_key and ANTHROPIC_AVAILABLE:
                # Set longer timeout for operations that may take more than 10 minutes
                # Default is 600 seconds (10 min), we set to 1800 seconds (30 min)
                self.client = create_anthropic_client(self.api_key, timeout=1800.0)
                self.async_client = create_async_anthropic_client(
                    self.api_key, timeout=1800.0
                )
                return True

            return False

        except Exception as e:
            logger.error(f"Error loading API key: {e}")
            return False

    def has_api_key(self) -> bool:
        """Return True if this ClaudeService can call the Anthropic SDK.

        Deliberately Anthropic-specific: every caller that gates on this
        method goes on to invoke ``self.client`` / ``self.async_client``
        (the Anthropic SDK). Reporting True for a non-Anthropic provider
        would let those callers through and then crash with AttributeError
        when ``self.client`` is None on an Ollama/OpenAI-only deployment.

        Non-Anthropic routing is handled separately by the chat endpoints
        in ``services/api/routers/claude.py``, which resolve the active provider via
        ``get_default_provider_spec()`` and dispatch through ``LLMRouter``
        without ever touching ClaudeService.
        """
        return self.api_key is not None and self.client is not None

    def _extract_content_blocks(
        self, content, include_thinking: bool = False
    ) -> Union[str, List[Dict]]:
        """
        Extract content blocks from Claude's response.

        Args:
            content: Response content blocks
            include_thinking: Whether to include thinking blocks in the output

        Returns:
            String (if only one text block) or list of content blocks
        """
        blocks = []

        logger.debug(
            f"🔍 Extracting content blocks - include_thinking: {include_thinking}, content_len: {len(content) if content else 0}"
        )

        for i, content_block in enumerate(content):
            if hasattr(content_block, "type"):
                block_type = content_block.type

                if block_type == "text" and hasattr(content_block, "text"):
                    text_len = len(content_block.text)
                    logger.debug(f"  Block {i}: text ({text_len} chars)")
                    blocks.append({"type": "text", "text": content_block.text})
                elif (
                    block_type == "thinking"
                    and include_thinking
                    and hasattr(content_block, "thinking")
                ):
                    thinking_len = len(content_block.thinking)
                    logger.info(f"  💭 Block {i}: thinking ({thinking_len} chars)")
                    blocks.append({"type": "thinking", "text": content_block.thinking})
                elif block_type == "thinking" and not include_thinking:
                    logger.debug(
                        f"  Block {i}: thinking (skipped - include_thinking=False)"
                    )

        logger.debug(f"📦 Extracted {len(blocks)} blocks")

        # If only one text block, return as string for backward compatibility
        if len(blocks) == 1 and blocks[0]["type"] == "text":
            logger.debug("   Returning single text block as string")
            return blocks[0]["text"]

        # If we have multiple blocks or thinking blocks, return as list
        if blocks:
            logger.debug("   Returning multiple blocks as list")
            return blocks

        logger.warning("   No blocks extracted!")
        return None

    # ------------------------------------------------------------------
    # Reasoning-trace persistence (GH #79)
    # ------------------------------------------------------------------

    @staticmethod
    def _serialize_response_blocks(content) -> List[Dict]:
        """Convert Anthropic SDK content blocks to JSON-safe dicts."""
        if not content:
            return []
        out = []
        for block in content:
            btype = (
                getattr(block, "type", None)
                if not isinstance(block, dict)
                else block.get("type")
            )
            if btype == "text":
                text = (
                    block.text if not isinstance(block, dict) else block.get("text", "")
                )
                out.append({"type": "text", "text": text})
            elif btype == "thinking":
                text = (
                    block.thinking
                    if not isinstance(block, dict)
                    else block.get("text") or block.get("thinking", "")
                )
                out.append({"type": "thinking", "text": text})
            elif btype == "tool_use":
                out.append(
                    {
                        "type": "tool_use",
                        "id": (
                            getattr(block, "id", None)
                            if not isinstance(block, dict)
                            else block.get("id")
                        ),
                        "name": (
                            getattr(block, "name", None)
                            if not isinstance(block, dict)
                            else block.get("name")
                        ),
                        "input": (
                            getattr(block, "input", None)
                            if not isinstance(block, dict)
                            else block.get("input")
                        ),
                    }
                )
            elif btype == "tool_result":
                out.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": (
                            getattr(block, "tool_use_id", None)
                            if not isinstance(block, dict)
                            else block.get("tool_use_id")
                        ),
                        "content": (
                            getattr(block, "content", None)
                            if not isinstance(block, dict)
                            else block.get("content")
                        ),
                        "is_error": (
                            getattr(block, "is_error", False)
                            if not isinstance(block, dict)
                            else block.get("is_error", False)
                        ),
                    }
                )
        return out

    # Keys the Anthropic content-block wire schema accepts per block type.
    # Response blocks are replayed verbatim into the next request during the
    # tool-use loop; some gateways (e.g. a LiteLLM proxy fronting
    # ANTHROPIC_BASE_URL) annotate returned tool_use blocks with a bookkeeping
    # "caller" field, and the Anthropic SDK retains such unknown fields. Strict
    # request validation then rejects them ("Extra inputs are not permitted").
    # Unlike _serialize_response_blocks, this preserves every spec field —
    # notably thinking-block "signature", which the API requires when extended
    # thinking and tool use are combined.
    _RESEND_ALLOWED_BLOCK_KEYS: Dict[str, set] = {
        "text": {"type", "text", "citations", "cache_control"},
        "thinking": {"type", "thinking", "signature", "cache_control"},
        "redacted_thinking": {"type", "data", "cache_control"},
        "tool_use": {"type", "id", "name", "input", "cache_control"},
        "tool_result": {
            "type",
            "tool_use_id",
            "content",
            "is_error",
            "cache_control",
        },
        "image": {"type", "source", "cache_control"},
    }

    @staticmethod
    def _sanitize_messages_for_log(messages: List[Dict]) -> List[Dict]:
        """Strip heavy image base64 payloads from messages before logging."""
        if not messages:
            return []
        sanitized = []
        for msg in messages:
            role = msg.get("role")
            content = msg.get("content")
            if isinstance(content, str):
                sanitized.append({"role": role, "content": content})
                continue
            if not isinstance(content, list):
                sanitized.append({"role": role, "content": content})
                continue
            clean_blocks = []
            for block in content:
                bdict = (
                    block
                    if isinstance(block, dict)
                    else {"type": getattr(block, "type", "unknown")}
                )
                btype = bdict.get("type")
                if btype == "image":
                    clean_blocks.append(
                        {"type": "image", "source": {"type": "redacted"}}
                    )
                else:
                    clean_blocks.append(
                        ClaudeService._serialize_response_blocks([block])[0]
                        if not isinstance(block, dict)
                        else block
                    )
            sanitized.append({"role": role, "content": clean_blocks})
        return sanitized

    @staticmethod
    def _extract_prior_tool_results(messages: List[Dict]) -> List[Dict]:
        """Return tool_result blocks from the most recent user message, if any.

        Used to capture the "input" context for an iteration that consumed
        tool results from the prior iteration's tool calls.
        """
        if not messages:
            return []
        for msg in reversed(messages):
            if msg.get("role") != "user":
                continue
            content = msg.get("content")
            if isinstance(content, list):
                results = [
                    b
                    for b in content
                    if (isinstance(b, dict) and b.get("type") == "tool_result")
                    or (
                        not isinstance(b, dict)
                        and getattr(b, "type", None) == "tool_result"
                    )
                ]
                if results:
                    return ClaudeService._serialize_response_blocks(results)
            return []
        return []

    def _persist_interaction(
        self,
        *,
        session_id: Optional[str],
        agent_id: Optional[str],
        investigation_id: Optional[str],
        model: str,
        system_prompt: Optional[str],
        request_messages: List[Dict],
        response_content: Optional[List[Dict]],
        thinking_enabled: bool,
        thinking_budget: Optional[int],
        stop_reason: Optional[str],
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int = 0,
        cache_creation_tokens: int = 0,
        duration_ms: int = 0,
        error: Optional[str] = None,
        interaction_id: Optional[str] = None,
    ) -> None:
        """Fire-and-forget insert of an LLMInteractionLog row.

        Runs in the calling thread; failures are logged but never re-raised
        so persistence can never break the request path.
        """
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import LLMInteractionLog

            blocks = self._serialize_response_blocks(response_content or [])
            thinking_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "thinking"
            )
            response_text = "\n\n".join(
                b["text"] for b in blocks if b["type"] == "text"
            )
            tool_calls = [b for b in blocks if b["type"] == "tool_use"]
            tool_results_in = self._extract_prior_tool_results(request_messages)

            try:
                # GH #89: use the model registry for per-provider pricing.
                # #184 Phase 3: include cache tokens so reads (0.1×) and
                # writes (1.25×) are priced correctly instead of being
                # treated as full-rate input.
                from core.llm.cost.calls import compute_call_cost

                cost_usd = compute_call_cost(
                    model,
                    "anthropic",
                    int(input_tokens or 0),
                    int(output_tokens or 0),
                    cache_read_tokens=int(cache_read_tokens or 0),
                    cache_creation_tokens=int(cache_creation_tokens or 0),
                )
            except Exception:
                cost_usd = 0.0

            # #186: capture which Bifrost VK serviced this call so we can
            # group spend per-VK in analytics. Empty in dev / bypass mode.
            try:
                from core.llm.cost.budget import get_active_vk

                _vk = get_active_vk()
            except Exception:
                _vk = None

            row = LLMInteractionLog(
                # Caller-supplied interaction_id (#185 Bifrost correlation)
                # falls back to a fresh UUID for legacy callers that don't
                # generate it upstream of the dispatch.
                interaction_id=interaction_id or str(uuid.uuid4()),
                session_id=session_id,
                agent_id=agent_id,
                investigation_id=investigation_id,
                model=model,
                system_prompt=system_prompt,
                request_messages=self._sanitize_messages_for_log(request_messages),
                thinking_enabled=bool(thinking_enabled),
                thinking_budget=thinking_budget,
                thinking_content=thinking_text or None,
                response_content=response_text or None,
                tool_calls=tool_calls,
                tool_results=tool_results_in,
                stop_reason=stop_reason,
                input_tokens=int(input_tokens or 0),
                output_tokens=int(output_tokens or 0),
                cache_read_tokens=int(cache_read_tokens or 0),
                cache_creation_tokens=int(cache_creation_tokens or 0),
                cost_usd=float(cost_usd or 0.0),
                duration_ms=int(duration_ms or 0),
                error=error,
                virtual_key_id=_vk,
            )
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                session.add(row)
        except Exception as exc:
            logger.warning(f"LLMInteractionLog persist failed (non-fatal): {exc}")

    def chat(
        self,
        message: Union[str, List[Dict]],
        system_prompt: Optional[str] = None,
        context: Optional[List[Dict]] = None,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 4096,
        session_id: Optional[str] = None,
        agent_id: Optional[str] = None,
        investigation_id: Optional[str] = None,
    ) -> Optional[str]:
        """One completion. No tools and no loop -- both live in the agent layer."""
        if not self.has_api_key():
            logger.error("No API key configured")
            return None

        messages = list(context or []) + [{"role": "user", "content": message}]
        effective_system_prompt = (
            system_prompt if system_prompt is not None else self.default_system_prompt
        )
        api_kwargs: Dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": messages,
            # Correlates the Bifrost LogEntry with the row persisted below.
            "extra_headers": {"x-bf-lh-vigil-interaction-id": str(uuid.uuid4())},
        }
        if effective_system_prompt:
            api_kwargs["system"] = effective_system_prompt

        started = time.monotonic()
        try:
            response = self.client.messages.create(**api_kwargs)
        except Exception as exc:
            logger.error(f"Error in Claude chat: {exc}")
            raise

        usage = getattr(response, "usage", None)
        self._persist_interaction(
            session_id=session_id,
            agent_id=agent_id,
            investigation_id=investigation_id,
            model=getattr(response, "model", model),
            system_prompt=effective_system_prompt,
            request_messages=messages,
            response_content=list(response.content) if response.content else [],
            thinking_enabled=False,
            thinking_budget=None,
            stop_reason=getattr(response, "stop_reason", None),
            input_tokens=getattr(usage, "input_tokens", 0) if usage else 0,
            output_tokens=getattr(usage, "output_tokens", 0) if usage else 0,
            duration_ms=int((time.monotonic() - started) * 1000),
        )

        extracted = self._extract_content_blocks(response.content)
        return extracted if isinstance(extracted, str) else json.dumps(extracted)

    def analyze_finding(self, finding: Dict) -> str:
        """
        Analyze a security finding using Claude.

        Args:
            finding: Finding dictionary.

        Returns:
            Analysis text.
        """
        system_prompt = (
            "You are a security analyst helping to analyze security findings. "
            "Provide clear, actionable analysis of security findings including "
            "threat assessment, recommended actions, and context."
        )

        # Build a clean copy: strip None values for a cleaner prompt
        clean = {k: v for k, v in finding.items() if v is not None}
        finding_text = json.dumps(clean, indent=2, default=str)

        message = f"Analyze this security finding:\n\n{finding_text}\n\nProvide a detailed analysis."

        return self.chat(message, system_prompt=system_prompt, model=DEFAULT_MODEL)
