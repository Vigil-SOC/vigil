"""Splunk enrichment service with Claude AI analysis."""

import json
import logging
import re
from datetime import datetime
from typing import Any, Dict, List

from core.integrations.splunk.client import SplunkService
from core.llm.harness.claude import ClaudeService
from core.storage.database_data_service import DatabaseDataService

logger = logging.getLogger(__name__)


class SplunkEnrichmentService:
    """Service for enriching cases and findings with Splunk data and Claude analysis."""

    def __init__(self, splunk_service: SplunkService, claude_service=None):
        """
        Initialize enrichment service.

        Args:
            splunk_service: Configured Splunk service
            claude_service: Optional Claude service for AI analysis (if None, creates one using factory)
        """
        self.splunk_service = splunk_service
        self.claude_service = claude_service or ClaudeService(use_mcp_tools=False)
        self.data_service = DatabaseDataService()

    def extract_indicators(
        self, case: Dict, findings: List[Dict]
    ) -> Dict[str, List[str]]:
        """
        Extract IOCs (Indicators of Compromise) from case and findings.

        Args:
            case: Case dictionary
            findings: List of finding dictionaries

        Returns:
            Dictionary with indicator types as keys and lists of indicators as values
        """
        indicators = {
            "ips": set(),
            "domains": set(),
            "hashes": set(),
            "usernames": set(),
            "hostnames": set(),
            "emails": set(),
        }

        # Patterns for extracting IOCs
        ip_pattern = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
        domain_pattern = (
            r"\b(?:[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]{2,}\b"
        )
        hash_pattern = r"\b[a-fA-F0-9]{32}\b|\b[a-fA-F0-9]{40}\b|\b[a-fA-F0-9]{64}\b"
        email_pattern = r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"

        # Extract from case
        case_text = json.dumps(case)
        indicators["ips"].update(re.findall(ip_pattern, case_text))
        indicators["domains"].update(re.findall(domain_pattern, case_text))
        indicators["hashes"].update(re.findall(hash_pattern, case_text))
        indicators["emails"].update(re.findall(email_pattern, case_text))

        # Extract from findings
        for finding in findings:
            finding_text = json.dumps(finding)
            indicators["ips"].update(re.findall(ip_pattern, finding_text))
            indicators["domains"].update(re.findall(domain_pattern, finding_text))
            indicators["hashes"].update(re.findall(hash_pattern, finding_text))
            indicators["emails"].update(re.findall(email_pattern, finding_text))

            # Extract entity context
            entity_context = finding.get("entity_context", {})

            # IPs
            if "src_ip" in entity_context:
                indicators["ips"].add(entity_context["src_ip"])
            if "dest_ip" in entity_context:
                indicators["ips"].add(entity_context["dest_ip"])

            # Usernames
            if "user" in entity_context:
                indicators["usernames"].add(entity_context["user"])
            if "username" in entity_context:
                indicators["usernames"].add(entity_context["username"])

            # Hostnames
            if "host" in entity_context:
                indicators["hostnames"].add(entity_context["host"])
            if "hostname" in entity_context:
                indicators["hostnames"].add(entity_context["hostname"])
            if "src_host" in entity_context:
                indicators["hostnames"].add(entity_context["src_host"])
            if "dest_host" in entity_context:
                indicators["hostnames"].add(entity_context["dest_host"])

        # Convert sets to sorted lists and filter out private IPs and common domains
        indicators["ips"] = sorted(
            [ip for ip in indicators["ips"] if not self._is_private_ip(ip)]
        )
        indicators["domains"] = sorted(
            [
                domain
                for domain in indicators["domains"]
                if not self._is_common_domain(domain)
            ]
        )
        indicators["hashes"] = sorted(list(indicators["hashes"]))
        indicators["usernames"] = sorted(list(indicators["usernames"]))
        indicators["hostnames"] = sorted(list(indicators["hostnames"]))
        indicators["emails"] = sorted(list(indicators["emails"]))

        return indicators

    def _is_private_ip(self, ip: str) -> bool:
        """Check if IP is private/internal."""
        try:
            parts = [int(p) for p in ip.split(".")]
            if len(parts) != 4:
                return True

            # RFC1918 private ranges
            if parts[0] == 10:
                return True
            if parts[0] == 172 and 16 <= parts[1] <= 31:
                return True
            if parts[0] == 192 and parts[1] == 168:
                return True

            # Loopback
            if parts[0] == 127:
                return True

            return False
        except Exception as e:
            logger.debug(f"Error checking private IP: {e}")
            return True

    def _is_common_domain(self, domain: str) -> bool:
        """Check if domain is a common/benign domain."""
        common_tlds = ["localhost", "local", "internal", "corp", "lan"]
        domain_lower = domain.lower()

        for tld in common_tlds:
            if domain_lower.endswith(f".{tld}"):
                return True

        return False

    def query_splunk_for_indicators(
        self, indicators: Dict[str, List[str]], hours: int = 168
    ) -> Dict[str, Any]:
        """
        Query Splunk for each indicator type.

        Args:
            indicators: Dictionary of indicator types and values
            hours: Number of hours to look back (default: 168 = 7 days)

        Returns:
            Dictionary with enrichment data from Splunk
        """
        enrichment_data = {
            "query_time": datetime.now().isoformat(),
            "lookback_hours": hours,
            "results": {},
            "summary": {},
        }

        total_events = 0

        # Query for IPs
        if indicators.get("ips"):
            enrichment_data["results"]["ips"] = {}
            for ip in indicators["ips"][:10]:  # Limit to 10 IPs
                logger.info(f"Querying Splunk for IP: {ip}")
                results = self.splunk_service.search_by_ip(ip, hours=hours)
                if results:
                    enrichment_data["results"]["ips"][ip] = results[
                        :100
                    ]  # Limit to 100 events per IP
                    total_events += len(results[:100])

        # Query for domains
        if indicators.get("domains"):
            enrichment_data["results"]["domains"] = {}
            for domain in indicators["domains"][:10]:  # Limit to 10 domains
                logger.info(f"Querying Splunk for domain: {domain}")
                results = self.splunk_service.search_by_domain(domain, hours=hours)
                if results:
                    enrichment_data["results"]["domains"][domain] = results[:100]
                    total_events += len(results[:100])

        # Query for hashes
        if indicators.get("hashes"):
            enrichment_data["results"]["hashes"] = {}
            for file_hash in indicators["hashes"][:10]:  # Limit to 10 hashes
                logger.info(f"Querying Splunk for hash: {file_hash}")
                results = self.splunk_service.search_by_hash(file_hash, hours=hours)
                if results:
                    enrichment_data["results"]["hashes"][file_hash] = results[:100]
                    total_events += len(results[:100])

        # Query for usernames
        if indicators.get("usernames"):
            enrichment_data["results"]["usernames"] = {}
            for username in indicators["usernames"][:10]:  # Limit to 10 usernames
                logger.info(f"Querying Splunk for username: {username}")
                results = self.splunk_service.search_by_username(username, hours=hours)
                if results:
                    enrichment_data["results"]["usernames"][username] = results[:100]
                    total_events += len(results[:100])

        # Query for hostnames
        if indicators.get("hostnames"):
            enrichment_data["results"]["hostnames"] = {}
            for hostname in indicators["hostnames"][:10]:  # Limit to 10 hostnames
                logger.info(f"Querying Splunk for hostname: {hostname}")
                results = self.splunk_service.search_by_hostname(hostname, hours=hours)
                if results:
                    enrichment_data["results"]["hostnames"][hostname] = results[:100]
                    total_events += len(results[:100])

        # Create summary
        enrichment_data["summary"] = {
            "total_events": total_events,
            "ips_queried": len(indicators.get("ips", [])),
            "domains_queried": len(indicators.get("domains", [])),
            "hashes_queried": len(indicators.get("hashes", [])),
            "usernames_queried": len(indicators.get("usernames", [])),
            "hostnames_queried": len(indicators.get("hostnames", [])),
        }

        return enrichment_data

    def analyze_with_claude(
        self, case: Dict, findings: List[Dict], enrichment_data: Dict[str, Any]
    ) -> str:
        """
        Use Claude to analyze the case with Splunk enrichment data.

        Args:
            case: Case dictionary
            findings: List of findings
            enrichment_data: Enrichment data from Splunk

        Returns:
            Claude's analysis as a string
        """
        if not self.claude_service.has_api_key():
            logger.warning("Claude API key not available, skipping AI analysis")
            return "AI analysis not available (API key not configured)"

        # Build prompt for Claude
        system_prompt = """You are a security analyst helping to enrich and analyze security cases.
You have access to case data, findings, and additional event data from Splunk.

Your task is to:
1. Analyze the Splunk data in the context of the case and findings
2. Identify patterns, anomalies, or correlations
3. Determine if the Splunk data confirms, contradicts, or expands upon the findings
4. Provide actionable insights and recommendations
5. Highlight any critical security concerns

Provide a clear, structured analysis that a SOC analyst can act upon."""

        # Prepare case context
        case_summary = f"""
**Case ID**: {case.get('case_id', 'Unknown')}
**Title**: {case.get('title', 'Untitled')}
**Priority**: {case.get('priority', 'unknown')}
**Status**: {case.get('status', 'unknown')}
**Description**: {case.get('description', 'No description')}
**Number of Findings**: {len(findings)}
"""

        # Prepare findings summary (limit to key details)
        findings_summary = "\n\n**Findings Summary**:\n"
        for i, finding in enumerate(findings[:5], 1):  # Limit to 5 findings
            findings_summary += f"\n{i}. **{finding.get('finding_id', 'Unknown')}**\n"
            findings_summary += f"   - Severity: {finding.get('severity', 'unknown')}\n"
            findings_summary += (
                f"   - Data Source: {finding.get('data_source', 'unknown')}\n"
            )
            findings_summary += (
                f"   - Timestamp: {finding.get('timestamp', 'unknown')}\n"
            )

            entity_context = finding.get("entity_context", {})
            if entity_context:
                findings_summary += (
                    f"   - Context: {json.dumps(entity_context, indent=6)}\n"
                )

        # Prepare enrichment summary
        summary = enrichment_data.get("summary", {})
        enrichment_summary = f"""
**Splunk Enrichment Summary**:
- Total Events Retrieved: {summary.get('total_events', 0)}
- IPs Queried: {summary.get('ips_queried', 0)}
- Domains Queried: {summary.get('domains_queried', 0)}
- Hashes Queried: {summary.get('hashes_queried', 0)}
- Usernames Queried: {summary.get('usernames_queried', 0)}
- Hostnames Queried: {summary.get('hostnames_queried', 0)}
- Lookback Period: {enrichment_data.get('lookback_hours', 0)} hours
"""

        # Sample some Splunk events for context (limit to avoid token overload)
        events_sample = "\n\n**Sample Splunk Events**:\n"
        results = enrichment_data.get("results", {})
        event_count = 0
        max_events = 20  # Limit total events to show Claude

        for indicator_type, indicator_results in results.items():
            if event_count >= max_events:
                break

            events_sample += f"\n**{indicator_type.upper()}**:\n"
            for indicator_value, events in indicator_results.items():
                if event_count >= max_events:
                    break

                events_sample += f"\n  {indicator_value}:\n"
                for event in events[:5]:  # Max 5 events per indicator
                    if event_count >= max_events:
                        break

                    # Show key fields from event
                    event_summary = "    - "
                    key_fields = [
                        "_time",
                        "sourcetype",
                        "source",
                        "host",
                        "action",
                        "result",
                        "user",
                        "src",
                        "dest",
                    ]
                    event_parts = []
                    for field in key_fields:
                        if field in event and event[field]:
                            event_parts.append(f"{field}={event[field]}")

                    if event_parts:
                        events_sample += (
                            event_summary + ", ".join(event_parts[:5]) + "\n"
                        )

                    event_count += 1

        # Build full prompt
        user_prompt = f"""Analyze the following security case with Splunk enrichment data:

{case_summary}

{findings_summary}

{enrichment_summary}

{events_sample}

Please provide a comprehensive analysis of this case considering the Splunk data.
Focus on:
1. Key findings from the Splunk data
2. Correlations between findings and Splunk events
3. Timeline of activities
4. Risk assessment
5. Recommended next steps for investigation
"""

        try:
            logger.info("Requesting Claude analysis of enriched case data")
            analysis = self.claude_service.chat(
                user_prompt, system_prompt=system_prompt
            )
            return analysis or "Analysis failed - no response from Claude"

        except Exception as e:
            logger.error(f"Error getting Claude analysis: {e}")
            return f"Error during AI analysis: {str(e)}"

    def enrich_case(self, case_id: str, lookback_hours: int = 168) -> Dict[str, Any]:
        """
        Enrich a case with Splunk data and Claude analysis.

        Args:
            case_id: Case ID to enrich
            lookback_hours: Hours to look back in Splunk (default: 168 = 7 days)

        Returns:
            Dictionary with enrichment results
        """
        logger.info(f"Starting enrichment for case {case_id}")

        # Get case and findings
        case = self.data_service.get_case(case_id)
        if not case:
            return {"success": False, "error": f"Case {case_id} not found"}

        finding_ids = case.get("finding_ids", [])
        findings = []
        for finding_id in finding_ids:
            finding = self.data_service.get_finding(finding_id)
            if finding:
                findings.append(finding)

        # Extract indicators
        logger.info("Extracting indicators from case and findings")
        indicators = self.extract_indicators(case, findings)

        # Query Splunk
        logger.info("Querying Splunk for indicators")
        enrichment_data = self.query_splunk_for_indicators(
            indicators, hours=lookback_hours
        )

        # Analyze with Claude
        logger.info("Analyzing with Claude AI")
        analysis = self.analyze_with_claude(case, findings, enrichment_data)

        # Prepare enrichment result
        enrichment_result = {
            "success": True,
            "case_id": case_id,
            "enrichment_timestamp": datetime.now().isoformat(),
            "indicators": {
                k: v for k, v in indicators.items() if v
            },  # Only include non-empty
            "splunk_data": enrichment_data,
            "claude_analysis": analysis,
        }

        # Update case with enrichment data
        notes_entry = f"""
=== Splunk Enrichment (from {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===

**Indicators Analyzed**:
- IPs: {len(indicators.get('ips', []))}
- Domains: {len(indicators.get('domains', []))}
- Hashes: {len(indicators.get('hashes', []))}
- Usernames: {len(indicators.get('usernames', []))}
- Hostnames: {len(indicators.get('hostnames', []))}

**Splunk Events Retrieved**: {enrichment_data['summary'].get('total_events', 0)}

**AI Analysis**:
{analysis}

---
"""

        # Add enrichment note to case
        self.data_service.update_case(case_id, notes=notes_entry)

        logger.info(f"Enrichment completed for case {case_id}")

        return enrichment_result
