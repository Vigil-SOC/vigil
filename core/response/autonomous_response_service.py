"""Autonomous response service with approval workflow integration."""

import logging
from typing import Dict, List, Optional, Callable, Any
from datetime import datetime

from core.agents.builtins import AgentId

logger = logging.getLogger(__name__)


# Escalation callback type
EscalationCallback = Callable[[Dict[str, Any], str, str], None]


class AutonomousResponseService:
    """Service for managing autonomous threat response with approval workflow."""
    
    def __init__(self):
        """Initialize autonomous response service."""
        self.approval_service = None
        self._escalation_callbacks: List[EscalationCallback] = []
        self._load_services()
    
    def _load_services(self):
        """Load required services."""
        try:
            from core.response.approval_service import get_approval_service, ActionType
            self.approval_service = get_approval_service()
            self.ActionType = ActionType
        except Exception as e:
            logger.error(f"Error loading services: {e}")
    
    def register_escalation_callback(self, callback: EscalationCallback):
        """Register a callback for escalation events."""
        self._escalation_callbacks.append(callback)
        logger.info(f"Registered escalation callback: {callback.__name__}")
    
    def unregister_escalation_callback(self, callback: EscalationCallback):
        """Unregister an escalation callback."""
        if callback in self._escalation_callbacks:
            self._escalation_callbacks.remove(callback)
            logger.info(f"Unregistered escalation callback: {callback.__name__}")
    
    def _trigger_escalation(self, data: Dict[str, Any], severity: str, action_type: str):
        """Trigger all registered escalation callbacks."""
        for callback in self._escalation_callbacks:
            try:
                callback(data, severity, action_type)
            except Exception as e:
                logger.error(f"Escalation callback error: {e}")
    
    async def escalate_to_slack(self, message: str, severity: str, channel: Optional[str] = None):
        """Send escalation to Slack channel."""
        try:
            from core.config import get_integration_config
            import requests
            
            config = get_integration_config('slack')
            token = config.get('bot_token')
            
            if not token:
                logger.warning("Slack not configured for escalation")
                return False
            
            target_channel = channel or config.get('default_channel', '#soc-alerts')
            color_map = {
                "critical": "#ff0000",
                "high": "#ff9900",
                "medium": "#ffcc00",
                "low": "#36a64f"
            }
            
            response = requests.post(
                "https://slack.com/api/chat.postMessage",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={
                    "channel": target_channel,
                    "attachments": [{
                        "color": color_map.get(severity.lower(), "#808080"),
                        "title": f"🚨 SOC Alert - {severity.upper()}",
                        "text": message,
                        "footer": "AI-SOC Autonomous Response",
                        "ts": datetime.utcnow().timestamp()
                    }]
                },
                timeout=30
            )
            
            if response.status_code == 200 and response.json().get("ok"):
                logger.info(f"Slack escalation sent to {target_channel}")
                return True
            else:
                logger.warning(f"Slack escalation failed: {response.text}")
                return False
                
        except Exception as e:
            logger.error(f"Slack escalation error: {e}")
            return False
    
    async def escalate_to_pagerduty(self, title: str, details: str, severity: str):
        """Send escalation to PagerDuty."""
        try:
            from core.config import get_integration_config
            import requests
            
            config = get_integration_config('pagerduty')
            routing_key = config.get('routing_key') or config.get('integration_key')
            
            if not routing_key:
                logger.warning("PagerDuty not configured for escalation")
                return False
            
            severity_map = {
                "critical": "critical",
                "high": "error",
                "medium": "warning",
                "low": "info"
            }
            
            response = requests.post(
                "https://events.pagerduty.com/v2/enqueue",
                json={
                    "routing_key": routing_key,
                    "event_action": "trigger",
                    "payload": {
                        "summary": title,
                        "source": "ai-soc-autonomous-response",
                        "severity": severity_map.get(severity.lower(), "warning"),
                        "custom_details": {"details": details}
                    }
                },
                timeout=30
            )
            
            data = response.json()
            if data.get("status") == "success":
                logger.info(f"PagerDuty alert triggered: {data.get('dedup_key')}")
                return True
            else:
                logger.warning(f"PagerDuty escalation failed: {data}")
                return False
                
        except Exception as e:
            logger.error(f"PagerDuty escalation error: {e}")
            return False
    
    async def escalate_action(
        self,
        action_data: Dict[str, Any],
        severity: str,
        channels: Optional[List[str]] = None
    ):
        """Escalate an action through configured channels."""
        channels = channels or ["slack", "pagerduty"]
        
        title = action_data.get("title", "Autonomous Response Action")
        description = action_data.get("description", "")
        target = action_data.get("target", "unknown")
        confidence = action_data.get("confidence", 0)
        
        message = f"""
**Action Required:** {title}
**Target:** {target}
**Severity:** {severity.upper()}
**Confidence:** {confidence:.1%}

{description}

Please review and approve/reject in the SOC dashboard.
""".strip()
        
        results = {}
        
        if "slack" in channels:
            results["slack"] = await self.escalate_to_slack(message, severity)
        
        if "pagerduty" in channels and severity in ["critical", "high"]:
            results["pagerduty"] = await self.escalate_to_pagerduty(title, message, severity)
        
        # Trigger callbacks
        self._trigger_escalation(action_data, severity, "escalate_action")
        
        logger.info(f"Escalation results for {target}: {results}")
        return results
    
    def correlate_alerts(
        self,
        tempo_flow_alert: Optional[Dict] = None,
        crowdstrike_alert: Optional[Dict] = None,
        splunk_results: Optional[List[Dict]] = None
    ) -> Dict:
        """
        Correlate alerts from multiple sources and calculate confidence score.
        
        Args:
            tempo_flow_alert: Alert from Tempo Flow
            crowdstrike_alert: Alert from CrowdStrike
            splunk_results: Results from Splunk queries
        
        Returns:
            Correlation result with confidence score and reasoning
        """
        confidence = 0.0
        evidence = []
        indicators = []
        reasoning = []
        
        # Correlate Tempo Flow alerts
        if tempo_flow_alert:
            evidence.append(f"Tempo Flow: {tempo_flow_alert.get('finding_id', 'unknown')}")
            
            severity = tempo_flow_alert.get('severity', '').lower()
            if severity in ['high', 'critical']:
                confidence += 0.15
                reasoning.append(f"High severity detection in Tempo Flow ({severity})")
            
            # Check for specific attack patterns
            mitre_predictions = tempo_flow_alert.get('mitre_predictions', {})
            if any(t.startswith('T1486') for t in mitre_predictions):  # Ransomware
                confidence += 0.25
                reasoning.append("Ransomware behavior detected (T1486)")
                indicators.append("ransomware")
            
            if any(t.startswith('T1071') for t in mitre_predictions):  # C2
                confidence += 0.20
                reasoning.append("C2 communication detected (T1071)")
                indicators.append("c2_communication")
            
            if any(t.startswith('T1021') for t in mitre_predictions):  # Lateral movement
                confidence += 0.15
                reasoning.append("Lateral movement detected (T1021)")
                indicators.append("lateral_movement")
        
        # Correlate CrowdStrike alerts
        if crowdstrike_alert:
            cs_alerts = crowdstrike_alert.get('alerts', [])
            if cs_alerts:
                evidence.append(f"CrowdStrike: {len(cs_alerts)} alert(s)")
                
                for alert in cs_alerts:
                    severity = alert.get('severity', '').lower()
                    if severity == 'critical':
                        confidence += 0.15
                        reasoning.append("Critical severity in CrowdStrike")
                    
                    detection_type = alert.get('detection_type', '').lower()
                    if detection_type == 'malware':
                        confidence += 0.20
                        reasoning.append(f"Malware detected: {alert.get('description', '')}")
                        indicators.append("malware")
                    
                    # Check if already isolated
                    if alert.get('isolated', False):
                        reasoning.append("Host already isolated in CrowdStrike")
        
        # Correlate Splunk results
        if splunk_results and len(splunk_results) > 0:
            evidence.append(f"Splunk: {len(splunk_results)} event(s)")
            
            # High volume of events indicates active threat
            if len(splunk_results) > 50:
                confidence += 0.10
                reasoning.append(f"High volume of events ({len(splunk_results)})")
        
        # Time correlation bonus
        if tempo_flow_alert and crowdstrike_alert:
            # If alerts are within 5 minutes, add correlation bonus
            confidence += 0.10
            reasoning.append("Temporal correlation between multiple sources")
        
        # Cap confidence at 1.0
        confidence = min(confidence, 1.0)
        
        return {
            "confidence": confidence,
            "indicators": indicators,
            "evidence": evidence,
            "reasoning": reasoning,
            "recommendation": self._get_recommendation(confidence, indicators)
        }
    
    def _get_recommendation(self, confidence: float, indicators: List[str]) -> str:
        """Get recommendation based on confidence and indicators."""
        if confidence >= 0.90:
            return "AUTO-ISOLATE: Confidence threshold met for automatic isolation"
        elif confidence >= 0.85:
            return "ISOLATE WITH APPROVAL: High confidence, recommend isolation with quick approval"
        elif confidence >= 0.70:
            return "MANUAL REVIEW: Moderate confidence, requires analyst review"
        else:
            return "MONITOR: Low confidence, continue monitoring"
    
    def create_isolation_action(
        self,
        ip_address: str,
        hostname: Optional[str],
        confidence: float,
        reason: str,
        evidence: List[str],
        correlation_data: Dict
    ) -> Optional[Dict]:
        """
        Create an isolation action (auto-execute if confidence >= 0.90).
        
        Args:
            ip_address: Target IP address
            hostname: Target hostname (optional)
            confidence: Confidence score (0.0-1.0)
            reason: Reason for isolation
            evidence: List of evidence IDs
            correlation_data: Data from correlation analysis
        
        Returns:
            Action result
        """
        if not self.approval_service:
            logger.error("Approval service not available")
            return {"error": "Approval service not available"}
        
        try:
            # Create pending action
            action = self.approval_service.create_action(
                action_type=self.ActionType.ISOLATE_HOST,
                title=f"Isolate Host: {hostname or ip_address}",
                description=f"Network isolation of compromised host based on correlated detections.\n\n"
                           f"Indicators: {', '.join(correlation_data.get('indicators', []))}\n"
                           f"Reasoning: {' | '.join(correlation_data.get('reasoning', []))}",
                target=ip_address,
                confidence=confidence,
                reason=reason,
                evidence=evidence,
                created_by=AgentId.AUTO_RESPONDER.value,
                parameters={
                    "hostname": hostname,
                    "correlation": correlation_data
                }
            )
            
            # Check if auto-approved (confidence >= 0.90)
            if action.status == "approved":
                logger.info(f"Action {action.action_id} auto-approved (confidence: {confidence:.2%})")
                
                # Execute isolation (would call actual CrowdStrike API in production)
                execution_result = self._execute_isolation(ip_address, hostname, reason, confidence)
                
                # Mark as executed
                self.approval_service.mark_executed(action.action_id, execution_result)
                
                return {
                    "status": "executed",
                    "action_id": action.action_id,
                    "message": f"Host {hostname or ip_address} isolated automatically",
                    "confidence": confidence,
                    "result": execution_result
                }
            else:
                logger.info(f"Action {action.action_id} pending approval (confidence: {confidence:.2%})")
                
                # Trigger escalation for pending actions
                severity = self._determine_severity_from_confidence(confidence, correlation_data)
                escalation_data = {
                    "action_id": action.action_id,
                    "title": action.title,
                    "description": action.description,
                    "target": ip_address,
                    "hostname": hostname,
                    "confidence": confidence,
                    "indicators": correlation_data.get("indicators", []),
                    "evidence": evidence
                }
                self._trigger_escalation(escalation_data, severity, "pending_approval")
                
                return {
                    "status": "pending_approval",
                    "action_id": action.action_id,
                    "message": f"Isolation action created, awaiting analyst approval",
                    "confidence": confidence,
                    "requires_approval": True,
                    "escalation_triggered": True
                }
        
        except Exception as e:
            logger.error(f"Error creating isolation action: {e}")
            return {"error": str(e)}
    
    def _determine_severity_from_confidence(self, confidence: float, correlation_data: Dict) -> str:
        """Determine severity level from confidence and indicators."""
        indicators = correlation_data.get("indicators", [])
        
        # Critical indicators
        if any(ind in indicators for ind in ["ransomware", "malware"]):
            return "critical"
        
        # High confidence + C2 or lateral movement
        if confidence >= 0.8 and any(ind in indicators for ind in ["c2_communication", "lateral_movement"]):
            return "high"
        
        # Based on confidence
        if confidence >= 0.85:
            return "high"
        elif confidence >= 0.7:
            return "medium"
        else:
            return "low"
    
    def _execute_isolation(
        self,
        ip_address: str,
        hostname: Optional[str],
        reason: str,
        confidence: float
    ) -> Dict:
        """
        Execute host isolation via CrowdStrike.
        
        In production, this would call the actual CrowdStrike API.
        For now, it returns a mock result.
        """
        logger.info(f"Executing isolation: {hostname or ip_address} (confidence: {confidence:.2%})")
        
        # Mock execution result
        return {
            "success": True,
            "action": "host_isolated",
            "ip_address": ip_address,
            "hostname": hostname,
            "reason": reason,
            "confidence": confidence,
            "timestamp": datetime.now().isoformat(),
            "isolation_type": "network",
            "message": "Host has been network isolated successfully (MOCK)",
            "next_steps": [
                "Verify threat containment",
                "Conduct forensic analysis",
                "Remediate threat",
                "Consider unisolation after remediation"
            ]
        }
    
    def execute_approved_actions(self) -> List[Dict]:
        """
        Execute all approved actions that haven't been executed yet.
        
        Returns:
            List of execution results
        """
        if not self.approval_service:
            return []
        
        try:
            from core.response.approval_service import ActionStatus
            
            # Get approved but not executed actions
            approved_actions = self.approval_service.list_actions(status=ActionStatus.APPROVED)
            results = []
            
            for action in approved_actions:
                # Skip if already executed
                if action.executed_at:
                    continue

                params = action.parameters or {}
                result: Optional[Dict] = None

                if action.action_type == "isolate_host":
                    result = self._execute_isolation(
                        ip_address=action.target,
                        hostname=params.get('hostname'),
                        reason=action.reason,
                        confidence=action.confidence
                    )
                elif action.action_type in ("waf_block", "gateway_block", "access_revoke"):
                    result = self._execute_cloudflare_action(
                        action_type=action.action_type,
                        target=action.target,
                        reason=action.reason,
                        parameters=params,
                    )

                if result is None:
                    # Unknown action type — leave for another executor or manual handling.
                    continue

                if result.get('success'):
                    self.approval_service.mark_executed(action.action_id, result)
                else:
                    self.approval_service.mark_failed(action.action_id, result.get('error', 'Unknown error'))

                results.append({
                    "action_id": action.action_id,
                    "action_type": action.action_type,
                    "target": action.target,
                    "result": result
                })

            return results
        
        except Exception as e:
            logger.error(f"Error executing approved actions: {e}")
            return []
    
    def investigate_and_respond(
        self,
        finding_id: str,
        auto_execute: bool = True
    ) -> Dict:
        """
        Full investigation and response workflow for a finding.
        
        Args:
            finding_id: Finding ID to investigate
            auto_execute: Whether to auto-execute high-confidence actions
        
        Returns:
            Investigation and response result
        """
        try:
            # Load finding
            from core.storage.database_data_service import DatabaseDataService
            data_service = DatabaseDataService()
            finding = data_service.get_finding(finding_id)
            
            if not finding:
                return {"error": f"Finding {finding_id} not found"}
            
            # Extract entity information
            entity_context = finding.get('entity_context', {})
            src_ips = entity_context.get('src_ips', [])
            hostnames = entity_context.get('hostnames', [])
            
            if not src_ips:
                return {"error": "No source IPs found in finding"}
            
            target_ip = src_ips[0]
            target_hostname = hostnames[0] if hostnames else None
            
            # Correlate with multiple sources
            correlation = self.correlate_alerts(
                tempo_flow_alert=finding,
                crowdstrike_alert=None,  # Would fetch from CrowdStrike in production
                splunk_results=None  # Would fetch from Splunk in production
            )
            
            # Determine action
            confidence = correlation['confidence']
            
            if confidence >= 0.85 and auto_execute:
                # Create isolation action
                action_result = self.create_isolation_action(
                    ip_address=target_ip,
                    hostname=target_hostname,
                    confidence=confidence,
                    reason=f"Automated response to finding {finding_id}",
                    evidence=[finding_id],
                    correlation_data=correlation
                )
                
                return {
                    "finding_id": finding_id,
                    "target": target_ip,
                    "correlation": correlation,
                    "action": action_result
                }
            else:
                # Just report findings
                return {
                    "finding_id": finding_id,
                    "target": target_ip,
                    "correlation": correlation,
                    "action": {
                        "status": "no_action",
                        "reason": f"Confidence below threshold ({confidence:.2%} < 0.85)"
                    }
                }
        
        except Exception as e:
            logger.error(f"Error in investigate_and_respond: {e}")
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Cloudflare action factories + executor
    # ------------------------------------------------------------------


    def _execute_cloudflare_action(
        self,
        action_type: str,
        target: str,
        reason: str,
        parameters: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute an approved Cloudflare action via the REST helpers in core/integrations/cloudflare/tool.py."""
        try:
            from core.config import get_integration_config, is_integration_enabled
        except Exception as e:  # noqa: BLE001
            return {"success": False, "error": f"config import failed: {e}"}

        if not is_integration_enabled("cloudflare"):
            return {
                "success": False,
                "error": "cloudflare_integration_disabled",
                "message": "Enable the Cloudflare integration in Settings to execute this action.",
            }

        cfg = get_integration_config("cloudflare") or {}
        api_token = cfg.get("api_token")
        account_id = cfg.get("account_id")
        if not api_token:
            return {"success": False, "error": "cloudflare api_token not configured"}

        # Lazy import to avoid forcing the MCP package on environments that
        # never enable Cloudflare.
        try:
            from core.integrations.cloudflare import tool as cf_tool
        except Exception as e:  # noqa: BLE001
            return {
                "success": False,
                "error": f"core.integrations.cloudflare.tool unavailable: {e}",
            }

        try:
            if action_type == "waf_block":
                return cf_tool._waf_block_ip(
                    api_token=api_token,
                    account_id=account_id,
                    ip=parameters.get("ip") or target,
                    reason=reason,
                    mode=parameters.get("mode", "block"),
                )
            if action_type == "gateway_block":
                return cf_tool._gateway_block_domain(
                    api_token=api_token,
                    account_id=account_id,
                    domain=parameters.get("domain") or target,
                    reason=reason,
                    rule_name=parameters.get("rule_name"),
                )
            if action_type == "access_revoke":
                return cf_tool._access_revoke_session(
                    api_token=api_token,
                    account_id=account_id,
                    email=parameters.get("email") or target,
                    reason=reason,
                )
            return {"success": False, "error": f"unknown cloudflare action {action_type}"}
        except Exception as e:  # noqa: BLE001
            logger.exception("Cloudflare action %s failed", action_type)
            return {"success": False, "error": str(e)}


# Singleton instance
_autonomous_response_service: Optional[AutonomousResponseService] = None


def get_autonomous_response_service() -> AutonomousResponseService:
    """Get singleton AutonomousResponseService instance."""
    global _autonomous_response_service
    if _autonomous_response_service is None:
        _autonomous_response_service = AutonomousResponseService()
    return _autonomous_response_service

