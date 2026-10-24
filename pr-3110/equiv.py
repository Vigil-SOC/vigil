import importlib.util, sys
spec = importlib.util.spec_from_file_location("old_siem", sys.argv[1]); old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
from core.ingestion.siem_ingestion_service import SIEMIngestionService
from core.integrations.aws_security_hub.ingestion import AWSSecurityHubIngestion
from core.integrations.azure_sentinel.ingestion import AzureSentinelIngestion
from core.integrations.elastic.ingestion import ElasticIngestion
from core.integrations.opensearch.ingestion import OpenSearchIngestion
from core.integrations.microsoft_defender.ingestion import MicrosoftDefenderIngestion
from core.integrations.darktrace.ingestion import DarktraceIngestionService
from core.ingestion.ingestion_service import IngestionService

inputs = [None, "", 0, 1, 2, 3, 4, 5, "CRITICAL", "warning", "notice", "debug", "zzz", 7.5, "Informational", "error", "crit", "Med", "emergency"]
for cls in (AWSSecurityHubIngestion, AzureSentinelIngestion, ElasticIngestion, OpenSearchIngestion, MicrosoftDefenderIngestion):
    inst = object.__new__(cls)
    for x in inputs:
        want = old.SIEMIngestionService.normalize_severity(None, x)
        got = inst.normalize_severity(x)
        assert got == want, (cls.__name__, x, got, want)
print(f"5 SIEM subclasses: method equals base-branch normalize_severity over {len(inputs)} inputs")

dt = DarktraceIngestionService(console_url="https://dt.example.com/")
for status in ["warning", "critical", "error", "notice", "info", "zzz", None]:
    f = dt.transform_system_status({"id": "x", "status": status, "time": 0})
    want = old.SIEMIngestionService.normalize_severity(None, str(status or "info").lower())
    assert f["severity"] == want, (status, f["severity"], want)
print("darktrace system_status severity matches base branch for 7 statuses")
print("darktrace is SIEM subclass:", issubclass(DarktraceIngestionService, SIEMIngestionService))
print("ingestion_service:", type(dt.ingestion_service).__name__, isinstance(dt.ingestion_service, IngestionService))
print("leftover attrs:", [a for a in ("fetch_alerts", "transform_alert_to_finding", "siem_name") if hasattr(dt, a)])
