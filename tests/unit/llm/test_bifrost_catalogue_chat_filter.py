"""Chat-model filter for the Bifrost catalogue.

Host-owned catalogues (Ollama) list local models with no datasheet pricing or
``max_output_tokens``. They were filtered out as non-chat entries.
"""

from core.llm.bifrost.admin import _is_chat_catalogue_entry


def test_datasheet_chat_model_needs_max_output_tokens():
    assert _is_chat_catalogue_entry({"name": "gpt-x", "max_output_tokens": 4096})
    assert not _is_chat_catalogue_entry({"name": "gpt-x"})


def test_host_owned_catalogue_entry_without_token_cap_is_chat():
    assert _is_chat_catalogue_entry({"name": "llama3:8b"}, provider_type="ollama")
    assert _is_chat_catalogue_entry({"name": "llama3:8b", "provider": "ollama"})


def test_host_owned_catalogue_still_drops_embedding_models():
    assert not _is_chat_catalogue_entry(
        {"name": "nomic-embed-text"}, provider_type="ollama"
    )


def test_other_providers_still_need_max_output_tokens():
    assert not _is_chat_catalogue_entry({"name": "m"}, provider_type="openai")
