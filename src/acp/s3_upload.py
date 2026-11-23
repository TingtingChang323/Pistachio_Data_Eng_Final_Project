"""Archive original source files without overwriting existing S3 objects."""

from __future__ import annotations

import hashlib
from contextlib import closing
from typing import NamedTuple

import boto3
from botocore.exceptions import ClientError


class UploadResult(NamedTuple):
    """The location and optional version of a verified S3 object."""

    uri: str
    version_id: str | None
    created: bool


def upload_raw_object(source_bytes, *, bucket, region, key, content_type):
    """Create an S3 object or verify an existing copy by SHA-256."""
    uri = f"s3://{bucket}/{key}"
    with closing(boto3.client("s3", region_name=region)) as s3:
        created = True
        try:
            response = s3.put_object(
                Bucket=bucket,
                Key=key,
                Body=source_bytes,
                ContentType=content_type,
                IfNoneMatch="*",
            )
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            response = s3.get_object(Bucket=bucket, Key=key)
            with response["Body"] as body:
                existing_hash = hashlib.sha256(body.read()).digest()
            if existing_hash != hashlib.sha256(source_bytes).digest():
                raise ValueError(f"S3 already contains different content at {uri}.") from None
            created = False
    return UploadResult(uri, response.get("VersionId"), created)


def upload_pdf(pdf_bytes, filename, report_month):
    """Archive an ACP PDF and format the result for the ACP command line."""
    if __package__:
        from .config import S3_BUCKET, S3_REGION
    else:
        from config import S3_BUCKET, S3_REGION

    result = upload_raw_object(
        pdf_bytes,
        bucket=S3_BUCKET,
        region=S3_REGION,
        key=f"raw/acp/{report_month}/{filename}",
        content_type="application/pdf",
    )
    status = "Uploaded" if result.created else "Skipped (identical PDF)"
    version = f" (VersionId: {result.version_id})" if result.version_id else ""
    return f"{status}: {result.uri}{version}"
