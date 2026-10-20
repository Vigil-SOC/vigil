"""The responder finds an isolation target under any source-address key."""

from unittest.mock import Mock

import pytest

from services.daemon.config import EscalationConfig, ResponseConfig
from services.daemon.responder import AutonomousResponder


def _responder():
    service = Mock()
    service.create_isolation_action.return_value = {"status": "pending_approval"}
    responder = AutonomousResponder(
        ResponseConfig(dry_run=False),
        EscalationConfig(),
        response_service=service,
        approvals=Mock(),
    )
    return responder, service


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entity_context",
    [
        {"src_ip": "10.11.11.5"},
        {"src_ips": ["10.11.11.5"]},
        {"src_ip": ["127.0.0.1", "10.11.11.5"]},
        {"source_ips": ["10.11.11.5"]},
    ],
)
async def test_isolation_target_from_any_source_key(entity_context):
    responder, service = _responder()
    await responder._create_response_action(
        {"finding_id": "f-1"}, "isolate", entity_context, "rule"
    )
    assert (
        service.create_isolation_action.call_args.kwargs["ip_address"] == "10.11.11.5"
    )


@pytest.mark.asyncio
async def test_dst_ip_alone_is_not_a_target():
    responder, service = _responder()
    await responder._create_response_action(
        {"finding_id": "f-1"}, "isolate", {"dst_ip": "10.11.11.5"}, "rule"
    )
    service.create_isolation_action.assert_not_called()


@pytest.mark.asyncio
async def test_singular_hostname_is_a_target():
    responder, service = _responder()
    await responder._create_response_action(
        {"finding_id": "f-1"}, "isolate", {"hostname": "ws-07"}, "rule"
    )
    assert service.create_isolation_action.call_args.kwargs["hostname"] == "ws-07"


def test_escalation_message_lists_every_source_key():
    responder, _ = _responder()
    message = responder._build_escalation_message(
        {
            "finding_id": "f-1",
            "entity_context": {
                "src_ip": "10.11.11.5",
                "source_ips": ["10.11.11.6", "10.11.11.5"],
                "hostname": "ws-07",
            },
        },
        "isolate",
    )
    assert "- IPs: 10.11.11.5, 10.11.11.6" in message
    assert "- Hosts: ws-07" in message
