import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from core.llm.oneshot import NOT_CONFIGURED, NoProviderConfigured, complete
from core.llm.router.router import ProviderSpec

pytestmark = pytest.mark.unit

OLLAMA = ProviderSpec(
    provider_id="ollama-local",
    provider_type="ollama",
    base_url="http://127.0.0.1:11434",
    api_key_ref=None,
    default_model="nemotron-3.5-lightning:30b-a3b-mlx",
    config={},
)


def test_dispatches_to_the_assigned_provider_not_anthropic():
    dispatch = AsyncMock(return_value={"content": "  draft  "})
    with (
        patch(
            "core.llm.target.resolve_dispatch",
            return_value=(OLLAMA, "nemotron-3.5-lightning:30b-a3b-mlx"),
        ),
        patch("core.llm.target.resolve_effort", return_value=None),
        patch("core.llm.router.router.LLMRouter.dispatch", dispatch),
    ):
        out = asyncio.run(
            complete(
                message="hi",
                system_prompt="sys",
                context=[{"role": "user", "content": "before"}],
            )
        )
    assert out == "draft"
    kwargs = dispatch.call_args.kwargs
    assert kwargs["provider"] is OLLAMA
    assert kwargs["model"] == "nemotron-3.5-lightning:30b-a3b-mlx"
    assert kwargs["system_prompt"] == "sys"
    assert kwargs["messages"] == [
        {"role": "user", "content": "before"},
        {"role": "user", "content": "hi"},
    ]


def test_says_no_provider_rather_than_naming_claude():
    with patch("core.llm.target.resolve_dispatch", return_value=None):
        with pytest.raises(NoProviderConfigured) as err:
            asyncio.run(complete(message="hi"))
    assert str(err.value) == NOT_CONFIGURED
    assert "Claude" not in NOT_CONFIGURED


def test_workflow_generator_uses_the_configured_provider():
    from core.workflows.workflow_ai_generator import WorkflowAIGenerator

    gen = WorkflowAIGenerator()
    with (
        patch.object(gen, "_build_user_prompt", return_value="u"),
        patch(
            "core.llm.oneshot.complete",
            AsyncMock(side_effect=NoProviderConfigured(NOT_CONFIGURED)),
        ),
    ):
        result = asyncio.run(gen.generate("phishing triage"))
    assert result["success"] is False
    assert result["error"] == NOT_CONFIGURED
