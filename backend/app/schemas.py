from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class AssetPlatformGmvOut(BaseModel):
    platform: str
    label: str
    gmv_yuan: float
    updated_at: str | None = None


class AssetOut(BaseModel):
    id: int
    filename: str
    object_key: str
    media_type: str
    size: int
    modified_at: datetime
    etag: str = ""
    uploaded_by_number: str = ""
    uploaded_by_name: str = ""
    category: str
    content_type: str
    status: str
    asset_scope: str
    library_type: str
    asset_subtype: str
    folder_name: str = ""
    tags: list[str]
    favorite: bool
    favorite_count: int = 0
    preview_url: str
    download_url: str
    cover_url: str
    source: str
    account_name: str
    ingest_source: str
    reference_url: str = ""
    reference_video_key: str = ""
    reference_video_name: str = ""
    reference_video_url: str = ""
    material_description: str = ""
    performance_screenshots: list[dict[str, str]] = Field(default_factory=list)
    can_manage: bool = False
    can_delete: bool = False
    deleted_at: datetime | None = None
    deleted_by_name: str = ""
    purge_after: datetime | None = None
    purged_at: datetime | None = None
    purge_error: str = ""
    can_purge: bool = False
    historical_gmv_yuan: float | None = None
    gmv_target_count: int = 0
    gmv_updated_at: str | None = None
    platform_gmv: list[AssetPlatformGmvOut] = Field(default_factory=list)
    effective: bool = False
    effective_marked_by_name: str = ""
    effective_marked_at: str | None = None
    review_status: str = ""
    review_version: int = 0
    review_note: str = ""
    review_updated_at: str | None = None


class AssetUpdate(BaseModel):
    filename: str | None = Field(default=None, min_length=1, max_length=512)
    category: str | None = Field(default=None, max_length=100)
    content_type: str | None = Field(default=None, max_length=100)
    status: str | None = Field(default=None, max_length=100)
    library_type: str | None = Field(default=None, pattern="^(source|remix)$")
    asset_subtype: str | None = Field(default=None, max_length=100)
    folder_name: str | None = Field(default=None, max_length=160)
    tags: list[str] | None = None
    reference_url: str | None = Field(default=None, max_length=2048)
    reference_video_key: str | None = Field(default=None, max_length=1024)
    reference_video_name: str | None = Field(default=None, max_length=512)
    material_description: str | None = Field(default=None, max_length=10000)
    performance_screenshots: list[dict[str, str]] | None = Field(default=None, max_length=9)


class AssetEffectiveUpdate(BaseModel):
    effective: bool


class AssetPage(BaseModel):
    items: list[AssetOut]
    total: int
    page: int
    page_size: int
    source: str
    source_updated_at: str | None = None


class StatsOut(BaseModel):
    total: int
    oss_total: int
    videos: int
    images: int
    source_materials: int
    remix_outputs: int
    product_images: int
    favorites: int
    trash: int
    new_this_week: int
    storage_bytes: int
    indexed_records: int
    source: str
    source_updated_at: str | None = None
    hit_materials: int = 0
    effective_materials: int = 0


class QianchuanTarget(BaseModel):
    advertiser_id: str = Field(min_length=1, max_length=80)
    advertiser_name: str = Field(default="", max_length=255)
    plan_id: str = Field(default="", max_length=80)
    plan_name: str = Field(default="", max_length=255)
    plan_type: str = Field(default="", max_length=30)


class QianchuanPushCreate(BaseModel):
    asset_id: int | None = Field(default=None, gt=0)
    asset_ids: list[int] = Field(default_factory=list, max_length=10)
    targets: list[QianchuanTarget] = Field(min_length=1, max_length=50)


class QianchuanMetricsSync(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=50)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class AdqTarget(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    account_name: str = Field(default="", max_length=255)
    adgroup_id: str = Field(min_length=1, max_length=80)
    adgroup_name: str = Field(default="", max_length=255)
    source_dynamic_creative_id: str = Field(default="", max_length=80)


class AdqPushCreate(BaseModel):
    asset_id: int | None = Field(default=None, gt=0)
    asset_ids: list[int] = Field(default_factory=list, max_length=10)
    targets: list[AdqTarget] = Field(min_length=1, max_length=50)


class AdqSharedUploadCreate(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=10)


class AdqMetricsSync(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=50)
    start_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class ChannelsCoverInput(BaseModel):
    asset_id: int = Field(ge=1)
    object_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=512)


class ChannelsTitleInput(BaseModel):
    asset_id: int = Field(ge=1)
    title: str = Field(default="", max_length=64)


