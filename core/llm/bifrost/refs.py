"""Where Vigil keeps its own copy of a gateway key's fields.

Bifrost masks these on read and offers no unmasked read, so the config proxy
(``services/api/routers/bifrost_config``) files them in the secrets store on
write, and ``core.llm.residency`` reads the Ollama URL back to classify the
provider. One home for the names, so neither side can drift from the other.
"""

from __future__ import annotations


def secret_ref(key_id: str) -> str:
    """A key's credential."""
    return f"llm_key_{key_id}"


def scope_ref(key_id: str) -> str:
    """Where a Vertex key's project/region are kept.

    Not secrets, but Bifrost masks them like one and offers no unmasked read —
    ``?unmasked``/``?reveal`` all come back ``prod****oglm`` — so the real
    values cannot be recovered from the gateway. Keeping our own copy is what
    lets an edit that only changes the weight leave the scope intact.
    """
    return f"llm_key_{key_id}_vertex_scope"


def ollama_ref(key_id: str) -> str:
    """Where an Ollama key's endpoint URL is kept.

    Same reason as ``scope_ref``: not a secret, but Bifrost masks it on read
    and offers no way to get it back, so an edit that only changes the weight
    would otherwise hand the gateway a mask in place of the endpoint.
    """
    return f"llm_key_{key_id}_ollama_url"
