"""Runtime agent library and manager (Reorg R1 / #482)."""

import logging
from typing import Dict, List, Optional

from core.agents.builtins import BUILTIN_AGENTS, AgentProfile
from core.agents.prompts import prompt_for_row

logger = logging.getLogger(__name__)


class SOCAgentLibrary:
    @staticmethod
    def get_all_agents() -> Dict[str, AgentProfile]:
        # Built-ins travel through the same builder as customs (#482).
        return {r["id"]: SOCAgentLibrary.build_profile(r) for r in BUILTIN_AGENTS}

    @staticmethod
    def build_profile(row: dict) -> AgentProfile:
        """Build an AgentProfile from an agent record dict.

        Shared by built-ins (core.agents.builtins.BUILTIN_AGENTS) and customs
        (a custom_agents row's to_dict()). Uses system_prompt_override verbatim
        when set; otherwise renders BASE_PROMPT with the row's
        role/extra_principles/methodology fragments.
        """
        prompt = prompt_for_row(row)
        return AgentProfile(
            id=row["id"],
            name=row.get("name") or row["id"],
            description=row.get("description") or "",
            system_prompt=prompt,
            icon=row.get("icon") or "C",
            color=row.get("color") or "#888888",
            specialization=row.get("specialization") or "Custom",
            recommended_tools=list(row.get("recommended_tools") or []),
            max_tokens=int(row.get("max_tokens") or 4096),
            enable_thinking=bool(row.get("enable_thinking") or False),
            thinking_budget=(
                int(row["thinking_budget"])
                if row.get("thinking_budget") is not None
                else None
            ),
            # GH #89 — custom agents can pin a model; falling back to the
            # component_category (default 'investigation') if not set.
            model=(row.get("model") or None),
            component_category=(row.get("component_category") or "investigation"),
            # GH #476 — built-ins carry their action id; custom agents have no
            # action of their own, so they log under their agent id.
            decision_id=(row.get("decision_id") or row["id"]),
        )

    @staticmethod
    def get_agent(agent_id: str) -> Optional[AgentProfile]:
        agents = SOCAgentLibrary.get_all_agents()
        return agents.get(agent_id)


CUSTOM_AGENT_ID_PREFIX = "custom-"


class AgentManager:
    def __init__(self):
        self.agents = SOCAgentLibrary.get_all_agents()
        # Load DB-backed custom agents at startup so /agents/agents returns
        # a unified list without waiting for a later CRUD call to trigger
        # refresh. Failures (DB not ready) are logged inside the helper,
        # so this remains safe when imported before the DB is initialised.
        self.refresh_custom_agents()

    def refresh_custom_agents(self) -> int:
        """Reload custom agents from the DB.

        Clears only entries with the custom- prefix so built-ins are never touched.
        Returns the number of custom agents loaded. Failures (e.g. DB unavailable
        at import time) are logged and swallowed so the built-in set remains usable.
        """
        # Drop existing custom agents first
        custom_keys = [k for k in self.agents if k.startswith(CUSTOM_AGENT_ID_PREFIX)]
        for k in custom_keys:
            del self.agents[k]

        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import CustomAgent
            from core.storage.schemas import CustomAgentSchema
        except Exception as e:
            logger.warning(f"CustomAgent model unavailable, skipping refresh: {e}")
            return 0

        try:
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                rows = session.query(CustomAgent).all()
                loaded = 0
                for row in rows:
                    try:
                        profile = SOCAgentLibrary.build_profile(
                            CustomAgentSchema.dump(row)
                        )
                        self.agents[profile.id] = profile
                        loaded += 1
                    except Exception as e:
                        logger.error(f"Failed to load custom agent {row.id}: {e}")
                return loaded
        except Exception as e:
            logger.warning(f"Unable to refresh custom agents from DB: {e}")
            return 0

    def get_agent_list(self) -> List[Dict]:
        return [
            {
                "id": a.id,
                "name": a.name,
                "description": a.description,
                "icon": a.icon,
                "color": a.color,
                "specialization": a.specialization,
                "decision_id": a.decision_id,
            }
            for a in self.agents.values()
        ]
