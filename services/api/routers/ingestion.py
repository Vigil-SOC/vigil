"""
Data Ingestion API endpoints.

Handles uploading and ingesting findings/cases from various file formats:
- JSON files
- CSV files
- JSONL (JSON Lines) files
- Parquet files
- S3 sync
"""

import asyncio
import logging
from datetime import datetime
from typing import Dict, Optional, List
from fastapi import APIRouter, UploadFile, File, Form, HTTPException, Query
from pydantic import BaseModel
from pathlib import Path
from starlette.concurrency import run_in_threadpool
import tempfile

from core.ingestion.ingestion_service import IngestionService
from core.ingestion.ingestion_jobs import (
    IngestionJob,
    IngestionJobConflict,
    get_job_registry,
    run_job,
    summarize_stats,
)
from core.storage.database_data_service import DatabaseDataService
from core.routing import Auth, RouterMeta
from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter()

ROUTER_META = RouterMeta(
    prefix="/api/ingest",
    tags=["ingestion"],
    auth=Auth.REQUIRED,
)

MAX_UPLOAD_SIZE_BYTES = get_settings().max_upload_size_mb * 1024 * 1024

EXTENSION_FORMATS = {
    '.json': 'json',
    '.csv': 'csv',
    '.jsonl': 'jsonl',
    '.ndjson': 'jsonl',
    '.parquet': 'parquet',
}

_running_tasks: set = set()  # asyncio only weak-refs tasks; unheld ones get GC'd


class IngestionStats(BaseModel):
    """Ingestion statistics response."""
    findings_total: int
    findings_imported: int
    findings_skipped: int
    findings_errors: int
    cases_total: int
    cases_imported: int
    cases_skipped: int
    cases_errors: int
    success: bool
    message: str


class IngestionJobStatus(BaseModel):
    """Snapshot of a background ingestion job."""
    job_id: str
    filename: str
    format: str
    data_type: str
    status: str
    determinate: bool
    processed: int
    total: int
    created_at: datetime
    finished_at: Optional[datetime]
    message: str
    error: Optional[str]
    stats: Dict[str, int]


async def _spool_upload(file: UploadFile, suffix: str) -> Path:
    """Stream an upload to a temp file, enforcing the size cap as it goes."""

    with tempfile.NamedTemporaryFile(mode='wb', delete=False, suffix=suffix) as temp_file:
        temp_path = Path(temp_file.name)
        total_size = 0
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            total_size += len(chunk)
            if total_size > MAX_UPLOAD_SIZE_BYTES:
                temp_file.close()
                temp_path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=413,
                    detail=f"File too large. Maximum upload size is {MAX_UPLOAD_SIZE_BYTES // (1024*1024)}MB."
                )
            temp_file.write(chunk)
    return temp_path


@router.post("/upload", response_model=IngestionJobStatus, status_code=202)
async def upload_and_ingest_file(
    file: UploadFile = File(...),
    data_type: str = Form("finding"),
    format: Optional[str] = Form(None)
):
    """Accept a file and ingest it as a background job; poll /ingest/jobs/{id}."""
    if data_type not in ['finding', 'case']:
        raise HTTPException(status_code=400, detail="data_type must be 'finding' or 'case'")

    filename = file.filename or 'upload'
    if not format:
        format = EXTENSION_FORMATS.get(Path(filename).suffix.lower())
        if format is None:
            raise HTTPException(
                status_code=400,
                detail="Unable to detect file format. Please specify format parameter or use .json, .csv, .jsonl, or .parquet extension"
            )

    if format not in ['json', 'csv', 'jsonl', 'parquet']:
        raise HTTPException(status_code=400, detail="format must be 'json', 'csv', 'jsonl', or 'parquet'")

    temp_path = await _spool_upload(file, f'.{format}')

    job = IngestionJob(filename=filename, fmt=format, data_type=data_type)
    try:
        get_job_registry().start(job)
    except IngestionJobConflict as conflict:
        temp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=409,
            detail=f"Already ingesting '{conflict.active.filename}'. Wait for it to finish."
        )

    task = asyncio.create_task(run_in_threadpool(run_job, job, temp_path))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)

    return IngestionJobStatus(**job.snapshot())


@router.get("/jobs", response_model=List[IngestionJobStatus])
async def list_ingestion_jobs():
    """List recent jobs newest first, so the UI can re-attach to a running one."""
    return [IngestionJobStatus(**job.snapshot()) for job in get_job_registry().recent()]


