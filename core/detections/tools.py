"""
Security-Detections tool integration - Pure Python implementation
Provides access to 7,200+ detection rules across Sigma, Splunk, Elastic, and KQL formats
"""
import re
import yaml
import os
from pathlib import Path
from typing import List, Dict, Optional
from collections import defaultdict


class SecurityDetectionsTools:
    """
    Main class for security detection tools integration.
    Loads and indexes detection rules from multiple formats.
    
    Paths are resolved dynamically from DetectionRulesService when available,
    falling back to environment variables and then default paths.
    """
    
    def __init__(self):
        """Initialize with detection rule paths from dynamic service or environment."""
        # Try to get paths dynamically from DetectionRulesService
        paths = self._get_dynamic_paths()
        
        self.sigma_path = Path(paths.get("sigma", os.getenv("SIGMA_PATHS", str(Path.home() / "security-detections/sigma/rules"))))
        self.splunk_path = Path(paths.get("splunk", os.getenv("SPLUNK_PATHS", str(Path.home() / "security-detections/security_content/detections"))))
        self.elastic_path = Path(paths.get("elastic", os.getenv("ELASTIC_PATHS", str(Path.home() / "security-detections/detection-rules/rules"))))
        self.kql_path = Path(paths.get("kql", os.getenv("KQL_PATHS", str(Path.home() / "security-detections/Hunting-Queries-Detection-Rules"))))
        
        self.detections = []
        self.detections_by_technique = defaultdict(list)
        self.detections_by_source = defaultdict(list)
        self._loaded = False
    
    def _get_dynamic_paths(self) -> Dict:
        """Get paths from DetectionRulesService if available."""
        try:
            from core.detections.detection_rules_service import get_detection_rules_service
            service = get_detection_rules_service()
            env_vars = service.get_mcp_env_vars()
            
            paths = {}
            if "SIGMA_PATHS" in env_vars:
                # Take only the first path if multiple are comma-separated
                paths["sigma"] = env_vars["SIGMA_PATHS"].split(",")[0]
            if "SPLUNK_PATHS" in env_vars:
                paths["splunk"] = env_vars["SPLUNK_PATHS"].split(",")[0]
            if "ELASTIC_PATHS" in env_vars:
                paths["elastic"] = env_vars["ELASTIC_PATHS"].split(",")[0]
            if "KQL_PATHS" in env_vars:
                paths["kql"] = env_vars["KQL_PATHS"].split(",")[0]
            
            return paths
        except Exception:
            return {}
    
    def reload(self):
        """Reload detection rules (clears cache and re-reads from sources)."""
        self._loaded = False
        self.detections = []
        self.detections_by_technique = defaultdict(list)
        self.detections_by_source = defaultdict(list)
        
        # Refresh paths from dynamic service
        paths = self._get_dynamic_paths()
        if paths.get("sigma"):
            self.sigma_path = Path(paths["sigma"])
        if paths.get("splunk"):
            self.splunk_path = Path(paths["splunk"])
        if paths.get("elastic"):
            self.elastic_path = Path(paths["elastic"])
        if paths.get("kql"):
            self.kql_path = Path(paths["kql"])
        
        self._load_detections()
    
    def _load_detections(self):
        """Load all detection files (lazy loading)"""
        if self._loaded:
            return
        
        print("Loading detection rules...")
        
        # Load Sigma rules
        if self.sigma_path.exists():
            for file_path in self.sigma_path.rglob("*.yml"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        detection = yaml.safe_load(f)
                        if detection and isinstance(detection, dict):
                            detection['_source'] = 'sigma'
                            detection['_file_path'] = str(file_path)
                            self.detections.append(detection)
                            self.detections_by_source['sigma'].append(detection)
                            
                            # Index by MITRE technique
                            tags = detection.get('tags', [])
                            for tag in tags:
                                if isinstance(tag, str) and tag.startswith('attack.t'):
                                    technique = tag.replace('attack.', '').upper()
                                    self.detections_by_technique[technique].append(detection)
                except Exception:
                    pass  # Skip malformed files
        
        # Load Splunk ESCU rules
        if self.splunk_path.exists():
            for file_path in self.splunk_path.rglob("*.yml"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        detection = yaml.safe_load(f)
                        if detection and isinstance(detection, dict):
                            detection['_source'] = 'splunk'
                            detection['_file_path'] = str(file_path)
                            self.detections.append(detection)
                            self.detections_by_source['splunk'].append(detection)
                            
                            # Index by MITRE technique
                            tags = detection.get('tags', {}).get('mitre_attack_id', [])
                            if isinstance(tags, list):
                                for tag in tags:
                                    self.detections_by_technique[tag.upper()].append(detection)
                except Exception:
                    pass
        
        # Load Elastic rules
        if self.elastic_path.exists():
            for file_path in self.elastic_path.rglob("*.toml"):
                try:
                    # Simple TOML parsing for threat section
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        detection = {'_source': 'elastic', '_file_path': str(file_path), '_content': content}
                        self.detections.append(detection)
                        self.detections_by_source['elastic'].append(detection)
                        
                        # Extract technique IDs from content
                        techniques = re.findall(r'technique_id\s*=\s*"(T\d{4}(?:\.\d{3})?)"', content)
                        for technique in techniques:
                            self.detections_by_technique[technique.upper()].append(detection)
                except Exception:
                    pass
        
        # Load KQL rules (.yaml and .md files)
        if self.kql_path.exists():
            # Load .yaml KQL rules (if any exist)
            for file_path in self.kql_path.rglob("*.yaml"):
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        detection = yaml.safe_load(f)
                        if detection and isinstance(detection, dict):
                            detection['_source'] = 'kql'
                            detection['_file_path'] = str(file_path)
                            self.detections.append(detection)
                            self.detections_by_source['kql'].append(detection)
                            
                            # Index by MITRE technique
                            techniques = detection.get('mitre', [])
                            if isinstance(techniques, list):
                                for technique in techniques:
                                    self.detections_by_technique[technique.upper()].append(detection)
                except Exception:
                    pass
            
            # Load .md KQL rules (Hunting-Queries-Detection-Rules format)
            for file_path in self.kql_path.rglob("*.md"):
                # Skip README files and templates
                if file_path.name.lower() in ('readme.md', 'detectiontemplate.md'):
                    continue
                try:
                    detection = self._parse_kql_markdown(file_path)
                    if detection:
                        self.detections.append(detection)
                        self.detections_by_source['kql'].append(detection)
                        
                        # Index by MITRE technique
                        for technique in detection.get('mitre', []):
                            self.detections_by_technique[technique.upper()].append(detection)
                except Exception:
                    pass
        
        self._loaded = True
        print(f"Loaded {len(self.detections)} detection rules")
        print(f"  Sigma: {len(self.detections_by_source['sigma'])}")
        print(f"  Splunk: {len(self.detections_by_source['splunk'])}")
        print(f"  Elastic: {len(self.detections_by_source['elastic'])}")
        print(f"  KQL: {len(self.detections_by_source['kql'])}")
    
    async def analyze_coverage(self, techniques: List[str]) -> Dict:
        """
        Analyze detection coverage for MITRE ATT&CK techniques.
        
        Args:
            techniques: List of MITRE technique IDs (e.g., ["T1059.001", "T1071.001"])
        
        Returns:
            Dictionary mapping technique IDs to coverage information
        """
        self._load_detections()
        
        coverage = {}
        for technique in techniques:
            technique_upper = technique.upper()
            matching = self.detections_by_technique.get(technique_upper, [])
            
            # Count by source
            by_source = defaultdict(int)
            for detection in matching:
                by_source[detection['_source']] += 1
            
            coverage[technique] = {
                "count": len(matching),
                "by_source": dict(by_source),
                "detections": [
                    {
                        "title": d.get('title') or d.get('name', 'Untitled'),
                        "source": d['_source'],
                        "id": d.get('id', ''),
                        "description": d.get('description', '')[:200]
                    }
                    for d in matching[:5]  # Top 5
                ]
            }
        
        return coverage
    
    async def search_detections(self, query: str, source_type: Optional[str] = None, limit: int = 20) -> List[Dict]:
        """
        Search across all detection rules using keywords.
        
        Args:
            query: Search query (e.g., "powershell base64", "lateral movement")
            source_type: Optional filter by source ("sigma", "splunk", "elastic", "kql")
            limit: Maximum number of results
        
        Returns:
            List of matching detection rules
        """
        self._load_detections()
        
        query_lower = query.lower()
        results = []
        
        detections_to_search = self.detections
        if source_type:
            detections_to_search = self.detections_by_source.get(source_type, [])
        
        for detection in detections_to_search:
            # Search in title, description, tags
            searchable = str(detection).lower()
            if query_lower in searchable:
                results.append({
                    "title": detection.get('title') or detection.get('name', 'Untitled'),
                    "source": detection['_source'],
                    "id": detection.get('id', ''),
                    "description": detection.get('description', '')[:300],
                    "file_path": detection['_file_path']
                })
                
                if len(results) >= limit:
                    break
        
        return results
    
    async def get_detection_count(self, source_type: Optional[str] = None) -> Dict:
        """
        Get count of detections by source.
        
        Args:
            source_type: Optional filter by source
        
        Returns:
            Count information
        """
        self._load_detections()
        
        if source_type:
            return {
                "source": source_type,
                "count": len(self.detections_by_source.get(source_type, []))
            }
        
        return {
            "total": len(self.detections),
            "by_source": {
                source: len(detections)
                for source, detections in self.detections_by_source.items()
            }
        }
    
    async def identify_gaps(self, context: str) -> Dict:
        """
        Identify detection gaps for a given context (e.g., "ransomware", "APT29").
        
        Args:
            context: Context for gap analysis
        
        Returns:
            Gap analysis results
        """
        self._load_detections()
        
        # Common ransomware techniques
        ransomware_techniques = [
            "T1486",  # Data Encrypted for Impact
            "T1490",  # Inhibit System Recovery
            "T1489",  # Service Stop
            "T1047",  # Windows Management Instrumentation
            "T1059.001",  # PowerShell
        ]
        
        # For demonstration, analyze common techniques
        # In production, this would map context to specific technique sets
        techniques_to_check = ransomware_techniques if "ransomware" in context.lower() else []
        
        gaps = []
        coverage_map = {}
        
        for technique in techniques_to_check:
            matching = self.detections_by_technique.get(technique, [])
            count = len(matching)
            coverage_map[technique] = count
            
            if count < 3:  # Threshold for "gap"
                gaps.append({
                    "technique": technique,
                    "current_coverage": count,
                    "priority": "high" if count == 0 else "medium"
                })
        
        return {
            "context": context,
            "techniques_analyzed": len(techniques_to_check),
            "gaps_identified": len(gaps),
            "gaps": gaps,
            "coverage_map": coverage_map
        }
    
    async def get_coverage_stats(self, source_type: Optional[str] = None) -> Dict:
        """
        Get overall coverage statistics.
        
        Args:
            source_type: Optional filter by source
        
        Returns:
            Coverage statistics
        """
        self._load_detections()
        
        stats = {
            "total_detections": len(self.detections),
            "techniques_covered": len(self.detections_by_technique),
            "by_source": {
                source: {
                    "count": len(detections),
                    "techniques": len(set(
                        tech for det in detections
                        for tech in self._extract_techniques(det)
                    ))
                }
                for source, detections in self.detections_by_source.items()
            }
        }
        
        if source_type and source_type in self.detections_by_source:
            return {
                "source": source_type,
                **stats["by_source"][source_type]
            }
        
        return stats
    
    def _parse_kql_markdown(self, file_path: Path) -> Optional[Dict]:
        """
        Parse a KQL markdown file from the Hunting-Queries-Detection-Rules repo.
        
        Expected format:
            # Title
            ## Query Information
            #### MITRE ATT&CK Technique(s)
            | Technique ID | Title | Link |
            | --- | --- | --- |
            | T1210 | Exploitation of Remote Services | ... |
            #### Description
            Description text...
            ## Defender XDR / Sentinel
            ```KQL
            ...query...
            ```
        """
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if not content.strip():
            return None
        
        # Extract title from first markdown heading
        title_match = re.search(r'^#\s+(.+)', content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else file_path.stem
        
        # Extract description from #### Description section
        desc_match = re.search(
            r'####\s+Description\s*\n(.*?)(?=\n####|\n##|\n```|\Z)',
            content, re.DOTALL
        )
        description = desc_match.group(1).strip() if desc_match else ''
        
        # Extract MITRE technique IDs from the table or anywhere in the doc
        mitre_techniques = list(set(re.findall(r'(T\d{4}(?:\.\d{3})?)', content)))
        
        # Extract KQL queries from code blocks
        kql_blocks = re.findall(r'```(?:KQL|kql)\s*\n(.*?)```', content, re.DOTALL)
        query = '\n---\n'.join(block.strip() for block in kql_blocks) if kql_blocks else ''
        
        # Determine category from parent directory
        category = file_path.parent.name if file_path.parent != self.kql_path else 'General'
        
        detection = {
            '_source': 'kql',
            '_file_path': str(file_path),
            'title': title,
            'name': title,
            'description': description,
            'query': query,
            'category': category,
            'mitre': mitre_techniques,
            'id': f"kql-{file_path.stem}",
        }
        
        return detection

    def _extract_techniques(self, detection: Dict) -> List[str]:
        """Extract MITRE techniques from a detection"""
        techniques = []
        
        if detection['_source'] == 'sigma':
            tags = detection.get('tags', [])
            for tag in tags:
                if isinstance(tag, str) and tag.startswith('attack.t'):
                    techniques.append(tag.replace('attack.', '').upper())
        elif detection['_source'] == 'splunk':
            tags = detection.get('tags', {}).get('mitre_attack_id', [])
            if isinstance(tags, list):
                techniques.extend([t.upper() for t in tags])
        elif detection['_source'] == 'kql':
            techs = detection.get('mitre', [])
            if isinstance(techs, list):
                techniques.extend([t.upper() for t in techs])
        
        return techniques


# Global instance for reuse
_security_detection_tools = None


def get_security_detection_tools() -> SecurityDetectionsTools:
    """Get or create global instance"""
    global _security_detection_tools
    if _security_detection_tools is None:
        _security_detection_tools = SecurityDetectionsTools()
    return _security_detection_tools

