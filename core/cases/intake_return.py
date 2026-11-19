"""Name the person on an alert a removal sends back to intake.

The send-back lives in ``core.storage``, which cannot import this package.
"""

from core.cases.agent_closure import activity_actor
from core.storage.activity_actor_hook import install


def _fields(session) -> dict:
    return activity_actor(session=session)


install(_fields)