class ChannelsAnnotationInput(BaseModel):
    asset_id: int = Field(ge=1)
    annotation: str = Field(
        default="none",
        pattern="^(none|ai_generated|fictional|personal_opinion|marketing_ad|self_shot|repost)$",
    )
    shooting_time: str = Field(default="", max_length=80)
    shooting_location: str = Field(default="", max_length=255)
    repost_source: str = Field(default="", max_length=500)


class ChannelsPushCreate(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=10)
    account_ids: list[str] = Field(min_length=1, max_length=10)
    title: str = Field(default="", max_length=64)
    description: str = Field(default="", max_length=1000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    product_id: str = Field(default="", max_length=120)
    product_name: str = Field(default="", max_length=500)
    covers: list[ChannelsCoverInput] = Field(default_factory=list, max_length=10)
    titles: list[ChannelsTitleInput] = Field(default_factory=list, max_length=10)
    annotations: list[ChannelsAnnotationInput] = Field(default_factory=list, max_length=10)


class ChannelsTaskEdit(BaseModel):
    title: str = Field(min_length=6, max_length=16)
    description: str = Field(default="", max_length=1000)
    product_id: str = Field(default="", max_length=120)
    product_name: str = Field(default="", max_length=500)
    cover_object_key: str | None = Field(default=None, max_length=1024)
    cover_filename: str = Field(default="", max_length=512)
    retry: bool = False


class ChannelsMetricsSync(BaseModel):
    task_ids: list[str] = Field(default_factory=list, max_length=20)


class ChannelsPromotionQuote(BaseModel):
    promotion_target: str = Field(
        default="net_deal_roi",
        pattern="^(play|like|follow|click|heart|smart|product_click|product_pay|deal_roi|net_deal_roi|net_product_pay)$",
    )
    budget_wecoin: int = Field(ge=50, le=30_000_000)
    duration_hours: int = Field(default=24)
    funding_type: str = Field(default="wecoin", pattern="^(wecoin|cash|auto)$")
    bid_mode: str = Field(default="volume", pattern="^(volume|cost_control)$")
    bid_value: float | None = Field(default=None, gt=0)
    start_mode: str = Field(default="immediate", pattern="^(immediate|scheduled)$")
    scheduled_at: datetime | None = None
    billing_method: str = Field(default="prepaid", pattern="^(prepaid|realtime)$")
    promotion_mode: str = Field(default="smart", pattern="^(smart|targeted)$")
    portrait_mode: str = Field(default="none", pattern="^(none|authorized)$")
    voucher_mode: str = Field(default="none", pattern="^(none|max)$")


class ChannelsPromotionOrderCreate(ChannelsPromotionQuote):
    order_name: str = Field(min_length=1, max_length=120)
    confirmed: bool = False
    idempotency_key: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class ChannelsPromotionPaymentSessionCreate(BaseModel):
    request_token: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


class ChannelsPromotionPaymentEvent(BaseModel):
    action: str = Field(pattern="^(consumeSuccess|consumeError|consumeClose|consumeStatusChange)$")
    message: str = Field(default="", max_length=500)


class UploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=150)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)
    asset_scope: str = Field(default="marketing_video", pattern="^(marketing_video|product_image|reference_video)$")
    category: str = Field(default="待分类", max_length=100)


class UploadComplete(BaseModel):
    session_id: str | None = Field(default=None, max_length=36)
    object_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    category: str = Field(default="待分类", max_length=100)
    content_type: str = Field(default="其他", max_length=100)
    asset_scope: str = Field(default="marketing_video", pattern="^(marketing_video|product_image)$")
    library_type: str = Field(default="source", pattern="^(source|remix)$")
    asset_subtype: str = Field(default="其他视频素材", max_length=100)
    folder_name: str = Field(default="", max_length=160)
    tags: list[str] = Field(default_factory=list)
    reference_url: str = Field(default="", max_length=2048)
    reference_video_key: str = Field(default="", max_length=1024)
    reference_video_name: str = Field(default="", max_length=512)
    material_description: str = Field(default="", max_length=10000)
    performance_screenshots: list[dict[str, str]] = Field(default_factory=list, max_length=9)


class UploadTicket(BaseModel):
    session_id: str | None = None
    object_key: str
    upload_url: str
    public_url: str
    headers: dict[str, str]
    expires_in: int


