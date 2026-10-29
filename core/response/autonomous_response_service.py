"""Autonomous response service with approval workflow integration."""

import logging
from typing import Any, Dict, List, Optional

from core.response.approval_service import ActionStatus, ApprovalService
from core.response.ceiling import execution_held
from core.response.config import ResponseConfig

logger = logging.getLogger(__name__)


class AutonomousResponseService:
    """Service for managing autonomous threat response with approval workflow."""

    def __init__(
        self,
        approvals: Optional[ApprovalService] = None,
        config: Optional[ResponseConfig] = None,
    ):
        """Initialize autonomous response service.

        ``config`` defaults to the approval service's band so the two never
        compare against different lines; the no-arg form reads Settings.
        """
        self.approval_service = approvals or ApprovalService(config=config)
        self.config = config or self.approval_service.config

    def _execute_isolation(
        self, ip_address: str, hostname: Optional[str], reason: str, confidence: float
    ) -> Dict:
        """
        Report that host isolation has no executor.

        No EDR containment call is wired. A success result would record a
        containment that never happened.
        """
        logger.info(
            f"Isolation not executed: {hostname or ip_address} "
            f"(confidence: {confidence:.2%}); no EDR executor"
        )
        return {
            "success": False,
            "error": "unsupported_action_type",
            "message": "No EDR executor is configured for host isolation",
        }

    def execute_approved_actions(self) -> List[Dict]:
        """
        Execute all approved actions that haven't been executed yet.

        Returns:
            List of execution results
        """
        try:
            # Stop holds every approved row, including one a person released.
            if execution_held():
                return []

            # Get approved but not executed actions
            approved_actions = self.approval_service.list_actions(
                status=ActionStatus.APPROVED
            )
            results = []

            for action in approved_actions:
                # Released by a confidence figure and no person: whoever
                # supplied that figure also chose the outcome.
                if not action.requires_approval and not action.approved_by:
                    logger.warning(
                        "Action %s was never decided by a person; not executing",
                        action.action_id,
                    )
                    continue

                params = action.parameters or {}
                result: Optional[Dict] = None

                if action.action_type == "isolate_host":
                    result = self._execute_isolation(
                        ip_address=action.target,
                        hostname=params.get("hostname"),
                        reason=action.reason,
                        confidence=action.confidence,
                    )
                elif action.action_type in (
                    "waf_block",
                    "gateway_block",
                    "access_revoke",
                ):
                    result = self._execute_cloudflare_action(
                        action_type=action.action_type,
                        target=action.target,
                        reason=action.reason,
                        parameters=params,
                    )

                if result is None:
                    # Unknown action type — leave for another executor or manual handling.
                    continue

                if result.get("success"):
                    self.approval_service.mark_executed(action.action_id, result)
                else:
                    self.approval_service.mark_failed(
                        action.action_id, result.get("error", "Unknown error")
                    )

                results.append(
                    {
                        "action_id": action.action_id,
                        "action_type": action.action_type,
                        "target": action.target,
                        "result": result,
                    }
                )

            return results

        except Exception as e:
            logger.error(f"Error executing approved actions: {e}")
            return []

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
            return {
                "success": False,
                "error": f"unknown cloudflare action {action_type}",
            }
        except Exception as e:  # noqa: BLE001
            logger.exception("Cloudflare action %s failed", action_type)
            return {"success": False, "error": str(e)}