@router.get("/jobs/{job_id}", response_model=IngestionJobStatus)
async def get_ingestion_job(job_id: str):
    """Get one job's status, progress, and final statistics."""
    job = get_job_registry().get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Unknown ingestion job '{job_id}'")
    return IngestionJobStatus(**job.snapshot())


@router.post("/ingest-string", response_model=IngestionStats)
async def ingest_from_string(
    data: str = Form(...),
    format: str = Form("json"),
    data_type: str = Form("finding")
):
    """Ingest data from a string; format is 'json', 'csv', or 'jsonl'."""
    if data_type not in ['finding', 'case']:
        raise HTTPException(status_code=400, detail="data_type must be 'finding' or 'case'")

    if format not in ['json', 'csv', 'jsonl']:
        raise HTTPException(status_code=400, detail="format must be 'json', 'csv', or 'jsonl'")

    ingestion_service = IngestionService()
    stats = ingestion_service.ingest_from_string(data, format=format, data_type=data_type)
    success, message = summarize_stats(stats)
    return IngestionStats(**stats, success=success, message=message)


@router.get("/formats")
async def get_supported_formats():
    """
    Get information about supported file formats.
    
    Returns:
        Dictionary of supported formats and their specifications
    """
    return {
        "formats": {
            "json": {
                "name": "JSON",
                "extensions": [".json"],
                "description": "Standard JSON format",
                "example_structure": {
                    "findings": [
                        {
                            "finding_id": "f-20260114-abc123",
                            "embedding": [0.1, 0.2, "...768 values"],
                            "mitre_predictions": {"T1071.001": 0.85},
                            "anomaly_score": 0.75,
                            "timestamp": "2026-01-14T10:00:00Z",
                            "data_source": "flow",
                            "severity": "high",
                            "status": "new"
                        }
                    ],
                    "cases": [
                        {
                            "case_id": "case-2026-01-14-xyz789",
                            "title": "Investigation",
                            "finding_ids": ["f-20260114-abc123"],
                            "status": "open",
                            "priority": "high"
                        }
                    ]
                }
            },
            "jsonl": {
                "name": "JSON Lines",
                "extensions": [".jsonl", ".ndjson"],
                "description": "One JSON object per line",
                "example": '{"finding_id": "f-123", "anomaly_score": 0.8}\n{"finding_id": "f-456", "anomaly_score": 0.9}'
            },
            "csv": {
                "name": "CSV",
                "extensions": [".csv"],
                "description": "Comma-separated values",
                "finding_columns": [
                    "finding_id",
                    "anomaly_score",
                    "timestamp",
                    "data_source",
                    "severity",
                    "status",
                    "cluster_id",
                    "embedding (comma-separated floats)",
                    "mitre_predictions (JSON string or technique:score pairs)",
                    "entity_context (JSON string)"
                ],
                "case_columns": [
                    "case_id",
                    "title",
                    "description",
                    "finding_ids (comma-separated)",
                    "status",
                    "priority",
                    "assignee",
                    "tags (comma-separated)"
                ]
            },
            "parquet": {
                "name": "Parquet",
                "extensions": [".parquet"],
                "description": "Supported parquet files. Columns are auto-mapped to findings when the schema is recognized.",
                "expected_columns": [
                    "sequence_id (-> finding_id)",
                    "embedding (variable-dimension float vector)",
                    "mitre_pred (integer class label -> mitre_predictions)",
                    "focal_ip (-> entity_context.src_ip)",
                    "engaged_ip (-> entity_context.dst_ip)",
                    "event_start_time (epoch ms -> timestamp)",
                    "event_end_time (epoch ms -> entity_context)",
                    "malicious (bool -> severity + anomaly_score)",
                    "row_count, incident_pred, attack_id, created_at (metadata)"
                ]
            }
        },
        "data_types": {
            "finding": "Security findings with embeddings and MITRE predictions",
            "case": "Investigation cases grouping related findings"
        },
        "notes": [
            "finding_id and case_id are auto-generated if not provided",
            "Duplicate IDs are automatically skipped",
            "All data is stored in PostgreSQL when available, falls back to JSON files",
            "Embeddings default to 768-dimensional zero vector if not provided",
            "Timestamps are parsed from various formats or default to current time"
        ]
    }


