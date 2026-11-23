"""Upload original ACP PDFs without overwriting existing S3 objects."""

import hashlib
from contextlib import closing

import boto3
from botocore.exceptions import ClientError

from config import S3_BUCKET, S3_REGION


def upload_pdf(pdf_bytes, filename, report_month):
    """Create the PDF object, or verify an existing copy by SHA-256."""
    key = f"raw/acp/{report_month}/{filename}"
    uri = f"s3://{S3_BUCKET}/{key}"
    with closing(boto3.client("s3", region_name=S3_REGION)) as s3:
        try:
            response = s3.put_object(
                Bucket=S3_BUCKET,
                Key=key,
                Body=pdf_bytes,
                ContentType="application/pdf",
                IfNoneMatch="*",
            )
            status = "Uploaded"
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
                raise
            response = s3.get_object(Bucket=S3_BUCKET, Key=key)
            with response["Body"] as body:
                existing_hash = hashlib.sha256(body.read()).digest()
            if existing_hash != hashlib.sha256(pdf_bytes).digest():
                raise ValueError(f"S3 already contains a different PDF at {uri}.") from None
            status = "Skipped (identical PDF)"

    version_id = response.get("VersionId")
    version = f" (VersionId: {version_id})" if version_id else ""
    return f"{status}: {uri}{version}"
