"""Outside sampler (C7-limits).

Run the same way as tests/medic_compose, without the backend conftest::

    python -m pytest --noconftest -o addopts= tests/medic_eval
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

from tests.medic_eval.sample import (
    _HELM_SELECTOR,
    BUDGET,
    COMPOSE_CONTAINERS,
    DOCKER_STATS_FORMAT,
    parse_bytes,
    parse_cpu_percent,
    parse_cpu_quantity,
    parse_docker_stats,
    parse_du_kib,
    parse_kubectl_top,
    percentile,
    run_sample,
    slope_per_day,
)

_DAY = 86400.0
_MIB = 1024.0 * 1024.0

# Two formatted snapshots, one day apart, mixed IEC and SI units.
_STATS = [
    (
        "deeptempo-medic\t100.00%\t128MiB / 1GiB\n"
        "deeptempo-medic-gateway\t0.00%\t1.5MB / 256MB\n"
        "deeptempo-medic-dockerproxy\t50.00%\t512KiB / 256MiB"
    ),
    (
        "deeptempo-medic\t0.00%\t256MiB / 1GiB\n"
        "deeptempo-medic-gateway\t200.00%\t2.5MB / 256MB\n"
        "deeptempo-medic-dockerproxy\t10.00%\t1GiB / 2GiB"
    ),
]
# du -sk: 0, then 12 MiB.
_DU = ["0\t/var/lib/vigil-medic\n", f"{12 * 1024}\t/var/lib/vigil-medic\n"]


def _clock():
    state = {"t": 0.0}

    def monotonic():
        return state["t"]

    def sleep(seconds):
        state["t"] += seconds

    return monotonic, sleep


def _docker_run():
    calls = {"n": 0, "argv": []}

    def run(argv):
        calls["argv"].append(list(argv))
        if argv[1] == "stats":
            return _STATS[calls["n"]] + "\n"
        if argv[1] == "exec":
            text = _DU[calls["n"]]
            calls["n"] += 1
            return text
        raise AssertionError(argv)

    return calls, run


def test_budget_is_the_epic_pass_bar():
    assert BUDGET == {
        "cpu_p95_vcpu": 0.5,
        "peak_rss_mib": 512.0,
        "rss_slope_mib_per_day": 5.0,
        "store_growth_mib_per_day": 12.0,
    }


def test_parse_size_units():
    assert parse_bytes("512KiB") == 512 * 1024
    assert parse_bytes("128MiB") == 128 * _MIB
    assert parse_bytes("1GiB") == 1024.0**3
    assert parse_bytes("1.5MB") == 1.5 * 1000 * 1000
    assert parse_bytes("2GB") == 2 * 1000**3
    assert parse_bytes("128Mi") == 128 * _MIB
    assert parse_bytes("40960Ki") == 40960 * 1024
    assert parse_bytes("1024") == 1024
    assert parse_bytes("0B") == 0
    assert parse_cpu_percent("100%") == 1
    assert parse_cpu_percent("150.0") == 1.5
    assert parse_cpu_quantity("500m") == 0.5
    assert parse_cpu_quantity("2") == 2
    assert parse_cpu_quantity("100n") == 1e-7


def test_parse_docker_stats_table_and_mixed_units():
    table = (
        "CONTAINER ID   NAME                CPU %     MEM USAGE / LIMIT   MEM %\n"
        "92310b1e2c60   vigil-sampler-fmt   99.29%    456KiB / 15.63GiB   0.00%\n"
    )
    rows = parse_docker_stats(table)
    assert rows == [("vigil-sampler-fmt", parse_cpu_percent("99.29%"), 456 / 1024)]
    formatted = parse_docker_stats(_STATS[0])
    assert formatted[0] == ("deeptempo-medic", 1.0, 128.0)
    assert formatted[1][0] == "deeptempo-medic-gateway"
    assert formatted[1][2] == (1.5 * 1000 * 1000) / _MIB
    assert formatted[2][2] == 0.5  # 512 KiB


def test_slope_and_percentile():
    # 0, 0, 3 MiB at day 0, 1, 2 → slope 1.5 MiB/day.
    assert slope_per_day([0.0, _DAY, 2 * _DAY], [0.0, 0.0, 3.0]) == 1.5
    assert slope_per_day([0.0], [4.0]) == 0.0
    assert percentile([1.0]) == 1.0
    # Two points: rank 0.95 of sorted [0, 1] is 0.95.
    assert percentile([1.0, 0.0]) == 0.0 * 0.05 + 1.0 * 0.95


def test_docker_backend_exact_numbers():
    calls, run = _docker_run()
    monotonic, sleep = _clock()
    report = run_sample(
        "compose",
        interval=_DAY,
        duration=_DAY,
        monotonic=monotonic,
        sleep=sleep,
        run=run,
    )
    assert report["sample_count"] == 2
    assert report["elapsed_s"] == _DAY
    by_name = {item["name"]: item for item in report["components"]}
    assert list(by_name) == list(COMPOSE_CONTAINERS)

    medic = by_name["deeptempo-medic"]
    assert medic["cpu_p95_vcpu"] == percentile([1.0, 0.0])
    assert medic["peak_rss_mib"] == 256.0
    assert medic["rss_slope_mib_per_day"] == 128.0
    assert medic["within_budget"] is False  # p95 above 0.5 vCPU, slope above 5 MiB/day

    gateway_rss = [(1.5 * 1000 * 1000) / _MIB, (2.5 * 1000 * 1000) / _MIB]
    gateway = by_name["deeptempo-medic-gateway"]
    assert gateway["cpu_p95_vcpu"] == percentile([0.0, 2.0])
    assert gateway["peak_rss_mib"] == gateway_rss[1]
    assert gateway["rss_slope_mib_per_day"] == gateway_rss[1] - gateway_rss[0]

    proxy = by_name["deeptempo-medic-dockerproxy"]
    assert proxy["cpu_p95_vcpu"] == percentile([0.5, 0.1])
    assert proxy["peak_rss_mib"] == 1024.0
    assert proxy["rss_slope_mib_per_day"] == 1024.0 - 0.5
    assert proxy["within_budget"] is False

    assert report["store"]["path"] == "/var/lib/vigil-medic"
    assert report["store"]["growth_mib_per_day"] == 12.0
    assert report["store"]["within_budget"] is True
    assert report["budget"] == BUDGET
    assert report["within_budget"] is False

    stats_argv = calls["argv"][0]
    assert stats_argv[:4] == ["docker", "stats", "--no-stream", "--format"]
    assert stats_argv[4] == DOCKER_STATS_FORMAT
    assert stats_argv[5:] == list(COMPOSE_CONTAINERS)
    assert calls["argv"][1][:4] == ["docker", "exec", "deeptempo-medic", "du"]
    assert calls["argv"][1][4:] == ["-sk", "/var/lib/vigil-medic"]
    assert parse_du_kib(_DU[1]) == 12 * 1024 * 1024


def test_helm_quantities_and_store():
    snapshots = [
        "vigil-medic-aaa 500m 256Mi\nvigil-medic-gateway-bbb 0m 40Mi\n",
        "vigil-medic-aaa 0m 512Mi\nvigil-medic-gateway-bbb 1000m 40Mi\n",
    ]
    du = ["0\t/var/lib/vigil-medic\n", f"{12 * 1024}\t/var/lib/vigil-medic\n"]
    calls = {"n": 0, "argv": []}

    def run(argv):
        calls["argv"].append(list(argv))
        if argv[:3] == ["kubectl", "top", "pod"]:
            return snapshots[calls["n"]]
        text = du[calls["n"]]
        calls["n"] += 1
        return text

    monotonic, sleep = _clock()
    report = run_sample(
        "helm",
        interval=_DAY,
        duration=_DAY,
        monotonic=monotonic,
        sleep=sleep,
        run=run,
    )
    by_name = {item["name"]: item for item in report["components"]}
    medic = by_name["vigil-medic-aaa"]
    assert medic["cpu_p95_vcpu"] == percentile([0.5, 0.0])
    assert medic["peak_rss_mib"] == 512.0
    assert medic["rss_slope_mib_per_day"] == 256.0
    assert by_name["vigil-medic-gateway-bbb"]["peak_rss_mib"] == 40.0
    assert report["store"]["growth_mib_per_day"] == 12.0
    assert calls["argv"][0][:4] == ["kubectl", "top", "pod", "--no-headers"]
    assert _HELM_SELECTOR in calls["argv"][0]
    assert calls["argv"][1][:5] == [
        "kubectl",
        "exec",
        "vigil-medic-aaa",
        "--",
        "du",
    ]
    assert parse_kubectl_top(snapshots[0])[1][2] == 40.0


def test_store_growth_temp_directory(tmp_path):
    monotonic, advance = _clock()

    def sleep(seconds):
        (tmp_path / "grown.bin").write_bytes(b"x" * int(_MIB))
        advance(seconds)

    report = run_sample(
        "host",
        interval=_DAY,
        duration=_DAY,
        targets=[f"self={os.getpid()}"],
        store=tmp_path,
        monotonic=monotonic,
        sleep=sleep,
    )
    assert report["backend"] == "host"
    assert report["sample_count"] == 2
    assert report["store"]["path"] == str(tmp_path)
    assert report["store"]["growth_mib_per_day"] == 1.0
    assert set(report["components"][0]) == {
        "name",
        "cpu_p95_vcpu",
        "peak_rss_mib",
        "rss_slope_mib_per_day",
        "within_budget",
    }


def test_host_cpu_is_interval_not_lifetime():
    """Lifetime %cpu must not stand in for the window. 30s of CPU over 60s is 0.5."""
    snaps = [
        "42 0 1024 80.0\n",
        "42 30 2048 80.0\n",
        "42 60 3072 80.0\n",
    ]
    index = {"n": 0}

    def run(argv):
        assert argv[:3] == ["ps", "-o", "pid=,cputimes=,rss=,pcpu="]
        text = snaps[index["n"]]
        index["n"] += 1
        return text

    monotonic, sleep = _clock()
    report = run_sample(
        "host",
        interval=60,
        duration=120,
        targets=["busy=42"],
        measure_store=False,
        run=run,
        monotonic=monotonic,
        sleep=sleep,
    )
    component = report["components"][0]
    assert component["cpu_p95_vcpu"] == 0.5
    assert component["peak_rss_mib"] == 3.0
    assert component["rss_slope_mib_per_day"] == slope_per_day(
        [0.0, 60.0, 120.0], [1.0, 2.0, 3.0]
    )


def test_helm_pod_may_come_and_go():
    snaps = [
        "vigil-medic-aaa 100m 100Mi\n",
        "vigil-medic-aaa 100m 100Mi\nvigil-medic-bbb 200m 200Mi\n",
        "vigil-medic-bbb 200m 400Mi\n",
    ]
    index = {"n": 0}

    def run(argv):
        text = snaps[index["n"]]
        index["n"] += 1
        return text

    monotonic, sleep = _clock()
    report = run_sample(
        "helm",
        interval=60,
        duration=120,
        measure_store=False,
        monotonic=monotonic,
        sleep=sleep,
        run=run,
    )
    by_name = {item["name"]: item for item in report["components"]}
    assert list(by_name) == ["vigil-medic-aaa", "vigil-medic-bbb"]
    assert by_name["vigil-medic-aaa"]["peak_rss_mib"] == 100.0
    assert by_name["vigil-medic-aaa"]["cpu_p95_vcpu"] == 0.1
    assert by_name["vigil-medic-bbb"]["peak_rss_mib"] == 400.0
    assert by_name["vigil-medic-bbb"]["cpu_p95_vcpu"] == 0.2


def test_ps_busy_child():
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import time\n"
                "buf = []\n"
                "deadline = time.monotonic() + 12\n"
                "while time.monotonic() < deadline:\n"
                "    buf.append(bytearray(512 * 1024))\n"
                "    acc = 0\n"
                "    for i in range(30000):\n"
                "        acc += i\n"
            ),
        ]
    )
    try:
        time.sleep(0.2)
        report = run_sample(
            "host",
            interval=1.0,
            duration=3.0,
            targets=[f"busy={child.pid}"],
            measure_store=False,
        )
    finally:
        child.terminate()
        child.wait(timeout=5)

    assert report["backend"] == "host"
    assert report["store"] is None
    assert report["sample_count"] >= 2
    component = report["components"][0]
    assert component["name"] == "busy"
    assert component["cpu_p95_vcpu"] > 0
    assert component["peak_rss_mib"] > 0
    assert component["rss_slope_mib_per_day"] > 0
    assert set(report) == {
        "backend",
        "interval_s",
        "duration_s",
        "sample_count",
        "elapsed_s",
        "budget",
        "within_budget",
        "store",
        "components",
    }
