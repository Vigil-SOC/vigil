"""Legacy CrowdStrike poll keeps one finding per detection on a host.

``DataPoller._crowdstrike_detection_to_finding`` used to keep 32 characters
of ``ldt:<agent>:<number>``. The number starts later, so every detection on
one host shared a finding id and the later ones were deduped away.
"""

from core.integrations._base.ids import FINDING_ID_MAX, fit_id
from services.daemon.poller import DataPoller

_SAME_HOST = (
    "ldt:0d6ca15ba400411eafe2e6b38245df9e:236244939242",
    "ldt:0d6ca15ba400411eafe2e6b38245df9e:240543365083",
    "ldt:0d6ca15ba400411eafe2e6b38245df9e:249113529425",
)


def test_legacy_converter_keeps_same_host_detections_distinct():
    ids = [
        DataPoller._crowdstrike_detection_to_finding(
            None, {"detection_id": det, "max_severity_displayname": "High"}
        )["finding_id"]
        for det in _SAME_HOST
    ]
    assert ids == [fit_id("cs-", det, FINDING_ID_MAX) for det in _SAME_HOST]
    assert len(set(ids)) == len(_SAME_HOST)
    assert all(len(i) <= FINDING_ID_MAX for i in ids)


def test_legacy_converter_leaves_a_short_id_intact():
    finding = DataPoller._crowdstrike_detection_to_finding(
        None, {"detection_id": "det-1", "max_severity_displayname": "High"}
    )
    assert finding["finding_id"] == "cs-det-1"
