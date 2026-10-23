import importlib.metadata as m, types, collections.abc as abc
from mcp.types import Tool, ListToolsResult
print("mcp", m.version("mcp"))
class M(abc.Mapping):
    def __init__(s): s.d={"type":"object"}
    def __getitem__(s,k): return s.d[k]
    def __iter__(s): return iter(s.d)
    def __len__(s): return 1
cases={"dict":{"type":"object"},"None":None,"list":[["type","object"]],"str":"x","int":1,
       "mappingproxy":types.MappingProxyType({"type":"object"}),"Mapping":M()}
for k,v in cases.items():
    for how in ("ctor","wire"):
        try:
            t = Tool(name="t", inputSchema=v) if how=="ctor" else ListToolsResult.model_validate({"tools":[{"name":"t","inputSchema":v}]}).tools[0]
            r=type(t.input_schema).__name__
            assert type(t.input_schema) is dict, f"NON-DICT SURVIVED: {r}"
            print(how,k,"->",r)
        except AssertionError: raise
        except Exception as e: print(how,k,"-> rejected",type(e).__name__)
try: ListToolsResult.model_validate({"tools":[{"name":"t"}]}); print("missing -> accepted")
except Exception as e: print("missing -> rejected",type(e).__name__)
