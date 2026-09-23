import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse


DATA_DIR = Path(__file__).resolve().parent.parent / "data"
CATALOG_PATH = DATA_DIR / "wis-assets.json"
META_PATH = DATA_DIR / "catalog-meta.json"
PUBLIC_PREFIX = "https://oss.fandow.com/marketing-video-dashboard/"


def _read_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return fallback


def _datetime(value: object) -> datetime:
    text = str(value or "").strip().replace("Z", "+00:00")
    if not text:
        return datetime.utcnow()
    try:
        parsed = datetime.fromisoformat(text)
        return parsed.replace(tzinfo=None)
    except ValueError:
        return datetime.utcnow()


def _object_key(url: str) -> str:
    path = unquote(urlparse(url).path).lstrip("/")
    bucket_prefix = "marketing-video-dashboard/"
    return path[len(bucket_prefix):] if path.startswith(bucket_prefix) else path


def infer_category(title: str, default: str = "其他 WIS 素材") -> str:
    value = title.lower()
    if "黑晶" in value:
        return "黑晶面膜"
    if "眼膜" in value:
        return "晶润眼膜"
    if "次抛" in value or "深海" in value:
        return "深海次抛"
    if "喷雾" in value:
        return "肌活蛋白喷雾"
    if "燕窝" in value:
        return "燕窝面膜"
    if "泥膜" in value:
        return "其他 WIS 素材"
    if "面膜" in value:
        return "隐形水润面膜"
    return default


def infer_content_type(title: str) -> str:
    rules = (
        ("上脸展示", ("上脸", "敷脸", "贴脸")),
        ("数字人", ("数字人", "虚拟人")),
        ("图文", ("图文", "图片轮播", "轮播图")),
        ("产品展示", ("产展", "产品展示", "开箱")),
        ("痛点", ("痛点",)),
        ("科普", ("科普", "成分")),
        ("测评", ("测评", "对比")),
        ("口播", ("口播",)),
        ("剧情", ("剧情",)),
    )
    for label, words in rules:
        if any(word in title for word in words):
            return label
    return "其他"


def infer_library_metadata(*values: object) -> tuple[str, str]:
    """Conservatively separate source footage from explicitly named remixes."""
    text = " ".join(
        str(item)
        for value in values
        for item in (value if isinstance(value, (list, tuple, set)) else [value])
        if item
    ).lower()
    remix_markers = ("混剪", "二创", "成片", "剪辑成稿", "剪辑终版")
    is_remix = any(marker in text for marker in remix_markers)
    if is_remix:
        if "ai" in text or "智能" in text:
            return "remix", "AI混剪成片"
        if "明星" in text:
            return "remix", "明星素材混剪"
        if "达人" in text or "kol" in text or "koc" in text:
            return "remix", "达人素材混剪"
        return "remix", "其他混剪成片"

    if "品牌创意" in text or "创意广告" in text:
        return "source", "品牌创意广告"
    if "品牌ip" in text or "ip广告" in text:
        return "source", "品牌IP广告"
    if "明星" in text or "信息流原片" in text:
        return "source", "明星信息流原片"
    if "实拍" in text or "自产" in text:
        return "source", "实拍自产素材"
    if "产品镜" in text or "产品展示" in text or "产展" in text or "质地" in text:
        return "source", "产品镜"
    if "ai" in text or "原创" in text:
        return "source", "AI原创素材"
    if "koc" in text:
        return "source", "达人/KOC原片"
    if "达人" in text or "kol" in text:
        return "source", "达人/KOL原片"
    return "source", "其他视频素材"


def _tags(value: object, source: str) -> list[str]:
    tags: list[str] = []
    if isinstance(value, list):
        tags = [str(item).strip().lstrip("#") for item in value]
    elif isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            decoded = json.loads(raw)
            if isinstance(decoded, list):
                tags = [str(item).strip().lstrip("#") for item in decoded]
            else:
                tags = re.split(r"[,，\s]+", raw)
        except ValueError:
            tags = re.split(r"[,，\s]+", raw)
    source_tag = {"qianchuan": "千川", "wechat_channel": "视频号", "chanmama": "蝉妈妈"}.get(source)
    if source_tag:
        tags.append(source_tag)
    return list(dict.fromkeys(tag for tag in tags if tag))[:12]


class CatalogService:
    @property
    def configured(self) -> bool:
        return CATALOG_PATH.is_file()

    @property
    def meta(self) -> dict:
        return _read_json(META_PATH, {})

    @property
    def updated_at(self) -> str | None:
        return self.meta.get("source_updated_at") or self.meta.get("exported_at")

    def list_assets(self) -> list[dict]:
        rows = _read_json(CATALOG_PATH, [])
        assets: list[dict] = []
        for row in rows:
            url = str(row.get("video_url") or "").strip()
            if not url.startswith(PUBLIC_PREFIX + "yxb/"):
                continue
            title = str(row.get("title") or "").strip() or f"WIS 素材 {row.get('id', '')}".strip()
            source = str(row.get("source") or "").strip()
            library_type = "remix"
            _, asset_subtype = infer_library_metadata("混剪成片", title, row.get("hashtags"), source)
            assets.append(
                {
                    "object_key": _object_key(url),
                    "filename": title,
                    "media_type": "video",
                    "size": 0,
                    "etag": str(row.get("id") or ""),
                    "modified_at": _datetime(row.get("publish_time") or row.get("collected_at") or row.get("updated_at")),
                    "category": infer_category(title),
                    "content_type": infer_content_type(title),
                    "library_type": library_type,
                    "asset_subtype": asset_subtype,
                    "status": "待整理",
                    "tags": _tags(row.get("hashtags"), source),
                    "cover_url": str(row.get("cover_url") or "").strip(),
                    "source": source,
                    "account_name": str(row.get("account_name") or "").strip(),
                }
            )
        return assets

    def url_for(self, key: str) -> str:
        return PUBLIC_PREFIX + "/".join(part for part in key.split("/") if part)


catalog_service = CatalogService()
