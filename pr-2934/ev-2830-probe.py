import logging, sys
logging.basicConfig(level=logging.INFO, stream=sys.stdout, format="%(levelname)s %(name)s %(message)s")
from core.cases.case_sla_service import open_case
class Fake:
    def create_case(self, **kw): return {**kw, "case_id": "c-probe"}
case = open_case(Fake(), title="t", finding_ids=[], priority="high", description="")
print("RETURNED", case["case_id"])
assert case["case_id"] == "c-probe"
