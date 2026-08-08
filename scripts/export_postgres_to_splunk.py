#!/usr/bin/env python3
"""
Export PostgreSQL data to Splunk HTTP Event Collector.

This script exports half of the findings and cases from PostgreSQL
and sends them to Splunk HEC for analysis and visualization.
"""

import sys
import os
import logging
import argparse
import json
import requests
from typing import List, Dict, Any
from datetime import datetime
import urllib3

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.storage.connection import get_db_manager
from core.storage.models import Finding, Case
from sqlalchemy import func

# Disable SSL warnings for self-signed certificates
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class PostgresToSplunkExporter:
    """Export PostgreSQL data to Splunk HEC."""
    
    def __init__(self, hec_url: str, hec_token: str, index: str = "main", 
                 verify_ssl: bool = False):
        """
        Initialize exporter.
        
        Args:
            hec_url: Splunk HEC URL (e.g., https://splunk:8088/services/collector)
            hec_token: HEC token for authentication
            index: Target Splunk index
            verify_ssl: Whether to verify SSL certificates
        """
        self.hec_url = hec_url
        self.hec_token = hec_token
        self.index = index
        self.verify_ssl = verify_ssl
        
        self.headers = {
            "Authorization": f"Splunk {hec_token}",
            "Content-Type": "application/json"
        }
    
    def fetch_half_findings(self, db) -> List[Finding]:
        """
        Fetch half of the findings from the database.
        
        Args:
            db: Database session
        
        Returns:
            List of Finding objects
        """
        try:
            # Get total count
            total_count = db.query(func.count(Finding.finding_id)).scalar()
            logger.info(f"Total findings in database: {total_count}")
            
            # Calculate half
            half_count = total_count // 2
            
            if half_count == 0:
                logger.warning("No findings to export")
                return []
            
            # Fetch half, ordered by timestamp
            findings = db.query(Finding).order_by(Finding.timestamp).limit(half_count).all()
            logger.info(f"Fetched {len(findings)} findings (half of total)")
            
            return findings
        
        except Exception as e:
            logger.error(f"Error fetching findings: {e}")
            return []
    
    def fetch_half_cases(self, db) -> List[Case]:
        """
        Fetch half of the cases from the database.
        
        Args:
            db: Database session
        
        Returns:
            List of Case objects
        """
        try:
            # Get total count
            total_count = db.query(func.count(Case.case_id)).scalar()
            logger.info(f"Total cases in database: {total_count}")
            
            # Calculate half
            half_count = total_count // 2
            
            if half_count == 0:
                logger.warning("No cases to export")
                return []
            
            # Fetch half, ordered by created_at
            cases = db.query(Case).order_by(Case.created_at).limit(half_count).all()
            logger.info(f"Fetched {len(cases)} cases (half of total)")
            
            return cases
        
        except Exception as e:
            logger.error(f"Error fetching cases: {e}")
            return []
    
    def finding_to_splunk_event(self, finding: Finding) -> Dict[str, Any]:
        """
        Convert a Finding object to a Splunk event.
        
        Args:
            finding: Finding object
        
        Returns:
            Dictionary formatted for Splunk HEC
        """
        # Get the finding as dict
        
        # Create event for Splunk
        event = {
            # Core fields
            "finding_id": finding.finding_id,
            "data_source": finding.data_source,
            "severity": finding.severity,
            "status": finding.status,
            "anomaly_score": finding.anomaly_score,
            
            # MITRE ATT&CK
            "mitre_predictions": finding.mitre_predictions,
            
            # Entity context (if available)
            "entity_context": finding.entity_context,
            
            # Metadata
            "cluster_id": finding.cluster_id,
            "created_at": finding.created_at.isoformat() if finding.created_at else None,
            "updated_at": finding.updated_at.isoformat() if finding.updated_at else None,
            
            # AI enrichment (if available)
            "ai_enrichment": finding.ai_enrichment,
            
            # Evidence links
            "evidence_links": finding.evidence_links,
            
            # Type marker
            "event_type": "finding",
            "source_system": "deeptempo_postgres"
        }
        
        # HEC format
        hec_event = {
            "time": finding.timestamp.timestamp() if finding.timestamp else datetime.utcnow().timestamp(),
            "sourcetype": "deeptempo:finding",
            "source": "postgresql_export",
            "host": "deeptempo-soc",
            "index": self.index,
            "event": event
        }
        
        return hec_event
    
    def case_to_splunk_event(self, case: Case) -> Dict[str, Any]:
        """
        Convert a Case object to a Splunk event.
        
        Args:
            case: Case object
        
        Returns:
            Dictionary formatted for Splunk HEC
        """
        # Get the case as dict
        
        # Create event for Splunk
        event = {
            # Core fields
            "case_id": case.case_id,
            "title": case.title,
            "description": case.description,
            "status": case.status,
            "priority": case.priority,
            "assignee": case.assignee,
            
            # Tags and categorization
            "tags": case.tags or [],
            "mitre_techniques": case.mitre_techniques or [],
            
            # Timeline and activities
            "timeline": case.timeline or [],
            "activities": case.activities or [],
            "notes": case.notes or [],
            "resolution_steps": case.resolution_steps or [],
            
            # Finding references
            "finding_ids": [f.finding_id for f in case.findings],
            "finding_count": len(case.findings),
            
            # Metadata
            "created_at": case.created_at.isoformat() if case.created_at else None,
            "updated_at": case.updated_at.isoformat() if case.updated_at else None,
            
            # Type marker
            "event_type": "case",
            "source_system": "deeptempo_postgres"
        }
        
        # HEC format
        hec_event = {
            "time": case.created_at.timestamp() if case.created_at else datetime.utcnow().timestamp(),
            "sourcetype": "deeptempo:case",
            "source": "postgresql_export",
            "host": "deeptempo-soc",
            "index": self.index,
            "event": event
        }
        
        return hec_event
    
    def send_to_splunk(self, events: List[Dict[str, Any]], batch_size: int = 100) -> tuple[int, int]:
        """
        Send events to Splunk HEC.
        
        Args:
            events: List of HEC-formatted events
            batch_size: Number of events per batch
        
        Returns:
            Tuple of (success_count, error_count)
        """
        success_count = 0
        error_count = 0
        
        logger.info(f"Sending {len(events)} events to Splunk in batches of {batch_size}")
        
        # Send events in batches
        for i in range(0, len(events), batch_size):
            batch = events[i:i + batch_size]
            
            # Format for HEC (multiple events, newline-delimited JSON)
            hec_payload = ""
            for event in batch:
                hec_payload += json.dumps(event) + "\n"
            
            try:
                response = requests.post(
                    self.hec_url,
                    headers=self.headers,
                    data=hec_payload.encode('utf-8'),
                    verify=self.verify_ssl,
                    timeout=30
                )
                
                if response.status_code == 200:
                    success_count += len(batch)
                    logger.info(f"✓ Batch {i // batch_size + 1}/{(len(events) + batch_size - 1) // batch_size}: "
                               f"Sent {len(batch)} events successfully")
                else:
                    error_count += len(batch)
                    logger.error(f"✗ Batch {i // batch_size + 1} failed: "
                               f"{response.status_code} - {response.text}")
            
            except Exception as e:
                error_count += len(batch)
                logger.error(f"✗ Error sending batch {i // batch_size + 1}: {e}")
        
        logger.info(f"\nExport Summary:")
        logger.info(f"  ✓ Successfully sent: {success_count} events")
        logger.info(f"  ✗ Failed: {error_count} events")
        
        return success_count, error_count
    
    def export_findings_to_splunk(self, db) -> tuple[int, int]:
        """
        Export half of findings to Splunk.
        
        Args:
            db: Database session
        
        Returns:
            Tuple of (success_count, error_count)
        """
        logger.info("=" * 60)
        logger.info("EXPORTING FINDINGS TO SPLUNK")
        logger.info("=" * 60)
        
        # Fetch findings
        findings = self.fetch_half_findings(db)
        
        if not findings:
            logger.warning("No findings to export")
            return 0, 0
        
        # Convert to Splunk events
        logger.info(f"Converting {len(findings)} findings to Splunk events...")
        events = [self.finding_to_splunk_event(f) for f in findings]
        
        # Send to Splunk
        return self.send_to_splunk(events)
    
    def export_cases_to_splunk(self, db) -> tuple[int, int]:
        """
        Export half of cases to Splunk.
        
        Args:
            db: Database session
        
        Returns:
            Tuple of (success_count, error_count)
        """
        logger.info("=" * 60)
        logger.info("EXPORTING CASES TO SPLUNK")
        logger.info("=" * 60)
        
        # Fetch cases
        cases = self.fetch_half_cases(db)
        
        if not cases:
            logger.warning("No cases to export")
            return 0, 0
        
        # Convert to Splunk events
        logger.info(f"Converting {len(cases)} cases to Splunk events...")
        events = [self.case_to_splunk_event(c) for c in cases]
        
        # Send to Splunk
        return self.send_to_splunk(events)
    
    def export_all(self, db) -> Dict[str, Any]:
        """
        Export both findings and cases to Splunk.
        
        Args:
            db: Database session
        
        Returns:
            Summary dictionary
        """
        # Export findings
        findings_success, findings_error = self.export_findings_to_splunk(db)
        
        print()  # Blank line for readability
        
        # Export cases
        cases_success, cases_error = self.export_cases_to_splunk(db)
        
        # Summary
        summary = {
            "findings": {
                "success": findings_success,
                "errors": findings_error,
                "total": findings_success + findings_error
            },
            "cases": {
                "success": cases_success,
                "errors": cases_error,
                "total": cases_success + cases_error
            },
            "overall": {
                "success": findings_success + cases_success,
                "errors": findings_error + cases_error,
                "total": findings_success + findings_error + cases_success + cases_error
            }
        }
        
        return summary


