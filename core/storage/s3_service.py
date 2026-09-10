"""S3 client for listing and downloading objects for ingestion."""

import logging
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError, NoCredentialsError

logger = logging.getLogger(__name__)


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
            self.s3_client = None

    def test_connection(self) -> tuple[bool, str]:
        """
        Test S3 connection.

        Returns:
            Tuple of (success, message)
        """
        if not self.s3_client:
            return False, "S3 client not initialized"

        try:
            # Try to list bucket contents (head_bucket is lighter but list_objects_v2 gives better error)
            self.s3_client.head_bucket(Bucket=self.bucket_name)
            return True, f"Successfully connected to bucket: {self.bucket_name}"
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "404":
                return False, f"Bucket '{self.bucket_name}' not found"
            elif error_code == "403":
                return (
                    False,
                    f"Access denied to bucket '{self.bucket_name}'. Check your credentials and permissions.",
                )
            else:
                return False, f"Error connecting to S3: {error_code} - {str(e)}"
        except NoCredentialsError:
            return False, "AWS credentials not found. Please configure credentials."
        except Exception as e:
            return False, f"Unexpected error: {str(e)}"

    def list_files(self, prefix: str = "") -> List[str]:
        """
        List files in the bucket with optional prefix.

        Args:
            prefix: Prefix to filter files (e.g., "findings/" or "data/")

        Returns:
            List of file keys
        """
        if not self.s3_client:
            return []

        try:
            files = []
            paginator = self.s3_client.get_paginator("list_objects_v2")

            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                if "Contents" in page:
                    for obj in page["Contents"]:
                        files.append(obj["Key"])

            return files
        except Exception as e:
            logger.error(f"Error listing S3 files: {e}")
            return []

    def list_files_detailed(self, prefix: str = "") -> List[Dict[str, Any]]:
        """
        List files in the bucket with metadata (size, last modified).

        Args:
            prefix: Prefix to filter files (e.g., "findings/" or "data/")

        Returns:
            List of dicts with keys: key, size, last_modified
        """
        if not self.s3_client:
            return []

        try:
            files = []
            paginator = self.s3_client.get_paginator("list_objects_v2")

            for page in paginator.paginate(Bucket=self.bucket_name, Prefix=prefix):
                if "Contents" in page:
                    for obj in page["Contents"]:
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
        except Exception as e:
            logger.error(f"Error listing S3 files (detailed): {e}")
            return []

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
