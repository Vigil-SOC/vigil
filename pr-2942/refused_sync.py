import asyncio, logging, os, sys
os.environ.update(DEV_MODE="false", JWT_SECRET_KEY="x", VIGIL_DISABLE_DOTENV="1")
sys.path.insert(0, os.getcwd())
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
for n in list(logging.root.manager.loggerDict):
    if n != "core.api.v1.cases_router":
        logging.getLogger(n).setLevel(logging.CRITICAL)
from core.integrations.elastic import ingestion
from core.api.v1 import cases_router as cases

calls = []
async def refuse(self, alert_id, status, note=None):
    calls.append(alert_id); return False
ingestion.ElasticIngestion.__init__ = lambda self: None
ingestion.ElasticIngestion.update_upstream_alert_status = refuse
F = {"f1": {"data_source": "elastic", "metadata": {"elastic_alert_id": "a1"}},
     "f2": {"data_source": "opensearch", "metadata": {"opensearch_finding_id": "o1"}}}
class DS:
    def get_case(self, cid): return {"case_id": cid, "finding_ids": list(F)}
    def get_finding(self, fid): return F[fid]
    def get_findings_by_case(self, cid): return list(F.values())
cases.data_service = DS()
asyncio.run(cases._sync_upstream_status("c1", "closed"))
print("vendor calls:", calls)