class WorkstationReturnCreate(BaseModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    mime_type: str = Field(default="video/mp4", pattern=r"^video/[A-Za-z0-9.+-]+$")
    category: str = Field(default="待分类", max_length=100)
    content_type: str = Field(default="其他", max_length=100)
    asset_subtype: str = Field(default="AI混剪成片", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=30)
    source_asset_ids: list[int] = Field(default_factory=list, max_length=100)
    source_clip_ids: list[str] = Field(default_factory=list, max_length=100)
    framework_id: str = Field(default="", max_length=120)
    framework_name: str = Field(default="", max_length=255)
    render_id: str = Field(min_length=1, max_length=120)
    variant_id: str = Field(min_length=1, max_length=120)
    maker_id: str = Field(default="", max_length=80)
    maker_name: str = Field(default="", max_length=120)
    review_status: str = Field(pattern=r"^approved$")
    review_note: str = Field(default="", max_length=1000)
    automatic_review_enabled: bool = False
    automatic_assessment: dict = Field(default_factory=dict)


class WorkstationReturnComplete(BaseModel):
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )


class WorkstationQianchuanTarget(BaseModel):
    advertiser_id: str = Field(min_length=1, max_length=80, pattern=r"^\d+$")
    advertiser_name: str = Field(default="", max_length=255)
    plan_id: str = Field(min_length=1, max_length=80, pattern=r"^\d+$")
    plan_name: str = Field(default="", max_length=255)
    plan_alias: str = Field(default="", max_length=255)
    plan_type: str = Field(pattern=r"^(multiplication|full_domain|standard)$")


class WorkstationQianchuanPushCreate(BaseModel):
    asset_id: int = Field(gt=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
    )
    product_category: str = Field(min_length=1, max_length=100)
    actor_number: str = Field(min_length=1, max_length=80)
    actor_name: str = Field(default="WIS混剪工作台", max_length=120)
    enabled: bool = False
    confirmed: bool = False
    daily_material_limit: int = Field(default=1, ge=1, le=20)
    daily_spend_guard_yuan: float | None = Field(default=None, gt=0, le=10_000_000)
    target: WorkstationQianchuanTarget


class MultipartUploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=150)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    asset_scope: str = Field(default="marketing_video", pattern="^(marketing_video|product_image|reference_video)$")
    category: str = Field(default="待分类", max_length=100)
    # Opt in to relay-safe parts. Clients predating this field still expect
    # 64 MiB above 1 GiB and reject any other returned size.
    part_size: Literal[33554432] | None = None


class MultipartUploadLookup(BaseModel):
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")
    legacy_sha256: str | None = Field(default=None, pattern="^[0-9a-fA-F]{64}$")
    asset_scope: str = Field(default="marketing_video", pattern="^(marketing_video|product_image|reference_video)$")
    category: str = Field(default="待分类", max_length=100)


class MultipartPartUrlsCreate(BaseModel):
    part_numbers: list[int] = Field(min_length=1, max_length=50)


class MultipartUploadedPart(BaseModel):
    part_number: int = Field(ge=1, le=10000)
    etag: str = Field(min_length=1, max_length=200)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)


class MultipartUploadComplete(BaseModel):
    parts: list[MultipartUploadedPart] = Field(min_length=1, max_length=10000)
    sha256: str = Field(pattern="^[0-9a-fA-F]{64}$")


class UploadLeaseCreate(BaseModel):
    session_id: str = Field(min_length=1, max_length=36)
    request_id: str = Field(default="", max_length=36)


class UploadLeaseRenew(BaseModel):
    lease_id: str = Field(min_length=1, max_length=36)


class JianyingPairingClaim(BaseModel):
    code: str = Field(min_length=8, max_length=20)
    device_name: str = Field(default="Windows 剪映助手", max_length=120)


class JianyingDeviceUploadCreate(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(default="application/octet-stream", max_length=150)
    size: int = Field(gt=0, le=5 * 1024 * 1024 * 1024)


class JianyingDeviceUploadComplete(BaseModel):
    object_key: str = Field(min_length=1, max_length=1024)
    filename: str = Field(min_length=1, max_length=255)
    category: str = Field(default="待分类", max_length=100)
    content_type: str = Field(default="其他", max_length=100)
    asset_subtype: str = Field(default="其他混剪成片", max_length=100)
    tags: list[str] = Field(default_factory=list, max_length=29)


class JianyingImportProgress(BaseModel):
    status: str = Field(
        pattern="^(claimed|downloading|downloaded|opening|importing|completed|failed)$"
    )
    progress: int = Field(ge=0, le=100)
    message: str = Field(default="", max_length=500)
    downloaded_bytes: int = Field(default=0, ge=0, le=5 * 1024 * 1024 * 1024)
    total_bytes: int = Field(default=0, ge=0, le=5 * 1024 * 1024 * 1024)
    speed_bps: int = Field(default=0, ge=0)
    eta_seconds: int = Field(default=0, ge=0, le=24 * 60 * 60)
    helper_version: str = Field(default="", max_length=30)
    cache_hit: bool = False


class SyncStatusOut(BaseModel):
    state: str
    processed: int = 0
    total: int = 0
    started_at: str | None = None
    completed_at: str | None = None
    error: str = ""
