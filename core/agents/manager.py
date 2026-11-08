"""Runtime agent library and manager (Reorg R1 / #482)."""

import logging
from typing import Dict, List, Optional

from core.agents.builtins import BUILTIN_AGENTS, AgentProfile, blank_model
from core.agents.enablement import disabled_agent_ids
from core.agents.prompts import prompt_for_row, render_confidence_bands
from core.agents.run_stats import agent_run_stats
from core.llm.providers.registry import get_registry, model_display_name
from core.response.ceiling import asks_first
from core.response.config import ResponseConfig
from core.skills.skill_library import READ_SKILL_TOOL, load_skills, skill_roots

logger = logging.getLogger(__name__)


class SOCAgentLibrary:
    @staticmethod
    def get_all_agents(
        response_config: Optional[ResponseConfig] = None,
    ) -> Dict[str, AgentProfile]:
        # Built-ins travel through the same builder as customs (#482), after
        # their confidence-band lines are filled from the configured values
        # (#916) so the prompt states the thresholds the gate enforces.
        config = response_config or ResponseConfig.from_settings()
        return {
            r["id"]: SOCAgentLibrary.build_profile(render_confidence_bands(r, config))
            for r in BUILTIN_AGENTS
        }

    @staticmethod
    def build_profile(row: dict) -> AgentProfile:
        """Build an AgentProfile from a built-in's record
        (core.agents.builtins.BUILTIN_AGENTS). Uses system_prompt_override
        verbatim when set; otherwise renders BASE_PROMPT with the record's
        role/extra_principles/methodology fragments. A custom agent is its
        manifest instead: see profile_from_manifest.
        """
        prompt = prompt_for_row(row)
        return AgentProfile(
            id=row["id"],
            name=row.get("name") or row["id"],
            description=row.get("description") or "",
            system_prompt=prompt,
            # Blank stays blank so the web falls back to the name's initials.
            icon=row.get("icon") or "",
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
            model=blank_model(row.get("model")),
            fallback_model=blank_model(row.get("fallback_model")),
            component_category=(row.get("component_category") or "investigation"),
            # GH #476 — each built-in carries its action id.
            decision_id=(row.get("decision_id") or row["id"]),
        )

    @staticmethod
    def get_agent(agent_id: str) -> Optional[AgentProfile]:
        agents = SOCAgentLibrary.get_all_agents()
        return agents.get(agent_id)


CUSTOM_AGENT_ID_PREFIX = "custom-"


def profile_from_manifest(row: dict) -> AgentProfile:
    """A custom agent as its manifest says, and nothing else: name is the id
    without its prefix; description, prompt (system) and tools (needs) are the
    manifest's. It runs on the investigation assignment and logs under its id."""
    from core.agents.operator_manifest import manifest_needs, read_manifest

    text = row.get("manifest") or ""
    doc = read_manifest(text)
    return AgentProfile(
        id=row["id"],
        name=row["id"].removeprefix(CUSTOM_AGENT_ID_PREFIX),
        description=str(doc.get("description") or ""),
        system_prompt=str(doc.get("system") or ""),
        # Blank so the web shows the name's initials.
        icon="",
        color="#888888",
        specialization="Custom",
        recommended_tools=manifest_needs(doc),
        decision_id=row["id"],
        manifest=text,
    )


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
            db_manager = get_db_manager()
            with db_manager.session_scope() as session:
                for row in session.query(CustomAgent).all():
                    try:
                        profile = profile_from_manifest(CustomAgentSchema.dump(row))
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

        ``model`` is the label (registry display name, else the stored id) and
        ``model_source`` is ``agent``, ``assignment`` or ``default``; both are
        None when nothing is configured. ``skills`` is the number a manifest
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
            if a.model:
                model, source = a.model, "agent"
            elif a.component_category in assignments:
                model, source = assignments[a.component_category], "assignment"
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
                    "icon": a.icon,
                    "color": a.color,
                    "specialization": a.specialization,
                    "decision_id": a.decision_id,
                    "recommended_tools": list(a.recommended_tools),
                    "model": model_display_name(model) if model else None,
                    "model_source": source,
                    "component_category": a.component_category,
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
        from core.agents.operator_manifest import read_manifest

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
