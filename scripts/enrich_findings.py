#!/usr/bin/env python3
"""
Finding Enrichment Script

This script demonstrates how to enrich findings with MITRE ATT&CK techniques
after they've been imported into Vigil SOC.

Usage:
    python scripts/enrich_findings.py --help
    python scripts/enrich_findings.py --finding-id f-20260114-001
    python scripts/enrich_findings.py --data-source firewall --auto-enrich
    python scripts/enrich_findings.py --bulk-file enrichment.json
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Any

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.database_data_service import DatabaseDataService

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

data_service = DatabaseDataService()


def enrich_single_finding(finding_id: str, enrichment: Dict[str, Any]) -> bool:
    """
    Enrich a single finding.
    
    Args:
        finding_id: Finding ID to enrich
        enrichment: Enrichment data
    
    Returns:
        True if successful
    """
    # Verify finding exists
    finding = data_service.get_finding(finding_id)
    if not finding:
        logger.error(f"Finding not found: {finding_id}")
        return False
    
    # Update finding
    success = data_service.update_finding(finding_id, **enrichment)
    
    if success:
        logger.info(f"✓ Enriched {finding_id}")
        return True
    else:
        logger.error(f"✗ Failed to enrich {finding_id}")
        return False


def auto_enrich_by_pattern(data_source: str = None, dry_run: bool = False) -> int:
    """
    Automatically enrich findings based on common patterns.
    
    Args:
        data_source: Filter by data source
        dry_run: If True, only show what would be done
    
    Returns:
        Number of findings enriched
    """
    # Get findings without MITRE techniques
    findings = data_service.get_findings()
    
    if data_source:
        findings = [f for f in findings if f.get('data_source') == data_source]
    
    # Filter to unenriched findings
    unenriched = [f for f in findings if not f.get('mitre_predictions')]
    
    logger.info(f"Found {len(unenriched)} findings to enrich")
    
    enriched_count = 0
    
    for finding in unenriched:
        finding_id = finding.get('finding_id')
        entity = finding.get('entity_context', {})
        raw_data = finding.get('raw_log', '')
        
        enrichment = {}
        
        # Pattern 1: DNS tunneling
        if finding.get('data_source') == 'dns':
            query_name = entity.get('query_name', '')
            if len(query_name) > 50 or query_name.count('.') > 5:
                enrichment = {
                    'mitre_predictions': {
                        'T1071.004': 0.70,  # DNS for C2
                        'T1048.003': 0.65   # Exfiltration over DNS
                    },
                    'severity': 'high'
                }
        
        # Pattern 2: High-frequency connections (beaconing)
        elif entity.get('connection_count', 0) > 100:
            enrichment = {
                'mitre_predictions': {
                    'T1071': 0.75,  # Application Layer Protocol
                    'T1573': 0.60   # Encrypted Channel
                },
                'severity': 'medium'
            }
        
        # Pattern 3: PowerShell execution
        elif 'powershell' in raw_data.lower() and '-encodedcommand' in raw_data.lower():
            enrichment = {
                'mitre_predictions': {
                    'T1059.001': 0.85,  # PowerShell
                    'T1027': 0.70       # Obfuscated Files or Information
                },
                'severity': 'high'
            }
        
        # Pattern 4: RDP connections from unusual sources
        elif entity.get('dst_port') == 3389 and not entity.get('src_ip', '').startswith('10.'):
            enrichment = {
                'mitre_predictions': {
                    'T1021.001': 0.80   # Remote Desktop Protocol
                },
                'severity': 'medium'
            }
        
        # Pattern 5: Large data transfers
        elif entity.get('bytes_out', 0) > 100_000_000:  # > 100 MB
            enrichment = {
                'mitre_predictions': {
                    'T1048': 0.70  # Exfiltration Over Alternative Protocol
                },
                'severity': 'medium'
            }
        
        if enrichment:
            if dry_run:
                logger.info(f"[DRY RUN] Would enrich {finding_id} with: {enrichment}")
            else:
                if enrich_single_finding(finding_id, enrichment):
                    enriched_count += 1
    
    logger.info(f"Enriched {enriched_count} findings")
    return enriched_count


def bulk_enrich_from_file(file_path: Path) -> int:
    """
    Bulk enrich findings from a JSON file.
    
    File format:
    {
      "f-001": {
        "mitre_predictions": {"T1071.001": 0.85},
        "severity": "high"
      },
      "f-002": { ... }
    }
    
    Args:
        file_path: Path to enrichment JSON file
    
    Returns:
        Number of findings enriched
    """
    if not file_path.exists():
        logger.error(f"File not found: {file_path}")
        return 0
    
    try:
        with open(file_path, 'r') as f:
            enrichment_data = json.load(f)
        
        logger.info(f"Loaded enrichment data for {len(enrichment_data)} findings")
        
        enriched_count = 0
        for finding_id, enrichment in enrichment_data.items():
            if enrich_single_finding(finding_id, enrichment):
                enriched_count += 1
        
        return enriched_count
        
    except Exception as e:
        logger.error(f"Error loading enrichment file: {e}")
        return 0


def list_unenriched_findings(data_source: str = None, limit: int = 20):
    """
    List findings that haven't been enriched with MITRE techniques.
    
    Args:
        data_source: Filter by data source
        limit: Maximum number to show
    """
    findings = data_service.get_findings()
    
    if data_source:
        findings = [f for f in findings if f.get('data_source') == data_source]
    
    unenriched = [f for f in findings if not f.get('mitre_predictions')]
    
    print(f"\n📋 Found {len(unenriched)} unenriched findings\n")
    print(f"{'Finding ID':<30} {'Data Source':<15} {'Anomaly Score':<15} {'Severity':<10}")
    print("-" * 70)
    
    for finding in unenriched[:limit]:
        finding_id = finding.get('finding_id', '')
        source = finding.get('data_source', 'unknown')
        score = finding.get('anomaly_score', 0.0)
        severity = finding.get('severity', 'unset')
        print(f"{finding_id:<30} {source:<15} {score:<15.2f} {severity:<10}")
    
    if len(unenriched) > limit:
        print(f"\n... and {len(unenriched) - limit} more")


def export_enrichment_template(output_file: Path, data_source: str = None):
    """
    Export a template JSON file for manual enrichment.
    
    Args:
        output_file: Output file path
        data_source: Filter by data source
    """
    findings = data_service.get_findings()
    
    if data_source:
        findings = [f for f in findings if f.get('data_source') == data_source]
    
    unenriched = [f for f in findings if not f.get('mitre_predictions')]
    
    # Create template
    template = {}
    for finding in unenriched:
        finding_id = finding.get('finding_id')
        template[finding_id] = {
            "_comment": f"Data source: {finding.get('data_source')}, "
                       f"Score: {finding.get('anomaly_score', 0):.2f}",
            "mitre_predictions": {},
            "severity": finding.get('severity', 'medium'),
            "status": finding.get('status', 'new')
        }
    
    with open(output_file, 'w') as f:
        json.dump(template, f, indent=2)
    
    logger.info(f"✓ Exported enrichment template to {output_file}")
    logger.info(f"  Contains {len(template)} findings")
    logger.info(f"  Edit the file and run: python scripts/enrich_findings.py --bulk-file {output_file}")


def main():
    parser = argparse.ArgumentParser(
        description='Enrich findings with MITRE ATT&CK techniques',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # List unenriched findings
  python scripts/enrich_findings.py --list

  # Auto-enrich based on patterns (dry run)
  python scripts/enrich_findings.py --auto-enrich --dry-run

  # Auto-enrich firewall findings
  python scripts/enrich_findings.py --auto-enrich --data-source firewall

  # Enrich single finding
  python scripts/enrich_findings.py --finding-id f-20260114-001 \\
    --mitre-techniques T1071.001:0.85,T1048.003:0.72 \\
    --severity high

  # Bulk enrich from file
  python scripts/enrich_findings.py --bulk-file enrichment.json

  # Export template for manual enrichment
  python scripts/enrich_findings.py --export-template enrichment_template.json
        """
    )
    
    parser.add_argument('--list', action='store_true',
                       help='List unenriched findings')
    parser.add_argument('--finding-id', type=str,
                       help='Finding ID to enrich')
    parser.add_argument('--mitre-techniques', type=str,
                       help='MITRE techniques (format: T1071.001:0.85,T1048.003:0.72)')
    parser.add_argument('--severity', type=str,
                       choices=['low', 'medium', 'high', 'critical'],
                       help='Set severity level')
    parser.add_argument('--auto-enrich', action='store_true',
                       help='Automatically enrich based on patterns')
    parser.add_argument('--bulk-file', type=Path,
                       help='Bulk enrich from JSON file')
    parser.add_argument('--export-template', type=Path,
                       help='Export enrichment template')
    parser.add_argument('--data-source', type=str,
                       help='Filter by data source')
    parser.add_argument('--dry-run', action='store_true',
                       help='Show what would be done without making changes')
    parser.add_argument('--limit', type=int, default=20,
                       help='Limit number of results (default: 20)')
    
    args = parser.parse_args()
    
    # List unenriched findings
    if args.list:
        list_unenriched_findings(data_source=args.data_source, limit=args.limit)
        return
    
    # Export template
    if args.export_template:
        export_enrichment_template(args.export_template, data_source=args.data_source)
        return
    
    # Auto-enrich
    if args.auto_enrich:
        count = auto_enrich_by_pattern(data_source=args.data_source, dry_run=args.dry_run)
        if not args.dry_run:
            logger.info(f"🎉 Successfully enriched {count} findings")
        return
    
    # Bulk enrich
    if args.bulk_file:
        count = bulk_enrich_from_file(args.bulk_file)
        logger.info(f"🎉 Successfully enriched {count} findings")
        return
    
    # Single finding enrichment
    if args.finding_id:
        if not args.mitre_techniques:
            logger.error("--mitre-techniques required for single finding enrichment")
            return
        
        # Parse MITRE techniques
        mitre_predictions = {}
        for pair in args.mitre_techniques.split(','):
            technique, confidence = pair.split(':')
            mitre_predictions[technique.strip()] = float(confidence.strip())
        
        enrichment = {'mitre_predictions': mitre_predictions}
        
        if args.severity:
            enrichment['severity'] = args.severity
        
        if enrich_single_finding(args.finding_id, enrichment):
            logger.info("🎉 Successfully enriched finding")
        return
    
    # No action specified
    parser.print_help()


if __name__ == '__main__':
    main()

