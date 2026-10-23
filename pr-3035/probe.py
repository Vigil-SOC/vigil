import asyncio, inspect, os, sys
sys.path.insert(0, os.getcwd())
for k in ("ANTHROPIC_API_KEY", "CLAUDE_API_KEY", "anthropic_api_key"):
    os.environ.pop(k, None)
import core.llm.gateway.gateway as gw
from core.reporting.ai_insights_service import AIInsightsService
calls = []
class G:
    async def submit(self, **kw):
        calls.append(kw); return {"content": '[{"type":"info","title":"t","description":"d"}]'}
async def get(): return G()
gw.get_llm_gateway = get
M = {"totalFindings":1,"findingsChange":0.0,"totalCases":1,"casesChange":0.0,"avgResponseTime":5,"responseTimeChange":0.0,"falsePositiveRate":1,"falsePositiveChange":0.0}
svc = AIInsightsService()
args = (M, [], "7d")
if "db" in inspect.signature(svc.generate_insights).parameters: args = (None,) + args
out = asyncio.run(svc.generate_insights(*args))
print("gateway calls:", len(calls), "titles:", [i["title"] for i in out])
sys.exit(0 if len(calls) == 1 else 1)
