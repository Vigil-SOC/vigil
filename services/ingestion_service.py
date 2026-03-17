"""
Data Ingestion Service for Vigil SOC

Handles ingestion of findings and cases from various formats:
- JSON files
- CSV files  
- JSONL (JSON Lines) files
- Parquet files (DeepTempo LogLM embeddings)
- Direct JSON data

All data is stored in PostgreSQL when available, with fallback to JSON files.
"""

import json
import csv
import hashlib
import logging
import tempfile
from pathlib import Path
from typing import List, Dict, Any, Optional, Union
from datetime import datetime
from io import StringIO

logger = logging.getLogger(__name__)

MITRE_TACTIC_MAP = {
    0: 'Impact', 1: 'Execution', 2: 'Reconnaissance', 3: 'Credential Access',
    4: 'Initial Access', 5: 'Persistence', 6: 'Discovery', 7: 'Command and Control',
    8: 'Lateral Movement', 9: 'Defense Evasion', 10: 'Collection',
    11: 'Privilege Escalation', 12: 'Exfiltration',
}


class IngestionService:
    """Service for ingesting data from various formats into the database."""
    
    def __init__(self):
        """Initialize the ingestion service."""
        # Import here to avoid circular dependencies
        try:
            from database.service import DatabaseService
            from database.connection import get_db_manager
            
            db_manager = get_db_manager()
            if db_manager.health_check():
                self.db_service = DatabaseService()
                self.use_database = True
                logger.info("Ingestion service using PostgreSQL database")
            else:
                self.db_service = None
                self.use_database = False
                logger.warning("Database unavailable, using JSON fallback")
        except Exception as e:
            logger.warning(f"Database not available: {e}, using JSON fallback")
            self.db_service = None
            self.use_database = False
        
        # Statistics for reporting
        self.stats = {
            'findings_total': 0,
            'findings_imported': 0,
            'findings_skipped': 0,
            'findings_errors': 0,
            'cases_total': 0,
            'cases_imported': 0,
            'cases_skipped': 0,
            'cases_errors': 0,
        }
    
    def reset_stats(self):
        """Reset ingestion statistics."""
        for key in self.stats:
            self.stats[key] = 0
    
    def parse_timestamp(self, timestamp_value: Any) -> datetime:
        """
        Parse various timestamp formats to datetime.
        
        Args:
            timestamp_value: Timestamp as string, int, or datetime
        
        Returns:
            datetime object
        """
        if isinstance(timestamp_value, datetime):
            return timestamp_value
        
        if not timestamp_value:
            return datetime.utcnow()
        
        # If it's a Unix timestamp (int or float)
        if isinstance(timestamp_value, (int, float)):
            try:
                return datetime.fromtimestamp(timestamp_value)
            except (ValueError, OSError):
                pass
        
        # Try various string formats
        timestamp_str = str(timestamp_value)
        formats = [
            '%Y-%m-%dT%H:%M:%S.%fZ',
            '%Y-%m-%dT%H:%M:%S.%f%z',
            '%Y-%m-%dT%H:%M:%SZ',
            '%Y-%m-%dT%H:%M:%S%z',
            '%Y-%m-%dT%H:%M:%S',
            '%Y-%m-%d %H:%M:%S',
            '%Y-%m-%d',
        ]
        
        for fmt in formats:
            try:
                ts_str = timestamp_str.replace('+00:00', '').replace('Z', '')
                return datetime.strptime(ts_str, fmt.replace('Z', '').replace('%z', ''))
            except ValueError:
                continue
        
        logger.warning(f"Could not parse timestamp: {timestamp_value}, using current time")
        return datetime.utcnow()
    
    def ingest_finding(self, finding_data: Dict[str, Any]) -> bool:
        """
        Ingest a single finding into the database.
        
        Args:
            finding_data: Finding dictionary
        
        Returns:
            True if successful, False otherwise
        """
        finding_id = finding_data.get('finding_id')
        if not finding_id:
            logger.error("Finding missing finding_id")
            self.stats['findings_errors'] += 1
            return False
        
        try:
            if self.use_database and self.db_service:
                # Check if finding already exists
                existing = self.db_service.get_finding(finding_id)
                if existing:
                    logger.debug(f"Finding {finding_id} already exists, skipping")
                    self.stats['findings_skipped'] += 1
                    return True
                
                # Parse timestamp
                timestamp = self.parse_timestamp(finding_data.get('timestamp'))
                
                # Create finding in database
                finding = self.db_service.create_finding(
                    finding_id=finding_id,
                    embedding=finding_data.get('embedding', [0.0] * 768),
                    mitre_predictions=finding_data.get('mitre_predictions', {}),
                    anomaly_score=float(finding_data.get('anomaly_score', 0.0)),
                    timestamp=timestamp,
                    data_source=finding_data.get('data_source', 'imported'),
                    description=finding_data.get('description'),
                    entity_context=finding_data.get('entity_context'),
                    evidence_links=finding_data.get('evidence_links'),
                    cluster_id=finding_data.get('cluster_id'),
                    severity=finding_data.get('severity'),
                    status=finding_data.get('status', 'new')
                )
                
                if finding:
                    self.stats['findings_imported'] += 1
                    logger.debug(f"Imported finding: {finding_id}")
                    return True
                else:
                    self.stats['findings_errors'] += 1
                    logger.error(f"Failed to create finding: {finding_id}")
                    return False
            else:
                # Fallback to JSON file storage
                from services.database_data_service import DatabaseDataService
                data_service = DatabaseDataService()
                findings = data_service.get_findings()
                
                # Check for duplicate
                if any(f.get('finding_id') == finding_id for f in findings):
                    self.stats['findings_skipped'] += 1
                    return True
                
                findings.append(finding_data)
                if data_service.save_findings(findings):
                    self.stats['findings_imported'] += 1
                    return True
                else:
                    self.stats['findings_errors'] += 1
                    return False
        
        except Exception as e:
            self.stats['findings_errors'] += 1
            logger.error(f"Error ingesting finding {finding_id}: {e}")
            return False
    
    def ingest_case(self, case_data: Dict[str, Any]) -> bool:
        """
        Ingest a single case into the database.
        
        Args:
            case_data: Case dictionary
        
        Returns:
            True if successful, False otherwise
        """
        case_id = case_data.get('case_id')
        if not case_id:
            logger.error("Case missing case_id")
            self.stats['cases_errors'] += 1
            return False
        
        try:
            if self.use_database and self.db_service:
                # Check if case already exists
                existing = self.db_service.get_case(case_id)
                if existing:
                    logger.debug(f"Case {case_id} already exists, skipping")
                    self.stats['cases_skipped'] += 1
                    return True
                
                # Create case in database
                case = self.db_service.create_case(
                    case_id=case_id,
                    title=case_data.get('title', 'Imported Case'),
                    finding_ids=case_data.get('finding_ids', []),
                    description=case_data.get('description', ''),
                    status=case_data.get('status', 'new'),
                    priority=case_data.get('priority', 'medium'),
                    assignee=case_data.get('assignee'),
                    tags=case_data.get('tags', []),
                    notes=case_data.get('notes', []),
                    timeline=case_data.get('timeline', []),
                    activities=case_data.get('activities', []),
                    resolution_steps=case_data.get('resolution_steps', []),
                    mitre_techniques=case_data.get('mitre_techniques')
                )
                
                if case:
                    self.stats['cases_imported'] += 1
                    logger.debug(f"Imported case: {case_id}")
                    return True
                else:
                    self.stats['cases_errors'] += 1
                    logger.error(f"Failed to create case: {case_id}")
                    return False
            else:
                # Fallback to JSON file storage
                from services.database_data_service import DatabaseDataService
                data_service = DatabaseDataService()
                cases = data_service.get_cases()
                
                # Check for duplicate
                if any(c.get('case_id') == case_id for c in cases):
                    self.stats['cases_skipped'] += 1
                    return True
                
                cases.append(case_data)
                if data_service.save_cases(cases):
                    self.stats['cases_imported'] += 1
                    return True
                else:
                    self.stats['cases_errors'] += 1
                    return False
        
        except Exception as e:
            self.stats['cases_errors'] += 1
            logger.error(f"Error ingesting case {case_id}: {e}")
            return False
    
    def ingest_json_file(self, file_path: Union[str, Path]) -> Dict[str, Any]:
        """
        Ingest data from a JSON file using streaming for large files.
        
        Supports:
        - {"findings": [...], "cases": [...]}  (dict with findings/cases keys)
        - [{"finding_id": ...}, ...]  (top-level array of findings)
        - [{"case_id": ...}, ...]  (top-level array of cases)
        
        Uses ijson for streaming when available, falls back to json.load for
        small files or unsupported structures.
        """
        self.reset_stats()
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return self.stats
        
        try:
            import ijson
            self._ingest_json_streaming(file_path, ijson)
        except ImportError:
            logger.info("ijson not available, falling back to full json.load")
            self._ingest_json_full(file_path)
        except Exception as e:
            logger.warning(f"Streaming JSON parse failed, falling back to json.load: {e}")
            try:
                self._ingest_json_full(file_path)
            except Exception as e2:
                logger.error(f"Error ingesting JSON file: {e2}")
        
        logger.info(f"JSON ingestion complete: {self.stats}")
        return self.stats

    def _ingest_json_streaming(self, file_path: Path, ijson) -> None:
        """Stream-parse a JSON file item by item to avoid loading it all into memory."""
        with open(file_path, 'rb') as f:
            peek = f.read(1)
            f.seek(0)
            
            if peek == b'{':
                for item in ijson.items(f, 'findings.item'):
                    self.stats['findings_total'] += 1
                    self.ingest_finding(item)
                f.seek(0)
                for item in ijson.items(f, 'cases.item'):
                    self.stats['cases_total'] += 1
                    self.ingest_case(item)
            elif peek == b'[':
                first_key = None
                for item in ijson.items(f, 'item'):
                    if first_key is None:
                        first_key = 'finding' if 'finding_id' in item else 'case' if 'case_id' in item else 'finding'
                    if first_key == 'finding':
                        self.stats['findings_total'] += 1
                        self.ingest_finding(item)
                    else:
                        self.stats['cases_total'] += 1
                        self.ingest_case(item)

    def _ingest_json_full(self, file_path: Path) -> None:
        """Fallback: load entire JSON file into memory."""
        with open(file_path, 'r') as f:
            data = json.load(f)
        
        findings = []
        cases = []
        
        if isinstance(data, dict):
            findings = data.get('findings', [])
            cases = data.get('cases', [])
        elif isinstance(data, list):
            if data and 'finding_id' in data[0]:
                findings = data
            elif data and 'case_id' in data[0]:
                cases = data
        
        self.stats['findings_total'] = len(findings)
        self.stats['cases_total'] = len(cases)
        
        for finding in findings:
            self.ingest_finding(finding)
        for case in cases:
            self.ingest_case(case)
    
    def ingest_jsonl_file(self, file_path: Union[str, Path], data_type: str = 'finding') -> Dict[str, Any]:
        """
        Ingest data from a JSONL (JSON Lines) file.
        
        Args:
            file_path: Path to JSONL file
            data_type: Type of data ('finding' or 'case')
        
        Returns:
            Dictionary with statistics
        """
        self.reset_stats()
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return self.stats
        
        try:
            with open(file_path, 'r') as f:
                for line_num, line in enumerate(f, 1):
                    line = line.strip()
                    if not line:
                        continue
                    
                    try:
                        data = json.loads(line)
                        
                        if data_type == 'finding':
                            self.stats['findings_total'] += 1
                            self.ingest_finding(data)
                        elif data_type == 'case':
                            self.stats['cases_total'] += 1
                            self.ingest_case(data)
                    
                    except json.JSONDecodeError as e:
                        logger.error(f"Invalid JSON on line {line_num}: {e}")
                        if data_type == 'finding':
                            self.stats['findings_errors'] += 1
                        else:
                            self.stats['cases_errors'] += 1
            
            logger.info(f"JSONL ingestion complete: {self.stats}")
            return self.stats
        
        except Exception as e:
            logger.error(f"Error ingesting JSONL file: {e}")
            return self.stats
    
    def ingest_csv_file(self, file_path: Union[str, Path], data_type: str = 'finding') -> Dict[str, Any]:
        """
        Ingest data from a CSV file.
        
        Args:
            file_path: Path to CSV file
            data_type: Type of data ('finding' or 'case')
        
        Returns:
            Dictionary with statistics
        """
        self.reset_stats()
        file_path = Path(file_path)
        
        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return self.stats
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                reader = csv.DictReader(f)
                
                for row_num, row in enumerate(reader, 1):
                    try:
                        if data_type == 'finding':
                            self.stats['findings_total'] += 1
                            finding_data = self._csv_row_to_finding(row)
                            self.ingest_finding(finding_data)
                        elif data_type == 'case':
                            self.stats['cases_total'] += 1
                            case_data = self._csv_row_to_case(row)
                            self.ingest_case(case_data)
                    
                    except Exception as e:
                        logger.error(f"Error processing CSV row {row_num}: {e}")
                        if data_type == 'finding':
                            self.stats['findings_errors'] += 1
                        else:
                            self.stats['cases_errors'] += 1
            
            logger.info(f"CSV ingestion complete: {self.stats}")
            return self.stats
        
        except Exception as e:
            logger.error(f"Error ingesting CSV file: {e}")
            return self.stats
    
    def _is_tempo_csv(self, row: Dict[str, str]) -> bool:
        """Detect Tempo alert CSV format by checking for characteristic columns."""
        tempo_cols = {'sequence_id', 'mitre_tactic', 'incident_confidence'}
        return bool(tempo_cols & set(row.keys()))

    def _csv_row_to_finding(self, row: Dict[str, str]) -> Dict[str, Any]:
        """
        Convert CSV row to finding dictionary.
        Handles both the generic finding CSV format and the Tempo alert CSV
        format (sequence_id, IP1, IP2, mitre_tactic, incident_confidence, etc.).
        
        Args:
            row: CSV row as dictionary
        
        Returns:
            Finding dictionary
        """
        if self._is_tempo_csv(row):
            return self._tempo_csv_row_to_finding(row)

        # Generate finding_id if not present
        finding_id = row.get('finding_id')
        if not finding_id:
            import uuid
            finding_id = f"f-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:8]}"
        
        # Parse embedding if present (comma-separated floats)
        embedding = []
        if 'embedding' in row and row['embedding']:
            try:
                embedding = [float(x.strip()) for x in row['embedding'].split(',')]
            except ValueError:
                logger.warning(f"Invalid embedding format for {finding_id}, using empty")
                embedding = [0.0] * 768
        else:
            embedding = [0.0] * 768
        
        # Parse MITRE predictions (JSON string or comma-separated)
        mitre_predictions = {}
        if 'mitre_predictions' in row and row['mitre_predictions']:
            try:
                mitre_predictions = json.loads(row['mitre_predictions'])
            except json.JSONDecodeError:
                # Try comma-separated format: T1071.001:0.85,T1048.003:0.72
                try:
                    for pair in row['mitre_predictions'].split(','):
                        technique, score = pair.split(':')
                        mitre_predictions[technique.strip()] = float(score.strip())
                except Exception as e:
                    logger.warning(f"Invalid mitre_predictions format for {finding_id}: {e}")
        
        # Parse entity context (JSON string)
        entity_context = None
        if 'entity_context' in row and row['entity_context']:
            try:
                entity_context = json.loads(row['entity_context'])
            except json.JSONDecodeError:
                logger.warning(f"Invalid entity_context format for {finding_id}")
        
        return {
            'finding_id': finding_id,
            'embedding': embedding,
            'mitre_predictions': mitre_predictions,
            'anomaly_score': float(row.get('anomaly_score', 0.0)),
            'timestamp': row.get('timestamp', datetime.utcnow().isoformat()),
            'data_source': row.get('data_source', 'csv_import'),
            'entity_context': entity_context,
            'evidence_links': None,
            'cluster_id': row.get('cluster_id'),
            'severity': row.get('severity'),
            'status': row.get('status', 'new')
        }

    def _tempo_csv_row_to_finding(self, row: Dict[str, str]) -> Dict[str, Any]:
        """
        Convert a Tempo alert CSV row to a finding dictionary.

        Tempo CSV columns:
            sequence_id, attack_id, IP1, IP2, mitre_tactic,
            incident_confidence, event_start, event_end, created_at, user_feedback
        """
        sequence_id = str(row.get('sequence_id', ''))

        # Parse event_start as timestamp
        event_start_str = row.get('event_start', '')
        event_ts = self.parse_timestamp(event_start_str) if event_start_str else datetime.utcnow()

        # Generate finding_id from sequence_id + attack_id to ensure uniqueness
        # when the same sequence appears with different attack clusters
        attack_id = row.get('attack_id', '').strip()
        unique_key = f"{sequence_id}_{attack_id}" if attack_id else sequence_id
        id_hash = hashlib.sha256(unique_key.encode()).hexdigest()[:8]
        finding_id = f"f-{event_ts.strftime('%Y%m%d')}-{id_hash}"

        # MITRE tactic comes as a name (e.g. "Command and Control")
        mitre_predictions = {}
        mitre_tactic = row.get('mitre_tactic', '').strip()
        if mitre_tactic:
            mitre_predictions[mitre_tactic] = 1.0

        # incident_confidence is 0-100 scale; normalise to 0-1
        raw_confidence = float(row.get('incident_confidence', 0))
        anomaly_score = raw_confidence / 100.0 if raw_confidence > 1.0 else raw_confidence

        # Derive severity from anomaly score
        if anomaly_score >= 0.9:
            severity = 'critical'
        elif anomaly_score >= 0.7:
            severity = 'high'
        elif anomaly_score >= 0.4:
            severity = 'medium'
        else:
            severity = 'low'

        entity_context = {
            'src_ip': row.get('IP1', '').strip() or None,
            'dst_ip': row.get('IP2', '').strip() or None,
            'sequence_id': sequence_id,
            'confidence_score': anomaly_score,
        }
        event_end_str = row.get('event_end', '')
        if event_end_str:
            entity_context['event_end'] = event_end_str

        user_feedback = row.get('user_feedback', '').strip()
        if user_feedback:
            entity_context['user_feedback'] = int(user_feedback) if user_feedback.lstrip('-').isdigit() else user_feedback

        cluster_id = attack_id or None

        return {
            'finding_id': finding_id,
            'embedding': [0.0] * 768,
            'mitre_predictions': mitre_predictions,
            'anomaly_score': anomaly_score,
            'timestamp': event_ts.isoformat(),
            'data_source': row.get('data_source', 'csv_import'),
            'entity_context': entity_context,
            'evidence_links': None,
            'cluster_id': cluster_id,
            'severity': severity,
            'status': 'new',
        }
    
    def _csv_row_to_case(self, row: Dict[str, str]) -> Dict[str, Any]:
        """
        Convert CSV row to case dictionary.
        
        Args:
            row: CSV row as dictionary
        
        Returns:
            Case dictionary
        """
        # Generate case_id if not present
        case_id = row.get('case_id')
        if not case_id:
            import uuid
            case_id = f"case-{datetime.now().strftime('%Y-%m-%d')}-{uuid.uuid4().hex[:8]}"
        
        # Parse finding_ids (comma-separated)
        finding_ids = []
        if 'finding_ids' in row and row['finding_ids']:
            finding_ids = [fid.strip() for fid in row['finding_ids'].split(',')]
        
        # Parse tags (comma-separated)
        tags = []
        if 'tags' in row and row['tags']:
            tags = [tag.strip() for tag in row['tags'].split(',')]
        
        return {
            'case_id': case_id,
            'title': row.get('title', 'Imported Case'),
            'description': row.get('description', ''),
            'finding_ids': finding_ids,
            'status': row.get('status', 'new'),
            'priority': row.get('priority', 'medium'),
            'assignee': row.get('assignee'),
            'tags': tags,
            'notes': [],
            'timeline': [],
            'activities': [],
            'resolution_steps': [],
            'mitre_techniques': None
        }
    
    def ingest_parquet_file(
        self,
        file_path: Union[str, Path],
        data_source: str = 'flow'
    ) -> Dict[str, Any]:
        """
        Ingest findings from a DeepTempo LogLM parquet file.

        Parquet files contain embedding vectors and metadata from the LogLM
        model. Columns are mapped to the findings schema as follows:
          sequence_id   -> finding_id (hashed to f-YYYYMMDD-xxxxxxxx)
          embedding     -> embedding (variable dimension, stored as-is)
          mitre_pred    -> mitre_predictions (integer label stored as key)
          incident_pred -> severity (1=attack, 0=benign)
          confidence_score -> anomaly_score
          focal_ip      -> entity_context.src_ip
          engaged_ip    -> entity_context.dst_ip
          event_start/end_time -> timestamp + entity_context

        Args:
            file_path: Path to parquet file
            data_source: Data source label (default 'flow')

        Returns:
            Dictionary with ingestion statistics
        """
        self.reset_stats()
        file_path = Path(file_path)

        if not file_path.exists():
            logger.error(f"File not found: {file_path}")
            return self.stats

        try:
            import pyarrow.parquet as pq

            parquet_file = pq.ParquetFile(file_path)
            col_names = set(parquet_file.schema_arrow.names)
            schema_str = str(parquet_file.schema_arrow)
            logger.info(f"Parquet schema: {schema_str}")
            logger.info(f"Parquet columns: {sorted(col_names)}")
            self.stats['findings_total'] = parquet_file.metadata.num_rows

            sampled_first_row = False
            batch_size = 1000
            for batch in parquet_file.iter_batches(batch_size=batch_size):
                batch_dict = batch.to_pydict()
                batch_len = len(next(iter(batch_dict.values()))) if batch_dict else 0
                for i in range(batch_len):
                    try:
                        row = {col: batch_dict[col][i] for col in col_names if col in batch_dict}
                        if not sampled_first_row:
                            sample = {k: (type(v).__name__, v) for k, v in row.items() if k != 'embedding'}
                            logger.info(f"Parquet sample row (types+values): {sample}")
                            sampled_first_row = True
                        finding_data = self._parquet_row_to_finding(row, data_source)
                        self.ingest_finding(finding_data)
                    except Exception as e:
                        logger.error(f"Error processing parquet row: {e}")
                        self.stats['findings_errors'] += 1

            logger.info(f"Parquet ingestion complete: {self.stats}")
            return self.stats

        except ImportError:
            logger.error("pyarrow is required for parquet ingestion: pip install pyarrow")
            return self.stats
        except Exception as e:
            logger.error(f"Error ingesting parquet file: {e}")
            return self.stats

    def _parquet_row_to_finding(
        self,
        row: Dict[str, Any],
        data_source: str = 'flow'
    ) -> Dict[str, Any]:
        """
        Transform a row from a DeepTempo LogLM parquet file into a finding dict.

        Args:
            row: Dictionary of column values for one row
            data_source: Data source label

        Returns:
            Finding dictionary ready for ingest_finding()
        """
        sequence_id = str(row.get('sequence_id', ''))

        # Derive event timestamp from event_start_time (epoch milliseconds)
        event_start_ms = row.get('event_start_time')
        if event_start_ms is not None:
            event_ts = datetime.utcfromtimestamp(int(event_start_ms) / 1000.0)
        else:
            event_ts = datetime.utcnow()

        # Generate finding_id: f-{YYYYMMDD}-{8-char hash of sequence_id}
        id_hash = hashlib.sha256(sequence_id.encode()).hexdigest()[:8]
        finding_id = f"f-{event_ts.strftime('%Y%m%d')}-{id_hash}"

        # Embedding: stored as-is regardless of dimension
        embedding = row.get('embedding')
        if embedding is not None:
            embedding = [float(v) for v in embedding]
        else:
            embedding = [0.0] * 768

        # MITRE predictions from logits (softmax) when available, else from argmax label
        mitre_predictions = {}
        mitre_logits = row.get('mitre_logits')
        if mitre_logits is not None and len(mitre_logits) > 0:
            import math
            max_l = max(mitre_logits)
            exps = [math.exp(l - max_l) for l in mitre_logits]
            total = sum(exps)
            for idx, prob in enumerate(exps):
                p = prob / total
                if p >= 0.05:
                    tactic = MITRE_TACTIC_MAP.get(idx, f"mitre_class_{idx}")
                    mitre_predictions[tactic] = round(p, 4)
        elif (mitre_pred := row.get('mitre_pred')) is not None:
            tactic = MITRE_TACTIC_MAP.get(int(mitre_pred))
            if tactic:
                mitre_predictions[tactic] = 1.0
            else:
                mitre_predictions[f"mitre_class_{int(mitre_pred)}"] = 1.0

        # Severity from incident_pred (1=attack, 0=benign)
        incident_pred = int(row.get('incident_pred', 0))
        is_attack = incident_pred == 1

        # anomaly_score from confidence_score if available, else derive from incident_pred
        confidence = row.get('confidence_score')
        if confidence is not None:
            anomaly_score = float(confidence)
        else:
            anomaly_score = 0.85 if is_attack else 0.15

        if is_attack:
            severity = 'critical' if anomaly_score >= 0.9 else 'high'
        else:
            severity = 'medium' if anomaly_score >= 0.5 else 'low'

        # Build entity_context with all available metadata
        entity_context = {
            'src_ip': row.get('focal_ip'),
            'dst_ip': row.get('engaged_ip'),
            'incident_pred': incident_pred,
            'confidence_score': anomaly_score,
            'sequence_id': sequence_id,
        }

        if row.get('event_start_time') is not None:
            entity_context['event_start_time'] = int(row['event_start_time'])
        if row.get('event_end_time') is not None:
            entity_context['event_end_time'] = int(row['event_end_time'])
        if row.get('row_count') is not None:
            entity_context['row_count'] = int(row['row_count'])
        if row.get('incident_pred') is not None:
            entity_context['incident_pred'] = int(row['incident_pred'])

        # cluster_id from attack_id if populated
        attack_id = row.get('attack_id')
        cluster_id = attack_id if attack_id else None

        return {
            'finding_id': finding_id,
            'embedding': embedding,
            'mitre_predictions': mitre_predictions,
            'anomaly_score': anomaly_score,
            'timestamp': event_ts.isoformat(),
            'data_source': data_source,
            'entity_context': entity_context,
            'evidence_links': None,
            'cluster_id': cluster_id,
            'severity': severity,
            'status': 'new',
        }

    # Extension -> (ingestion method name, temp file suffix, file mode for write)
    _S3_FORMAT_MAP = {
        '.parquet': 'parquet',
        '.csv':     'csv',
        '.json':    'json',
        '.jsonl':   'jsonl',
        '.ndjson':  'jsonl',
    }

    def ingest_s3_folder(
        self,
        s3_service,
        prefix: str = "",
        data_source: str = 'flow'
    ) -> Dict[str, Any]:
        """
        Discover and ingest all supported files from an S3 prefix.

        Lists files under the prefix and auto-routes each by extension:
          .parquet          -> ingest_parquet_file
          .csv              -> ingest_csv_file
          .json             -> ingest_json_file
          .jsonl / .ndjson  -> ingest_jsonl_file
        Unsupported extensions are skipped with a warning.

        Args:
            s3_service: An initialised S3Service instance
            prefix: S3 key prefix (folder path), e.g. "embeddings/"
            data_source: Data source label for parquet files

        Returns:
            Dictionary with aggregated ingestion statistics plus
            files_processed and files_skipped counts.
        """
        self.reset_stats()
        files_processed = 0
        files_skipped = 0

        all_keys = s3_service.list_files(prefix=prefix)
        if not all_keys:
            logger.warning(f"No files found under S3 prefix '{prefix}'")
            return {**self.stats, 'files_processed': 0, 'files_skipped': 0}

        logger.info(f"Found {len(all_keys)} file(s) under S3 prefix '{prefix}'")

        for key in all_keys:
            ext = self._s3_key_extension(key)
            fmt = self._S3_FORMAT_MAP.get(ext)

            if fmt is None:
                logger.debug(f"Skipping unsupported file type '{ext}': {key}")
                files_skipped += 1
                continue

            logger.info(f"Downloading s3://{s3_service.bucket_name}/{key} (format: {fmt})")
            content = s3_service.get_file(key)
            if content is None:
                logger.error(f"Failed to download {key} from S3")
                self.stats['findings_errors'] += 1
                continue

            tmp_path = None
            try:
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(content)
                    tmp_path = Path(tmp.name)

                file_stats = self._ingest_file_by_format(tmp_path, fmt, data_source)

                self.stats['findings_total'] += file_stats.get('findings_total', 0)
                self.stats['findings_imported'] += file_stats.get('findings_imported', 0)
                self.stats['findings_skipped'] += file_stats.get('findings_skipped', 0)
                self.stats['findings_errors'] += file_stats.get('findings_errors', 0)
                self.stats['cases_total'] += file_stats.get('cases_total', 0)
                self.stats['cases_imported'] += file_stats.get('cases_imported', 0)
                self.stats['cases_skipped'] += file_stats.get('cases_skipped', 0)
                self.stats['cases_errors'] += file_stats.get('cases_errors', 0)
                files_processed += 1

            except Exception as e:
                logger.error(f"Error processing S3 file {key}: {e}")
                self.stats['findings_errors'] += 1
            finally:
                if tmp_path and tmp_path.exists():
                    tmp_path.unlink()

        logger.info(
            f"S3 folder ingestion complete: {files_processed} processed, "
            f"{files_skipped} skipped, stats={self.stats}"
        )
        return {**self.stats, 'files_processed': files_processed, 'files_skipped': files_skipped}

    def ingest_parquet_from_s3(
        self,
        s3_service,
        prefix: str = "",
        data_source: str = 'flow'
    ) -> Dict[str, Any]:
        """Backward-compatible wrapper: ingest only .parquet files from S3."""
        parquet_keys = s3_service.get_parquet_keys(prefix=prefix)
        if not parquet_keys:
            logger.warning(f"No .parquet files found under S3 prefix '{prefix}'")
            self.reset_stats()
            return self.stats
        return self.ingest_s3_folder(s3_service, prefix=prefix, data_source=data_source)

    @staticmethod
    def _s3_key_extension(key: str) -> str:
        """Extract the lowercase file extension from an S3 key."""
        return Path(key).suffix.lower()

    def _ingest_file_by_format(
        self,
        file_path: Path,
        fmt: str,
        data_source: str = 'flow'
    ) -> Dict[str, Any]:
        """Dispatch a local file to the appropriate ingestion method."""
        if fmt == 'parquet':
            return self.ingest_parquet_file(file_path, data_source=data_source)
        elif fmt == 'csv':
            return self.ingest_csv_file(file_path, data_type='finding')
        elif fmt == 'json':
            return self.ingest_json_file(file_path)
        elif fmt == 'jsonl':
            return self.ingest_jsonl_file(file_path, data_type='finding')
        else:
            logger.warning(f"No handler for format '{fmt}', skipping {file_path}")
            return {}

    def ingest_from_string(
        self,
        data_string: str,
        format: str = 'json',
        data_type: str = 'finding'
    ) -> Dict[str, Any]:
        """
        Ingest data from a string.
        
        Args:
            data_string: Data as string
            format: Format ('json', 'jsonl', 'csv')
            data_type: Type of data ('finding' or 'case')
        
        Returns:
            Dictionary with statistics
        """
        self.reset_stats()
        
        try:
            if format == 'json':
                data = json.loads(data_string)
                
                findings = []
                cases = []
                
                if isinstance(data, dict):
                    # Check if it's a single finding or case based on data_type
                    if data_type == 'finding' and 'finding_id' in data:
                        findings = [data]
                    elif data_type == 'case' and 'case_id' in data:
                        cases = [data]
                    else:
                        # Try to get from wrapped arrays
                        findings = data.get('findings', [])
                        cases = data.get('cases', [])
                elif isinstance(data, list):
                    if data and 'finding_id' in data[0]:
                        findings = data
                    elif data and 'case_id' in data[0]:
                        cases = data
                
                self.stats['findings_total'] = len(findings)
                self.stats['cases_total'] = len(cases)
                
                for finding in findings:
                    self.ingest_finding(finding)
                
                for case in cases:
                    self.ingest_case(case)
            
            elif format == 'jsonl':
                for line in data_string.strip().split('\n'):
                    line = line.strip()
                    if not line:
                        continue
                    
                    data = json.loads(line)
                    
                    if data_type == 'finding':
                        self.stats['findings_total'] += 1
                        self.ingest_finding(data)
                    elif data_type == 'case':
                        self.stats['cases_total'] += 1
                        self.ingest_case(data)
            
            elif format == 'csv':
                reader = csv.DictReader(StringIO(data_string))
                
                for row in reader:
                    if data_type == 'finding':
                        self.stats['findings_total'] += 1
                        finding_data = self._csv_row_to_finding(row)
                        self.ingest_finding(finding_data)
                    elif data_type == 'case':
                        self.stats['cases_total'] += 1
                        case_data = self._csv_row_to_case(row)
                        self.ingest_case(case_data)
            
            logger.info(f"String ingestion complete: {self.stats}")
            return self.stats
        
        except Exception as e:
            logger.error(f"Error ingesting from string: {e}")
            return self.stats