def main():
    """Main function."""
    parser = argparse.ArgumentParser(
        description="Export half of PostgreSQL data to Splunk HEC",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Export findings and cases to Splunk
  python scripts/export_postgres_to_splunk.py \\
      --hec-url https://your-splunk:8088/services/collector \\
      --hec-token your-hec-token \\
      --index deeptempo

  # Export only findings
  python scripts/export_postgres_to_splunk.py \\
      --hec-url https://your-splunk:8088/services/collector \\
      --hec-token your-hec-token \\
      --findings-only

  # Export only cases
  python scripts/export_postgres_to_splunk.py \\
      --hec-url https://your-splunk:8088/services/collector \\
      --hec-token your-hec-token \\
      --cases-only

  # Save to file instead of sending
  python scripts/export_postgres_to_splunk.py \\
      --save-to-file export.json \\
      --findings-only
        """
    )
    
    # Connection arguments
    parser.add_argument(
        "--hec-url",
        help="Splunk HEC URL (e.g., https://splunk:8088/services/collector)"
    )
    parser.add_argument(
        "--hec-token",
        help="Splunk HEC authentication token"
    )
    parser.add_argument(
        "--index",
        default="main",
        help="Target Splunk index (default: main)"
    )
    parser.add_argument(
        "--no-verify-ssl",
        action="store_true",
        help="Disable SSL verification"
    )
    
    # Export options
    parser.add_argument(
        "--findings-only",
        action="store_true",
        help="Export only findings"
    )
    parser.add_argument(
        "--cases-only",
        action="store_true",
        help="Export only cases"
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Number of events to send per batch (default: 100)"
    )
    
    # File export option
    parser.add_argument(
        "--save-to-file",
        metavar="FILE",
        help="Save events to JSON file instead of sending to Splunk"
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if not args.save_to_file:
        if not args.hec_url or not args.hec_token:
            logger.error("--hec-url and --hec-token are required (or use --save-to-file)")
            parser.print_help()
            sys.exit(1)
    
    try:
        # Initialize database connection
        logger.info("Connecting to PostgreSQL database...")
        db_manager = get_db_manager()
        db_manager.initialize()
        db = db_manager.get_session()
        
        if args.save_to_file:
            # Save to file mode
            logger.info(f"Exporting data to file: {args.save_to_file}")
            
            events = []
            
            # Create temporary exporter just for conversion
            temp_exporter = PostgresToSplunkExporter(
                hec_url="http://localhost",
                hec_token="dummy",
                index=args.index
            )
            
            if not args.cases_only:
                findings = temp_exporter.fetch_half_findings(db)
                events.extend([temp_exporter.finding_to_splunk_event(f) for f in findings])
            
            if not args.findings_only:
                cases = temp_exporter.fetch_half_cases(db)
                events.extend([temp_exporter.case_to_splunk_event(c) for c in cases])
            
            # Save to file
            with open(args.save_to_file, 'w') as f:
                json.dump(events, f, indent=2, default=str)
            
            logger.info(f"✓ Saved {len(events)} events to {args.save_to_file}")
            
        else:
            # Send to Splunk mode
            exporter = PostgresToSplunkExporter(
                hec_url=args.hec_url,
                hec_token=args.hec_token,
                index=args.index,
                verify_ssl=not args.no_verify_ssl
            )
            
            if args.findings_only:
                success, errors = exporter.export_findings_to_splunk(db)
            elif args.cases_only:
                success, errors = exporter.export_cases_to_splunk(db)
            else:
                summary = exporter.export_all(db)
                
                # Print summary
                logger.info("\n" + "=" * 60)
                logger.info("EXPORT COMPLETE")
                logger.info("=" * 60)
                logger.info(f"Findings: {summary['findings']['success']} sent, "
                          f"{summary['findings']['errors']} errors")
                logger.info(f"Cases:    {summary['cases']['success']} sent, "
                          f"{summary['cases']['errors']} errors")
                logger.info(f"Total:    {summary['overall']['success']} sent, "
                          f"{summary['overall']['errors']} errors")
                
                # Exit with error if any failures
                if summary['overall']['errors'] > 0:
                    sys.exit(1)
    
    except KeyboardInterrupt:
        logger.warning("\nExport interrupted by user")
        sys.exit(130)
    
    except Exception as e:
        logger.error(f"Error during export: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()

