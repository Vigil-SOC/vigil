"""Fixture scores for services.medic.eval.

Times below are 2026-10-07. Fault traces use three abutting windows:
baseline [10:00, 10:10), fault [10:10, 10:20), recovery [10:20, 10:30).
"""

from services.medic.eval import (
    FalseAlarms,
    LaneMatrix,
    Median,
    Rate,
    score,
)

MI = "mi_aaaaaaaaaaaaaaaa"
OTHER = "mi_bbbbbbbbbbbbbbbb"


def z(hms: str) -> str:
    return f"2026-10-07T{hms}Z"


def _exp(fires, may, detect, clear):
    return {
        "fires": list(fires),
        "may_fire": list(may),
        "detect_within_s": detect,
        "clear_within_s": clear,
    }


def labels(
    *,
    mode="P-4",
    lane=2,
    fires=None,
    detect=120,
    clear=180,
    fault_may=(),
    base_may=(),
    rec_may=(),
    injection_at="10:10:00",
    injection=True,
    control=False,
    baseline_end="10:10:00",
):
    doc = {
        "fault": {
            "mode": mode,
            "cause": None if mode is None else f"{mode}.a",
            "class": None if mode is None else "pipeline",
            "lane_expected": lane,
        },
        "windows": {"baseline": {"start": z("10:00:00"), "end": z(baseline_end)}},
        "expected": {"baseline": _exp((), base_may, None, None)},
    }
    if not control:
        doc["windows"]["fault"] = {"start": z("10:10:00"), "end": z("10:20:00")}
        doc["windows"]["recovery"] = {"start": z("10:20:00"), "end": z("10:30:00")}
        doc["expected"]["fault"] = _exp(
            (mode,) if fires is None else fires, fault_may, detect, None
        )
        doc["expected"]["recovery"] = _exp((), rec_may, None, clear)
    if injection:
        doc["injection"] = {"at": z(injection_at), "actions": ["inject"]}
    return doc


def opened(
    at,
    incident,
    mode,
    lane,
    *,
    instance=MI,
    detection="event",
    subject="vigil",
    causes=None,
    active_since=None,
):
    if mode is None:
        fault = None
    else:
        fault = {
            "class": "pipeline",
            "mode": mode,
            "causes": [f"{mode}.a"] if causes is None else list(causes),
        }
    return {
        "type": "incident_opened",
        "at": z(at),
        "body": {
            "incident_id": incident,
            "instance_id": instance,
            "subject": subject,
            "fault": fault,
            "detection_type": detection,
            "lane": {"value": lane, "reason": "rule"},
            "active_since": z(active_since or at),
        },
    }


def resolved(at, incident, how="cleared"):
    return {
        "type": "incident_resolved",
        "at": z(at),
        "body": {"incident_id": incident, "how": how, "eval": "false"},
    }


def cell(doc, instance=MI):
    return {"instance_id": instance, "labels": doc}


def one(doc, records, instance=MI):
    return score((cell(doc, instance),), records).cells[0]


def test_hit_at_the_deadline_uses_fault_mode_only():
    # injection.at is 5s after the window start, so time to detect is 55s,
    # not the 60s from the window. The open lands on the detect deadline.
    doc = labels(
        fires=("P-4", "L-4", "I-2"),
        detect=60,
        clear=180,
        injection_at="10:10:05",
    )
    records = (
        opened("10:10:10", "inc_other", "P-4", 3, instance=OTHER),
        opened("10:10:30", "inc_symptom", "L-4", 1, detection="absence"),
        opened("10:11:00", "inc_hit", "P-4", 2),
        resolved("10:23:00", "inc_hit"),
        resolved("10:29:00", "inc_hit"),  # a later resolve must not fail the clear
        resolved("10:23:01", "inc_symptom"),
        {"type": "feedback", "at": z("10:12:00"), "body": {"incident_id": "inc_hit"}},
    )
    run = score((cell(doc),), records)
    hit, symptom, missing = run.cells[0].modes
    assert hit.outcome == "on_time"
    assert hit.incident_id == "inc_hit"
    assert hit.time_to_detect_s == 55.0
    assert hit.lane == 2
    assert hit.cleared is True
    assert symptom.outcome == "on_time"
    assert symptom.time_to_detect_s == 25.0
    assert symptom.cleared is False
    assert missing.outcome == "miss"
    assert missing.cleared is None
    assert run.cells[0].false_alarms == ()
    # Rate, median and the matrix follow fault.mode (P-4), not every fires mode.
    assert run.metrics.detection_rate == Rate(hits=1, n=1, rate=1.0)
    assert run.metrics.time_to_detect_s == Median(n=1, median=55.0)
    assert run.metrics.time_to_detect_s_by_type["event"] == Median(n=1, median=55.0)
    assert run.metrics.time_to_detect_s_by_type["absence"] == Median(n=0, median=None)
    assert run.metrics.lane_matrix == LaneMatrix(
        cells=((0, 0, 0), (0, 1, 0), (0, 0, 0)), n=1
    )


