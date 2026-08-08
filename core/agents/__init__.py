"""SOC agents domain package (Reorg R1 / #482).

The agent record type, the built-in agent definitions, and the runtime
manager/library live here. Callers import these directly from the
submodules (``core.agents.builtins`` / ``core.agents.manager`` /
``core.agents.prompts``); the legacy ``services.soc_agents`` path was
removed with this move.
"""
