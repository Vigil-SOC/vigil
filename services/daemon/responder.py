"""Autonomous response handler for the SOC daemon."""

import asyncio
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from core.response.approval_service import ApprovalService
from core.response.autonomous_response_service import AutonomousResponseService
from services.daemon.config import EscalationConfig, ResponseConfig

logger = logging.getLogger(__name__)


class AutonomousResponder:
    """Handles autonomous response actions with escalation."""

    def __init__(
        self,
        response_config: ResponseConfig,
        escalation_config: EscalationConfig,
        response_service: AutonomousResponseService,
        approvals: ApprovalService,
    ):
        self.response_config = response_config
        self.escalation_config = escalation_config
        self.input_queue: asyncio.Queue = asyncio.Queue()

        self._response_service = response_service
        self._approval_service = approvals
        if response_config.force_manual_approval:
            self._approval_service.set_force_manual_approval(True)

        # Stats
        self.stats = {
            "evaluated": 0,
            "auto_executed": 0,
            "pending_approval": 0,
            "escalated": 0,
            "errors": 0,
        }

    async def run(self, shutdown_event: asyncio.Event):
        """Run the response handler loop."""
        logger.info("Autonomous responder starting...")

        # Start worker tasks
        workers = [
            asyncio.create_task(self._response_worker(shutdown_event)),
            asyncio.create_task(self._approved_action_executor(shutdown_event)),
        ]

        await shutdown_event.wait()

        for worker in workers:
            worker.cancel()

        await asyncio.gather(*workers, return_exceptions=True)
        logger.info("Autonomous responder stopped")

    async def _response_worker(self, shutdown_event: asyncio.Event):
        """Process response candidates from queue."""
        while not shutdown_event.is_set():
            try:
                try:
                    item = await asyncio.wait_for(self.input_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue

                if item.get("type") == "response_candidate":
                    await self._evaluate_response(item["finding"])

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Response worker error: {e}")
                self.stats["errors"] += 1

    async def _approved_action_executor(self, shutdown_event: asyncio.Event):
        """Periodically execute approved actions."""
        while not shutdown_event.is_set():
            try:
                if self.response_config.auto_response_enabled:
                    # Execute approved actions
                    results = self._response_service.execute_approved_actions()

                    for result in results:
                        if result.get("result", {}).get("success"):
                            self.stats["auto_executed"] += 1
                            logger.info(
                                f"Executed approved action: {result.get('action_id')}"
                            )
                        else:
                            logger.warning(f"Action execution failed: {result}")

            except Exception as e:
                logger.error(f"Action executor error: {e}")

            # Check every 30 seconds
            try:
                await asyncio.wait_for(shutdown_event.wait(), timeout=30)
                break
            except asyncio.TimeoutError:
                pass

    async def _evaluate_response(self, finding: Dict[str, Any]):
        """Evaluate finding and determine response action."""
        finding_id = finding.get("finding_id", "unknown")
        self.stats["evaluated"] += 1

        logger.debug(f"Evaluating response for finding {finding_id}")

        if not self.response_config.auto_response_enabled:
            logger.debug("Auto-response disabled, skipping")
            return

        # Extract relevant data
        severity = finding.get("severity", "medium").lower()
        confidence = finding.get("triage_confidence", 0.5)
        recommended_action = finding.get("recommended_action", "").lower()
        entity_context = finding.get("entity_context", {})

        # Determine if response is needed
        response_action = self._determine_action(
            severity, confidence, recommended_action
        )

        if not response_action:
            logger.debug(f"No response action needed for {finding_id}")
            return

        # Check if escalation is needed
        should_escalate = self._should_escalate(severity, confidence)

        if should_escalate:
            await self._escalate_finding(finding, response_action)

        # Create response action
        if response_action in ["isolate", "block"]:
            await self._create_response_action(finding, response_action, entity_context)

    def _determine_action(
        self, severity: str, confidence: float, recommended: str
    ) -> Optional[str]:
        """Determine what response action to take."""
        # High confidence + recommended isolation/block
        if confidence >= self.response_config.confidence_threshold:
            if recommended in ["isolate", "block"]:
                return recommended

        # Critical severity always warrants action
        if severity == "critical" and confidence >= 0.7:
            return "isolate"

        # High severity with good confidence
        if severity == "high" and confidence >= 0.8:
            return "investigate"

        return None

    def _should_escalate(self, severity: str, confidence: float) -> bool:
        """Determine if finding should be escalated."""
        if not self.escalation_config.enabled:
            return False

        return severity in self.escalation_config.escalate_severities

    async def _escalate_finding(self, finding: Dict[str, Any], action: str):
        """Escalate finding via configured channels."""
        finding_id = finding.get("finding_id")
        severity = finding.get("severity", "unknown")
        title = finding.get("title", "Security Alert")

        message = self._build_escalation_message(finding, action)

        # Slack escalation
        if self.escalation_config.slack_enabled:
            await self._send_slack_alert(message, severity)

        # PagerDuty escalation
        if self.escalation_config.pagerduty_enabled and severity in [
            "critical",
            "high",
        ]:
            await self._send_pagerduty_alert(title, message, severity)

        self.stats["escalated"] += 1
        logger.info(f"Escalated finding {finding_id} (severity: {severity})")

    def _build_escalation_message(self, finding: Dict[str, Any], action: str) -> str:
        """Build escalation message."""
        entity_context = finding.get("entity_context", {})

        parts = [
            f"**Finding ID:** {finding.get('finding_id')}",
            f"**Severity:** {finding.get('severity', 'unknown').upper()}",
            f"**Title:** {finding.get('title', 'N/A')}",
            f"**Source:** {finding.get('data_source', 'unknown')}",
            f"**Recommended Action:** {action.upper()}",
            "",
            "**Affected Entities:**",
        ]

        if entity_context.get("src_ips"):
            parts.append(f"- IPs: {', '.join(entity_context['src_ips'][:5])}")
        if entity_context.get("hostnames"):
            parts.append(f"- Hosts: {', '.join(entity_context['hostnames'][:5])}")
        if entity_context.get("usernames"):
            parts.append(f"- Users: {', '.join(entity_context['usernames'][:5])}")

        if finding.get("triage_reasoning"):
            parts.extend(["", f"**AI Assessment:** {finding['triage_reasoning']}"])

        return "\n".join(parts)

    async def _send_slack_alert(self, message: str, severity: str):
        """Send alert to Slack."""
        try:
            from core.config import get_integration_config

            config = get_integration_config("slack")
            token = config.get("bot_token")

            if not token:
                logger.warning("Slack not configured, skipping escalation")
                return

            import httpx

            color_map = {
                "critical": "#ff0000",
                "high": "#ff9900",
                "medium": "#ffcc00",
                "low": "#36a64f",
            }

            response = await asyncio.to_thread(
                httpx.post,
                "https://slack.com/api/chat.postMessage",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json={
                    "channel": self.escalation_config.slack_channel,
                    "attachments": [
                        {
                            "color": color_map.get(severity, "#808080"),
                            "title": f"🚨 SOC Alert - {severity.upper()}",
                            "text": message,
                            "footer": "AI-SOC Daemon",
                            "ts": datetime.utcnow().timestamp(),
                        }
                    ],
                },
                timeout=30,
                follow_redirects=True,
            )

            if response.status_code == 200 and response.json().get("ok"):
                logger.debug("Slack alert sent successfully")
            else:
                logger.warning(f"Slack alert failed: {response.text}")

        except Exception as e:
            logger.error(f"Slack escalation error: {e}")

    async def _send_pagerduty_alert(self, title: str, details: str, severity: str):
        """Send alert to PagerDuty."""
        try:
            from core.config import get_integration_config

            config = get_integration_config("pagerduty")
            routing_key = config.get("routing_key") or config.get("integration_key")

            if not routing_key:
                logger.warning("PagerDuty not configured, skipping escalation")
                return

            import httpx

            pd_severity = self.escalation_config.pagerduty_severity_map.get(
                severity, "warning"
            )

            response = await asyncio.to_thread(
                httpx.post,
                "https://events.pagerduty.com/v2/enqueue",
                json={
                    "routing_key": routing_key,
                    "event_action": "trigger",
                    "payload": {
                        "summary": title,
                        "source": "ai-soc-daemon",
                        "severity": pd_severity,
                        "custom_details": {"details": details},
                    },
                },
                timeout=30,
                follow_redirects=True,
            )

            data = response.json()
            if data.get("status") == "success":
                logger.debug(f"PagerDuty alert triggered: {data.get('dedup_key')}")
            else:
                logger.warning(f"PagerDuty alert failed: {data}")

        except Exception as e:
            logger.error(f"PagerDuty escalation error: {e}")

    async def _create_response_action(
        self, finding: Dict[str, Any], action_type: str, entity_context: Dict[str, Any]
    ):
        """Create a response action (pending or auto-approved)."""
        if self.response_config.dry_run:
            logger.info(
                f"[DRY RUN] Would create {action_type} action for finding {finding.get('finding_id')}"
            )
            return

        finding_id = finding.get("finding_id")
        confidence = finding.get("triage_confidence", 0.5)

        # Get target
        target_ip = None
        hostname = None

        if entity_context.get("src_ips"):
            target_ip = entity_context["src_ips"][0]
        if entity_context.get("hostnames"):
            hostname = entity_context["hostnames"][0]

        if not target_ip and not hostname:
            logger.warning(f"No target available for response action on {finding_id}")
            return

        # Build correlation data
        correlation_data = self._response_service.correlate_alerts(
            tempo_flow_alert=finding
        )

        # Create the action
        result = self._response_service.create_isolation_action(
            ip_address=target_ip or "unknown",
            hostname=hostname,
            confidence=confidence,
            reason=f"Automated response to {finding_id}",
            evidence=[finding_id],
            correlation_data=correlation_data,
        )

        if result:
            if result.get("status") == "executed":
                self.stats["auto_executed"] += 1
                logger.info(f"Auto-executed {action_type} action for {finding_id}")
            elif result.get("status") == "pending_approval":
                self.stats["pending_approval"] += 1
                logger.info(f"Created pending {action_type} action for {finding_id}")
            else:
                logger.warning(f"Action creation result: {result}")
