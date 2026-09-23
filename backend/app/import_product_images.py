"""Idempotently import a categorized local folder into the product-image library."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import requests
from sqlalchemy import select

from .database import SessionLocal
from .main import ensure_asset_schema, infer_product_image_type
from .models import Asset
from .oss_service import IMAGE_EXTENSIONS, _safe_filename, _safe_segment, oss_service
from .config import settings


def file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def import_product_images(root: Path, uploader_number: str, uploader_name: str) -> dict:
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"导入目录不存在：{root}")
    if not oss_service.configured:
        raise RuntimeError("OSS 未配置")

    ensure_asset_schema()
    created = 0
    existing = 0
    uploaded = 0
    errors: list[dict[str, str]] = []
    prefix = settings.prefix.rstrip("/")
    safe_uploader = _safe_segment(uploader_name or uploader_number, "管理员")

    with SessionLocal() as db:
        for category_dir in sorted(path for path in root.iterdir() if path.is_dir()):
            category = _safe_segment(category_dir.name, "待分类")
            for path in sorted(category_dir.rglob("*")):
                if not path.is_file() or path.suffix.lower() not in IMAGE_EXTENSIONS:
                    continue
                try:
                    safe_name = _safe_filename(path.name)
                    object_key = (
                        f"{prefix}/product-images/{category}/{safe_uploader}/"
                        f"{file_digest(path)}-{safe_name}"
                    )
                    asset = db.scalar(select(Asset).where(Asset.object_key == object_key))
                    remote = oss_service.head_asset(object_key)
                    if remote is None:
                        mime = "image/png" if path.suffix.lower() == ".png" else "application/octet-stream"
                        upload_url = oss_service.client.generate_presigned_url(
                            "put_object",
                            Params={"Bucket": settings.bucket, "Key": object_key, "ContentType": mime},
                            ExpiresIn=3600,
                        )
                        with path.open("rb") as source:
                            response = requests.put(
                                upload_url,
                                data=source,
                                headers={"Content-Type": mime},
                                timeout=(30, 600),
                            )
                        if not 200 <= response.status_code < 300:
                            raise RuntimeError(f"OSS 直传失败（HTTP {response.status_code}）")
                        remote = oss_service.head_asset(object_key)
                        uploaded += 1
                    if remote is None:
                        raise RuntimeError("上传后未能从 OSS 回读")
                    remote["filename"] = path.name
                    if asset is None:
                        asset = Asset(
                            **remote,
                            category=category,
                            content_type=infer_product_image_type(path.name),
                            status="待整理",
                            asset_scope="product_image",
                            library_type="source",
                            asset_subtype="产品图片",
                            tags=["产品图片", category],
                            favorite=False,
                            cover_url="",
                            source="oa_upload",
                            account_name=uploader_name,
                            ingest_source="oa_upload",
                            uploaded_by_number=uploader_number,
                            uploaded_by_name=uploader_name,
                        )
                        db.add(asset)
                        created += 1
                    else:
                        for field, value in remote.items():
                            setattr(asset, field, value)
                        asset.category = category
                        asset.content_type = infer_product_image_type(path.name)
                        asset.status = asset.status or "待整理"
                        asset.asset_scope = "product_image"
                        asset.library_type = "source"
                        asset.asset_subtype = "产品图片"
                        asset.tags = list(dict.fromkeys([*(asset.tags or []), "产品图片", category]))
                        asset.source = "oa_upload"
                        asset.account_name = uploader_name
                        asset.ingest_source = "oa_upload"
                        asset.uploaded_by_number = uploader_number
                        asset.uploaded_by_name = uploader_name
                        existing += 1
                    db.commit()
                except Exception as error:  # keep the rest of the batch importable
                    db.rollback()
                    errors.append({"file": str(path.relative_to(root)), "error": str(error)})

    return {
        "created": created,
        "existing": existing,
        "uploaded": uploaded,
        "failed": len(errors),
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--uploader-number", default="FD-026222")
    parser.add_argument("--uploader-name", default="舒豪")
    args = parser.parse_args()
    print(json.dumps(import_product_images(args.root, args.uploader_number, args.uploader_name), ensure_ascii=False))


if __name__ == "__main__":
    main()
