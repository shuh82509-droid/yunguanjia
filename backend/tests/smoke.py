from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app import main as main_module
from app.database import Base
from app.models import Asset
from app.oss_service import oss_service


def asset(key: str, *, size: int = 10) -> dict:
    return {
        "object_key": key,
        "filename": key.rsplit("/", 1)[-1],
        "media_type": "video",
        "size": size,
        "etag": "etag",
        "modified_at": datetime(2026, 8, 6, 12, 0, 0),
    }


def main() -> None:
    ticket = oss_service.create_upload("test-video.mp4", "video/mp4", "测试用户")
    assert ticket["object_key"].startswith("yxb/uploads/测试用户/")
    assert "/YXB/" not in ticket["object_key"]

    test_engine = create_engine("sqlite://")
    Base.metadata.create_all(test_engine)
    original_catalog = main_module.catalog_service.list_assets
    main_module.catalog_service.list_assets = lambda: []
    try:
        with Session(test_engine) as db:
            existing = Asset(
                **asset("yxb/test/existing.mp4", size=1),
                category="人工分类",
                content_type="产品展示",
                status="待整理",
                tags=["保留标签"],
                favorite=True,
                cover_url="",
                source="oss",
                account_name="",
            )
            db.add(existing)
            db.commit()
            total = main_module._merge_oss_assets(
                db,
                [asset("yxb/test/existing.mp4", size=99), asset("yxb/uploads/测试用户/new.mp4", size=20)],
                datetime(2026, 8, 6, 11, 0, 0),
            )
            assert total == 2
            saved = db.scalar(select(Asset).where(Asset.object_key == "yxb/test/existing.mp4"))
            assert saved and saved.size == 99 and saved.favorite and saved.tags == ["保留标签"] and saved.category == "人工分类"
            uploaded = db.scalar(select(Asset).where(Asset.object_key == "yxb/uploads/测试用户/new.mp4"))
            assert uploaded and uploaded.source == "oa_upload" and uploaded.account_name == "测试用户"
    finally:
        main_module.catalog_service.list_assets = original_catalog

    print("backend smoke checks passed")


if __name__ == "__main__":
    main()
