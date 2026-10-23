"""Content-addressed file storage: files live at  ab/cd/<sha256><ext>.

The same PDF is only stored once, and any corruption is detectable by
re-hashing the file and comparing it with its name.
"""

import asyncio
import hashlib
import mimetypes
from pathlib import Path

from app.config import get_settings

EXTENSIONS = {"application/pdf": ".pdf", "text/html": ".html", "application/xml": ".xml", "text/xml": ".xml"}


def storage_root() -> Path:
    return get_settings().storage_dir


def extension_for(mimetype: str, original_name: str = "") -> str:
    if mimetype in EXTENSIONS:
        return EXTENSIONS[mimetype]
    suffix = Path(original_name).suffix.lower()
    if suffix and len(suffix) <= 6:
        return suffix
    return mimetypes.guess_extension(mimetype or "") or ".bin"


def key_for(sha256: str, ext: str) -> str:
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}{ext}"


def path_for(key: str) -> Path:
    return storage_root() / key


def store(tmp_path: Path, sha256: str, ext: str) -> str:
    """Move a downloaded temp file into place. Returns its storage key."""
    key = key_for(sha256, ext)
    dest = path_for(key)
    if dest.exists():
        tmp_path.unlink(missing_ok=True)  # identical content already stored
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.replace(dest)
    return key


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


async def verify(key: str, expected_sha256: str) -> bool:
    path = path_for(key)
    if not path.exists():
        return False
    return await asyncio.to_thread(_sha256_of, path) == expected_sha256


def _s3_client():
    import boto3

    s = get_settings()
    return boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url or None,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region or None,
    )


def _upload(key: str, mimetype: str) -> None:
    _s3_client().upload_file(
        str(path_for(key)), get_settings().s3_bucket, f"files/{key}", ExtraArgs={"ContentType": mimetype or "application/octet-stream"}
    )


async def replicate(key: str, mimetype: str) -> None:
    """Copy a stored file to the off-site S3-compatible bucket (e.g. Backblaze B2)."""
    await asyncio.to_thread(_upload, key, mimetype)
