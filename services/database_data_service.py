import json
import logging
import time
from pathlib import Path
from typing import Optional, List, Dict, Tuple
from datetime import datetime
import uuid

import numpy as np

from database.connection import get_db_manager, init_database
from database.service import DatabaseService
from core.exceptions import DatabaseError
from core.config import is_demo_mode

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"
FINDINGS_FILE = DATA_DIR / "findings.json"
CASES_FILE = DATA_DIR / "cases.json"


def _cosine_sim(a, b):
    """Compute cosine similarity between two vectors.
    
    Returns None if the vectors have incompatible dimensions.
    """
    a, b = np.array(a), np.array(b)
    if a.shape != b.shape:
        return None
    norm = np.linalg.norm(a) * np.linalg.norm(b)
    return float(np.dot(a, b) / norm) if norm > 0 else 0.0


class DatabaseDataService:
    # Minimum seconds between reconnection attempts when DB is unreachable.
    # Without this, a Postgres outage during startup would trap the singleton
    # in JSON-file fallback for the rest of the process lifetime.
    _RECONNECT_INTERVAL_SECONDS = 10.0

    def __init__(self, require_db: bool = False):
        self._db_service = None
        self._db_connected = False
        self._use_json_fallback = False
        self._last_reconnect_attempt = 0.0
        self._demo_mode = is_demo_mode()
        self._demo_service = None
        self._s3_service = None

        if self._demo_mode:
            logger.info("Demo mode enabled - using generated sample data")
            from services.demo_data_service import get_demo_service
            self._demo_service = get_demo_service()
        else:
            self._init_database(require_db)
        DATA_DIR.mkdir(exist_ok=True)

    def _init_database(self, require_db: bool = False):
        try:
            init_database(echo=False, create_tables=True)
            db_manager = get_db_manager()
            if not db_manager.health_check():
                raise DatabaseError("Database health check failed")
            self._db_service = DatabaseService()
            self._db_connected = True
            if self._use_json_fallback:
                logger.info("PostgreSQL reconnected - exiting JSON file fallback")
                self._use_json_fallback = False
            else:
                logger.info("PostgreSQL connection established")
        except Exception as e:
            self._db_connected = False
            self._db_service = None
            logger.warning(f"PostgreSQL not available: {e}")
            if require_db:
                raise DatabaseError(f"Failed to connect to PostgreSQL: {e}")
            if not self._use_json_fallback:
                self._use_json_fallback = True
                logger.info("Using JSON file fallback for data storage")

    @property
    def _db_available(self) -> bool:
        """True when the Postgres connection is healthy.

        If currently disconnected, transparently retries `_init_database`
        at most once per `_RECONNECT_INTERVAL_SECONDS` so the service
        recovers automatically when Postgres becomes reachable again.
        """
        if self._db_connected:
            return True
        if self._demo_mode:
            return False
        now = time.monotonic()
        if now - self._last_reconnect_attempt < self._RECONNECT_INTERVAL_SECONDS:
            return False
        self._last_reconnect_attempt = now
        self._init_database(require_db=False)
        return self._db_connected
    
    def is_using_database(self) -> bool:
        return self._db_available and self._db_service is not None
    
    def is_demo_mode(self) -> bool:
        return self._demo_mode
    
    def get_backend_info(self) -> dict:
        if self._demo_mode:
            return {'backend': 'demo', 'database_available': False, 'demo_mode': True}
        if self._db_available:
            return {'backend': 'postgresql', 'database_available': True, 'demo_mode': False}
        elif self._use_json_fallback:
            return {'backend': 'json', 'database_available': False, 'demo_mode': False}
        return {'backend': 'none', 'database_available': False, 'demo_mode': False}
    
    def _load_findings_json(self) -> List[Dict]:
        if FINDINGS_FILE.exists():
            try:
                with open(FINDINGS_FILE) as f:
                    data = json.load(f)
                    return data.get('findings', []) if isinstance(data, dict) else data
            except Exception as e:
                logger.error(f"Error loading findings JSON: {e}")
        return []
    
    def _save_findings_json(self, findings: List[Dict]) -> bool:
        try:
            with open(FINDINGS_FILE, 'w') as f:
                json.dump({'findings': findings}, f, indent=2, default=str)
            return True
        except Exception as e:
            logger.error(f"Error saving findings JSON: {e}")
            return False
    
    def _load_cases_json(self) -> List[Dict]:
        if CASES_FILE.exists():
            try:
                with open(CASES_FILE) as f:
                    data = json.load(f)
                    return data.get('cases', []) if isinstance(data, dict) else data
            except Exception as e:
                logger.error(f"Error loading cases JSON: {e}")
        return []
    
    def _save_cases_json(self, cases: List[Dict]) -> bool:
        try:
            with open(CASES_FILE, 'w') as f:
                json.dump({'cases': cases}, f, indent=2, default=str)
            return True
        except Exception as e:
            logger.error(f"Error saving cases JSON: {e}")
            return False
    
    def get_findings(
        self, limit: int = 10000, offset: int = 0,
        severity: Optional[str] = None, data_source: Optional[str] = None,
        cluster_id: Optional[str] = None, min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None, search_query: Optional[str] = None,
        sort_by: str = "timestamp", sort_order: str = "desc"
    ) -> List[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_findings(limit)
        if self._db_available:
            try:
                findings = self._db_service.get_findings(
                    severity=severity, data_source=data_source,
                    cluster_id=cluster_id, min_anomaly_score=min_anomaly_score,
                    status=status, search_query=search_query,
                    limit=limit, offset=offset,
                    sort_by=sort_by, sort_order=sort_order,
                )
                return [f.to_dict() for f in findings]
            except Exception as e:
                logger.error(f"Error getting findings from DB: {e}")
                return []
        elif self._use_json_fallback:
            findings = self._load_findings_json()
            if severity:
                findings = [f for f in findings if f.get('severity') == severity]
            if data_source:
                findings = [f for f in findings if f.get('data_source') == data_source]
            if cluster_id is not None:
                findings = [f for f in findings if f.get('cluster_id') == cluster_id]
            if min_anomaly_score is not None:
                findings = [f for f in findings if f.get('anomaly_score', 0) >= min_anomaly_score]
            if status:
                findings = [f for f in findings if f.get('status') == status]
            if search_query:
                q = search_query.lower()
                findings = [f for f in findings if (
                    q in f.get('finding_id', '').lower()
                    or q in str(f.get('entity_context', '')).lower()
                    or q in f.get('description', '').lower()
                )]
            reverse = sort_order == "desc"
            findings.sort(key=lambda f: f.get(sort_by, ''), reverse=reverse)
            return findings[offset:offset + limit]
        return []

    def count_findings(
        self, severity: Optional[str] = None, data_source: Optional[str] = None,
        cluster_id: Optional[str] = None, min_anomaly_score: Optional[float] = None,
        status: Optional[str] = None, search_query: Optional[str] = None,
    ) -> int:
        if self._demo_mode and self._demo_service:
            return len(self._demo_service.get_findings(10000))
        if self._db_available:
            try:
                return self._db_service.count_findings(
                    severity=severity, data_source=data_source,
                    cluster_id=cluster_id, min_anomaly_score=min_anomaly_score,
                    status=status, search_query=search_query,
                )
            except Exception as e:
                logger.error(f"Error counting findings from DB: {e}")
                return 0
        elif self._use_json_fallback:
            findings = self._load_findings_json()
            if severity:
                findings = [f for f in findings if f.get('severity') == severity]
            if data_source:
                findings = [f for f in findings if f.get('data_source') == data_source]
            if cluster_id is not None:
                findings = [f for f in findings if f.get('cluster_id') == cluster_id]
            if min_anomaly_score is not None:
                findings = [f for f in findings if f.get('anomaly_score', 0) >= min_anomaly_score]
            if status:
                findings = [f for f in findings if f.get('status') == status]
            if search_query:
                q = search_query.lower()
                findings = [f for f in findings if (
                    q in f.get('finding_id', '').lower()
                    or q in str(f.get('entity_context', '')).lower()
                    or q in f.get('description', '').lower()
                )]
            return len(findings)
        return 0
    
    def get_finding(self, finding_id: str) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_finding(finding_id)
        if self._db_available:
            try:
                finding = self._db_service.get_finding(finding_id)
                return finding.to_dict() if finding else None
            except Exception as e:
                logger.error(f"Error getting finding from DB: {e}")
                return None
        elif self._use_json_fallback:
            findings = self._load_findings_json()
            for f in findings:
                if f.get('finding_id') == finding_id:
                    return f
        return None
    
    def get_nearest_neighbors(self, finding_id: str, limit: int = 10) -> Dict:
        """Find similar findings using embedding-based cosine similarity.
        
        Args:
            finding_id: Reference finding ID to find neighbors for
            limit: Maximum number of neighbors to return
            
        Returns:
            Dict with seed_finding and neighbors list
        """
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_nearest_neighbors(finding_id, limit)
        
        # Get all findings (from DB or JSON fallback)
        if self._db_available:
            try:
                findings_objs = self._db_service.get_findings(limit=10000)
                findings = [f.to_dict() for f in findings_objs]
            except Exception as e:
                logger.error(f"Error getting findings from DB for nearest_neighbors: {e}")
                return {"error": str(e)}
        elif self._use_json_fallback:
            findings = self._load_findings_json()
        else:
            return {"error": "No data backend available"}
        
        # Find the seed finding
        seed = next((f for f in findings if f.get('finding_id') == finding_id), None)
        if not seed or 'embedding' not in seed:
            return {"error": f"Finding {finding_id} not found or has no embedding"}
        
        # Compute cosine similarity against all other findings with embeddings
        sims = []
        skipped = 0
        for f in findings:
            if f.get('finding_id') != finding_id and 'embedding' in f:
                sim = _cosine_sim(seed['embedding'], f['embedding'])
                if sim is None:
                    skipped += 1
                    continue
                sims.append({
                    "finding_id": f['finding_id'],
                    "similarity": round(sim, 4),
                    "cluster_id": f.get('cluster_id'),
                    "severity": f.get('severity'),
                    "data_source": f.get('data_source'),
                    "anomaly_score": float(f.get('anomaly_score', 0)),
                })
        if skipped:
            logger.warning(f"Skipped {skipped} findings with incompatible embedding dimensions")
        
        sims.sort(key=lambda x: x['similarity'], reverse=True)
        return {"seed_finding": finding_id, "neighbors": sims[:limit]}
    
    def create_finding(self, finding_data: Dict) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.create_finding(finding_data)
        if self._db_available:
            try:
                finding = self._db_service.create_finding(
                    finding_id=finding_data.get('finding_id'),
                    embedding=finding_data.get('embedding', [0.0] * 768),
                    mitre_predictions=finding_data.get('mitre_predictions', {}),
                    anomaly_score=float(finding_data.get('anomaly_score', 0.0)),
                    timestamp=finding_data.get('timestamp', datetime.utcnow()),
                    data_source=finding_data.get('data_source', 'imported'),
                    description=finding_data.get('description'),
                    entity_context=finding_data.get('entity_context'),
                    evidence_links=finding_data.get('evidence_links'),
                    cluster_id=finding_data.get('cluster_id'),
                    severity=finding_data.get('severity'),
                    status=finding_data.get('status', 'new')
                )
                return finding.to_dict() if finding else None
            except Exception as e:
                logger.error(f"Error creating finding in DB: {e}")
                return None
        elif self._use_json_fallback:
            findings = self._load_findings_json()
            findings.append(finding_data)
            if self._save_findings_json(findings):
                return finding_data
        return None
    
    def update_finding(self, finding_id: str, **updates) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.update_finding(finding_id, **updates)
        if self._db_available:
            try:
                return self._db_service.update_finding(finding_id, **updates)
            except Exception as e:
                logger.error(f"Error updating finding in DB: {e}")
                return False
        elif self._use_json_fallback:
            findings = self._load_findings_json()
            for f in findings:
                if f.get('finding_id') == finding_id:
                    f.update(updates)
                    return self._save_findings_json(findings)
        return False
    
    def save_findings(self, findings: List[Dict]) -> bool:
        if self._use_json_fallback:
            return self._save_findings_json(findings)
        return False
    
    def get_cases(self, limit: int = 10000) -> List[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_cases(limit)
        if self._db_available:
            try:
                cases = self._db_service.get_cases(limit=limit)
                return [c.to_dict() for c in cases]
            except Exception as e:
                logger.error(f"Error getting cases from DB: {e}")
                return []
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            return cases[:limit]
        return []
    
    def get_case(self, case_id: str) -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.get_case(case_id)
        if self._db_available:
            try:
                case = self._db_service.get_case(case_id, include_findings=True)
                return case.to_dict() if case else None
            except Exception as e:
                logger.error(f"Error getting case from DB: {e}")
                return None
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            for c in cases:
                if c.get('case_id') == case_id:
                    return c
        return None
    
    def create_case(self, title: str, finding_ids: List[str], priority: str = "medium",
                    description: str = "", status: str = "open") -> Optional[Dict]:
        if self._demo_mode and self._demo_service:
            return self._demo_service.create_case(title, finding_ids, priority, description, status)
        
        case_id = f"case-{datetime.now().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"
        
        if self._db_available:
            try:
                case = self._db_service.create_case(
                    case_id=case_id, title=title, finding_ids=finding_ids,
                    description=description, status=status, priority=priority
                )
                return case.to_dict() if case else None
            except Exception as e:
                logger.error(f"Error creating case in DB: {e}")
                return None
        elif self._use_json_fallback:
            now = datetime.utcnow().isoformat()
            case_data = {
                'case_id': case_id,
                'title': title,
                'description': description,
                'finding_ids': finding_ids,
                'status': status,
                'priority': priority,
                'created_at': now,
                'updated_at': now,
                'notes': [],
                'timeline': [{'timestamp': now, 'event': 'Case created'}]
            }
            cases = self._load_cases_json()
            cases.append(case_data)
            if self._save_cases_json(cases):
                return case_data
        return None
    
    def create_case_from_dict(self, case_data: Dict) -> Optional[Dict]:
        if self._db_available:
            try:
                case = self._db_service.create_case(
                    case_id=case_data.get('case_id'),
                    title=case_data.get('title', ''),
                    finding_ids=case_data.get('finding_ids', []),
                    description=case_data.get('description', ''),
                    status=case_data.get('status', 'open'),
                    priority=case_data.get('priority', 'medium')
                )
                return case.to_dict() if case else None
            except Exception as e:
                logger.error(f"Error creating case in DB: {e}")
                return None
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            cases.append(case_data)
            if self._save_cases_json(cases):
                return case_data
        return None
    
    def update_case(self, case_id: str, **updates) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.update_case(case_id, **updates)
        if self._db_available:
            try:
                return self._db_service.update_case(case_id, **updates)
            except Exception as e:
                logger.error(f"Error updating case in DB: {e}")
                return False
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            for c in cases:
                if c.get('case_id') == case_id:
                    c.update(updates)
                    c['updated_at'] = datetime.utcnow().isoformat()
                    return self._save_cases_json(cases)
        return False
    
    def delete_case(self, case_id: str) -> bool:
        if self._demo_mode and self._demo_service:
            return self._demo_service.delete_case(case_id)
        if self._db_available:
            try:
                return self._db_service.delete_case(case_id)
            except Exception as e:
                logger.error(f"Error deleting case from DB: {e}")
                return False
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            cases = [c for c in cases if c.get('case_id') != case_id]
            return self._save_cases_json(cases)
        return False
    
    def get_findings_by_case(self, case_id: str) -> List[Dict]:
        """Get all findings associated with a case.
        
        Args:
            case_id: ID of the case
            
        Returns:
            List of finding dictionaries
        """
        if self._demo_mode and self._demo_service:
            # Get case and extract finding IDs
            case = self._demo_service.get_case(case_id)
            if case and 'finding_ids' in case:
                findings = []
                for fid in case['finding_ids']:
                    finding = self._demo_service.get_finding(fid)
                    if finding:
                        findings.append(finding)
                return findings
            return []
        
        if self._db_available:
            try:
                # Get case with findings
                case = self._db_service.get_case(case_id, include_findings=True)
                if case and case.findings:
                    return [f.to_dict() for f in case.findings]
                return []
            except Exception as e:
                logger.error(f"Error getting findings for case from DB: {e}")
                return []
        elif self._use_json_fallback:
            # Get case and lookup findings
            case = self.get_case(case_id)
            if case and 'finding_ids' in case:
                findings = self._load_findings_json()
                return [f for f in findings if f.get('finding_id') in case['finding_ids']]
        return []
    
    def add_finding_to_case(self, case_id: str, finding_id: str) -> bool:
        """Add a finding to an existing case.
        
        Args:
            case_id: The case ID
            finding_id: The finding ID to add
            
        Returns:
            True if successful, False otherwise
        """
        if self._demo_mode and self._demo_service:
            return self._demo_service.add_finding_to_case(case_id, finding_id)
        
        if self._db_available:
            try:
                return self._db_service.add_finding_to_case(case_id, finding_id)
            except Exception as e:
                logger.error(f"Error adding finding to case in DB: {e}")
                return False
        elif self._use_json_fallback:
            cases = self._load_cases_json()
            for c in cases:
                if c.get('case_id') == case_id:
                    if 'finding_ids' not in c:
                        c['finding_ids'] = []
                    if finding_id not in c['finding_ids']:
                        c['finding_ids'].append(finding_id)
                        c['updated_at'] = datetime.utcnow().isoformat()
                    return self._save_cases_json(cases)
            return False
        return False
    
    def save_cases(self, cases: List[Dict]) -> bool:
        if self._use_json_fallback:
            return self._save_cases_json(cases)
        return False
    
    def export_findings(self, output_path: Path, fmt: str = "json") -> bool:
        findings = self.get_findings()
        try:
            with open(output_path, 'w') as f:
                if fmt == "jsonl":
                    for finding in findings:
                        f.write(json.dumps(finding) + '\n')
                else:
                    json.dump({"findings": findings}, f, indent=2)
            return True
        except Exception as e:
            logger.error(f"Error exporting findings: {e}")
            return False
    
    def _init_s3_service(self) -> bool:
        """
        Initialize S3 service from configuration.
        
        Returns:
            True if S3 is configured and initialized, False otherwise
        """
        try:
            # Load S3 config from database
            from database.config_service import get_config_service
            from backend.secrets_manager import get_secret
            
            config_service = get_config_service()
            s3_config = config_service.get_integration_config('s3')
            
            if not s3_config:
                # Fallback to file-based config
                config_file = Path.home() / '.deeptempo' / 's3_config.json'
                if config_file.exists():
                    with open(config_file, 'r') as f:
                        s3_config = json.load(f)
                else:
                    return False
            
            # Unwrap nested config if present (DB stores config under 'config' key)
            if 'config' in s3_config and isinstance(s3_config['config'], dict):
                s3_config = s3_config['config']

            # Parse bucket name -- handle full S3 URIs like s3://bucket/path
            raw_bucket = s3_config.get('bucket_name', '')
            if raw_bucket.startswith('s3://'):
                parts = raw_bucket[5:].split('/', 1)
                bucket_name = parts[0]
            else:
                bucket_name = raw_bucket

            from services.s3_service import S3Service

            auth_method = s3_config.get('auth_method', 'credentials')
            aws_profile = s3_config.get('aws_profile', '')

            if auth_method == 'profile' and aws_profile:
                self._s3_service = S3Service(
                    bucket_name=bucket_name,
                    region_name=s3_config.get('region', 'us-east-1'),
                    aws_profile=aws_profile,
                )
            else:
                access_key_id = get_secret("AWS_ACCESS_KEY_ID")
                secret_access_key = get_secret("AWS_SECRET_ACCESS_KEY")
                session_token = get_secret("AWS_SESSION_TOKEN")

                if not access_key_id or not secret_access_key:
                    logger.warning("S3 credentials not found in secrets manager")
                    return False

                self._s3_service = S3Service(
                    bucket_name=bucket_name,
                    region_name=s3_config.get('region', 'us-east-1'),
                    aws_access_key_id=access_key_id,
                    aws_secret_access_key=secret_access_key,
                    aws_session_token=session_token or None,
                )
            
            # Test connection
            success, message = self._s3_service.test_connection()
            if success:
                logger.info(f"S3 service initialized: {message}")
                return True
            else:
                logger.warning(f"S3 connection test failed: {message}")
                self._s3_service = None
                return False
                
        except Exception as e:
            logger.error(f"Error initializing S3 service: {e}")
            self._s3_service = None
            return False
    
    def is_s3_configured(self) -> bool:
        """Check if S3 is configured and available."""
        if self._s3_service is None:
            self._init_s3_service()
        return self._s3_service is not None
    
    def sync_from_s3(self) -> Tuple[bool, str, Dict]:
        """
        Sync findings and cases from S3 to local storage.
        
        Returns:
            Tuple of (success, message, stats)
        """
        if self._demo_mode:
            return False, "Cannot sync from S3 in demo mode", {}
        
        # Initialize S3 if not already done
        if self._s3_service is None:
            if not self._init_s3_service():
                return False, "S3 not configured or connection failed", {}
        
        try:
            # Load S3 config to get file paths
            from database.config_service import get_config_service
            config_service = get_config_service()
            s3_config = config_service.get_integration_config('s3')
            
            if not s3_config:
                config_file = Path.home() / '.deeptempo' / 's3_config.json'
                if config_file.exists():
                    with open(config_file, 'r') as f:
                        s3_config = json.load(f)
                else:
                    s3_config = {}
            
            findings_path = s3_config.get('findings_path', 'findings.json')
            cases_path = s3_config.get('cases_path', 'cases.json')
            
            findings_synced = 0
            cases_synced = 0
            errors = []
            
            # Sync findings
            logger.info(f"Fetching findings from S3: {findings_path}")
            s3_findings = self._s3_service.get_findings(key=findings_path)
            
            if s3_findings:
                logger.info(f"Retrieved {len(s3_findings)} findings from S3")
                
                if self._db_available:
                    # Sync to PostgreSQL
                    for finding in s3_findings:
                        try:
                            # Check if finding exists
                            existing = self._db_service.get_finding(finding.get('finding_id'))
                            
                            if existing:
                                # Update existing finding
                                self._db_service.update_finding(finding.get('finding_id'), **finding)
                            else:
                                # Create new finding
                                self._db_service.create_finding(
                                    finding_id=finding.get('finding_id'),
                                    embedding=finding.get('embedding', [0.0] * 768),
                                    mitre_predictions=finding.get('mitre_predictions', {}),
                                    anomaly_score=float(finding.get('anomaly_score', 0.0)),
                                    timestamp=finding.get('timestamp', datetime.utcnow()),
                                    data_source=finding.get('data_source', 's3_import'),
                                    entity_context=finding.get('entity_context'),
                                    evidence_links=finding.get('evidence_links'),
                                    cluster_id=finding.get('cluster_id'),
                                    severity=finding.get('severity'),
                                    status=finding.get('status', 'new')
                                )
                            findings_synced += 1
                        except Exception as e:
                            logger.error(f"Error syncing finding {finding.get('finding_id')}: {e}")
                            errors.append(f"Finding {finding.get('finding_id')}: {str(e)}")
                
                elif self._use_json_fallback:
                    # Sync to JSON file
                    if self._save_findings_json(s3_findings):
                        findings_synced = len(s3_findings)
                    else:
                        errors.append("Failed to save findings to JSON file")
            else:
                logger.warning(f"No findings found in S3 at {findings_path}")
            
            # Sync cases
            logger.info(f"Fetching cases from S3: {cases_path}")
            s3_cases = self._s3_service.get_cases(key=cases_path)
            
            if s3_cases:
                logger.info(f"Retrieved {len(s3_cases)} cases from S3")
                
                if self._db_available:
                    # Sync to PostgreSQL
                    for case in s3_cases:
                        try:
                            # Check if case exists
                            existing = self._db_service.get_case(case.get('case_id'), include_findings=False)
                            
                            if existing:
                                # Update existing case
                                self._db_service.update_case(case.get('case_id'), **case)
                            else:
                                # Create new case
                                self._db_service.create_case(
                                    case_id=case.get('case_id'),
                                    title=case.get('title', ''),
                                    finding_ids=case.get('finding_ids', []),
                                    description=case.get('description', ''),
                                    status=case.get('status', 'open'),
                                    priority=case.get('priority', 'medium')
                                )
                            cases_synced += 1
                        except Exception as e:
                            logger.error(f"Error syncing case {case.get('case_id')}: {e}")
                            errors.append(f"Case {case.get('case_id')}: {str(e)}")
                
                elif self._use_json_fallback:
                    # Sync to JSON file
                    if self._save_cases_json(s3_cases):
                        cases_synced = len(s3_cases)
                    else:
                        errors.append("Failed to save cases to JSON file")
            else:
                logger.warning(f"No cases found in S3 at {cases_path}")
            
            # Build result message
            stats = {
                "findings_synced": findings_synced,
                "cases_synced": cases_synced,
                "errors": errors
            }
            
            if findings_synced > 0 or cases_synced > 0:
                message = f"Successfully synced {findings_synced} findings and {cases_synced} cases from S3"
                if errors:
                    message += f" (with {len(errors)} errors)"
                logger.info(message)
                return True, message, stats
            else:
                message = "No data synced from S3"
                if errors:
                    message += f": {'; '.join(errors)}"
                return False, message, stats
                
        except Exception as e:
            error_msg = f"Error syncing from S3: {str(e)}"
            logger.error(error_msg)
            return False, error_msg, {"findings_synced": 0, "cases_synced": 0, "errors": [error_msg]}
