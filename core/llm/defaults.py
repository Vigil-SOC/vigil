"""Central default values for Vigil.

Import from here rather than scattering literals across the codebase.
All values are overridable via environment variables so operator deployments
can change them without code changes.
"""

from typing import Any, Dict, Optional

# Fallback model ID used when no provider-specific model can be resolved
# (e.g. fresh install, DB unavailable, no ai_model_configs row).
# Operators on Ollama-only deployments should set this to their local model
# (e.g. "llama3.2:1b") so the failsafe never tries to call an Anthropic model.
from core.config import get_settings

DEFAULT_MODEL: str = get_settings().default_model

# Anthropic models that reject the legacy extended-thinking shape
# thinking={"type": "enabled", "budget_tokens": N} with a 400 and instead
# require adaptive thinking + output_config.effort (Opus 4.7/4.8, Fable 5,
# Mythos). Matched as substrings so provider prefixes ("anthropic.") and any
# date/speed suffixes still hit.
_ADAPTIVE_THINKING_MODELS = (
    "opus-4-7",
    "opus-4-8",
    "fable-5",
    "mythos-5",
    "mythos-preview",
)


def model_requires_adaptive_thinking(model: str) -> bool:
    """True if ``model`` rejects budget_tokens thinking and needs adaptive."""
    m = (model or "").lower()
    return any(tag in m for tag in _ADAPTIVE_THINKING_MODELS)


def _thinking_budget_to_effort(budget: Optional[int]) -> str:
    """Map a legacy token budget onto an adaptive-thinking effort level."""
    if not budget or budget <= 4096:
        return "low"
    if budget <= 12000:
        return "medium"
    return "high"


def build_thinking_kwargs(model: str, budget: Optional[int]) -> Dict[str, Any]:
    """Build the messages.create/stream kwargs that enable extended thinking.

    Two model families need different shapes, and the Bifrost gateway that all
    Anthropic traffic routes through (``core/llm/providers/clients.py``) constrains
    what we can send:

    * **Adaptive-only models** (Opus 4.7/4.8, Fable 5, Mythos) reject
      ``thinking={"type": "enabled", "budget_tokens": N}`` with a 400. They want
      ``thinking={"type": "adaptive"}`` + ``output_config.effort`` — but the
      pinned ``maximhq/bifrost`` image cannot convert ``thinking.type=adaptive``
      and 500s ("failed to convert bifrost request..."). Since neither thinking
      shape survives the round trip for these models, we omit ``thinking``
      entirely and steer reasoning depth with ``output_config.effort`` alone
      (Bifrost forwards it, and it is valid direct-to-Anthropic too). Thinking
      blocks won't stream for these models until Bifrost gains adaptive support.
    * **Older models** (Sonnet 4.x, Haiku 4.5, Opus 4.6) keep the budget-based
      ``enabled`` shape, which both Anthropic and Bifrost accept.

    Returns a dict to merge into the API kwargs.
    """
    if model_requires_adaptive_thinking(model):
        return {"output_config": {"effort": _thinking_budget_to_effort(budget)}}
    return {"thinking": {"type": "enabled", "budget_tokens": budget}}
