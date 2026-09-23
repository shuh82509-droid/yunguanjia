import mimetypes
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable
from urllib.parse import quote, unquote
from uuid import uuid4

import boto3
import requests
from botocore.client import Config
from botocore.exceptions import ClientError

from .config import settings


VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".webm", ".avi", ".mkv"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
ALLOWED_EXTENSIONS = VIDEO_EXTENSIONS | IMAGE_EXTENSIONS
INTERNAL_UPLOAD_PREFIX = re.compile(
    r"^\d{8}T\d{6}Z-[0-9a-fA-F]{8,40}-(?P<filename>.+)$"
)


def media_type_for(key: str) -> str | None:
    suffix = Path(key).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    return None


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", value).strip(" .-")
    return (cleaned or fallback)[:80]


def _safe_filename(value: str) -> str:
    name = normalize_upload_filename(value)
    name = _safe_segment(Path(name).name, "asset")
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError("仅支持 MP4、MOV、M4V、WEBM、JPG、JPEG、PNG、WEBP、GIF 素材")
    return name[-180:]


def normalize_upload_filename(value: str) -> str:
    name = Path(value).name
    if re.search(r"%[0-9a-fA-F]{2}", name):
        decoded = unquote(name)
        if decoded and "�" not in decoded:
            name = decoded
    name = unicodedata.normalize("NFC", name)
    name = "".join(char for char in name if char >= " " and char != "\x7f").strip(" .")
    if any(sequence in name for sequence in ("绱犳潗", "瑙嗛", "鍓槧", "瀵煎嚭", "涓婁紶", "娴嬭瘯", "鏂囦欢", "鎴愮墖")):
        try:
            repaired = name.encode("gbk").decode("utf-8")
            if repaired:
                name = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    elif any("\x80" <= char <= "\xff" for char in name):
        try:
            repaired = name.encode("latin1").decode("utf-8")
            if repaired:
                name = repaired
        except (UnicodeEncodeError, UnicodeDecodeError):
            pass
    return name or "asset"


def display_filename_from_object_key(value: str) -> str:
    """Return the original display name embedded in a managed OSS object key.

    Uploaded objects deliberately include a timestamp and random token so two
    colleagues can upload files with the same name.  That storage-only prefix
    must never be shown as the material name.
    """
    name = normalize_upload_filename(Path(value).name)
    matched = INTERNAL_UPLOAD_PREFIX.fullmatch(name)
    if not matched:
        return name
    return normalize_upload_filename(matched.group("filename"))


