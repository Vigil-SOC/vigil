"""A built-in's content is pinned to its version (#1617).

Runs record the version of the definition they ran. Editing a built-in without
bumping `version` makes two different definitions read as the same version.
"""

import hashlib
import re
from pathlib import Path

import pytest

from core.workflows.workflows_service import WorkflowsService

pytestmark = pytest.mark.unit

DEFINITIONS = Path(__file__).resolve().parents[3] / "core" / "workflows" / "definitions"

# id -> (version, sha256 of WORKFLOW.md without its `version:` line)
PINNED = {
    "cloud-incident": (
        1,
        "e55d670dad54161adefd0c53b13f848e8f63145f10b620fea02b9cd2ef8080f7",
    ),
    "forensic-analysis": (
        1,
        "5d92035ff67486b3d4ef35bc337fc56c4d2a076848b1f8c5a5d4823a7e83f203",
    ),
    "full-investigation": (
        1,
        "6427075c3fdd4b4cc6d2c061dfa21ac63d87e0278c2618cb1a400bf8365ee156",
    ),
    "incident-response": (
        1,
        "b2ec7cfc1eb091d8c1d55642001bd61a94142a7e7178583383b96dae267f907c",
    ),
    "root-cause-analysis": (
        1,
        "a001d2ee9f2cd3a8914fc789ec3145ecb5615afa0f74a2ccad6231d454dfcf96",
    ),
    "shadow-adjudication": (
        1,
        "af72161dab2da187b1a1c95ddf589723433e9641247b694dd59e9c2b8a01021f",
    ),
    "threat-hunt": (
        1,
        "925d2509a7a106ee3cc7ce9ff27e39f7bf75b355a36de12683c166b291bc120d",
    ),
}


def _digest(path: Path) -> str:
    text = re.sub(
        r"^version:.*\n", "", path.read_text(encoding="utf-8"), count=1, flags=re.M
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_every_builtin_is_pinned():
    on_disk = {p.parent.name for p in DEFINITIONS.glob("*/WORKFLOW.md")}
    assert on_disk == set(PINNED), "add the new built-in to PINNED (version 1)"


@pytest.mark.parametrize("workflow_id", sorted(PINNED))
def test_builtin_content_matches_its_version(workflow_id):
    version, digest = PINNED[workflow_id]
    path = DEFINITIONS / workflow_id / "WORKFLOW.md"
    assert (
        WorkflowsService().get_workflow(workflow_id).version == version
    ), f"{workflow_id}: `version:` in the file and PINNED disagree"
    assert _digest(path) == digest, (
        f"{workflow_id}/WORKFLOW.md changed. Bump `version:` in the file and "
        f"update PINNED to ({version + 1}, {_digest(path)!r})."
    )
