"""The one renderer of an agent manifest's prompt (#482, #4041).

Built-in and operator manifests alike write placeholders in ``system``; this
fills them wherever that prompt reaches a model.
"""

from string import Template
from typing import Dict, Iterable, Optional

from core.memory.recall_contract import RECALL_TOOL
from core.response.config import ResponseConfig
from core.skills.skill_library import READ_SKILL_TOOL, Skill, load_skills, skill_roots

# Read-only, and the wording carries ADR 0015 rather than gesturing at it. A
# prior Verdict is not a disposition: the ADR's first named failure is a benign
# history burying a compromised host, and a triage agent told to move fast is
# exactly who acts on one. So the block names Verdicts without ranking them and
# says plainly what recall may and may not change (#735, #732).
_MEMORY_BLOCK = """<memory_operations>
Call recall_entity to read what past investigations saw and concluded about an
entity: its Sightings, its Verdicts, its Declared Gaps and the notes analysts
wrote about it. Pass entity_keys, a list of `type:value` strings — ip:10.2.3.4,
hash:5d41402abc4b..., domain:evil.com, user:jdoe, host:web-01. The type must
be one memory knows; a hash is `hash:`, never `sha256:` or `md5:`. Every read is
logged, so pass your own caller_kind and caller_id.

What comes back is what earlier runs concluded from the evidence they had, not a
standing judgement about the entity. A prior verdict of benign is not a reason
to look less hard: an adversary working inside a window three runs called routine
is the case this exists to catch. A prior verdict of malicious is not evidence
for a new one either — recall never corroborates. Verdicts from an `imported`
investigation are tickets the team closed in another system before Vigil was
installed, and are held to the same rule as any benign verdict.

Notes are what a person on the team said about an entity, with who wrote it and
when. They are context to weigh, not evidence this run gathered, and a note
calling an entity expected is held to the same rule as a benign verdict.

Memory may change what you look at first. It never changes what counts as having
found something; you conclude from evidence you gathered yourself. It is
read-only to you: your conclusions reach memory when the investigation ends, not
from here, and there is no tool to write one.
</memory_operations>
"""


def _memory_section(tools: Optional[Iterable[str]]) -> str:
    """Return the memory block for an agent granted the recall tool, else ''.

    Gated on the agent's own grant rather than on the tool existing, because
    ``ALL_TOOLS`` always carries it and the question the prompt answers is
    whether *this* agent can call it. ``_declare`` keeps only the names in an
    agent's ``recommended_tools``, so a custom agent that was never granted
    recall would otherwise be told to call a tool its turn does not carry —
    the #129 defect on a different tool.

    No grant and an unknown grant are the same answer. Promising a tool that
    turns out to be absent is the failure being avoided; omitting the block from
    an agent that could have used it costs a lookup it did not know to make.
    """
    return _MEMORY_BLOCK if RECALL_TOOL in set(tools or ()) else ""


# Names and descriptions only, as the spec has it: the body is read on demand
# through read_skill so the prompt stays the size of an index, not a library.
_SKILLS_HEADER = """<available_skills>
Skills are procedures written for you. When a task matches a description below,
call read_skill with the skill's name and follow the SKILL.md body it returns;
a body may name supporting files you read with read_skill(name, file).
"""


def _skills_section(
    tools: Optional[Iterable[str]], skills: Optional[Iterable[Skill]]
) -> str:
    """The skills index for an agent granted read_skill, else ''.

    Gated on the grant as ``_memory_section`` is, and never on the library being
    non-empty: an agent without the grant must not be told about a tool its turn
    does not carry, whatever is on disk. ``skills`` is None when the caller
    wants the configured roots read; a granted agent with nothing loaded gets
    an empty index rather than no block, which tells it the tool exists.
    """
    if READ_SKILL_TOOL not in set(tools or ()):
        return ""
    if skills is None:
        skills = load_skills(skill_roots())
    # One line per skill even when the description was a YAML block scalar.
    lines = [f"- {s.name}: {' '.join(s.description.split())}" for s in skills]
    return (
        _SKILLS_HEADER
        + "\n".join(lines or ["(no skills loaded)"])
        + "\n</available_skills>\n"
    )


def confidence_band_values(config: ResponseConfig) -> Dict[str, str]:
    """Placeholder values for the band lines. Bands are written half-open
    (``0.85-<0.90``) so no upper edge has to be derived from the next line."""
    return {
        "auto_approve": f"{config.confidence_threshold:.2f}",
        "review": f"{config.review_threshold:.2f}",
        "monitor": f"{config.monitor_threshold:.2f}",
    }


def render_system(
    system: str,
    needs: Optional[Iterable[str]],
    config: Optional[ResponseConfig] = None,
    skills: Optional[Iterable[Skill]] = None,
) -> str:
    """A manifest's ``system`` as a model reads it: the band placeholders from
    ``config`` (else the configured thresholds), ``$memory`` and ``$skills``
    from what ``needs`` grants, empty without the grant.

    ``safe_substitute``, so text without placeholders, or a stray ``$``, comes
    back unchanged; nothing is appended. ``skills`` overrides the configured
    roots.
    """
    if "$" not in system:
        return system
    needs = list(needs or ())
    return Template(system).safe_substitute(
        confidence_band_values(config or ResponseConfig.from_settings()),
        memory=_memory_section(needs),
        skills=_skills_section(needs, skills),
    )