@router.get("/csv-template/{data_type}")
async def get_csv_template(data_type: str):
    """
    Get a CSV template for the specified data type.
    
    Args:
        data_type: Type of data ('finding' or 'case')
    
    Returns:
        CSV template string
    """
    if data_type == 'finding':
        return {
            "template": "finding_id,anomaly_score,timestamp,data_source,severity,status,cluster_id,mitre_predictions,entity_context\n" +
                       "f-20260114-example,0.85,2026-01-14T10:00:00Z,flow,high,new,c-beaconing-001,\"{\"\"T1071.001\"\": 0.85}\",\"{\"\"src_ip\"\": \"\"192.168.1.100\"\"}\"\n",
            "description": "CSV template for findings. Note: embedding column omitted for brevity (768 values)"
        }
    elif data_type == 'case':
        return {
            "template": "case_id,title,description,finding_ids,status,priority,assignee,tags\n" +
                       "case-2026-01-14-example,Suspicious Activity,Investigation of unusual network traffic,\"f-123,f-456\",open,high,analyst@example.com,\"lateral-movement,investigation\"\n",
            "description": "CSV template for cases"
        }
    else:
        raise HTTPException(status_code=400, detail="data_type must be 'finding' or 'case'")


@router.post("/sync-s3")
def sync_from_s3():
    """
    Sync findings and cases from AWS S3.
    
    Requires S3 to be configured in settings.
    Fetches data from the configured S3 bucket and syncs to local storage.
    
    Returns:
        Sync status and statistics
    """
    data_service = DatabaseDataService()
    
    # Check if S3 is configured
    if not data_service.is_s3_configured():
        raise HTTPException(
            status_code=400,
            detail="S3 is not configured. Please configure S3 in Settings first."
        )
    
    # Perform sync
    logger.info("Starting S3 sync via API endpoint")
    success, message, stats = data_service.sync_from_s3()
    
    return {
        "success": success,
        "message": message,
        "findings_synced": stats.get("findings_synced", 0),
        "cases_synced": stats.get("cases_synced", 0),
        "errors": stats.get("errors", [])
    }


@router.post("/sync-s3-folder", response_model=IngestionStats)
@router.post("/sync-s3-parquet", response_model=IngestionStats, include_in_schema=False)
def sync_s3_folder(prefix: Optional[str] = Query(None)):
    """
    Discover and ingest all supported files from an S3 prefix.

    Scans the given prefix (or the configured parquet_prefix) and auto-detects
    file types by extension:
      .parquet -> supported parquet ingestion
      .csv     -> CSV finding ingestion
      .json    -> JSON finding/case ingestion
      .jsonl / .ndjson -> JSON-Lines finding ingestion

    Unsupported file types are skipped.

    Args:
        prefix: S3 key prefix (folder) to scan. Overrides the configured
                parquet_prefix if provided.

    Returns:
        Ingestion statistics
    """
    data_service = DatabaseDataService()

    if not data_service.is_s3_configured():
        raise HTTPException(
            status_code=400,
            detail="S3 is not configured. Please configure S3 in Settings first."
        )

    if prefix is None:
        from core.storage.config_service import get_config_service
        config_service = get_config_service()
        s3_config = config_service.get_integration_config('s3') or {}
        prefix = s3_config.get('parquet_prefix', '')

    logger.info(f"Starting S3 folder sync with prefix='{prefix}'")

    ingestion_service = IngestionService()
    stats = ingestion_service.ingest_s3_folder(
        s3_service=data_service._s3_service,
        prefix=prefix
    )

    success, summary = summarize_stats(stats)

    prefix = []
    fp = stats.get('files_processed', 0)
    fs = stats.get('files_skipped', 0)
    if fp > 0:
        prefix.append(f"Processed {fp} file(s)")
    if fs > 0:
        prefix.append(f"Skipped {fs} unsupported file(s)")

    if not prefix and summary == "No data imported":
        message = "No supported files found or no data imported"
    else:
        message = ". ".join(prefix + [summary])

    return IngestionStats(
        findings_total=stats.get('findings_total', 0),
        findings_imported=stats.get('findings_imported', 0),
        findings_skipped=stats.get('findings_skipped', 0),
        findings_errors=stats.get('findings_errors', 0),
        cases_total=stats.get('cases_total', 0),
        cases_imported=stats.get('cases_imported', 0),
        cases_skipped=stats.get('cases_skipped', 0),
        cases_errors=stats.get('cases_errors', 0),
        success=success,
        message=message
    )


