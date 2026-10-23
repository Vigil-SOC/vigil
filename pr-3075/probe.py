from types import SimpleNamespace
from core.integrations.mcp import registry as R
T=[{"name":"search","description":"d","inputSchema":{}}]
client=SimpleNamespace(tools_cache={"sd":T},mcp_service=SimpleNamespace(servers={}))
r=R.MCPRegistry()
assert R.register_connected(r,client,"sd") is True
assert r.get_active_servers()==["sd"], r.get_active_servers()
R.deactivate(r,"sd"); assert r.get_active_servers()==[] and r.get_all_tools()==[]
r.set_active("ghost",True); assert r.get_active_servers()==[] and "ghost" not in r._servers
r.register_server("sd",T); assert r.get_tool_names()==["sd_search"]
r.register_server("b"); r.set_active("b",False); r.set_active("b",True); assert r.get_active_servers()==["sd","b"]
from core.integrations.mcp import in_process
assert in_process.register(r)>0 and "vigil" in r.get_active_servers()
print("probe ok", r.get_active_servers(), r._servers)
