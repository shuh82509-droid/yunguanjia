from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .database import Base


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    filename: Mapped[str] = mapped_column(String(512), index=True)
    media_type: Mapped[str] = mapped_column(String(30), index=True)
    size: Mapped[int] = mapped_column(Integer, default=0)
    etag: Mapped[str] = mapped_column(String(255), default="")
    modified_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    category: Mapped[str] = mapped_column(String(100), default="待分类", index=True)
    content_type: Mapped[str] = mapped_column(String(100), default="其他", index=True)
    status: Mapped[str] = mapped_column(String(100), default="待整理", index=True)
    asset_scope: Mapped[str] = mapped_column(String(30), default="marketing_video", index=True)
    library_type: Mapped[str] = mapped_column(String(30), default="source", index=True)
    asset_subtype: Mapped[str] = mapped_column(String(100), default="其他视频素材", index=True)
    folder_name: Mapped[str] = mapped_column(String(160), default="", index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False)
    cover_url: Mapped[str] = mapped_column(String(2048), default="")
    source: Mapped[str] = mapped_column(String(100), default="")
    account_name: Mapped[str] = mapped_column(String(255), default="")
    ingest_source: Mapped[str] = mapped_column(String(30), default="oss_scan", index=True)
    uploaded_by_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    uploaded_by_name: Mapped[str] = mapped_column(String(120), default="")
    reference_url: Mapped[str] = mapped_column(String(2048), default="")
    reference_video_key: Mapped[str] = mapped_column(String(1024), default="")
    reference_video_name: Mapped[str] = mapped_column(String(512), default="")
    material_description: Mapped[str] = mapped_column(Text, default="")
    performance_screenshots: Mapped[list[dict]] = mapped_column(JSON, default=list)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by_number: Mapped[str] = mapped_column(String(80), default="")
    deleted_by_name: Mapped[str] = mapped_column(String(120), default="")
    purged_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    purge_error: Mapped[str] = mapped_column(Text, default="")


