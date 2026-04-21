# Kafka Ingestion

Stream security findings into Vigil from Apache Kafka topics. Runs
alongside the existing REST polling (`daemon/poller.py`) and file/webhook
upload (`backend/api/ingestion.py`) paths — Kafka messages land in the
same processing pipeline (triage, enrichment, autonomous investigation).

---

## When to use it

- You already publish security events to Kafka (Splunk HEC forwarders, Falco, custom
  producers) and want Vigil to consume them directly, rather than polling the source.
- You need lower ingestion latency than polling (REST pollers run on 60–300s intervals).
- You want horizontal scalability via Kafka consumer groups.

If none of those apply, stick with polling or the webhook upload — Kafka adds infra
overhead you don't need.

---

## Scope (MVP)

| Supported                                            | Not supported (yet)                   |
|------------------------------------------------------|---------------------------------------|
| JSON messages                                        | Avro, Protobuf                        |
| One consumer group                                   | Confluent Schema Registry             |
| Redis-backed durable dedup                           | Dead-letter topic                     |
| PLAINTEXT / SSL / SASL_PLAINTEXT / SASL_SSL          | Per-partition lag metrics / Grafana   |
| Settings UI config + env-var fallback                | Topic → normalizer mapping            |

See the "Follow-ups" section of
[plans/please-examine-gh-issue-squishy-hippo.md](../.claude/plans/please-examine-gh-issue-squishy-hippo.md)
or [GH issue #83](https://github.com/Vigil-SOC/vigil/issues/83) for the
post-MVP roadmap.

---

## Quick start

### 1. Start the Kafka broker

Vigil ships a single-broker KRaft-mode Kafka service under the `kafka`
Docker Compose profile (no Zookeeper):

```bash
cd docker
docker compose --profile kafka up -d kafka
```

Broker listens on `localhost:9092` (host) and `kafka:29092` (inside the
`deeptempo-network` Docker network). Data is persisted in the
`kafka_data` named volume.

### 2. Create a topic (optional)

Auto-create is enabled in the bundled broker, so producers can publish
to new topics on first write. To pre-create one:

```bash
docker exec deeptempo-kafka /opt/kafka/bin/kafka-topics.sh \
  --bootstrap-server localhost:9092 \
  --create --if-not-exists --topic security.findings \
  --partitions 1 --replication-factor 1
```

### 3. Enable Kafka in Vigil

Two equivalent paths — pick whichever you prefer.

**Option A: Settings UI** (recommended)

1. Open Vigil → **Settings** → **Kafka**.
2. Toggle **Enable Kafka consumer** on.
3. Set **Bootstrap servers** (e.g. `localhost:9092` for local dev,
   `kafka:29092` when the daemon runs inside Docker).
4. Add one or more **Topics**.
5. Click **Save Kafka settings**. The daemon picks up the change within
   ~5 seconds — no restart needed.

**Option B: REST API**

```bash
curl -X PUT http://localhost:6987/api/kafka/config \
  -H 'Content-Type: application/json' \
  -d '{
    "enabled": true,
    "bootstrap_servers": "localhost:9092",
    "consumer_group": "vigil-soc",
    "topics": ["security.findings"],
    "auto_offset_reset": "latest",
    "security_protocol": "PLAINTEXT",
    "max_poll_records": 500,
    "session_timeout_ms": 30000
  }'
```

**Option C: Env vars only** (no DB write)

Set these before starting the daemon; they're the fallback when no
`kafka.settings` row exists in `SystemConfig`:

```bash
export KAFKA_ENABLED=true
export KAFKA_BOOTSTRAP_SERVERS=localhost:9092
export KAFKA_TOPICS=security.findings
```

### 4. Publish a message

Messages must be UTF-8 JSON objects. At minimum each message needs a
`finding_id`. Everything else is passed through to
`IngestionService.ingest_finding` — same schema as the REST upload
endpoint.

```python
# produce.py
import asyncio, json
from aiokafka import AIOKafkaProducer

async def main():
    p = AIOKafkaProducer(bootstrap_servers="localhost:9092")
    await p.start()
    try:
        finding = {
            "finding_id": "my-producer-0001",
            "data_source": "my-edr",
            "severity": "high",
            "description": "Suspicious PowerShell command line",
            "entity_context": {
                "src_ips": ["10.0.0.42"],
                "hostnames": ["WORKSTATION-07"],
                "usernames": ["alice"],
            },
        }
        await p.send_and_wait("security.findings", json.dumps(finding).encode())
    finally:
        await p.stop()

asyncio.run(main())
```

```bash
python produce.py
```

### 5. Verify

- **Settings UI → Kafka tab**: the status panel shows
  `CONNECTED — N consumed, N enqueued, N dupes skipped` and a last-message
  timestamp.
- **Findings page**: the new finding appears with `data_source` set to
  `kafka:<topic>` (auto-populated if the producer didn't set one).
- **Daemon health**: `curl http://localhost:9091/status | jq .kafka`
  gives the same stats the UI shows.
- **Direct DB check**:

  ```bash
  docker exec -e PGPASSWORD=$POSTGRES_PASSWORD deeptempo-postgres \
    psql -U deeptempo -d deeptempo_soc \
    -c "SELECT finding_id, data_source, severity FROM findings \
        WHERE data_source LIKE 'kafka:%' ORDER BY created_at DESC LIMIT 5;"
  ```

---

## Message format

The consumer accepts any JSON object with a `finding_id` string. Common
fields (all optional except `finding_id`):

```jsonc
{
  "finding_id": "unique-string",       // required — used for dedup
  "data_source": "my-edr",             // defaults to "kafka:<topic>"
  "severity": "critical|high|medium|low",
  "description": "human-readable summary",
  "entity_context": {
    "src_ips":    ["1.2.3.4"],
    "dest_ips":   ["5.6.7.8"],
    "hostnames":  ["host-01"],
    "usernames":  ["alice"]
  },
  "mitre_predictions": ["T1059.001"],
  "anomaly_score": 0.87,
  "timestamp": "2026-04-21T12:00:00Z"
}
```

Malformed messages are logged at WARNING and skipped — the consumer
keeps running. Check `stats.decode_errors` / `stats.missing_id_errors`
in the status endpoint to see how many were dropped.

---

## Deduplication

Each `finding_id` is tracked in a Redis sorted set
(`vigil:dedup:kafka`). A duplicate within the retention window (24h
TTL, capped at 10k entries) is skipped and `stats.duplicates_skipped`
is incremented — the finding is **not** re-enqueued and does **not**
create a second DB row.

If Redis is unreachable, the consumer falls back to an in-memory set
(identical behaviour to the pre-Kafka poller). A warning is logged
and the dedup set is lost on daemon restart.

The same `RedisDedupSet` now backs the REST poller too
(`daemon/poller.py`), so poller restarts no longer re-process findings.

---

## Configuration reference

All env vars, with their defaults:

| Variable | Default | Purpose |
|---|---|---|
| `KAFKA_ENABLED` | `false` | Master switch — also toggleable in UI |
| `KAFKA_BOOTSTRAP_SERVERS` | `localhost:9092` | Broker list |
| `KAFKA_CONSUMER_GROUP` | `vigil-soc` | Consumer group id |
| `KAFKA_TOPICS` | _(empty)_ | Comma-separated list |
| `KAFKA_AUTO_OFFSET_RESET` | `latest` | `latest` or `earliest` |
| `KAFKA_SECURITY_PROTOCOL` | `PLAINTEXT` | `PLAINTEXT`\|`SSL`\|`SASL_PLAINTEXT`\|`SASL_SSL` |
| `KAFKA_SASL_MECHANISM` | _(empty)_ | `PLAIN`, `SCRAM-SHA-256`, `SCRAM-SHA-512` |
| `KAFKA_SASL_USERNAME` | _(empty)_ | |
| `KAFKA_SASL_PASSWORD` | _(empty)_ | **env-only — never stored in DB** |
| `KAFKA_SSL_CA_LOCATION` | _(empty)_ | Path to CA cert — **env-only** |
| `KAFKA_MAX_POLL_RECORDS` | `500` | Per-poll batch size |
| `KAFKA_SESSION_TIMEOUT_MS` | `30000` | Broker session timeout |

**Precedence:** env vars set the defaults when the daemon starts;
values in `SystemConfig["kafka.settings"]` (written by the Settings
UI / `PUT /api/kafka/config`) override on top. Secrets (`SASL_PASSWORD`,
`SSL_CA_LOCATION`) are **never** read from or written to the DB — they
must be set via env.

---

## Applying config changes

- **Enabled flag**: picked up within ~5s by the daemon's sync loop. No
  restart needed.
- **Other fields (topics, broker list, consumer group…)**: the daemon
  reads them into its in-memory config on each sync tick, but the
  running consumer isn't rebuilt automatically. To apply changes to a
  running consumer, **toggle Enabled off and back on** via the Settings
  UI. This will gracefully stop the consumer (committing offsets) and
  restart it with the new config.

A future issue will make non-enabled config changes trigger an
automatic restart.

---

## Docker deployment

In production docker-compose, the daemon reads Kafka env vars from the
`soc-daemon` service block. When running everything in Docker:

```bash
KAFKA_ENABLED=true \
KAFKA_BOOTSTRAP_SERVERS=kafka:29092 \
KAFKA_TOPICS=security.findings \
docker compose --profile kafka up -d
```

Note the internal hostname `kafka:29092` (the `INTERNAL` listener),
not `localhost:9092` — that one's for producers running on the host.

---

## REST endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/kafka/config` | Current persisted config (secrets redacted) |
| PUT | `/api/kafka/config` | Upsert config — body matches `KafkaConfigBody` |
| GET | `/api/kafka/status` | Config + live stats from the daemon |

`GET /api/kafka/status` response shape:

```json
{
  "enabled": true,
  "daemon_reachable": true,
  "config": { /* persisted non-secret config */ },
  "stats": {
    "connected": true,
    "messages_consumed": 42,
    "messages_enqueued": 40,
    "duplicates_skipped": 1,
    "decode_errors": 1,
    "missing_id_errors": 0,
    "last_message_at": "2026-04-21T12:00:00",
    "last_error": null,
    "last_error_at": null,
    "topics": ["security.findings"],
    "consumer_group": "vigil-soc"
  }
}
```

If the daemon's health endpoint (port 9091) is unreachable,
`daemon_reachable` is `false` and `stats` is returned with zeroed
counters so the UI still renders.

---

## Troubleshooting

**UI shows `ENABLED (not yet connected)` indefinitely**
- Check `logs/daemon.log` for `aiokafka` errors.
- Verify broker reachability: `docker exec deeptempo-daemon nc -z kafka 29092`
  (inside Docker) or `nc -z localhost 9092` (from host).
- If using SASL/SSL, confirm the secret env vars are set on the
  daemon process — they're env-only.

**Messages not deduplicated across daemon restarts**
- Redis is probably unreachable — check for
  `RedisDedupSet[kafka] using in-memory fallback` in `logs/daemon.log`.
- Once Redis is healthy, restart the daemon; dedup will rebuild
  automatically on first message.

**Consumer keeps re-reading old messages after restart**
- You changed `consumer_group` — a new group starts from the earliest
  offset (or `latest`, depending on `auto_offset_reset`). Use the same
  group id to pick up where the previous instance left off.

**"Decoding error on topic X" logged repeatedly**
- A producer is publishing non-JSON or malformed JSON. The consumer
  skips these safely — find the producer via the topic name and fix it
  there. Check `stats.decode_errors` for volume.

**Changes to topics don't take effect**
- Toggle **Enabled** off and back on in the Settings UI (see "Applying
  config changes" above).

---

## Related files

- [services/kafka_consumer_service.py](../services/kafka_consumer_service.py) — consumer loop
- [daemon/kafka_ingestor.py](../daemon/kafka_ingestor.py) — start/stop wrapper
- [daemon/dedup.py](../daemon/dedup.py) — Redis-backed dedup shared with the poller
- [backend/api/kafka.py](../backend/api/kafka.py) — REST endpoints
- [frontend/src/components/settings/KafkaTab.tsx](../frontend/src/components/settings/KafkaTab.tsx) — UI
- [docker/docker-compose.yml](../docker/docker-compose.yml) — `kafka` profile
- [tests/unit/test_dedup.py](../tests/unit/test_dedup.py), [tests/unit/test_kafka_consumer.py](../tests/unit/test_kafka_consumer.py) — unit tests
