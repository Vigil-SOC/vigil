import asyncio, json, sys
from unittest.mock import MagicMock, patch
from core.ingestion.kafka_config import KafkaConfig
from core.ingestion.kafka_consumer_service import KafkaConsumerService
from services.daemon.kafka_ingestor import KafkaIngestor
from core.ingestion import kafka_router

def router(stored, raises=False, daemon=None):
    cs = MagicMock()
    if raises: cs.get_system_config.side_effect = RuntimeError("db down")
    else: cs.get_system_config.return_value = stored
    async def fetch(): return daemon
    with patch.object(kafka_router, "get_config_service", return_value=cs), \
         patch.object(kafka_router, "_fetch_daemon_kafka_stats", fetch):
        return asyncio.run(kafka_router.get_kafka_status())

out = {}
for name, cfg in {"defaults": {}, "set": {"topics": ["alerts", "b"], "consumer_group": "g2"}}.items():
    c = KafkaConfig(**cfg)
    svc = KafkaConsumerService(c, asyncio.Queue(), dedup=MagicMock())
    ing = KafkaIngestor(c)
    c.topics.append("later")
    out[f"svc/{name}"] = svc.stats
    out[f"ing/{name}"] = ing.stats
    ing._service = svc; svc.stats["messages_consumed"] = 7
    out[f"ing-live/{name}"] = [ing.stats, ing.stats is svc.stats]
for name, kw in {
    "none": dict(stored=None), "empty": dict(stored={}),
    "set": dict(stored={"topics": ["db"], "consumer_group": "dbg", "sasl_password": "s"}),
    "badtypes": dict(stored={"topics": "x,y", "consumer_group": None}),
    "notdict": dict(stored=["junk"]), "raises": dict(stored=None, raises=True),
    "daemon": dict(stored={}, daemon={"connected": True, "messages_consumed": 3}),
}.items():
    out[f"router/{name}"] = router(**kw)
print(json.dumps(out, indent=1, sort_keys=True, default=str))
