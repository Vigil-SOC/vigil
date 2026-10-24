import tools.mcp.vigil as v, core.cases.agent_closure as ac
from core.integrations.mcp.surface import acting_as
assert v.service_session is ac.service_session and v.actor is ac.actor
print("unbound", v.actor()); 
with acting_as("nestor"): print("bound", v.actor())
with acting_as(""): print("empty", v.actor())
try:
    with v.service_session() as s: s.execute(__import__("sqlalchemy").text("select 1"))
    print("DB reached?!")
except Exception as e: print("unpatched session hits DB:", type(e).__name__)
