import pytest

from services.worker.jobs import WorkerSettings

pytestmark = pytest.mark.unit


def test_worker_waits_out_a_slow_redis():
    rs = WorkerSettings.redis_settings
    # ARQ defaults (5 tries, 1 s timeout, 1 s delay) give up within seconds
    assert rs.conn_retries * (rs.conn_retry_delay + rs.conn_timeout) >= 60
