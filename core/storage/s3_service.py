"""S3 client for listing and downloading objects for ingestion."""

import logging
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError, NoCredentialsError

logger = logging.getLogger(__name__)

# Errors list_files_detailed can raise; callers catch these.
S3_LIST_ERRORS = (BotoCoreError, ClientError, RuntimeError)


def describe_s3_error(exc: Exception) -> str:
    """Short message for an S3 listing failure, including the botocore error code."""
    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return f"{err.get('Code', 'Unknown')}: {err.get('Message', str(exc))}"
    if isinstance(exc, NoCredentialsError):
        return "AWS credentials not found. Please configure credentials."
    return str(exc)


class S3Service:
    """Service for accessing data from AWS S3 buckets."""

    def __init__(
        self,
        bucket_name: str,
        region_name: str = "us-east-1",
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        aws_profile: Optional[str] = None,
    ):
        """
        Initialize S3 service.

        Args:
            bucket_name: S3 bucket name
            region_name: AWS region (default: us-east-1)
            aws_access_key_id: AWS access key ID (optional, uses credentials chain if not provided)
            aws_secret_access_key: AWS secret access key (optional, uses credentials chain if not provided)
            aws_session_token: AWS session token for temporary STS credentials (optional)
            aws_profile: AWS CLI profile name (e.g. an SSO profile). Overrides explicit keys.
        """
        self.bucket_name = bucket_name
        self.region_name = region_name
        self._init_error: Optional[str] = None

        try:
            if aws_profile:
                session = boto3.Session(
                    profile_name=aws_profile, region_name=region_name
                )
                self.s3_client = session.client("s3")
                logger.info(f"S3 client initialized using AWS profile '{aws_profile}'")
            elif aws_access_key_id and aws_secret_access_key:
                kwargs: Dict[str, Any] = {
                    "region_name": region_name,
                    "aws_access_key_id": aws_access_key_id,
                    "aws_secret_access_key": aws_secret_access_key,
                }
                if aws_session_token:
                    kwargs["aws_session_token"] = aws_session_token
                self.s3_client = boto3.client("s3", **kwargs)
            else:
                self.s3_client = boto3.client("s3", region_name=region_name)
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            self._init_error = str(e)
            self.s3_client = None

    def _require_client(self):
        if not self.s3_client:
            raise RuntimeError(f"S3 client not initialized: {self._init_error}")
        return self.s3_client

    def list_files_detailed(self, prefix: str = "") -> List[Dict[str, Any]]:
        """
        List files in the bucket with metadata (size, last modified).

        Args:
            prefix: Prefix to filter files (e.g., "findings/" or "data/")

        Returns:
            List of dicts with keys: key, size, last_modified

        Raises:
            S3_LIST_ERRORS: if the listing fails (e.g. AccessDenied, NoSuchBucket)
        """
        paginator = self._require_client().get_paginator("list_objects_v2")
        files = []
        for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
            for obj in page.get("Contents", []):
                if obj["Key"].endswith("/"):
                    continue
                files.append(
                    {
                        "key": obj["Key"],
                        "size": obj["Size"],
                        "last_modified": obj["LastModified"].isoformat(),
                    }
                )
        return files

    def get_file(self, key: str) -> Optional[bytes]:
        """
        Get any file from S3 as bytes.

        Args:
            key: S3 object key

        Returns:
            File content as bytes, or None if error
        """
        if not self.s3_client:
            return None

        try:
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
            return response["Body"].read()
        except Exception as e:
            logger.error(f"Error reading file from S3: {e}")
            return None