def test_miss_ignores_cause_edges_and_active_since():
    doc = labels(detect=120, base_may=("P-4",), rec_may=("P-4",), fault_may=())
    got = one(
        doc,
        (
            # Cause id P-4.a on a different mode is not a P-4 open.
            opened("10:11:00", "inc_cause", "I-1", 2, causes=("P-4.a",)),
            opened("10:09:00", "inc_early", "P-4", 2),  # before the fault window
            opened(
                "10:20:00", "inc_boundary", "P-4", 2
            ),  # fault end belongs to recovery
            opened("10:30:00", "inc_after", "P-4", 2, active_since="10:11:00"),
        ),
    )
    assert got.modes[0].outcome == "miss"
    assert got.modes[0].time_to_detect_s is None
    assert got.modes[0].cleared is None
    assert [(a.mode, a.window) for a in got.false_alarms] == [("I-1", "fault")]


def test_late_uses_the_first_open_inside_the_fault_window():
    doc = labels(detect=60, base_may=("P-4",), injection_at="10:10:00")
    got = one(
        doc,
        (
            opened("10:05:00", "inc_early", "P-4", 3),
            opened("10:12:00", "inc_late", "P-4", 2, detection="absence"),
            opened("10:15:00", "inc_later", "P-4", 1),
        ),
    )
    assert got.modes[0].outcome == "late"
    assert got.modes[0].incident_id == "inc_late"
    assert got.modes[0].lane == 2
    assert got.modes[0].time_to_detect_s == 120.0
    assert got.false_alarms == ()


def test_false_alarm_includes_a_null_fault_and_excludes_may_fire():
    doc = labels(fault_may=("L-4",), rec_may=(), base_may=())
    got = one(
        doc,
        (
            opened("10:05:00", "inc_base", "I-1", 1),
            opened("10:12:00", "inc_may", "L-4", 2),
            opened("10:15:00", "inc_blind", None, 3, subject="watcher"),
            opened("10:30:00", "inc_outside", "I-9", 1),
            opened("10:20:00", "inc_recovery", "P-4", 2),
        ),
    )
    assert [(a.incident_id, a.mode, a.window) for a in got.false_alarms] == [
        ("inc_base", "I-1", "baseline"),
        ("inc_blind", None, "fault"),
        ("inc_recovery", "P-4", "recovery"),
    ]
    assert got.opens_in_windows == 4  # the 10:30 open is in no window


def test_wrong_lane_still_counts_as_detected():
    doc = labels(lane=1, detect=120, injection_at="10:10:00")
    run = score((cell(doc),), (opened("10:11:00", "inc_wrong", "P-4", 3),))
    assert run.cells[0].modes[0].outcome == "on_time"
    assert run.cells[0].modes[0].lane == 3
    assert run.metrics.detection_rate == Rate(hits=1, n=1, rate=1.0)
    assert run.metrics.lane_matrix == LaneMatrix(
        cells=((0, 0, 1), (0, 0, 0), (0, 0, 0)), n=1
    )


def test_closed_quietly_is_neither_a_fire_nor_an_alarm():
    doc = labels(detect=120, clear=180)
    got = one(
        doc,
        (
            opened("10:11:00", "inc_child", "P-4", 2),
            resolved("10:12:00", "inc_child", how="cleared"),
            resolved("10:16:00", "inc_child", how="closed_quietly"),
            opened("10:14:00", "inc_extra", "I-9", 1),
            resolved("10:15:00", "inc_extra", how="closed_quietly"),
        ),
    )
    assert got.modes[0].outcome == "miss"
    assert got.false_alarms == ()
    assert got.opens_in_windows == 0


def test_fired_mode_that_does_not_clear():
    doc = labels(detect=120, clear=180, injection_at="10:10:00")
    late = one(
        doc,
        (
            opened("10:11:00", "inc_a", "P-4", 2),
            opened("10:14:00", "inc_b", "P-4", 2),
            resolved("10:21:00", "inc_b"),  # the later incident clearing does not count
        ),
    )
    assert late.modes[0].incident_id == "inc_a"
    assert late.modes[0].outcome == "on_time"
    assert late.modes[0].cleared is False
    after = one(
        doc,
        (
            opened("10:11:00", "inc_a", "P-4", 2),
            resolved("10:23:01", "inc_a"),
        ),
    )
    assert after.modes[0].cleared is False