class WorkstationReturn(Base):
    __tablename__ = "workstation_returns"

    idempotency_key: Mapped[str] = mapped_column(String(200), primary_key=True)
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    filename: Mapped[str] = mapped_column(String(512))
    file_size: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    mime_type: Mapped[str] = mapped_column(String(150), default="video/mp4")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
    error_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AssetFavorite(Base):
    __tablename__ = "asset_favorites"

    user_number: Mapped[str] = mapped_column(String(80), primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AssetEffectiveMark(Base):
    __tablename__ = "asset_effective_marks"

    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), primary_key=True
    )
    marked_by_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    marked_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class UploadSession(Base):
    __tablename__ = "upload_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    file_size: Mapped[int] = mapped_column(Integer)
    filename: Mapped[str] = mapped_column(String(512))
    content_type: Mapped[str] = mapped_column(String(150), default="application/octet-stream")
    asset_scope: Mapped[str] = mapped_column(String(30), default="marketing_video", index=True)
    category: Mapped[str] = mapped_column(String(100), default="待分类")
    object_key: Mapped[str] = mapped_column(String(1024), unique=True, index=True)
    multipart_upload_id: Mapped[str] = mapped_column(String(512), default="")
    part_size: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JianyingPairing(Base):
    __tablename__ = "jianying_pairings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    device_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JianyingDevice(Base):
    __tablename__ = "jianying_devices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    device_name: Mapped[str] = mapped_column(String(120), default="")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JianyingImportTicket(Base):
    __tablename__ = "jianying_import_tickets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="waiting", index=True)
    progress: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="等待桌面助手接收")
    downloaded_bytes: Mapped[int] = mapped_column(Integer, default=0)
    total_bytes: Mapped[int] = mapped_column(Integer, default=0)
    speed_bps: Mapped[int] = mapped_column(Integer, default=0)
    eta_seconds: Mapped[int] = mapped_column(Integer, default=0)
    helper_version: Mapped[str] = mapped_column(String(30), default="")
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PushPreference(Base):
    __tablename__ = "push_preferences"
    __table_args__ = (
        UniqueConstraint(
            "owner_number",
            "platform",
            "account_id",
            "target_id",
            name="uq_push_preferences_owner_target",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    platform: Mapped[str] = mapped_column(String(30), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    account_name: Mapped[str] = mapped_column(String(255), default="")
    target_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    target_name: Mapped[str] = mapped_column(String(255), default="")
    target_type: Mapped[str] = mapped_column(String(40), default="")
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    use_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QianchuanDelivery(Base):
    __tablename__ = "qianchuan_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    created_by_number: Mapped[str] = mapped_column(String(80), index=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    advertiser_id: Mapped[str] = mapped_column(String(80), index=True)
    advertiser_name: Mapped[str] = mapped_column(String(255), default="")
    plan_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    plan_name: Mapped[str] = mapped_column(String(255), default="")
    plan_type: Mapped[str] = mapped_column(String(30), default="")
    platform_asset_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    upload_task_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    delivery_entity_type: Mapped[str] = mapped_column(String(40), default="")
    delivery_entity_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    binding_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    binding_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(120), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_category: Mapped[str] = mapped_column(String(40), default="")
    failure_stage: Mapped[str] = mapped_column(String(40), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    error_advice: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    related_ad_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    related_creative_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    metrics_link_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    metrics_start_date: Mapped[str] = mapped_column(String(10), default="")
    metrics_end_date: Mapped[str] = mapped_column(String(10), default="")
    metrics_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metrics_message: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by_number: Mapped[str] = mapped_column(String(80), default="")
    deleted_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class QianchuanMetricDaily(Base):
    __tablename__ = "qianchuan_metric_daily"
    __table_args__ = (
        UniqueConstraint("task_id", "stat_date", name="uq_qianchuan_metric_daily_task_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("qianchuan_deliveries.id", ondelete="CASCADE"), index=True
    )
    advertiser_id: Mapped[str] = mapped_column(String(80), index=True)
    plan_id: Mapped[str] = mapped_column(String(80), index=True)
    video_id: Mapped[str] = mapped_column(String(120), index=True)
    stat_date: Mapped[str] = mapped_column(String(10), index=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    has_data: Mapped[bool] = mapped_column(Boolean, default=False)
    link_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    request_id: Mapped[str] = mapped_column(String(120), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdqDelivery(Base):
    __tablename__ = "adq_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    created_by_number: Mapped[str] = mapped_column(String(80), index=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    account_name: Mapped[str] = mapped_column(String(255), default="")
    adgroup_id: Mapped[str] = mapped_column(String(80), index=True)
    adgroup_name: Mapped[str] = mapped_column(String(255), default="")
    source_dynamic_creative_id: Mapped[str] = mapped_column(String(80), default="")
    dynamic_creative_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    platform_asset_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    root_material_id: Mapped[str] = mapped_column(String(120), default="", index=True)
    cover_id: Mapped[str] = mapped_column(String(120), default="")
    idempotency_key: Mapped[str] = mapped_column(String(255), default="", index=True)
    binding_evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    binding_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    request_id: Mapped[str] = mapped_column(String(120), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error_category: Mapped[str] = mapped_column(String(50), default="")
    failure_stage: Mapped[str] = mapped_column(String(40), default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    error_advice: Mapped[str] = mapped_column(Text, default="")
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    metrics_start_date: Mapped[str] = mapped_column(String(10), default="")
    metrics_end_date: Mapped[str] = mapped_column(String(10), default="")
    metrics_synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    metrics_message: Mapped[str] = mapped_column(Text, default="")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    deleted_by_number: Mapped[str] = mapped_column(String(80), default="")
    deleted_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AdqMetricDaily(Base):
    __tablename__ = "adq_metric_daily"
    __table_args__ = (UniqueConstraint("task_id", "stat_date", name="uq_adq_metric_daily_task_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("adq_deliveries.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(80), index=True)
    adgroup_id: Mapped[str] = mapped_column(String(80), index=True)
    video_id: Mapped[str] = mapped_column(String(120), index=True)
    stat_date: Mapped[str] = mapped_column(String(10), index=True)
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    has_data: Mapped[bool] = mapped_column(Boolean, default=False)
    message: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str] = mapped_column(String(80), default="")
    request_id: Mapped[str] = mapped_column(String(120), default="")
    synced_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class OaAccessGrant(Base):
    __tablename__ = "oa_access_grants"

    identifier: Mapped[str] = mapped_column(String(180), primary_key=True)
    identifier_type: Mapped[str] = mapped_column(String(20), default="name", index=True)
    real_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    user_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    department: Mapped[str] = mapped_column(String(255), default="")
    center: Mapped[str] = mapped_column(String(255), default="")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    source: Mapped[str] = mapped_column(String(40), default="admin")
    granted_by_number: Mapped[str] = mapped_column(String(80), default="")
    granted_by_name: Mapped[str] = mapped_column(String(120), default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_by_number: Mapped[str] = mapped_column(String(80), default="")
    revoked_by_name: Mapped[str] = mapped_column(String(120), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OaAccessAuditLog(Base):
    __tablename__ = "oa_access_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    identifier: Mapped[str] = mapped_column(String(180), index=True)
    real_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    action: Mapped[str] = mapped_column(String(30), index=True)
    actor_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ModuleAccessGrant(Base):
    __tablename__ = "module_access_grants"

    identifier: Mapped[str] = mapped_column(String(180), primary_key=True)
    identifier_type: Mapped[str] = mapped_column(String(20), default="name", index=True)
    real_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    user_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    department: Mapped[str] = mapped_column(String(255), default="")
    center: Mapped[str] = mapped_column(String(255), default="")
    access_mode: Mapped[str] = mapped_column(String(20), default="all", index=True)
    modules: Mapped[list[str]] = mapped_column(JSON, default=list)
    updated_by_number: Mapped[str] = mapped_column(String(80), default="")
    updated_by_name: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AssetAuditLog(Base):
    __tablename__ = "asset_audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    asset_id: Mapped[int] = mapped_column(Integer, index=True)
    object_key: Mapped[str] = mapped_column(String(1024), index=True)
    action: Mapped[str] = mapped_column(String(40), index=True)
    actor_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AdminGrant(Base):
    __tablename__ = "admin_grants"

    identifier: Mapped[str] = mapped_column(String(180), primary_key=True)
    identifier_type: Mapped[str] = mapped_column(String(20), default="name", index=True)
    real_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    user_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    department: Mapped[str] = mapped_column(String(255), default="")
    center: Mapped[str] = mapped_column(String(255), default="")
    role: Mapped[str] = mapped_column(String(30), default="operation_admin", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    protected: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    granted_by_number: Mapped[str] = mapped_column(String(80), default="")
    granted_by_name: Mapped[str] = mapped_column(String(120), default="")
    granted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revoked_by_number: Mapped[str] = mapped_column(String(80), default="")
    revoked_by_name: Mapped[str] = mapped_column(String(120), default="")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReviewWorkflowConfig(Base):
    __tablename__ = "review_workflow_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    naming_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    ai_redline_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    required_roles: Mapped[list[str]] = mapped_column(
        JSON,
        default=lambda: ["team_lead", "supervisor"],
    )
    updated_by_number: Mapped[str] = mapped_column(String(80), default="")
    updated_by_name: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewAiRule(Base):
    __tablename__ = "review_ai_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(80), unique=True, index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    severity: Mapped[str] = mapped_column(String(20), index=True)
    title: Mapped[str] = mapped_column(String(160))
    pattern: Mapped[str] = mapped_column(Text)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    sort_order: Mapped[int] = mapped_column(Integer, default=0)
    updated_by_number: Mapped[str] = mapped_column(String(80), default="")
    updated_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReviewRoleAssignment(Base):
    __tablename__ = "review_role_assignments"
    __table_args__ = (
        UniqueConstraint("role_code", "identifier", name="uq_review_role_assignment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    role_code: Mapped[str] = mapped_column(String(40), index=True)
    identifier: Mapped[str] = mapped_column(String(180), index=True)
    user_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    user_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    department: Mapped[str] = mapped_column(String(255), default="")
    center: Mapped[str] = mapped_column(String(120), default="", index=True)
    group_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    source: Mapped[str] = mapped_column(String(40), default="manual", index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_by_number: Mapped[str] = mapped_column(String(80), default="")
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AssetReviewSubmission(Base):
    __tablename__ = "asset_review_submissions"
    __table_args__ = (
        UniqueConstraint("asset_id", "version", name="uq_asset_review_submission_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    submitted_by_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    submitted_by_name: Mapped[str] = mapped_column(String(120), default="")
    note: Mapped[str] = mapped_column(Text, default="")
    filename_snapshot: Mapped[str] = mapped_column(String(512), default="")
    naming_evidence: Mapped[list[dict]] = mapped_column(JSON, default=list)
    naming_check: Mapped[dict] = mapped_column(JSON, default=dict)
    route_center: Mapped[str] = mapped_column(String(120), default="", index=True)
    route_group: Mapped[str] = mapped_column(String(120), default="", index=True)
    assignment_mode: Mapped[str] = mapped_column(String(30), default="organization", index=True)
    designated_reviewer_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    designated_reviewer_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AssetReviewAiResult(Base):
    __tablename__ = "asset_review_ai_results"
    __table_args__ = (
        UniqueConstraint("submission_id", name="uq_asset_review_ai_result_submission"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("asset_review_submissions.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    provider: Mapped[str] = mapped_column(String(80), default="cutter_rules")
    task_id: Mapped[str] = mapped_column(String(160), default="")
    rule_version: Mapped[str] = mapped_column(String(80), default="wis-redline-v1")
    summary: Mapped[str] = mapped_column(Text, default="")
    category_counts: Mapped[dict] = mapped_column(JSON, default=dict)
    findings: Mapped[list[dict]] = mapped_column(JSON, default=list)
    segments: Mapped[list[dict]] = mapped_column(JSON, default=list)
    error_message: Mapped[str] = mapped_column(Text, default="")
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AssetReviewDecision(Base):
    __tablename__ = "asset_review_decisions"
    __table_args__ = (
        UniqueConstraint("submission_id", "role_code", name="uq_asset_review_decision_role"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    submission_id: Mapped[str] = mapped_column(
        ForeignKey("asset_review_submissions.id", ondelete="CASCADE"), index=True
    )
    role_code: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    reviewer_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    reviewer_name: Mapped[str] = mapped_column(String(120), default="")
    candidate_reviewers: Mapped[list[dict]] = mapped_column(JSON, default=list)
    candidate_search: Mapped[str] = mapped_column(Text, default="")
    note: Mapped[str] = mapped_column(Text, default="")
    quality_scores: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_total: Mapped[int | None] = mapped_column(Integer, nullable=True)
    quality_grade: Mapped[str] = mapped_column(String(30), default="")
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class OperationLog(Base):
    __tablename__ = "operation_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    actor_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    department: Mapped[str] = mapped_column(String(255), default="")
    module: Mapped[str] = mapped_column(String(50), default="system", index=True)
    action: Mapped[str] = mapped_column(String(80), default="", index=True)
    method: Mapped[str] = mapped_column(String(10), default="")
    path: Mapped[str] = mapped_column(String(512), default="")
    result: Mapped[str] = mapped_column(String(20), default="success", index=True)
    status_code: Mapped[int] = mapped_column(Integer, default=200)
    resource_type: Mapped[str] = mapped_column(String(50), default="")
    resource_id: Mapped[str] = mapped_column(String(180), default="", index=True)
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CreativeIncentiveDirection(Base):
    __tablename__ = "creative_incentive_directions"
    __table_args__ = (UniqueConstraint("material_id", name="uq_creative_incentive_material_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    direction_name: Mapped[str] = mapped_column(String(180), index=True)
    material_id: Mapped[str] = mapped_column(String(120), index=True)
    material_name: Mapped[str] = mapped_column(String(255), default="")
    creator_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    creator_name: Mapped[str] = mapped_column(String(120), index=True)
    department: Mapped[str] = mapped_column(String(255), default="品牌营销部", index=True)
    center: Mapped[str] = mapped_column(String(255), default="", index=True)
    online_date: Mapped[str] = mapped_column(String(10), index=True)
    originality_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    originality_note: Mapped[str] = mapped_column(Text, default="")
    confirmed_by_number: Mapped[str] = mapped_column(String(80), default="")
    confirmed_by_name: Mapped[str] = mapped_column(String(120), default="")
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    copy_judgement: Mapped[str] = mapped_column(Text, default="")
    visual_judgement: Mapped[str] = mapped_column(Text, default="")
    voice_judgement: Mapped[str] = mapped_column(Text, default="")
    remix_plan: Mapped[str] = mapped_column(Text, default="")
    reference_url: Mapped[str] = mapped_column(String(2048), default="")
    metric_status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    latest_gmv_yuan: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_cost_yuan: Mapped[float | None] = mapped_column(Float, nullable=True)
    latest_roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_cutoff_at: Mapped[str] = mapped_column(String(40), default="")
    source_updated_at: Mapped[str] = mapped_column(String(40), default="")
    synced_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by_number: Mapped[str] = mapped_column(String(80), default="")
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class CreativeIncentiveMilestone(Base):
    __tablename__ = "creative_incentive_milestones"
    __table_args__ = (
        UniqueConstraint("direction_id", "point_number", name="uq_creative_incentive_direction_point"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    direction_id: Mapped[str] = mapped_column(
        ForeignKey("creative_incentive_directions.id", ondelete="CASCADE"), index=True
    )
    point_number: Mapped[int] = mapped_column(Integer)
    threshold_gmv_yuan: Mapped[int] = mapped_column(Integer)
    observed_gmv_yuan: Mapped[float] = mapped_column(Float)
    observed_cost_yuan: Mapped[float | None] = mapped_column(Float, nullable=True)
    observed_roi: Mapped[float | None] = mapped_column(Float, nullable=True)
    source_cutoff_at: Mapped[str] = mapped_column(String(40), default="")
    share_type: Mapped[str] = mapped_column(String(20), default="update")
    share_text: Mapped[str] = mapped_column(Text, default="")
    share_status: Mapped[str] = mapped_column(String(30), default="draft", index=True)
    published_by_number: Mapped[str] = mapped_column(String(80), default="")
    published_by_name: Mapped[str] = mapped_column(String(120), default="")
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    feishu_message_id: Mapped[str] = mapped_column(String(160), default="")
    delivery_error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AssistantConversation(Base):
    __tablename__ = "assistant_conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    title: Mapped[str] = mapped_column(String(160), default="新会话")
    last_message_preview: Mapped[str] = mapped_column(String(300), default="")
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AssistantConversationMessage(Base):
    __tablename__ = "assistant_conversation_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(20), index=True)
    content: Mapped[str] = mapped_column(Text, default="")
    actions: Mapped[list[dict]] = mapped_column(JSON, default=list)
    message_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class AssistantAttachment(Base):
    __tablename__ = "assistant_attachments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        ForeignKey("assistant_conversations.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    filename: Mapped[str] = mapped_column(String(255), default="image")
    mime_type: Mapped[str] = mapped_column(String(80), default="application/octet-stream")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    sha256: Mapped[str] = mapped_column(String(64), default="", index=True)
    storage_path: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class ChannelsAccount(Base):
    __tablename__ = "channels_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_number: Mapped[str] = mapped_column(String(80), index=True)
    owner_name: Mapped[str] = mapped_column(String(120), default="")
    nickname: Mapped[str] = mapped_column(String(255), default="")
    external_account_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    avatar_url: Mapped[str] = mapped_column(String(2048), default="")
    auth_file: Mapped[str] = mapped_column(String(1024), default="")
    channel_cookies_ciphertext: Mapped[str] = mapped_column(Text, default="")
    session_cookie_ciphertext: Mapped[str] = mapped_column(Text, default="")
    cookies_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="active", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelsDelivery(Base):
    __tablename__ = "channels_deliveries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(36), index=True)
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="CASCADE"), index=True)
    created_by_number: Mapped[str] = mapped_column(String(80), index=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    account_id: Mapped[str] = mapped_column(ForeignKey("channels_accounts.id", ondelete="CASCADE"), index=True)
    account_name: Mapped[str] = mapped_column(String(255), default="")
    title: Mapped[str] = mapped_column(String(255), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    product_id: Mapped[str] = mapped_column(String(120), default="")
    product_name: Mapped[str] = mapped_column(String(500), default="")
    cover_object_key: Mapped[str] = mapped_column(String(1024), default="")
    cover_filename: Mapped[str] = mapped_column(String(512), default="")
    cover_status: Mapped[str] = mapped_column(String(30), default="")
    cover_message: Mapped[str] = mapped_column(String(500), default="")
    video_annotation: Mapped[str] = mapped_column(String(40), default="none")
    annotation_shooting_time: Mapped[str] = mapped_column(String(80), default="")
    annotation_shooting_location: Mapped[str] = mapped_column(String(255), default="")
    annotation_repost_source: Mapped[str] = mapped_column(String(500), default="")
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    failure_stage: Mapped[str] = mapped_column(String(40), default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str] = mapped_column(Text, default="")
    platform_content_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    platform_export_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    platform_export_source: Mapped[str] = mapped_column(String(40), default="")
    platform_export_verified_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    platform_content_url: Mapped[str] = mapped_column(String(2048), default="")
    publish_clicked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    publish_client_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    publish_transport: Mapped[str] = mapped_column(String(30), default="")
    platform_check_attempts: Mapped[int] = mapped_column(Integer, default=0)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gmv_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    metrics_date: Mapped[str] = mapped_column(String(10), default="")
    metrics_message: Mapped[str] = mapped_column(Text, default="")
    metrics_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelsMetricDaily(Base):
    __tablename__ = "channels_metric_daily"
    __table_args__ = (UniqueConstraint("delivery_id", "data_date", name="uq_channels_metric_daily_delivery_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    delivery_id: Mapped[str] = mapped_column(ForeignKey("channels_deliveries.id", ondelete="CASCADE"), index=True)
    account_id: Mapped[str] = mapped_column(String(36), index=True)
    data_date: Mapped[str] = mapped_column(String(10), index=True)
    view_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comment_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    share_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    gmv_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source: Mapped[str] = mapped_column(String(40), default="channels_creator")
    message: Mapped[str] = mapped_column(Text, default="")
    collected_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelsPromotionAccount(Base):
    __tablename__ = "channels_promotion_accounts"

    account_id: Mapped[str] = mapped_column(
        ForeignKey("channels_accounts.id", ondelete="CASCADE"), primary_key=True
    )
    owner_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    auth_file: Mapped[str] = mapped_column(String(1024), default="")
    promote_uniq_id: Mapped[str] = mapped_column(String(160), default="")
    nickname: Mapped[str] = mapped_column(String(255), default="")
    user_type: Mapped[int] = mapped_column(Integer, default=0)
    balance_wecoin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    message: Mapped[str] = mapped_column(Text, default="")
    authorized_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelsPromotionOrder(Base):
    __tablename__ = "channels_promotion_orders"
    __table_args__ = (
        UniqueConstraint(
            "delivery_id",
            "created_by_number",
            "idempotency_key",
            name="uq_channels_promotion_order_idempotency",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    delivery_id: Mapped[str] = mapped_column(
        ForeignKey("channels_deliveries.id", ondelete="CASCADE"), index=True
    )
    account_id: Mapped[str] = mapped_column(
        ForeignKey("channels_accounts.id", ondelete="RESTRICT"), index=True
    )
    created_by_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    created_by_name: Mapped[str] = mapped_column(String(120), default="")
    platform_export_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    promotion_target: Mapped[str] = mapped_column(String(30), default="play")
    budget_wecoin: Mapped[int] = mapped_column(Integer, default=0)
    quoted_wecoin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_hours: Mapped[int] = mapped_column(Integer, default=24)
    order_name: Mapped[str] = mapped_column(String(120), default="")
    idempotency_key: Mapped[str] = mapped_column(String(80), default="")
    quote_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    request_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    create_attempts: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="submitting", index=True)
    promotion_id: Mapped[str] = mapped_column(String(160), default="", index=True)
    cost_wecoin: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str] = mapped_column(String(80), default="")
    error_message: Mapped[str] = mapped_column(Text, default="")
    response_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ChannelsPromotionPaymentSession(Base):
    __tablename__ = "channels_promotion_payment_sessions"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_channels_promotion_payment_idempotency"),
        UniqueConstraint("active_key", name="uq_channels_promotion_payment_active"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        ForeignKey("channels_promotion_orders.id", ondelete="CASCADE"), index=True
    )
    active_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(30), default="preparing", index=True)
    expected_amount: Mapped[int] = mapped_column(Integer, default=0)
    pc_sdk_info_ciphertext: Mapped[str] = mapped_column(Text, default="")
    sdk_status: Mapped[str] = mapped_column(String(40), default="")
    prepare_attempts: Mapped[int] = mapped_column(Integer, default=0)
    query_attempts: Mapped[int] = mapped_column(Integer, default=0)
    response_summary: Mapped[str] = mapped_column(Text, default="")
    requested_by: Mapped[str] = mapped_column(String(80), default="", index=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class VideoRequest(Base):
    __tablename__ = "video_requests"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    product: Mapped[str] = mapped_column(String(100), default="通用", index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    reference_url: Mapped[str] = mapped_column(String(2048), default="")
    reference_video_key: Mapped[str] = mapped_column(String(1024), default="")
    reference_video_name: Mapped[str] = mapped_column(String(512), default="")
    reference_videos: Mapped[list[dict]] = mapped_column(JSON, default=list)
    reference_images: Mapped[list[dict]] = mapped_column(JSON, default=list)
    requester_number: Mapped[str] = mapped_column(String(80), index=True)
    requester_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    requester_department: Mapped[str] = mapped_column(String(255), default="")
    assignee_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    assignee_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    status: Mapped[str] = mapped_column(String(40), default="submitted", index=True)
    latest_asset_id: Mapped[int | None] = mapped_column(
        ForeignKey("assets.id", ondelete="SET NULL"), nullable=True, index=True
    )
    delivery_version: Mapped[int] = mapped_column(Integer, default=0)
    latest_feedback: Mapped[str] = mapped_column(Text, default="")
    return_reason: Mapped[str] = mapped_column(Text, default="")
    returned_by_number: Mapped[str] = mapped_column(String(80), default="")
    returned_by_name: Mapped[str] = mapped_column(String(120), default="")
    returned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class VideoRequestDelivery(Base):
    __tablename__ = "video_request_deliveries"
    __table_args__ = (
        UniqueConstraint("request_id", "version", name="uq_video_request_delivery_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("video_requests.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id", ondelete="RESTRICT"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    submission_id: Mapped[str] = mapped_column(String(36), default="", index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    uploaded_by_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    uploaded_by_name: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class VideoRequestEvent(Base):
    __tablename__ = "video_request_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    request_id: Mapped[str] = mapped_column(
        ForeignKey("video_requests.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(40), index=True)
    actor_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    actor_name: Mapped[str] = mapped_column(String(120), default="")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    recipient_number: Mapped[str] = mapped_column(String(80), default="", index=True)
    recipient_name: Mapped[str] = mapped_column(String(120), default="", index=True)
    kind: Mapped[str] = mapped_column(String(40), default="video_request", index=True)
    title: Mapped[str] = mapped_column(String(180), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    resource_type: Mapped[str] = mapped_column(String(50), default="")
    resource_id: Mapped[str] = mapped_column(String(80), default="", index=True)
    external_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    external_message_id: Mapped[str] = mapped_column(String(200), default="")
    external_attempts: Mapped[int] = mapped_column(Integer, default=0)
    external_error: Mapped[str] = mapped_column(Text, default="")
    external_attempted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    read_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)
