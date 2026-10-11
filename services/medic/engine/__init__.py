"""Medic's rule engine (E4 core): a pure library over D3 observations.

See contracts/ENGINE_API.md (what a rule may say) and contracts/semantics.md (how
it is evaluated). The vectors in contracts/vectors/ are the executable spec.
"""

from services.medic.engine.core import Engine
from services.medic.engine.loader import (
    LoadedRule,
    RuleLoadError,
    load_rule,
    load_rule_file,
)

__all__ = ["Engine", "LoadedRule", "RuleLoadError", "load_rule", "load_rule_file"]
