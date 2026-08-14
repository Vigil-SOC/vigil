"""The ``enrich()`` seam: resolve provider → dispatch → parse → stamp → persist.

Two known-imperfect behaviours are carried over from the old inline handler
rather than fixed here, so any regression stays bisectable:

* Dispatch is asymmetric — Anthropic runs on a threadpool with no retry; other
  providers get ``LLMRouter`` + retry + local-Bifrost recovery.
* The ``ai_enrichment`` write is a full replace, and ``daemon/processor`` writes
  a disjoint set of triage keys to the same column. So a daemon payload can
  satisfy the API's cache check yet render as ``undefined``, writing here wipes
  daemon triage, and the finding then leaves the daemon's
  ``ai_enrichment IS NULL`` backfill for good. ``persist=False`` is the seam for
  fixing that separately.
"""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from core.findings.enrichment.errors import (
    FindingNotFound,
    NoProviderConfigured,
    ProviderUnavailable,
    UnidentifiableFinding,
)
from core.findings.enrichment.parse import parse_enrichment
from core.findings.enrichment.prompt import build_prompt, summarize_finding

logger = logging.getLogger(__name__)

# Bounded separately per path: the enrichment JSON schema is large, so a tight
# cap truncates it — but local models are slow per token, so they get a
# tighter bound than the cloud path while still leaving room for the object.
ANTHROPIC_MAX_TOKENS = 4096
LOCAL_MAX_TOKENS = 1400

LOCAL_SYSTEM_PROMPT = (
    "You are a cybersecurity analyst. Respond only with valid "
    "JSON matching the requested enrichment schema. Keep the "
    "response concise and do not include chain-of-thought."
)

_data_service: Any = None


def _default_data_service() -> Any:
    """Lazily build this module's own ``DatabaseDataService``.

    Constructing one runs ``init_database``, so it must not happen at import
    time: callers passing ``persist=False`` (or their own ``data_service``)
    should never pay for a connection they don't use.
    """
    global _data_service
    if _data_service is None:
        from core.storage.database_data_service import DatabaseDataService

        _data_service = DatabaseDataService()
    return _data_service


def _resolve_provider(component: str) -> Tuple[Any, str, Any]:
    """Resolve ``component`` to ``(provider_spec, model_id, claude_service)``.

    ``claude_service`` is None for every non-Anthropic provider — only the
    Anthropic path uses the SDK client, and only that path needs the API-key
    precheck (``has_api_key`` is Anthropic-specific by design).

    Raises:
        NoProviderConfigured: nothing resolved, or no Anthropic API key.
        ProviderUnavailable: the resolved provider id has no spec row.
    """
    from core.llm.providers.registry import get_registry
    from core.llm.router.router import get_provider_spec

    resolved_model = get_registry().resolve_model_for_component(component)
    if not resolved_model:
        raise NoProviderConfigured(
            f"No LLM provider is configured for component '{component}'"
        )

    provider_id, model_id = resolved_model
    provider = get_provider_spec(provider_id)
    if provider is None:
        raise ProviderUnavailable(f"Configured provider '{provider_id}' is unavailable")

    claude_service = None
    if provider.provider_type == "anthropic":
        from core.llm.harness.claude import ClaudeService

        claude_service = ClaudeService(use_backend_tools=True, use_mcp_tools=False)
        if not claude_service.has_api_key():
            raise NoProviderConfigured(
                f"Anthropic provider '{provider_id}' has no resolvable API key"
            )

    return provider, model_id, claude_service


async def _dispatch(
    *,
    provider: Any,
    model_id: str,
    prompt: str,
    claude_service: Any,
    finding_id: str,
) -> Optional[str]:
    """Send ``prompt`` to ``provider`` and return the raw text response.

    The two paths are asymmetric — see the module docstring. Preserved as-is.
    """
    loop = asyncio.get_event_loop()
    if provider.provider_type == "anthropic":
        # No retry here: the cloud path has never had one.
        return await loop.run_in_executor(
            None,
            lambda: claude_service.chat(
                message=prompt,
                model=model_id,
                max_tokens=ANTHROPIC_MAX_TOKENS,
            ),
        )

    dispatch_args = {
        "provider": provider,
        "messages": [{"role": "user", "content": f"/no_think\n{prompt}"}],
        "system_prompt": LOCAL_SYSTEM_PROMPT,
        "model": model_id,
        "max_tokens": LOCAL_MAX_TOKENS,
    }
    from core.llm.providers.recovery import (
        is_gateway_connection_error,
        local_bifrost_recovery_enabled,
        local_bifrost_recovery_retry_limit,
        recover_local_bifrost,
    )
    from core.llm.router.router import LLMRouter

    retry_limit = local_bifrost_recovery_retry_limit()
    for attempt in range(retry_limit + 1):
        try:
            result = await LLMRouter().dispatch(**dispatch_args)
            break
        except Exception as dispatch_error:
            eligible = (
                provider.provider_type == "ollama"
                and local_bifrost_recovery_enabled()
                and is_gateway_connection_error(dispatch_error)
            )
            if not eligible or attempt >= retry_limit:
                raise

            recovery = await recover_local_bifrost()
            if not recovery.ready:
                logger.warning(
                    "Local Bifrost recovery for %s failed: %s",
                    finding_id,
                    recovery.detail,
                )
                raise
            logger.info(
                "Local Bifrost recovery for %s: %s; retrying enrichment (%s/%s)",
                finding_id,
                recovery.detail,
                attempt + 1,
                retry_limit,
            )
    return result.get("content", "")