class OssService:
    def __init__(self):
        self.client = None
        if settings.oss_configured:
            self.client = boto3.client(
                "s3",
                endpoint_url=settings.endpoint,
                aws_access_key_id=settings.access_key,
                aws_secret_access_key=settings.secret_key,
                config=Config(signature_version="s3v4"),
                region_name="auto",
            )

    @property
    def configured(self) -> bool:
        return self.client is not None

    def list_assets(self, progress: Callable[[int], None] | None = None) -> list[dict]:
        if not self.client:
            return []
        paginator = self.client.get_paginator("list_objects_v2")
        assets: list[dict] = []
        for page in paginator.paginate(Bucket=settings.bucket, Prefix=settings.prefix):
            for item in page.get("Contents", []):
                key = str(item["Key"])
                if key.startswith(settings.prefix.rstrip("/") + "/references/") or key.startswith(
                    settings.prefix.rstrip("/") + "/covers/"
                ):
                    continue
                media_type = media_type_for(key)
                if not media_type:
                    continue
                assets.append(
                    {
                        "object_key": key,
                        "filename": Path(key).name,
                        "media_type": media_type,
                        "size": int(item.get("Size", 0) or 0),
                        "etag": str(item.get("ETag", "")).strip('"'),
                        "modified_at": item.get("LastModified", datetime.now(timezone.utc)).replace(tzinfo=None),
                    }
                )
            if progress:
                progress(len(assets))
        return assets

    def create_upload(
        self,
        filename: str,
        content_type: str,
        uploader: str,
        asset_scope: str = "marketing_video",
        category: str = "待分类",
    ) -> dict:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        safe_name = _safe_filename(filename)
        key = self.create_object_key(safe_name, uploader, asset_scope=asset_scope, category=category)
        mime = content_type if content_type.startswith(("video/", "image/")) else mimetypes.guess_type(safe_name)[0]
        mime = mime or "application/octet-stream"
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.bucket, "Key": key, "ContentType": mime},
            ExpiresIn=3600,
        )
        return {
            "object_key": key,
            "upload_url": upload_url,
            "public_url": self.url_for(key),
            "headers": {"Content-Type": mime},
            "expires_in": 3600,
        }

    def presign_upload(self, key: str, content_type: str) -> dict:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        normalized = key.strip().lstrip("/")
        managed_prefix = settings.prefix.rstrip("/") + "/uploads/"
        if not normalized.startswith(managed_prefix):
            raise ValueError("上传路径不在受管 uploads 目录内")
        mime = content_type if content_type.startswith("video/") else "video/mp4"
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={"Bucket": settings.bucket, "Key": normalized, "ContentType": mime},
            ExpiresIn=3600,
        )
        return {
            "object_key": normalized,
            "upload_url": upload_url,
            "public_url": self.url_for(normalized),
            "headers": {"Content-Type": mime},
            "expires_in": 3600,
        }

    def create_object_key(
        self,
        filename: str,
        uploader: str,
        *,
        asset_scope: str = "marketing_video",
        category: str = "待分类",
    ) -> str:
        safe_name = _safe_filename(filename)
        safe_uploader = _safe_segment(uploader, "OA用户")
        prefix = settings.prefix.rstrip("/")
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        if asset_scope == "product_image":
            safe_category = _safe_segment(category, "待分类")
            return f"{prefix}/product-images/{safe_category}/{safe_uploader}/{stamp}-{uuid4().hex[:10]}-{safe_name}"
        if asset_scope == "reference_video":
            return f"{prefix}/references/{safe_uploader}/{stamp}-{uuid4().hex[:10]}-{safe_name}"
        return f"{prefix}/uploads/{safe_uploader}/{stamp}-{uuid4().hex[:10]}-{safe_name}"

    def create_multipart_upload(self, key: str, content_type: str) -> str:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        mime = content_type if content_type.startswith(("video/", "image/")) else mimetypes.guess_type(key)[0]
        result = self.client.create_multipart_upload(
            Bucket=settings.bucket,
            Key=key,
            ContentType=mime or "application/octet-stream",
        )
        upload_id = str(result.get("UploadId") or "")
        if not upload_id:
            raise RuntimeError("OSS 未返回分片上传 ID")
        return upload_id

    def presign_upload_part(self, key: str, upload_id: str, part_number: int, expires_in: int = 3600) -> str:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        return self.client.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": settings.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": int(part_number),
            },
            ExpiresIn=max(300, int(expires_in)),
        )

    def upload_multipart_part(
        self,
        key: str,
        upload_id: str,
        part_number: int,
        body,
        content_length: int,
        content_md5: str,
    ) -> str:
        """Relay one verified part through the application server.

        Browser-to-OSS remains the fast path. This method is the durable
        fallback for colleagues whose office network or browser blocks the
        presigned OSS host before the first byte is accepted.
        """
        if not self.client:
            raise RuntimeError("OSS 未配置")
        # Alibaba OSS' S3 compatibility layer rejects botocore's streamed
        # x-amz-content-sha256 signature for UploadPart. A presigned HTTPS PUT
        # is the same proven path used by the browser, while Content-MD5 keeps
        # the server relay end-to-end verified.
        response = requests.put(
            self.presign_upload_part(key, upload_id, part_number),
            data=body,
            headers={
                "Content-Length": str(int(content_length)),
                "Content-MD5": content_md5,
            },
            timeout=(15, 300),
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"OSS 分片上传失败（{response.status_code}）")
        etag = str(response.headers.get("ETag") or "").strip()
        if not etag:
            raise RuntimeError("OSS 未返回分片校验值")
        return etag

    def list_multipart_parts(self, key: str, upload_id: str) -> list[dict]:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        parts: list[dict] = []
        marker = 0
        while True:
            result = self.client.list_parts(
                Bucket=settings.bucket,
                Key=key,
                UploadId=upload_id,
                PartNumberMarker=marker,
                MaxParts=1000,
            )
            for row in result.get("Parts") or []:
                parts.append(
                    {
                        "part_number": int(row.get("PartNumber") or 0),
                        "etag": str(row.get("ETag") or ""),
                        "size": int(row.get("Size") or 0),
                    }
                )
            if not result.get("IsTruncated"):
                break
            marker = int(result.get("NextPartNumberMarker") or marker)
        return parts

    def complete_multipart_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        if not self.client:
            raise RuntimeError("OSS 未配置")
        self.client.complete_multipart_upload(
            Bucket=settings.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": [
                    {"PartNumber": int(item["part_number"]), "ETag": str(item["etag"])}
                    for item in sorted(parts, key=lambda row: int(row["part_number"]))
                ]
            },
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        if not self.client or not upload_id:
            return
        self.client.abort_multipart_upload(
            Bucket=settings.bucket,
            Key=key,
            UploadId=upload_id,
        )

    def head_asset(self, key: str) -> dict | None:
        if not self.client:
            return None
        try:
            item = self.client.head_object(Bucket=settings.bucket, Key=key)
        except ClientError as error:
            status = error.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if status == 404:
                return None
            raise
        media_type = media_type_for(key)
        if not media_type:
            return None
        return {
            "object_key": key,
            "filename": Path(key).name,
            "media_type": media_type,
            "size": int(item.get("ContentLength", 0) or 0),
            "etag": str(item.get("ETag", "")).strip('"'),
            "modified_at": item.get("LastModified", datetime.now(timezone.utc)).replace(tzinfo=None),
        }

    def delete_asset(self, key: str) -> None:
        """Permanently delete one managed media object from the configured OSS prefix."""
        if not self.client:
            raise RuntimeError("OSS 未配置")
        normalized = key.strip().lstrip("/")
        managed_prefix = settings.prefix.rstrip("/") + "/"
        if not normalized.startswith(managed_prefix) or any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise ValueError("素材路径不在受管 OSS 目录内")
        self.client.delete_object(Bucket=settings.bucket, Key=normalized)

    def upload_private_share(self, local_path: str, key: str, content_type: str) -> None:
        """Explicit owner-confirmed copy only; private uploads never call this."""
        if not self.client:
            raise RuntimeError("OSS 未配置，私人文件仍保持私人状态")
        prefix = settings.prefix.rstrip('/') + '/uploads/private-shared/'
        if not key.startswith(prefix) or any(part in {'', '.', '..'} for part in key.split('/')):
            raise ValueError('共享副本路径无效')
        url = self.client.generate_presigned_url('put_object', Params={
            'Bucket': settings.bucket, 'Key': key, 'ContentType': content_type,
        }, ExpiresIn=600)
        try:
            with open(local_path, 'rb') as stream:
                response = requests.put(url, data=stream, headers={'Content-Type': content_type}, timeout=(15, 180))
            if not response.ok:
                raise RuntimeError(f'共享副本上传失败（HTTP {response.status_code}），私人原文件未改变')
        except requests.RequestException as error:
            raise RuntimeError('共享副本上传未确认，请重试核对同一副本，私人原文件未改变') from error

    def upload_file(self, local_path: str, key: str, content_type: str) -> None:
        """Upload a generated derivative without exposing credentials or proxying browsers."""
        if not self.client:
            raise RuntimeError("OSS 未配置")
        normalized = key.strip().lstrip("/")
        managed_prefix = settings.prefix.rstrip("/") + "/covers/"
        if not normalized.startswith(managed_prefix):
            raise ValueError("封面路径不在受管 covers 目录内")
        # The company S3-compatible gateway rejects SDK-signed PutObject bodies
        # with XAmzContentSHA256Mismatch. The existing browser upload path uses
        # a presigned PUT successfully, so generated covers follow the same
        # contract without exposing the signed URL outside this process.
        cache_control = "public, max-age=31536000, immutable"
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.bucket,
                "Key": normalized,
                "ContentType": content_type,
                "CacheControl": cache_control,
            },
            ExpiresIn=600,
        )
        try:
            response = requests.put(
                upload_url,
                data=Path(local_path).read_bytes(),
                headers={"Content-Type": content_type, "Cache-Control": cache_control},
                timeout=60,
            )
        except requests.RequestException as error:
            raise RuntimeError("封面上传网络异常") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"封面上传失败（HTTP {response.status_code}）")

    def upload_reference_preview(self, local_path: str, key: str) -> None:
        """Upload a browser-compatible derivative without replacing the original reference video."""
        if not self.client:
            raise RuntimeError("OSS 未配置")
        normalized = key.strip().lstrip("/")
        managed_prefix = settings.prefix.rstrip("/") + "/reference-previews/"
        if not normalized.startswith(managed_prefix):
            raise ValueError("兼容预览路径不在受管 reference-previews 目录内")
        cache_control = "public, max-age=31536000, immutable"
        upload_url = self.client.generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.bucket,
                "Key": normalized,
                "ContentType": "video/mp4",
                "CacheControl": cache_control,
            },
            ExpiresIn=1800,
        )
        try:
            with Path(local_path).open("rb") as source:
                response = requests.put(
                    upload_url,
                    data=source,
                    headers={"Content-Type": "video/mp4", "Cache-Control": cache_control},
                    timeout=900,
                )
        except requests.RequestException as error:
            raise RuntimeError("兼容预览上传网络异常") from error
        if response.status_code < 200 or response.status_code >= 300:
            raise RuntimeError(f"兼容预览上传失败（HTTP {response.status_code}）")

    def url_for(self, key: str, download: bool = False, expires_in: int = 3600) -> str:
        if settings.public_base_url and not download:
            return f"{settings.public_base_url}/{quote(key, safe='/')}"
        if not self.client:
            return ""
        params = {"Bucket": settings.bucket, "Key": key}
        if download:
            params["ResponseContentDisposition"] = f'attachment; filename="{Path(key).name}"'
        return self.client.generate_presigned_url(
            "get_object",
            Params=params,
            ExpiresIn=max(60, int(expires_in)),
        )


oss_service = OssService()
