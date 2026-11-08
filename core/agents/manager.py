"""Runtime agent library and manager (Reorg R1 / #482)."""

import logging
from typing import Dict, List, Optional

from core.agents.builtins import AgentProfile, builtin_manifests
from core.agents.enablement import disabled_agent_ids
from core.agents.operator_manifest import manifest_needs, read_manifest
from core.agents.prompts import render_system
from core.agents.run_stats import agent_run_stats
from core.llm.providers.registry import get_registry, model_display_name
from core.response.ceiling import asks_first
from core.response.config import ResponseConfig
from core.skills.skill_library import READ_SKILL_TOOL, load_skills, skill_roots

logger = logging.getLogger(__name__)

CUSTOM_AGENT_ID_PREFIX = "custom-"
# Every agent runs on this assignment, built-in or not (#4041).
AGENT_COMPONENT = "investigation"


def profile_from_manifest(
    row: dict, response_config: Optional[ResponseConfig] = None
) -> AgentProfile:
    """An agent as its manifest says, built-in or operator, and nothing else.

    ``row`` holds ``id`` and ``manifest``. Name is the manifest's ``name``, else
    the id without its prefix; description and needs are the manifest's, and
    the prompt is its ``system`` rendered. Only an operator (custom-) agent keeps
    the manifest on its profile: phase tools and the skills column branch on it.
    """
    text = row.get("manifest") or ""
    doc = read_manifest(text)
    needs = manifest_needs(doc)
    return AgentProfile(
        id=row["id"],
        name=str(doc.get("name") or "").strip()
        or row["id"].removeprefix(CUSTOM_AGENT_ID_PREFIX),
        description=str(doc.get("description") or ""),
        system_prompt=render_system(
            str(doc.get("system") or ""), needs, response_config
        ),
        recommended_tools=needs,
        manifest=text if row["id"].startswith(CUSTOM_AGENT_ID_PREFIX) else None,
    )


class SOCAgentLibrary:
    """The built-ins, from the manifests shipped in core/agents/manifests."""

    @staticmethod
    def get_all_agents(
        response_config: Optional[ResponseConfig] = None,
    ) -> Dict[str, AgentProfile]:
        # One read of the thresholds for the whole set (#916).
        config = response_config or ResponseConfig.from_settings()
        return {
            agent_id: profile_from_manifest({"id": agent_id, "manifest": text}, config)
            for agent_id, text in builtin_manifests().items()
        }

    @staticmethod
    def get_agent(agent_id: str) -> Optional[AgentProfile]:
        text = builtin_manifests().get(agent_id)
        if text is None:
            return None
        return profile_from_manifest({"id": agent_id, "manifest": text})


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

        Replaces only entries with the custom- prefix so built-ins are never touched.
        Returns the number of custom agents loaded. Failures (e.g. DB unavailable
        at import time) are logged and swallowed so the built-in set remains usable.

        CRUD routes call this from threadpool workers, so the new map is built
        aside and swapped in with one assignment: readers never see it half-built.
        """
        customs: Dict[str, AgentProfile] = {}
        try:
            from core.storage.connection import get_db_manager
            from core.storage.models import CustomAgent
            from core.storage.schemas import CustomAgentSchema
        except Exception as e:
            logger.warning(f"CustomAgent model unavailable, skipping refresh: {e}")
            self._swap_customs(customs)
            return 0

        try:
            config = ResponseConfig.from_settings()
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                for row in session.query(CustomAgent).all():
                    try:
                        profile = profile_from_manifest(
                            CustomAgentSchema.dump(row), config
                        )
                        customs[profile.id] = profile
                    except Exception as e:
                        logger.error(f"Failed to load custom agent {row.id}: {e}")
        except Exception as e:
            logger.warning(f"Unable to refresh custom agents from DB: {e}")
            customs = {}
        self._swap_customs(customs)
        return len(customs)

    def _swap_customs(self, customs: Dict[str, AgentProfile]) -> None:
        builtins = {
            k: v
            for k, v in list(self.agents.items())
            if not k.startswith(CUSTOM_AGENT_ID_PREFIX)
        }
        self.agents = {**builtins, **customs}

    def get_agent_list(self) -> List[Dict]:
        """One row per agent, with what the Agents tab shows beside it.

        ``model`` is the label (registry display name, else the stored id) of
        the investigation assignment every agent runs on, and ``model_source``
        is ``assignment`` or ``default``; both are None when nothing is
        configured. ``skills`` is the number a manifest
        names, else the size of the skill library when the agent can
        ``read_skill``, else 0. ``changes`` is
        ``read_only``, ``asks_first`` or ``on_its_own``. ``runs_7d``,
        ``success_rate`` (a fraction 0..1, None with no runs) and
        ``success_level`` cover the last 7 days; all three are None if the
        stats could not be read. ``enabled`` is False for an agent turned off.
        """
        disabled = disabled_agent_ids()
        assignments = _read_assignments()
        default = _UNSET
        library = len(load_skills(skill_roots()))
        try:
            stats: Optional[Dict[str, dict]] = agent_run_stats()
        except Exception as e:
            logger.warning(f"Agent run stats unavailable: {e}")
            stats = None
        # One lookup for every agent's tools.
        asking = asks_first(
            t for a in self.agents.values() for t in a.recommended_tools
        )
        rows = []
        for a in self.agents.values():
            if AGENT_COMPONENT in assignments:
                model, source = assignments[AGENT_COMPONENT], "assignment"
            else:
                if default is _UNSET:
                    default = _default_model(assignments)
                model, source = default, ("default" if default else None)
            if stats is None:
                run = dict.fromkeys(_STAT_FIELDS)
            else:
                run = stats.get(a.id) or {**dict.fromkeys(_STAT_FIELDS), "runs_7d": 0}
            rows.append(
                {
                    "id": a.id,
                    "name": a.name,
                    "description": a.description,
                    "recommended_tools": list(a.recommended_tools),
                    "model": model_display_name(model) if model else None,
                    "model_source": source,
                    "skills": _skill_count(a, library),
                    "changes": _changes(asking, a.recommended_tools),
                    "tool_changes": {
                        t: _changes(asking, [t]) for t in a.recommended_tools
                    },
                    **run,
                    "enabled": a.id not in disabled,
                }
            )
        return rows


_UNSET = object()
_STAT_FIELDS = ("runs_7d", "success_rate", "success_level")


def _skill_count(agent: AgentProfile, library: int) -> int:
    """A manifest's named skills; else the library, when the agent can read_skill."""
    if agent.manifest is not None:
        named = read_manifest(agent.manifest).get("skills")
        return len(named) if isinstance(named, list) else 0
    return library if READ_SKILL_TOOL in agent.recommended_tools else 0


def _changes(asking: frozenset, tools: List[str]) -> str:
    """``asks_first`` when any of ``tools`` waits for an approval, else ``read_only``."""
    return "asks_first" if asking.intersection(tools) else "read_only"


def _read_assignments() -> Dict[str, str]:
    """Configured model id per component; empty when the DB is unreachable."""
    try:
        return {c: a.model_id for c, a in get_registry().get_all_assignments().items()}
    except Exception as e:
        logger.warning(f"Model assignments unavailable: {e}")
        return {}


def _default_model(assignments: Dict[str, str]) -> Optional[str]:
    """The rest of resolve_model_for_component: chat_default, then the provider default."""
    if "chat_default" in assignments:
        return assignments["chat_default"]
    try:
        from core.llm.router.router import get_default_provider_spec

        spec = get_default_provider_spec()
    except Exception as e:
        logger.warning(f"Default provider unavailable: {e}")
        return None
    return spec.default_model if spec else None
