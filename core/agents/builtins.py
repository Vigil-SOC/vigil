"""Agent record type and the built-in agents' manifests (Reorg R1 / #482, #4041).

Each built-in is a plugin manifest shipped in ``manifests/``, built into a
profile by ``core.agents.manager.profile_from_manifest`` exactly as an operator
agent's stored manifest is. Its ``system`` keeps the ``$auto_approve``,
``$review``, ``$monitor``, ``$memory`` and ``$skills`` placeholders; they are
filled by ``core.agents.prompts.render_system`` wherever the prompt reaches a
model, so the agent is told the lines the approval gate enforces (#916).
"""

from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

MANIFEST_DIR = Path(__file__).parent / "manifests"


class AgentId(str, Enum):
    """Canonical id of every built-in agent — the *actor* vocabulary.

    Doubles as the ``id`` of the matching manifest in ``manifests/`` and the
    ``created_by`` value an agent stamps on records it authors. Custom agents
    are not members; they carry a ``custom-`` prefixed id from the database.
    """

    TRIAGE = "triage"
    INVESTIGATOR = "investigator"
    THREAT_HUNTER = "threat_hunter"
    CORRELATOR = "correlator"
    RESPONDER = "responder"
    REPORTER = "reporter"
    MITRE_ANALYST = "mitre_analyst"
    FORENSICS = "forensics"
    THREAT_INTEL = "threat_intel"
    COMPLIANCE = "compliance"
    MALWARE_ANALYST = "malware_analyst"
    NETWORK_ANALYST = "network_analyst"
    AUTO_RESPONDER = "auto_responder"


# The autonomous loop authors decisions of its own but is not an agent: it has
# no prompt, no manifest, and never appears in GET /agents.
ORCHESTRATOR_ACTOR = "orchestrator"


@dataclass
class AgentProfile:
    id: str
    name: str
    description: str
    # The manifest's system with its placeholders rendered.
    system_prompt: str
    # The manifest's needs.
    recommended_tools: List[str]
    # Custom agents only: the operator manifest a run of this agent loads (#4023).
    # Built-ins leave it None; phase tools and the skills column branch on it.
    manifest: Optional[str] = None


# Read once per process, in AgentId order (the Agents list's order).
@lru_cache(maxsize=1)
def builtin_manifests() -> Dict[str, str]:
    """Each built-in's manifest text, by id."""
    return {
        a.value: (MANIFEST_DIR / f"{a.value}.yaml").read_text(encoding="utf-8")
        for a in AgentId
    }