async def _persist(
    finding_id: str, enrichment: Dict[str, Any], data_service: Any
) -> bool:
    """Write ``enrichment`` to the finding's ``ai_enrichment`` column.

    A full replace, not a merge — see the module docstring. A failed write is
    logged and swallowed: the caller still gets the payload it paid a provider
    call for. Offloaded with ``to_thread`` because the data layer is sync
    SQLAlchemy and this runs on the event loop.
    """
    service = data_service if data_service is not None else _default_data_service()
    success = await asyncio.to_thread(
        service.update_finding, finding_id, ai_enrichment=enrichment
    )
    if not success:
        logger.error("Failed to save enrichment for %s", finding_id)
    else:
        logger.info("Successfully generated and cached enrichment for %s", finding_id)
    return bool(success)


async def enrich(
    finding: Dict[str, Any],
    *,
    finding_id: Optional[str] = None,
    component: str = "reporting",
    persist: bool = True,
    data_service: Any = None,
) -> Dict[str, Any]:
    """Generate AI enrichment for ``finding``. Returns the enrichment payload.

    Takes a finding *dict*, not an id: the fetch (and its 404) stays with the
    caller, so this module needs no database access for reads, and the daemon —
    which already holds finding dicts — doesn't re-read them.

    Does **not** do a cache check. Whether an existing ``ai_enrichment`` value
    counts as a usable cache hit is caller policy (the HTTP handler has a
    ``force_regenerate`` query param; see also the writer collision noted in
    the module docstring).

    Args:
        finding: The finding to enrich.
        finding_id: Authoritative id — the write target, and what the prompt
            reports. Overrides ``finding["finding_id"]``; pass it whenever you
            hold the id independently of the dict (the HTTP handler passes its
            path param). Required when ``persist`` is True and the dict carries
            no id of its own.
        component: ``ai_model_configs`` component whose model assignment to
            use. The HTTP handler uses ``"reporting"``.
        persist: Write the result to the finding's ``ai_enrichment`` column.
            Pass False to compose your own write.
        data_service: Persistence target; defaults to a lazily-built
            ``DatabaseDataService``. Pass an existing instance to avoid a
            second one.

    Raises:
        FindingNotFound: ``finding`` is empty.
        UnidentifiableFinding: ``persist`` is True but no id is available.
        NoProviderConfigured: no usable provider for ``component``.
        ProviderUnavailable: resolved provider id has no spec row.
        EmptyProviderResponse: the provider returned nothing.
    """
    if not finding:
        raise FindingNotFound("Finding not found")

    finding_id = finding_id or finding.get("finding_id") or ""
    if persist and not finding_id:
        # update_finding("") matches no row and only logs, so refuse the call
        # rather than paying a provider and dropping the result on the floor.
        raise UnidentifiableFinding(
            "Cannot persist enrichment: no finding_id was passed and the "
            "finding dict has none. Pass finding_id=..., or persist=False."
        )

    # Resolve before shaping input, so an unconfigured provider still reports
    # NoProviderConfigured rather than whatever a malformed finding raises.
    provider, model_id, claude_service = _resolve_provider(component)

    summary = summarize_finding(finding, finding_id=finding_id)
    prompt = build_prompt(summary)

    logger.info(
        "Generating AI enrichment for %s via %s/%s",
        finding_id,
        provider.provider_id,
        model_id,
    )
    response = await _dispatch(
        provider=provider,
        model_id=model_id,
        prompt=prompt,
        claude_service=claude_service,
        finding_id=finding_id,
    )

    enrichment = parse_enrichment(response, severity=summary.severity)

    # Keep the provider's original response even when it parsed cleanly.
    # Analysts can compare the rendered fields against the local model's
    # exact output without having to regenerate the enrichment.
    enrichment["raw_response"] = response
    enrichment["generated_at"] = datetime.utcnow().isoformat() + "Z"
    enrichment["model"] = model_id
    enrichment["provider_id"] = provider.provider_id
    enrichment["provider_type"] = provider.provider_type

    if persist:
        await _persist(finding_id, enrichment, data_service)

    return enrichment