def test_null_detect_and_clear_windows():
    doc = labels(detect=None, clear=None, injection_at="10:10:00")
    got = one(doc, (opened("10:19:59", "inc_any", "P-4", 2),))
    assert got.modes[0].outcome == "on_time"
    assert got.modes[0].time_to_detect_s == 599.0
    assert got.modes[0].cleared is None


def test_empty_run():
    run = score((), ())
    assert run.cells == ()
    assert run.metrics.detection_rate == Rate(hits=0, n=0, rate=None)
    assert run.metrics.time_to_detect_s == Median(n=0, median=None)
    assert run.metrics.false_alarms == FalseAlarms(count=0, n=0)
    assert run.metrics.lane_matrix.n == 0


def test_fixture_run_matches_hand_computed_metrics():
    """Five injected cells plus one control.

    On time: mi_a (60s, event, lane 2/2, clears), mi_d watcher (570s, event,
    lane 3/3 — in the rate and the median, out of the matrix), mi_f (30s,
    event, lane 2 expected / 1 actual — wrong lane stays a hit).
    Late: mi_c (120s, absence, lane 1/2, does not clear) — in the median, out
    of the rate. Miss: mi_b, which has one false alarm. The control is not
    injected. A stray other-instance open sits at 10:10:01 and must change
    nothing.

    Rate 3/5. Times sorted 30, 60, 120, 570 → median 90 (n=4). Event times
    30, 60, 570 → median 60 (n=3). Absence 120 (n=1). Matrix n=3:
    expected 1 → actual 2, expected 2 → actual 2, expected 2 → actual 1.
    False alarms 1 of 6 opens in windows.
    """
    a = "mi_aaaaaaaaaaaaaaaa"
    b = "mi_bbbbbbbbbbbbbbbb"
    c = "mi_cccccccccccccccc"
    d = "mi_dddddddddddddddd"
    e = "mi_eeeeeeeeeeeeeeee"
    f = "mi_ffffffffffffffff"
    cells = (
        cell(labels(lane=2, detect=120, clear=180), a),
        cell(labels(lane=3, detect=120, clear=180, fault_may=("L-4",)), b),
        cell(labels(lane=1, detect=60, clear=60), c),
        cell(labels(mode="L-2", lane=3, detect=None, clear=None), d),
        cell(
            labels(
                mode=None,
                lane=None,
                injection=False,
                control=True,
                base_may=("L-4",),
                baseline_end="10:30:00",
            ),
            e,
        ),
        cell(labels(lane=2, detect=120, clear=180), f),
    )
    records = (
        opened("10:10:01", "inc_stray", "P-4", 3, instance="mi_0000000000000000"),
        opened("10:11:00", "inc_a", "P-4", 2, instance=a),
        resolved("10:21:00", "inc_a"),
        opened("10:12:00", "inc_alarm", "I-9", 1, instance=b),
        opened("10:12:00", "inc_c", "P-4", 2, instance=c, detection="absence"),
        resolved("10:25:00", "inc_c"),
        opened("10:19:30", "inc_d", "L-2", 3, instance=d, subject="watcher"),
        opened("10:15:00", "inc_e", "L-4", 2, instance=e),
        opened("10:10:30", "inc_f", "P-4", 1, instance=f),
        resolved("10:20:00", "inc_f"),
        {"type": "gap", "at": z("10:00:00"), "body": {"reason": "off"}},
    )
    run = score(cells, records)
    outcomes = [
        (item.instance_id, item.modes[0].outcome if item.modes else None)
        for item in run.cells
    ]
    assert outcomes == [
        (a, "on_time"),
        (b, "miss"),
        (c, "late"),
        (d, "on_time"),
        (e, None),
        (f, "on_time"),
    ]
    assert run.cells[2].modes[0].cleared is False
    assert run.cells[2].modes[0].time_to_detect_s == 120.0
    assert run.cells[3].modes[0].subject == "watcher"
    assert run.cells[3].modes[0].cleared is None
    assert run.cells[3].modes[0].time_to_detect_s == 570.0
    assert run.cells[5].modes[0].lane == 1
    assert run.cells[5].modes[0].time_to_detect_s == 30.0
    assert [(alarm.mode, alarm.window) for alarm in run.cells[1].false_alarms] == [
        ("I-9", "fault")
    ]
    assert run.metrics.detection_rate == Rate(hits=3, n=5, rate=0.6)
    assert run.metrics.time_to_detect_s == Median(n=4, median=90.0)
    assert run.metrics.time_to_detect_s_by_type["event"] == Median(n=3, median=60.0)
    assert run.metrics.time_to_detect_s_by_type["absence"] == Median(n=1, median=120.0)
    assert run.metrics.lane_matrix == LaneMatrix(
        cells=((0, 1, 0), (1, 1, 0), (0, 0, 0)),
        n=3,
    )
    assert run.metrics.false_alarms == FalseAlarms(count=1, n=6)