def _get_s3_service():
    """
    Build an S3Service directly from saved config.

    Unlike DatabaseDataService.is_s3_configured() this skips the head_bucket
    test so it works with IAM policies that only grant list/get permissions.
    """
    from core.storage.config_service import get_config_service
    from core.secrets_manager import get_secret
    from core.storage.s3_service import S3Service

    config_service = get_config_service()
    s3_integration = config_service.get_integration_config('s3')
    if not s3_integration:
        return None

    cfg = s3_integration.get('config') if isinstance(s3_integration.get('config'), dict) else s3_integration

    raw_bucket = cfg.get('bucket_name', '')
    if raw_bucket.startswith('s3://'):
        bucket_name = raw_bucket[5:].split('/', 1)[0]
    else:
        bucket_name = raw_bucket

    if not bucket_name:
        return None

    auth_method = cfg.get('auth_method', 'credentials')
    aws_profile = cfg.get('aws_profile', '')

    if auth_method == 'profile' and aws_profile:
        return S3Service(
            bucket_name=bucket_name,
            region_name=cfg.get('region', 'us-east-1'),
            aws_profile=aws_profile,
        )

    access_key = get_secret("AWS_ACCESS_KEY_ID")
    secret_key = get_secret("AWS_SECRET_ACCESS_KEY")
    session_token = get_secret("AWS_SESSION_TOKEN")

    return S3Service(
        bucket_name=bucket_name,
        region_name=cfg.get('region', 'us-east-1'),
        aws_access_key_id=access_key or None,
        aws_secret_access_key=secret_key or None,
        aws_session_token=session_token or None,
    )


@router.get("/s3-files")
def list_s3_files(prefix: Optional[str] = Query("")):
    """
    List files in the configured S3 bucket with metadata.

    Args:
        prefix: S3 key prefix to filter by (default: "")

    Returns:
        List of files with key, size, and last_modified
    """
    s3 = _get_s3_service()
    if s3 is None:
        raise HTTPException(
            status_code=400,
            detail="S3 is not configured. Please configure S3 in Settings first."
        )

    files = s3.list_files_detailed(prefix=prefix or "")
    return {"files": files, "count": len(files), "prefix": prefix or ""}


class S3FileIngestRequest(BaseModel):
    key: str


@router.post("/s3-file", response_model=IngestionStats)
def ingest_s3_file(request: S3FileIngestRequest):
    """
    Download and ingest a single file from S3 by key.

    Auto-detects the file format from the extension.

    Args:
        request: S3 object key to ingest

    Returns:
        Ingestion statistics
    """
    key = request.key
    ext = Path(key).suffix.lower()
    fmt = EXTENSION_FORMATS.get(ext)
    if fmt is None:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported file extension '{ext}'. Supported: {', '.join(EXTENSION_FORMATS.keys())}"
        )

    s3 = _get_s3_service()
    if s3 is None:
        raise HTTPException(
            status_code=400,
            detail="S3 is not configured. Please configure S3 in Settings first."
        )

    content = s3.get_file(key)
    if content is None:
        raise HTTPException(status_code=404, detail=f"Failed to download '{key}' from S3")

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = Path(tmp.name)

        ingestion_service = IngestionService()
        stats = ingestion_service._ingest_file_by_format(tmp_path, fmt)
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()

    success, message = summarize_stats(stats)

    return IngestionStats(
        findings_total=stats.get('findings_total', 0),
        findings_imported=stats.get('findings_imported', 0),
        findings_skipped=stats.get('findings_skipped', 0),
        findings_errors=stats.get('findings_errors', 0),
        cases_total=stats.get('cases_total', 0),
        cases_imported=stats.get('cases_imported', 0),
        cases_skipped=stats.get('cases_skipped', 0),
        cases_errors=stats.get('cases_errors', 0),
        success=success,
        message=message,
    )


@router.get("/s3-status")
def get_s3_status():
    """
    Get S3 connection status.
    
    Returns:
        S3 configuration and connection status
    """
    try:
        data_service = DatabaseDataService()
        
        # Check if S3 is configured
        is_configured = data_service.is_s3_configured()
        
        if is_configured:
            # Test connection
            success, message = data_service._s3_service.test_connection()
            return {
                "configured": True,
                "connected": success,
                "message": message
            }
        else:
            return {
                "configured": False,
                "connected": False,
                "message": "S3 is not configured. Configure in Settings to enable S3 sync."
            }
        
    except Exception as e:
        logger.error(f"Error checking S3 status: {e}")
        return {
            "configured": False,
            "connected": False,
            "error": str(e)
        }
