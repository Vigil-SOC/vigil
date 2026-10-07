"""Which declared hypothesis subjects survive onto the board.

Two callers reach the same question from opposite ends -- the workflows service
reads them off a run request, the daemon reads them back out of the workdir --
and a Verdict's subjects come from whichever ran. Two filters would be two
answers to what a subject is, differing first in some detail nobody notices
until a key with a stray space names an entity no reader will ever query.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Mapping, Optional


def kept_subjects(
    declared: Optional[Mapping[str, Any]], asked: Iterable[str]
) -> Dict[str, List[str]]:
    """The declared subjects of the statements actually being put up.

    Only those: a subject keyed to a statement the caller then edited away
    belongs to no belief, and carrying it would name a Verdict's subject from a
    claim that was never made.

    Keys are passed through rather than parsed -- the agent layer owns the entity
    vocabulary and refuses a key it cannot read, which is one validator rather
    than two that drift. Trimmed, though, and a key that is nothing but space is
    dropped: it names no entity, and an empty subject list is no subjects rather
    than a statement about none.

    Absent, unreadable and empty all mean the same thing here: nobody said what
    the claims were about.
    """
    if not isinstance(declared, dict):
        return {}

    stated = set(asked)
    kept = {
        str(statement): [str(key).strip() for key in keys if str(key).strip()]
        for statement, keys in declared.items()
        if statement in stated and isinstance(keys, list)
    }
    return {statement: keys for statement, keys in kept.items() if keys}
