import asyncio
import base64
import binascii
import hashlib
import json
import logging
import os
import re
import subprocess
import tempfile
import time
import unicodedata
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from contextlib import asynccontextmanager, suppress
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Literal
from uuid import uuid4, uuid5, NAMESPACE_URL

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from .public_urls import oauth_result_url
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool
from sqlalchemy import Float, String, and_, cast, delete, func, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .catalog_read_queue import CatalogReadQueue, count_ordered_asset_hits

from .upload_delivery import (
    UPLOAD_PART_BYTES, finish_registration, registration_proof, remember_direct_ticket,
)

from .admission_policy import confirmed_departure
from .organization_identity import employee_key, personal_identity_match, canonical_members, match_timesheets
from .auth import (
    SESSION_COOKIE,
    _organization_from_user,
    cookie_value,
    login_with_password,
    require_user,
    send_login_captcha,
    user_allowed,
    user_from_token,
)
from .ai_assistant_service import AiAssistantConfig, AiAssistantError, AiAssistantService
from .ai_insight_service import ai_insight_service
from .business_intelligence_service import (
    BusinessIntelligenceError,
    business_intelligence_service,
)
from .creative_incentive_service import (
    POINT_GMV_YUAN,
    deliver_pending_milestones,
    deliver_share_text,
    sync_directions,
)
from .long_term_work_service import LongTermWorkError, long_term_work_service
from .muse_api import router as muse_router
from .omnichannel_intelligence_service import (
    OmnichannelIntelligenceError,
    omnichannel_intelligence_service,
)
from .qianchuan_product_plan_map import product_plan_map_snapshot
from .timesheet_service import TimesheetError, timesheet_service
from .catalog_service import catalog_service, infer_category, infer_content_type, infer_library_metadata
from .config import settings
from .database import Base, SessionLocal, engine, get_db
from .feishu_notification_service import feishu_notification_service
from .models import (
    AdqDelivery,
    AdqMetricDaily,
    AdminGrant,
    AssistantAttachment,
    AssistantConversation,
    AssistantConversationMessage,
    AppMeta,
    Asset,
    AssetAuditLog,
    AssetEffectiveMark,
    AssetFavorite,
    AssetReviewAiResult,
    AssetReviewDecision,
    AssetReviewSubmission,
    ChannelsAccount,
    ChannelsDelivery,
    ChannelsMetricDaily,
    ChannelsPromotionAccount,
    ChannelsPromotionOrder,
    ChannelsPromotionPaymentSession,
    CreativeIncentiveDirection,
    CreativeIncentiveMilestone,
    JianyingDevice,
    JianyingImportTicket,
    JianyingPairing,
    ModuleAccessGrant,
    OaAccessAuditLog,
    OaAccessGrant,
    OperationLog,
    PushPreference,
    QianchuanDelivery,
    QianchuanMetricDaily,
    ReviewAiRule,
    ReviewRoleAssignment,
    ReviewWorkflowConfig,
    UploadSession,
    UserNotification,
    VideoRequest,
    VideoRequestDelivery,
    VideoRequestEvent,
    WorkstationReturn,
)
from .adq_service import AdqError, adq_service
from .channels_service import ChannelsCancelled, ChannelsError, channels_service
from .channels_promotion_service import (
    ChannelsPromotionError,
    channels_promotion_service,
)
from .jianying_service import (
    claim_pairing,
    create_pairing,
    device_from_request,
    device_out,
    device_user,
    external_app_url,
)
from .oss_service import display_filename_from_object_key, normalize_upload_filename, oss_service
from .personal_sales_service import PEOPLE_PATH, PersonalSalesError, SourcePendingError, personal_sales_service
from .qianchuan_service import PLAN_MATERIAL_REPORT_FIELDS, QianchuanError, qianchuan_service
from .review_ai_service import (
    DEFAULT_RULES,
    RELAXATION_SOURCE_URL,
    evaluate_redlines,
    review_ai_service,
)
from .root_feedback_service import RootFeedbackError, root_feedback_service
from .root_material_upload_service import RootMaterialUploadError, root_material_upload_service
from .schemas import (
    AssetOut,
    AssetEffectiveUpdate,
    AssetPage,
    AssetUpdate,
    StatsOut,
    SyncStatusOut,
    QianchuanMetricsSync,
    QianchuanPushCreate,
    AdqMetricsSync,
    AdqPushCreate,
    AdqSharedUploadCreate,
    ChannelsPushCreate,
    ChannelsTaskEdit,
    ChannelsMetricsSync,
    ChannelsPromotionQuote,
    ChannelsPromotionOrderCreate,
    ChannelsPromotionPaymentEvent,
    ChannelsPromotionPaymentSessionCreate,
    UploadComplete,
    UploadCreate,
    UploadTicket,
    WorkstationReturnComplete,
    WorkstationReturnCreate,
    WorkstationQianchuanPushCreate,
    MultipartUploadCreate,
    MultipartUploadLookup,
    MultipartPartUrlsCreate,
    MultipartUploadComplete,
    UploadLeaseCreate,
    UploadLeaseRenew,
    JianyingPairingClaim,
    JianyingDeviceUploadComplete,
    JianyingDeviceUploadCreate,
    JianyingImportProgress,
)
from .workstation_auth import require_workstation


PRODUCT_CATEGORY_ALIASES = {
    "燕窝胜肽面膜": "燕窝面膜",
    "清洁泥膜": "其他 WIS 素材",
}
ADDITIONAL_PRODUCT_CATEGORIES = ("凝颜洁面", "极润洁面")

REVIEW_ROLE_DEFINITIONS = (
    ("member", "组员", False),
    ("team_lead", "组长", True),
    ("supervisor", "主管", True),
    ("brand_tone", "品牌调性（预留）", False),
    ("director", "总监（历史角色）", False),
    ("internal_control", "内控（历史角色）", False),
)
REVIEW_ROLE_LABELS = {code: label for code, label, _stage in REVIEW_ROLE_DEFINITIONS}
REVIEW_DESIGNATED_ROLE_CODE = "designated_reviewer"
REVIEW_ROLE_LABELS[REVIEW_DESIGNATED_ROLE_CODE] = "指定审核人"
REVIEW_STAGE_ORDER = [code for code, _label, stage in REVIEW_ROLE_DEFINITIONS if stage]
REVIEW_HUMAN_DECISION_CODES = {*REVIEW_STAGE_ORDER, REVIEW_DESIGNATED_ROLE_CODE}
REVIEW_LEGACY_STAGE_CODES = {"director", "internal_control"}
REVIEW_BRAND_TONE_DOC_URL = "https://www.feishu.cn/docx/Hpjkdiz7Lopi5exS8nRc6urCn9g"
REVIEW_NAMING_DOC_URL = "https://jqx28l0j4lx.feishu.cn/wiki/VKEhwBhhLiDdqdkFndVcVRBJnth"
REVIEW_NAMING_RULE_VERSION = "wis-remix-naming-v1"
REVIEW_NAMING_CATEGORY_LABELS = {
    "face": "上脸素材",
    "mechanism": "机制素材",
    "product_display": "产品展示素材",
    "ai_first_creation": "AI一创素材",
}
REVIEW_QUALITY_KEYS = ("hook", "selling_point", "rhythm", "production")


def canonical_product_category(value: str | None, default: str = "待分类") -> str:
    normalized = str(value or "").strip() or default
    return PRODUCT_CATEGORY_ALIASES.get(normalized, normalized)


def asset_tags_contain(term: str):
    """Search decoded JSON tag values instead of SQLite's escaped JSON text."""
    tag_values = func.json_each(Asset.tags).table_valued("value").alias("asset_tag_values")
    return (
        select(1)
        .select_from(tag_values)
        .where(cast(tag_values.c.value, String).contains(term))
        .correlate(Asset)
        .exists()
    )


logger = logging.getLogger(__name__)


ai_assistant_service = AiAssistantService(AiAssistantConfig(
    url=settings.jump_llm_url,
    token=settings.jump_llm_token,
    application=settings.jump_llm_application,
    provider=settings.jump_llm_provider,
    model=settings.jump_llm_model,
    vision_model=settings.jump_llm_vision_model,
    timeout_seconds=settings.jump_llm_timeout_seconds,
    temperature=settings.jump_llm_temperature,
))


async def ai_insight_loop() -> None:
    while True:
        await asyncio.sleep(settings.ai_insight_interval_seconds)
        try:
            await asyncio.to_thread(ai_insight_service.refresh)
        except Exception:
            logger.exception("AI insight scheduled refresh failed")


_upload_lease_lock = Lock()
_upload_leases: dict[str, tuple[str, str, float]] = {}
_upload_waiters: dict[str, tuple[str, str, float]] = {}
_upload_last_granted_owner = ""
_workstation_return_lock = Lock()


def _clean_upload_leases(now: float | None = None) -> None:
    current = now if now is not None else time.monotonic()
    for lease_id, (_, _, expires_at) in list(_upload_leases.items()):
        if expires_at <= current:
            _upload_leases.pop(lease_id, None)


def _clean_upload_waiters(now: float | None = None) -> None:
    current = now if now is not None else time.monotonic()
    for request_id, (_, _, expires_at) in list(_upload_waiters.items()):
        if expires_at <= current:
            _upload_waiters.pop(request_id, None)


def _fair_upload_waiter_ids(owner_counts: Counter) -> list[str]:
    """Interleave colleagues while retaining FIFO order within each owner."""
    grouped: dict[str, list[str]] = {}
    for request_id, (owner, _, _) in _upload_waiters.items():
        if owner_counts[owner] < settings.upload_user_part_limit:
            grouped.setdefault(owner, []).append(request_id)
    owners = list(grouped)
    if _upload_last_granted_owner in owners and len(owners) > 1:
        pivot = owners.index(_upload_last_granted_owner) + 1
        owners = owners[pivot:] + owners[:pivot]
    fair_order: list[str] = []
    while grouped:
        for owner in list(owners):
            if owner not in grouped:
                continue
            fair_order.append(grouped[owner].pop(0))
            if not grouped[owner]:
                grouped.pop(owner)
    return fair_order


def acquire_upload_lease(owner_number: str, session_id: str, request_id: str = "") -> dict:
    """Bound direct-to-OSS lanes across all browser sessions in the current server worker."""
    global _upload_last_granted_owner
    with _upload_lease_lock:
        now = time.monotonic()
        _clean_upload_leases(now)
        _clean_upload_waiters(now)
        if request_id:
            existing = _upload_waiters.get(request_id)
            if existing and (existing[0] != owner_number or existing[1] != session_id):
                return {"acquired": False, "retry_after_ms": 1000, "queue_position": 0}
            _upload_waiters[request_id] = (owner_number, session_id, now + 5)
        owner_counts = Counter(owner for owner, _, _ in _upload_leases.values())
        owner_count = owner_counts[owner_number]
        fair_order = _fair_upload_waiter_ids(owner_counts)
        position = fair_order.index(request_id) + 1 if request_id in fair_order else max(1, len(fair_order) + 1)
        can_acquire = (
            len(_upload_leases) < settings.upload_global_part_limit
            and owner_count < settings.upload_user_part_limit
            and (not request_id or (fair_order and fair_order[0] == request_id))
        )
        if not can_acquire:
            return {
                "acquired": False,
                "retry_after_ms": 500,
                "queue_position": position,
                "queue_total": len(_upload_waiters),
                "global_in_use": len(_upload_leases),
                "global_limit": settings.upload_global_part_limit,
                "user_in_use": owner_count,
                "user_limit": settings.upload_user_part_limit,
            }
        if request_id:
            _upload_waiters.pop(request_id, None)
            _upload_last_granted_owner = owner_number
        lease_id = str(uuid4())
        expires_at = now + settings.upload_lease_seconds
        _upload_leases[lease_id] = (owner_number, session_id, expires_at)
        return {
            "acquired": True,
            "lease_id": lease_id,
            "expires_in": settings.upload_lease_seconds,
            "global_in_use": len(_upload_leases),
            "global_limit": settings.upload_global_part_limit,
            "user_in_use": owner_count + 1,
            "user_limit": settings.upload_user_part_limit,
        }


def renew_upload_lease(owner_number: str, lease_id: str) -> bool:
    with _upload_lease_lock:
        _clean_upload_leases()
        current = _upload_leases.get(lease_id)
        if not current or current[0] != owner_number:
            return False
        _upload_leases[lease_id] = (current[0], current[1], time.monotonic() + settings.upload_lease_seconds)
        return True


def release_upload_lease(owner_number: str, lease_id: str) -> bool:
    with _upload_lease_lock:
        current = _upload_leases.get(lease_id)
        if not current or current[0] != owner_number:
            return False
        _upload_leases.pop(lease_id, None)
        return True


def ensure_asset_schema() -> None:
    """Keep existing SQLite volumes compatible with the current release."""
    Base.metadata.create_all(engine)
    if not str(settings.database_url).startswith("sqlite"):
        return
    with engine.begin() as connection:
        for table_name in ("oa_access_grants", "module_access_grants", "admin_grants"):
            permission_columns = {
                row[1] for row in connection.execute(text(f"PRAGMA table_info({table_name})"))
            }
            if "center" not in permission_columns:
                connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN center VARCHAR(255) DEFAULT ''"))
        existing = {row[1] for row in connection.execute(text("PRAGMA table_info(assets)"))}
        additions = {
            "cover_url": "VARCHAR(2048) DEFAULT ''",
            "source": "VARCHAR(100) DEFAULT ''",
            "account_name": "VARCHAR(255) DEFAULT ''",
            "ingest_source": "VARCHAR(30) DEFAULT 'oss_scan'",
            "uploaded_by_number": "VARCHAR(80) DEFAULT ''",
            "uploaded_by_name": "VARCHAR(120) DEFAULT ''",
            "deleted_at": "DATETIME NULL",
            "deleted_by_number": "VARCHAR(80) DEFAULT ''",
            "deleted_by_name": "VARCHAR(120) DEFAULT ''",
            "purged_at": "DATETIME NULL",
            "purge_error": "TEXT DEFAULT ''",
            "asset_scope": "VARCHAR(30) DEFAULT 'marketing_video'",
            "library_type": "VARCHAR(30) DEFAULT 'source'",
            "asset_subtype": "VARCHAR(100) DEFAULT '其他视频素材'",
            "folder_name": "VARCHAR(160) DEFAULT ''",
            "reference_url": "VARCHAR(2048) DEFAULT ''",
            "reference_video_key": "VARCHAR(1024) DEFAULT ''",
            "reference_video_name": "VARCHAR(512) DEFAULT ''",
            "material_description": "TEXT DEFAULT ''",
            "performance_screenshots": "JSON DEFAULT '[]'",
        }
        library_schema_added = "library_type" not in existing or "asset_subtype" not in existing
        for name, definition in additions.items():
            if name not in existing:
                connection.execute(text(f"ALTER TABLE assets ADD COLUMN {name} {definition}"))
        connection.execute(
            text(
                "UPDATE assets SET ingest_source = CASE "
                "WHEN source = 'oa_upload' OR object_key LIKE :upload_pattern THEN 'oa_upload' "
                "ELSE 'oss_scan' END"
            ),
            {"upload_pattern": settings.prefix.rstrip("/") + "/uploads/%"},
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_assets_uploaded_by_number ON assets (uploaded_by_number)")
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_asset_scope ON assets (asset_scope)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_library_type ON assets (library_type)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_asset_subtype ON assets (asset_subtype)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_folder_name ON assets (folder_name)"))
        connection.execute(
            text("UPDATE assets SET asset_scope = 'product_image' WHERE object_key LIKE :pattern"),
            {"pattern": settings.prefix.rstrip("/") + "/product-images/%"},
        )
        if library_schema_added:
            connection.execute(
                text(
                    "UPDATE assets SET library_type = 'remix', asset_subtype = CASE "
                    "WHEN lower(filename || ' ' || object_key) LIKE '%ai%' THEN 'AI混剪成片' "
                    "WHEN filename LIKE '%明星%' OR object_key LIKE '%明星%' THEN '明星素材混剪' "
                    "WHEN filename LIKE '%达人%' OR object_key LIKE '%达人%' OR lower(filename || ' ' || object_key) LIKE '%kol%' THEN '达人素材混剪' "
                    "ELSE '其他混剪成片' END "
                    "WHERE filename LIKE '%混剪%' OR object_key LIKE '%混剪%' "
                    "OR filename LIKE '%二创%' OR object_key LIKE '%二创%' "
                    "OR filename LIKE '%成片%' OR object_key LIKE '%成片%'"
                )
            )
            connection.execute(
                text(
                    "UPDATE assets SET asset_subtype = CASE "
                    "WHEN filename LIKE '%明星%' OR object_key LIKE '%明星%' OR filename LIKE '%信息流原片%' THEN '明星信息流原片' "
                    "WHEN filename LIKE '%实拍%' OR object_key LIKE '%实拍%' OR filename LIKE '%自产%' THEN '实拍自产素材' "
                    "WHEN lower(filename || ' ' || object_key) LIKE '%ai%' OR filename LIKE '%原创%' THEN 'AI原创素材' "
                    "WHEN filename LIKE '%达人%' OR object_key LIKE '%达人%' OR lower(filename || ' ' || object_key) LIKE '%kol%' THEN '达人/KOL原片' "
                    "ELSE '其他视频素材' END WHERE library_type = 'source'"
                )
            )
        business_cutover_key = "asset_library_business_cutover_20260811_v2"
        business_cutover_done = connection.execute(
            text("SELECT value FROM app_meta WHERE key = :key"), {"key": business_cutover_key}
        ).scalar()
        if not business_cutover_done:
            connection.execute(
                text(
                    "UPDATE assets SET library_type = 'remix', asset_subtype = CASE "
                    "WHEN lower(filename || ' ' || object_key) LIKE '%ai%' THEN 'AI混剪成片' "
                    "WHEN filename LIKE '%明星%' OR object_key LIKE '%明星%' THEN '明星素材混剪' "
                    "WHEN filename LIKE '%达人%' OR object_key LIKE '%达人%' "
                    "OR lower(filename || ' ' || object_key) LIKE '%kol%' "
                    "OR lower(filename || ' ' || object_key) LIKE '%koc%' THEN '达人素材混剪' "
                    "ELSE '其他混剪成片' END WHERE purged_at IS NULL"
                )
            )
            connection.execute(
                text("INSERT INTO app_meta (key, value) VALUES (:key, :value)"),
                {"key": business_cutover_key, "value": datetime.utcnow().isoformat(timespec="seconds") + "Z"},
            )
        koc_split_key = "asset_source_koc_split_20260812_v1"
        koc_split_done = connection.execute(
            text("SELECT value FROM app_meta WHERE key = :key"), {"key": koc_split_key}
        ).scalar()
        if not koc_split_done:
            connection.execute(
                text(
                    "UPDATE assets SET asset_subtype = '达人/KOC原片' "
                    "WHERE library_type = 'source' AND purged_at IS NULL "
                    "AND lower(filename || ' ' || object_key || ' ' || COALESCE(tags, '')) LIKE '%koc%'"
                )
            )
            connection.execute(
                text("INSERT INTO app_meta (key, value) VALUES (:key, :value)"),
                {"key": koc_split_key, "value": datetime.utcnow().isoformat(timespec="seconds") + "Z"},
            )
        category_cleanup_key = "asset_product_category_cleanup_20260814_v1"
        category_cleanup_done = connection.execute(
            text("SELECT value FROM app_meta WHERE key = :key"), {"key": category_cleanup_key}
        ).scalar()
        if not category_cleanup_done:
            connection.execute(
                text("UPDATE assets SET category = '燕窝面膜' WHERE category = '燕窝胜肽面膜'")
            )
            connection.execute(
                text("UPDATE assets SET category = '其他 WIS 素材' WHERE category = '清洁泥膜'")
            )
            connection.execute(
                text("INSERT INTO app_meta (key, value) VALUES (:key, :value)"),
                {"key": category_cleanup_key, "value": datetime.utcnow().isoformat(timespec="seconds") + "Z"},
            )
        delivery_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(qianchuan_deliveries)"))
        }
        delivery_additions = {
            "plan_type": "VARCHAR(30) DEFAULT ''",
            "upload_task_id": "VARCHAR(120) DEFAULT ''",
            "attempt_count": "INTEGER DEFAULT 0",
            "last_error_category": "VARCHAR(40) DEFAULT ''",
            "idempotency_key": "VARCHAR(255) DEFAULT ''",
            "delivery_entity_type": "VARCHAR(40) DEFAULT ''",
            "delivery_entity_id": "VARCHAR(120) DEFAULT ''",
            "binding_evidence": "JSON DEFAULT '{}'",
            "binding_verified_at": "DATETIME NULL",
            "related_ad_ids": "JSON DEFAULT '[]'",
            "related_creative_ids": "JSON DEFAULT '[]'",
            "metrics_link_status": "VARCHAR(30) DEFAULT 'pending'",
            "failure_stage": "VARCHAR(40) DEFAULT ''",
            "error_code": "VARCHAR(80) DEFAULT ''",
            "error_message": "TEXT DEFAULT ''",
            "error_advice": "TEXT DEFAULT ''",
            "metrics_message": "TEXT DEFAULT ''",
            "deleted_at": "DATETIME NULL",
            "deleted_by_number": "VARCHAR(80) DEFAULT ''",
            "deleted_by_name": "VARCHAR(120) DEFAULT ''",
        }
        for name, definition in delivery_additions.items():
            if name not in delivery_columns:
                connection.execute(
                    text(f"ALTER TABLE qianchuan_deliveries ADD COLUMN {name} {definition}")
                )
        adq_delivery_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(adq_deliveries)"))
        }
        if "root_material_id" not in adq_delivery_columns:
            connection.execute(
                text("ALTER TABLE adq_deliveries ADD COLUMN root_material_id VARCHAR(120) DEFAULT ''")
            )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_adq_deliveries_root_material_id "
                "ON adq_deliveries (root_material_id)"
            )
        )
        channels_delivery_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(channels_deliveries)"))
        }
        for name, definition in {
            "product_id": "VARCHAR(120) DEFAULT ''",
            "product_name": "VARCHAR(500) DEFAULT ''",
            "failure_stage": "VARCHAR(40) DEFAULT ''",
            "platform_content_id": "VARCHAR(160) DEFAULT ''",
            "platform_export_id": "VARCHAR(160) DEFAULT ''",
            "platform_export_source": "VARCHAR(40) DEFAULT ''",
            "platform_export_verified_at": "DATETIME NULL",
            "platform_content_url": "VARCHAR(2048) DEFAULT ''",
            "publish_clicked_at": "DATETIME NULL",
            "publish_client_id": "VARCHAR(80) DEFAULT ''",
            "publish_transport": "VARCHAR(30) DEFAULT ''",
            "platform_check_attempts": "INTEGER DEFAULT 0",
            "submitted_at": "DATETIME NULL",
            "view_count": "INTEGER NULL",
            "like_count": "INTEGER NULL",
            "comment_count": "INTEGER NULL",
            "share_count": "INTEGER NULL",
            "order_count": "INTEGER NULL",
            "gmv_fen": "INTEGER NULL",
            "metrics_date": "VARCHAR(10) DEFAULT ''",
            "metrics_message": "TEXT DEFAULT ''",
            "metrics_updated_at": "DATETIME NULL",
            "cover_object_key": "VARCHAR(1024) DEFAULT ''",
            "cover_filename": "VARCHAR(512) DEFAULT ''",
            "cover_status": "VARCHAR(30) DEFAULT ''",
            "cover_message": "VARCHAR(500) DEFAULT ''",
            "video_annotation": "VARCHAR(40) DEFAULT 'none'",
            "annotation_shooting_time": "VARCHAR(80) DEFAULT ''",
            "annotation_shooting_location": "VARCHAR(255) DEFAULT ''",
            "annotation_repost_source": "VARCHAR(500) DEFAULT ''",
        }.items():
            if name not in channels_delivery_columns:
                connection.execute(
                    text(f"ALTER TABLE channels_deliveries ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_channels_deliveries_platform_export_id "
                "ON channels_deliveries (platform_export_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_channels_deliveries_publish_clicked_at "
                "ON channels_deliveries (publish_clicked_at)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_channels_deliveries_publish_client_id "
                "ON channels_deliveries (publish_client_id)"
            )
        )
        account_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(channels_accounts)"))
        }
        if "external_account_id" not in account_columns:
            connection.execute(
                text("ALTER TABLE channels_accounts ADD COLUMN external_account_id VARCHAR(160) DEFAULT ''")
            )
        for name, definition in {
            "channel_cookies_ciphertext": "TEXT DEFAULT ''",
            "session_cookie_ciphertext": "TEXT DEFAULT ''",
            "cookies_updated_at": "DATETIME NULL",
            "last_verified_at": "DATETIME NULL",
        }.items():
            if name not in account_columns:
                connection.execute(text(f"ALTER TABLE channels_accounts ADD COLUMN {name} {definition}"))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_channels_accounts_external_account_id "
                "ON channels_accounts (external_account_id)"
            )
        )
        promotion_account_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(channels_promotion_accounts)"))
        }
        if "user_type" not in promotion_account_columns:
            connection.execute(
                text("ALTER TABLE channels_promotion_accounts ADD COLUMN user_type INTEGER DEFAULT 0")
            )
        promotion_order_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(channels_promotion_orders)"))
        }
        for name, definition in {
            "quote_snapshot": "JSON DEFAULT '{}'",
            "request_snapshot": "JSON DEFAULT '{}'",
            "create_attempts": "INTEGER DEFAULT 0",
            "confirmed_at": "DATETIME NULL",
        }.items():
            if name not in promotion_order_columns:
                connection.execute(
                    text(f"ALTER TABLE channels_promotion_orders ADD COLUMN {name} {definition}")
                )
        video_request_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(video_requests)"))
        }
        if "reference_images" not in video_request_columns:
            connection.execute(text("ALTER TABLE video_requests ADD COLUMN reference_images JSON DEFAULT '[]'"))
        if "reference_videos" not in video_request_columns:
            connection.execute(text("ALTER TABLE video_requests ADD COLUMN reference_videos JSON DEFAULT '[]'"))
        for name, definition in {
            "return_reason": "TEXT DEFAULT ''",
            "returned_by_number": "VARCHAR(80) DEFAULT ''",
            "returned_by_name": "VARCHAR(120) DEFAULT ''",
            "returned_at": "DATETIME NULL",
        }.items():
            if name not in video_request_columns:
                connection.execute(text(f"ALTER TABLE video_requests ADD COLUMN {name} {definition}"))
        video_request_delivery_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(video_request_deliveries)"))
        }
        if "submission_id" not in video_request_delivery_columns:
            connection.execute(text("ALTER TABLE video_request_deliveries ADD COLUMN submission_id VARCHAR(36) DEFAULT ''"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_video_request_deliveries_submission_id ON video_request_deliveries (submission_id)"))
        notification_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(user_notifications)"))
        }
        for name, definition in {
            "external_status": "VARCHAR(20) DEFAULT 'pending'",
            "external_message_id": "VARCHAR(200) DEFAULT ''",
            "external_attempts": "INTEGER DEFAULT 0",
            "external_error": "TEXT DEFAULT ''",
            "external_attempted_at": "DATETIME NULL",
            "external_sent_at": "DATETIME NULL",
        }.items():
            if name not in notification_columns:
                connection.execute(
                    text(f"ALTER TABLE user_notifications ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text(
                "UPDATE user_notifications SET external_status = COALESCE(NULLIF(external_status, ''), 'pending'), "
                "external_attempts = COALESCE(external_attempts, 0), "
                "external_error = COALESCE(external_error, '')"
            )
        )
        jianying_ticket_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(jianying_import_tickets)"))
        }
        for name, definition in {
            "status": "VARCHAR(30) DEFAULT 'waiting'",
            "progress": "INTEGER DEFAULT 0",
            "message": "TEXT DEFAULT '等待桌面助手接收'",
            "downloaded_bytes": "INTEGER DEFAULT 0",
            "total_bytes": "INTEGER DEFAULT 0",
            "speed_bps": "INTEGER DEFAULT 0",
            "eta_seconds": "INTEGER DEFAULT 0",
            "helper_version": "VARCHAR(30) DEFAULT ''",
            "cache_hit": "BOOLEAN DEFAULT 0",
            "completed_at": "DATETIME NULL",
            "updated_at": "DATETIME NULL",
        }.items():
            if name not in jianying_ticket_columns:
                connection.execute(
                    text(f"ALTER TABLE jianying_import_tickets ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text(
                "UPDATE jianying_import_tickets SET updated_at = COALESCE(updated_at, created_at), "
                "status = COALESCE(NULLIF(status, ''), 'waiting'), "
                "progress = COALESCE(progress, 0), "
                "message = COALESCE(NULLIF(message, ''), '等待桌面助手接收'), "
                "downloaded_bytes = COALESCE(downloaded_bytes, 0), "
                "total_bytes = COALESCE(total_bytes, 0), speed_bps = COALESCE(speed_bps, 0), "
                "eta_seconds = COALESCE(eta_seconds, 0), helper_version = COALESCE(helper_version, ''), "
                "cache_hit = COALESCE(cache_hit, 0)"
            )
        )
        connection.execute(
            text(
                "UPDATE qianchuan_deliveries SET "
                "message = :message, last_error_category = 'large_video_connection_interrupted' "
                "WHERE status = 'failed' "
                "AND message = :legacy_message "
                "AND asset_id IN (SELECT id FROM assets WHERE size > :large_video_bytes)"
            ),
            {
                "message": (
                    "检测到该任务的视频超过 100MB，此前连接在上传阶段中断。"
                    "新版本会按原文件零压缩传输，并在目标计划回读确认后完成；请点击“重试”。"
                    "原始错误：连接千川开放平台失败。"
                ),
                "legacy_message": "连接千川开放平台失败，请稍后重试",
                "large_video_bytes": 100 * 1024 * 1024,
            },
        )
        connection.execute(
            text(
                "UPDATE qianchuan_deliveries SET "
                "message = :message, last_error_category = 'legacy_direct_upload_interrupted' "
                "WHERE status = 'failed' "
                "AND asset_id IN (SELECT id FROM assets WHERE size > :large_video_bytes) "
                "AND (last_error_category IN ('connection_interrupted', 'large_video_connection_interrupted', "
                "'video_optimization_failed', 'video_too_large') "
                "OR message LIKE :compression_pattern OR message LIKE :optimization_pattern)"
            ),
            {
                "message": (
                    "此前大视频传输时连接中断。新版本会按原文件零压缩传输并自动查重重试，"
                    "不会压缩、转码或改变画质；请点击“重试”。"
                ),
                "large_video_bytes": 100 * 1024 * 1024,
                "compression_pattern": "%压缩%",
                "optimization_pattern": "%优化%",
            },
        )
        connection.execute(
            text(
                "UPDATE qianchuan_deliveries SET "
                "failure_stage = CASE WHEN status = 'partial' THEN 'plan_binding' ELSE 'upload' END, "
                "error_message = message, "
                "error_advice = CASE WHEN status = 'partial' "
                "THEN '素材已保留在账户素材库；请展开记录核对原因后重试。' "
                "ELSE '请展开记录查看完整错误后重试。' END "
                "WHERE status IN ('failed', 'partial') AND COALESCE(error_message, '') = ''"
            )
        )
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_deleted_at ON assets (deleted_at)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_assets_purged_at ON assets (purged_at)"))
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_qianchuan_deliveries_upload_task_id "
                "ON qianchuan_deliveries (upload_task_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_qianchuan_deliveries_idempotency_key "
                "ON qianchuan_deliveries (idempotency_key)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_qianchuan_deliveries_delivery_entity_id "
                "ON qianchuan_deliveries (delivery_entity_id)"
            )
        )
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_qianchuan_deliveries_deleted_at "
                "ON qianchuan_deliveries (deleted_at)"
            )
        )
        review_decision_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(asset_review_decisions)"))
        }
        for name, definition in {
            "quality_scores": "JSON DEFAULT '{}'",
            "quality_total": "INTEGER NULL",
            "quality_grade": "VARCHAR(30) DEFAULT ''",
            "candidate_reviewers": "JSON DEFAULT '[]'",
            "candidate_search": "TEXT DEFAULT ''",
        }.items():
            if name not in review_decision_columns:
                connection.execute(
                    text(f"ALTER TABLE asset_review_decisions ADD COLUMN {name} {definition}")
                )
        review_submission_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(asset_review_submissions)"))
        }
        for name, definition in {
            "filename_snapshot": "VARCHAR(512) DEFAULT ''",
            "naming_evidence": "JSON DEFAULT '[]'",
            "naming_check": "JSON DEFAULT '{}'",
            "route_center": "VARCHAR(120) DEFAULT ''",
            "route_group": "VARCHAR(120) DEFAULT ''",
            "assignment_mode": "VARCHAR(30) DEFAULT 'organization'",
            "designated_reviewer_number": "VARCHAR(80) DEFAULT ''",
            "designated_reviewer_name": "VARCHAR(120) DEFAULT ''",
        }.items():
            if name not in review_submission_columns:
                connection.execute(
                    text(f"ALTER TABLE asset_review_submissions ADD COLUMN {name} {definition}")
                )
        review_role_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(review_role_assignments)"))
        }
        for name, definition in {
            "center": "VARCHAR(120) DEFAULT ''",
            "group_name": "VARCHAR(120) DEFAULT ''",
            "source": "VARCHAR(40) DEFAULT 'manual'",
        }.items():
            if name not in review_role_columns:
                connection.execute(
                    text(f"ALTER TABLE review_role_assignments ADD COLUMN {name} {definition}")
                )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_review_role_assignments_center ON review_role_assignments (center)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_review_role_assignments_group_name ON review_role_assignments (group_name)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_asset_review_submissions_route_center ON asset_review_submissions (route_center)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_asset_review_submissions_route_group ON asset_review_submissions (route_group)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_asset_review_submissions_assignment_mode ON asset_review_submissions (assignment_mode)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_asset_review_submissions_designated_reviewer_number ON asset_review_submissions (designated_reviewer_number)")
        )
        connection.execute(
            text("CREATE INDEX IF NOT EXISTS ix_asset_review_submissions_designated_reviewer_name ON asset_review_submissions (designated_reviewer_name)")
        )
        review_workflow_columns = {
            row[1] for row in connection.execute(text("PRAGMA table_info(review_workflow_config)"))
        }
        for name, definition in {
            "naming_enabled": "BOOLEAN DEFAULT 0",
            "ai_redline_enabled": "BOOLEAN DEFAULT 0",
        }.items():
            if name not in review_workflow_columns:
                connection.execute(
                    text(f"ALTER TABLE review_workflow_config ADD COLUMN {name} {definition}")
                )
        review_cutover_key = "review_ai_quality_cutover_20260818_v1"
        review_cutover_done = connection.execute(
            text("SELECT value FROM app_meta WHERE key = :key"), {"key": review_cutover_key}
        ).scalar()
        if not review_cutover_done:
            connection.execute(
                text(
                    "UPDATE review_workflow_config SET required_roles = :roles, updated_at = :updated_at "
                    "WHERE id = 1"
                ),
                {
                    "roles": '["team_lead", "supervisor"]',
                    "updated_at": datetime.utcnow(),
                },
            )
            connection.execute(
                text("INSERT INTO app_meta (key, value) VALUES (:key, :value)"),
                {
                    "key": review_cutover_key,
                    "value": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                },
            )


def _meta(db: Session, key: str, fallback: str = "") -> str:
    row = db.get(AppMeta, key)
    return row.value if row else fallback


def _set_meta(db: Session, key: str, value: str) -> None:
    row = db.get(AppMeta, key)
    if row:
        row.value = value
    else:
        db.add(AppMeta(key=key, value=value))


def _source_updated_at(db: Session) -> str | None:
    return _meta(db, "oss_synced_at") or catalog_service.updated_at


_catalog_read_queue = CatalogReadQueue()
_catalog_cache_lock = Lock()
_catalog_cache: dict[str, tuple[float, object]] = {}


def _catalog_cache_get(key: str):
    with _catalog_cache_lock:
        item = _catalog_cache.get(key)
        if not item or item[0] <= time.monotonic():
            _catalog_cache.pop(key, None)
            return None
        return item[1]


def _catalog_cache_set(key: str, value: object) -> object:
    with _catalog_cache_lock:
        _catalog_cache[key] = (time.monotonic() + settings.catalog_cache_seconds, value)
    return value


def _invalidate_catalog_cache() -> None:
    with _catalog_cache_lock:
        _catalog_cache.clear()


def infer_product_image_type(filename: str) -> str:
    """Infer a stable product-image filter without changing the source filename."""
    value = filename.lower()
    if any(keyword in value for keyword in ("kv", "头图")):
        return "主视觉/KV"
    if any(keyword in value for keyword in ("无投影", "不带投影", "透明底")):
        return "无投影/透明底"
    if any(keyword in value for keyword in ("带投影", "有投影")):
        return "带投影"
    if "仰视" in value:
        return "仰视"
    if "俯视" in value:
        return "俯视"
    if any(keyword in value for keyword in ("平视", "正面")):
        return "平视/正面"
    if any(keyword in value for keyword in ("打开", "勺子", "片装")):
        return "开盒/内容物"
    if any(keyword in value for keyword in ("替换装+", "+替换装")):
        return "组合装"
    if "替换装" in value:
        return "替换装"
    if any(keyword in value for keyword in ("两盒", "5支", "5片", "10片", "24片")):
        return "组合装"
    return "其他产品图"


def bootstrap_catalog(db: Session) -> int:
    """Seed/enrich from the verified business index without removing live OSS rows."""
    reference_prefix = settings.prefix.rstrip("/") + "/references/"
    snapshot = [item for item in catalog_service.list_assets() if not item["object_key"].startswith(reference_prefix)]
    existing = {asset.object_key: asset for asset in db.scalars(select(Asset)).all()}
    for remote in snapshot:
        asset = existing.get(remote["object_key"])
        if not asset:
            if "/uploads/" in remote["object_key"] or "/product-images/" in remote["object_key"]:
                remote["filename"] = display_filename_from_object_key(remote["object_key"])
            if not remote.get("library_type") or not remote.get("asset_subtype"):
                _, remote["asset_subtype"] = infer_library_metadata(
                    "混剪成片", remote.get("filename"), remote.get("object_key"), remote.get("tags"), remote.get("content_type")
                )
                remote["library_type"] = "remix"
            db.add(Asset(**remote))
            continue
        if not asset.cover_url and remote.get("cover_url"):
            asset.cover_url = remote["cover_url"]
        if not asset.source and remote.get("source"):
            asset.source = remote["source"]
        if not asset.account_name and remote.get("account_name"):
            asset.account_name = remote["account_name"]
        if asset.filename == Path(asset.object_key).name and remote.get("filename"):
            asset.filename = (
                display_filename_from_object_key(asset.object_key)
                if "/uploads/" in asset.object_key or "/product-images/" in asset.object_key
                else remote["filename"]
            )
    repair_upload_asset_filenames(db)
    db.commit()
    _invalidate_catalog_cache()
    return len(snapshot)


_sync_lock = Lock()
_sync_state: dict = {
    "state": "idle",
    "processed": 0,
    "total": 0,
    "started_at": None,
    "completed_at": None,
    "error": "",
}


def _sync_snapshot() -> dict:
    with _sync_lock:
        return dict(_sync_state)


def _set_sync_state(**values) -> None:
    with _sync_lock:
        _sync_state.update(values)


def _merge_oss_assets(db: Session, remote_assets: list[dict], scan_started: datetime) -> int:
    reference_prefix = settings.prefix.rstrip("/") + "/references/"
    remote_assets = [item for item in remote_assets if not item["object_key"].startswith(reference_prefix)]
    snapshot = {
        item["object_key"]: item
        for item in catalog_service.list_assets()
        if not item["object_key"].startswith(reference_prefix)
    }
    existing = {asset.object_key: asset for asset in db.scalars(select(Asset)).all()}
    upload_sessions = {
        session.object_key: session.filename
        for session in db.scalars(
            select(UploadSession).where(UploadSession.object_key.in_([item["object_key"] for item in remote_assets]))
        ).all()
    }
    remote_keys = {item["object_key"] for item in remote_assets}

    for remote in remote_assets:
        key = remote["object_key"]
        indexed = snapshot.get(key)
        asset = existing.get(key)
        parts = [part for part in key.split("/") if part]
        is_product_image = len(parts) >= 3 and parts[1] == "product-images"
        is_upload = len(parts) >= 3 and parts[1] in {"uploads", "product-images"}
        if indexed:
            if is_upload:
                library_type, asset_subtype = infer_library_metadata(
                    indexed.get("filename"), key, indexed.get("tags"), indexed.get("content_type")
                )
            else:
                library_type = "remix"
                _, asset_subtype = infer_library_metadata(
                    "混剪成片", indexed.get("filename"), key, indexed.get("tags"), indexed.get("content_type")
                )
            incoming = {
                **remote,
                "filename": indexed["filename"],
                "cover_url": indexed.get("cover_url", ""),
                "source": indexed.get("source", ""),
                "account_name": indexed.get("account_name", ""),
                "ingest_source": "oa_upload" if is_upload else "oss_scan",
                "asset_scope": "product_image" if is_product_image else "marketing_video",
                "category": canonical_product_category(indexed["category"]),
                "content_type": indexed["content_type"],
                "status": indexed["status"],
                "tags": indexed["tags"],
                "library_type": library_type,
                "asset_subtype": asset_subtype,
            }
        else:
            title = display_filename_from_object_key(key) if is_upload else remote["filename"]
            remote = {**remote, "filename": title}
            if is_product_image:
                library_type, asset_subtype = "source", "产品图片"
            elif is_upload:
                library_type, asset_subtype = infer_library_metadata(title, key)
            else:
                library_type = "remix"
                _, asset_subtype = infer_library_metadata("混剪成片", title, key)
            cover_key = str(Path(key).with_suffix(".jpg")).replace("\\", "/")
            incoming = {
                **remote,
                "category": canonical_product_category(
                    parts[2] if is_product_image and len(parts) >= 3 else infer_category(title, default="待分类")
                ),
                "content_type": infer_product_image_type(title) if is_product_image else infer_content_type(title),
                "status": "待整理",
                "tags": ["上传素材"] if is_upload else [],
                "cover_url": oss_service.url_for(cover_key) if remote["media_type"] == "video" and cover_key in remote_keys else "",
                "source": "oa_upload" if is_upload else "oss",
                "account_name": parts[3] if is_product_image and len(parts) >= 4 else parts[2] if is_upload and len(parts) >= 3 else "",
                "ingest_source": "oa_upload" if is_upload else "oss_scan",
                "asset_scope": "product_image" if is_product_image else "marketing_video",
                "library_type": library_type,
                "asset_subtype": asset_subtype,
            }

        if is_upload:
            current_filename = normalize_upload_filename(asset.filename) if asset else ""
            storage_basename = Path(key).name
            storage_name_exposed = current_filename == storage_basename or current_filename != display_filename_from_object_key(current_filename)
            if asset and not storage_name_exposed:
                incoming["filename"] = current_filename
            else:
                incoming["filename"] = normalize_upload_filename(
                    upload_sessions.get(key) or display_filename_from_object_key(key)
                )

        if asset:
            if asset.purged_at is not None:
                continue
            for field in ("filename", "media_type", "size", "etag", "modified_at", "cover_url", "source", "account_name", "ingest_source"):
                if field == "cover_url" and not incoming[field] and asset.cover_url:
                    continue
                setattr(asset, field, incoming[field])
        else:
            db.add(Asset(**incoming))

    for key, asset in existing.items():
        if key in remote_keys:
            continue
        if asset.deleted_at is not None or asset.purged_at is not None:
            continue
        uploaded_during_scan = asset.ingest_source == "oa_upload" and asset.modified_at >= scan_started
        if not uploaded_during_scan:
            db.delete(asset)

    repair_upload_asset_filenames(db)
    completed = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    _set_meta(db, "oss_synced_at", completed)
    _set_meta(db, "oss_object_count", str(len(remote_assets)))
    db.commit()
    _invalidate_catalog_cache()
    return len(remote_assets)


def repair_upload_asset_filenames(db: Session) -> int:
    """Idempotently repair names damaged by historical OSS scans.

    UploadSession.filename is authoritative when available.  Older direct
    uploads can still be repaired without guessing because the original name
    is the suffix of the managed object key.
    """
    managed_patterns = (
        settings.prefix.rstrip("/") + "/uploads/%",
        settings.prefix.rstrip("/") + "/product-images/%",
    )
    assets = db.scalars(
        select(Asset).where(
            Asset.purged_at.is_(None),
            or_(Asset.object_key.like(managed_patterns[0]), Asset.object_key.like(managed_patterns[1])),
        )
    ).all()
    sessions = {
        session.object_key: session.filename
        for session in db.scalars(
            select(UploadSession).where(UploadSession.object_key.in_([asset.object_key for asset in assets]))
        ).all()
    }
    repaired = 0
    for asset in assets:
        current = normalize_upload_filename(asset.filename)
        storage_basename = Path(asset.object_key).name
        exposed_storage_name = current == storage_basename or current != display_filename_from_object_key(current)
        preferred = current
        if exposed_storage_name:
            preferred = normalize_upload_filename(
                sessions.get(asset.object_key) or display_filename_from_object_key(asset.object_key)
            )
        if preferred != asset.filename:
            asset.filename = preferred
            repaired += 1
    if repaired:
        _set_meta(db, "asset_filename_repair_last_count", str(repaired))
        _set_meta(db, "asset_filename_repair_last_at", datetime.utcnow().isoformat(timespec="seconds") + "Z")
    return repaired


def run_oss_sync() -> None:
    scan_started = datetime.utcnow()

    def progress(count: int) -> None:
        _set_sync_state(processed=count)

    try:
        rows = oss_service.list_assets(progress=progress)
        with SessionLocal() as db:
            total = _merge_oss_assets(db, rows, scan_started)
        completed = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _set_sync_state(state="completed", processed=total, total=total, completed_at=completed, error="")
    except Exception:
        _set_sync_state(state="failed", completed_at=datetime.utcnow().isoformat(timespec="seconds") + "Z", error="OSS 扫描失败，请稍后重试")


def start_oss_sync(background_tasks: BackgroundTasks) -> dict:
    if not oss_service.configured:
        raise HTTPException(503, "OSS 尚未配置")
    with _sync_lock:
        if _sync_state["state"] == "running":
            return dict(_sync_state)
        started = datetime.utcnow().isoformat(timespec="seconds") + "Z"
        _sync_state.update(state="running", processed=0, total=0, started_at=started, completed_at=None, error="")
        state = dict(_sync_state)
    background_tasks.add_task(run_oss_sync)
    return state


_trash_cleanup_lock = Lock()
_trash_cleanup_meta_key = "trash_cleanup_last_run"


def _audit_asset(
    db: Session,
    asset: Asset,
    action: str,
    *,
    actor_number: str = "system",
    actor_name: str = "系统任务",
    detail: str = "",
    created_at: datetime | None = None,
) -> None:
    db.add(
        AssetAuditLog(
            asset_id=asset.id,
            object_key=asset.object_key,
            action=action,
            actor_number=actor_number[:80],
            actor_name=actor_name[:120],
            detail=detail,
            created_at=created_at or datetime.utcnow(),
        )
    )


# Recovery holds preserve historical facts without resuming old side effects.
_RECOVERY_ASSET_HOLD_KEYS = {
    "visibility": "recovery.asset_hold.visibility.v1",
    "push": "recovery.asset_hold.push.v1",
    "purge": "recovery.asset_hold.purge.v1",
}


def _recovery_asset_hold_ids(db: Session, purpose: str) -> set[int]:
    raw = _meta(db, _RECOVERY_ASSET_HOLD_KEYS[purpose], "[]")
    try:
        values = json.loads(raw)
        if not isinstance(values, list) or any(type(value) is not int or value <= 0 for value in values):
            raise ValueError("invalid recovery asset IDs")
        return set(values)
    except (ValueError, TypeError):
        raise HTTPException(503, "素材恢复保护记录待核验，请联系维护人") from None


def _recovery_asset_allowed(db: Session, column, purpose: str):
    # json_each uses one bound JSON value; even a large archive cannot exceed
    # SQLite's per-statement variable limit or leak held IDs into a SQL string.
    ids = sorted(_recovery_asset_hold_ids(db, purpose))
    entries = func.json_each(json.dumps(ids)).table_valued("value")
    return column.not_in(select(entries.c.value))


def _recovery_require_asset_push(db: Session, asset_id: int) -> None:
    if asset_id in _recovery_asset_hold_ids(db, "push"):
        raise HTTPException(409, "该素材正在恢复核验中，暂不能推送；历史状态和回执已保留")


def purge_deleted_assets(db: Session, now: datetime | None = None) -> dict:
    """Delete expired trash objects from OSS while retaining their database audit tombstones."""
    current = now or datetime.utcnow()
    cutoff = current - timedelta(days=settings.trash_retention_days)
    rows = db.scalars(
        select(Asset).where(
            Asset.deleted_at.is_not(None),
            Asset.deleted_at <= cutoff,
            Asset.purged_at.is_(None),
            _recovery_asset_allowed(db, Asset.id, "purge"),
        )
    ).all()
    purged = 0
    failed = 0
    for asset in rows:
        try:
            oss_service.delete_asset(asset.object_key)
            asset.purged_at = current
            asset.purge_error = ""
            _audit_asset(
                db,
                asset,
                "purge",
                detail=f"已在回收站满 {settings.trash_retention_days} 天，OSS 原文件永久删除",
                created_at=current,
            )
            purged += 1
        except Exception as error:
            asset.purge_error = str(error)[:1000]
            _audit_asset(db, asset, "purge_failed", detail=asset.purge_error, created_at=current)
            failed += 1
    db.commit()
    if purged:
        _invalidate_catalog_cache()
    return {"checked": len(rows), "purged": purged, "failed": failed}


def run_trash_cleanup_if_due(force: bool = False, now: datetime | None = None) -> dict:
    if not _trash_cleanup_lock.acquire(blocking=False):
        return {"status": "running", "checked": 0, "purged": 0, "failed": 0}
    try:
        current = now or datetime.utcnow()
        with SessionLocal() as db:
            last_raw = _meta(db, _trash_cleanup_meta_key)
            last_run: datetime | None = None
            if last_raw:
                with suppress(ValueError):
                    last_run = datetime.fromisoformat(last_raw.removesuffix("Z"))
            due_at = (last_run + timedelta(days=settings.trash_cleanup_interval_days)) if last_run else None
            if not force and due_at and current < due_at:
                return {
                    "status": "waiting",
                    "checked": 0,
                    "purged": 0,
                    "failed": 0,
                    "next_run_at": due_at.isoformat(timespec="seconds") + "Z",
                }
            result = purge_deleted_assets(db, current)
            _set_meta(db, _trash_cleanup_meta_key, current.isoformat(timespec="seconds") + "Z")
            db.commit()
            return {"status": "completed", **result}
    finally:
        _trash_cleanup_lock.release()


async def trash_cleanup_loop() -> None:
    while True:
        try:
            await asyncio.to_thread(run_trash_cleanup_if_due)
        except Exception as error:
            with SessionLocal() as db:
                _set_meta(db, "trash_cleanup_last_error", str(error)[:1000])
                db.commit()
        await asyncio.sleep(60 * 60)


async def qianchuan_delivery_loop() -> None:
    """Resume queued deliveries through several controlled lanes without duplicate uploads."""
    running: set[asyncio.Task] = set()
    while True:
        try:
            completed = {task for task in running if task.done()}
            running.difference_update(completed)
            for task in completed:
                with suppress(Exception):
                    task.result()

            available = max(0, _QIANCHUAN_WORKER_LIMIT - len(running))
            claimed = await asyncio.to_thread(claim_qianchuan_tasks, available) if available else []
            for task_id in claimed:
                running.add(asyncio.create_task(asyncio.to_thread(run_qianchuan_push, [task_id])))
        except Exception as error:
            with SessionLocal() as db:
                _set_meta(db, "qianchuan.delivery_loop_last_error", str(error)[:1000])
                db.commit()
        await asyncio.sleep(0.8 if running else 2)


async def adq_delivery_loop() -> None:
    """Run Tencent ADQ jobs in bounded lanes and resume safely after restarts."""
    running: set[asyncio.Task] = set()
    while True:
        try:
            completed = {task for task in running if task.done()}
            running.difference_update(completed)
            for task in completed:
                with suppress(Exception):
                    task.result()
            available = max(0, _ADQ_WORKER_LIMIT - len(running))
            claimed = await asyncio.to_thread(claim_adq_tasks, available) if available else []
            for task_id in claimed:
                running.add(asyncio.create_task(asyncio.to_thread(run_adq_push, task_id)))
        except Exception as error:
            with SessionLocal() as db:
                _set_meta(db, "adq.delivery_loop_last_error", str(error)[:1000])
                db.commit()
        await asyncio.sleep(0.8 if running else 2)


async def channels_delivery_loop() -> None:
    """Run a small number of accounts concurrently; each account stays serialized."""
    running: set[asyncio.Task] = set()
    while True:
        try:
            completed = {task for task in running if task.done()}
            running.difference_update(completed)
            for task in completed:
                with suppress(Exception):
                    task.result()
            available = max(0, settings.channels_worker_limit - len(running))
            claimed = await asyncio.to_thread(claim_channels_tasks, available) if available else []
            for task_id in claimed:
                running.add(asyncio.create_task(asyncio.to_thread(run_channels_push, task_id)))
        except Exception as error:
            with SessionLocal() as db:
                _set_meta(db, "channels.delivery_loop_last_error", str(error)[:1000])
                db.commit()
        await asyncio.sleep(1 if running else 3)


async def channels_metrics_loop() -> None:
    """Confirm accepted posts frequently and collect one dated snapshot per day."""
    await asyncio.sleep(180)
    while True:
        try:
            await asyncio.to_thread(run_channels_confirmation_if_due)
            await asyncio.to_thread(run_channels_daily_metrics_if_due)
        except Exception:
            logger.exception("Video Channels readback loop failed")
        await asyncio.sleep(60)


async def personal_sales_refresh_loop() -> None:
    """Refresh the previous complete day while preserving the last successful snapshot."""
    await asyncio.sleep(settings.personal_sales_startup_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(personal_sales_service.refresh_if_due)
        except Exception:
            logger.exception("Personal sales refresh loop failed")
        await asyncio.sleep(settings.personal_sales_retry_interval_minutes * 60)


async def business_intelligence_refresh_loop() -> None:
    """Prepare the previous complete day without blocking application startup."""
    await asyncio.sleep(settings.business_intelligence_startup_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(business_intelligence_service.refresh_if_due)
        except Exception:
            logger.exception("Business intelligence refresh loop failed")
        await asyncio.sleep(settings.business_intelligence_retry_interval_minutes * 60)


def run_creative_incentive_refresh() -> None:
    with SessionLocal() as db:
        directions = db.scalars(select(CreativeIncentiveDirection)).all()
        if not directions:
            return
        snapshot = business_intelligence_service.get_overview(
            business_intelligence_service.expected_target_date(),
            7,
        )
        sync_directions(db, list(directions), snapshot)
        deliver_pending_milestones(db)


async def creative_incentive_refresh_loop() -> None:
    """Create idempotent 20k milestones and drain the optional Feishu outbox."""
    await asyncio.sleep(90)
    while True:
        try:
            await asyncio.to_thread(run_creative_incentive_refresh)
        except Exception:
            logger.exception("Creative incentive scheduled refresh failed")
        await asyncio.sleep(15 * 60)


async def long_term_work_refresh_loop() -> None:
    """Run the durable in-platform Feishu refresh lane once per Shanghai day."""
    await asyncio.sleep(20)
    while True:
        try:
            await asyncio.to_thread(long_term_work_service.run_if_due)
        except Exception:
            logger.exception("Long-term work refresh loop failed")
        await asyncio.sleep(60)


def run_feishu_notification_batch(limit: int = 12) -> int:
    """Drain the external notification outbox without blocking request actions."""
    if os.getenv("SERVICE_NOTIFICATION_DELIVERY_MODE") == "external":
        return 0
    cutoff = datetime.utcnow() - timedelta(minutes=5)
    with SessionLocal() as db:
        rows = db.scalars(
            select(UserNotification).where(
                UserNotification.kind == "video_request",
                UserNotification.external_status.in_({"pending", "failed"}),
                UserNotification.external_attempts < 5,
                or_(
                    UserNotification.external_attempted_at.is_(None),
                    UserNotification.external_attempted_at <= cutoff,
                ),
            ).order_by(UserNotification.created_at).limit(limit)
        ).all()
        ids = [item.id for item in rows]
        for item in rows:
            item.external_status = "sending"
            item.external_attempts = int(item.external_attempts or 0) + 1
            item.external_attempted_at = datetime.utcnow()
        if rows:
            db.commit()

    for notification_id in ids:
        with SessionLocal() as db:
            item = db.get(UserNotification, notification_id)
            if not item:
                continue
            result = feishu_notification_service.send(
                recipient_number=item.recipient_number,
                recipient_name=item.recipient_name,
                title=item.title,
                message=item.message,
            )
            item.external_status = result.status
            item.external_error = result.error
            if result.status == "sent":
                item.external_sent_at = datetime.utcnow()
            db.commit()
    return len(ids)


async def feishu_notification_loop() -> None:
    await asyncio.sleep(3)
    while True:
        try:
            processed = await asyncio.to_thread(run_feishu_notification_batch)
        except Exception:
            logger.exception("Feishu notification outbox loop failed")
            processed = 0
        await asyncio.sleep(2 if processed else 30)


def claim_review_ai_tasks(limit: int = 1) -> list[int]:
    """Claim durable AI-review rows; processing rows are requeued at startup."""
    if limit <= 0:
        return []
    with SessionLocal() as db:
        if not _review_workflow_config(db).ai_redline_enabled:
            return []
        rows = db.scalars(
            select(AssetReviewAiResult)
            .where(AssetReviewAiResult.status == "pending")
            .order_by(AssetReviewAiResult.created_at)
            .limit(limit)
        ).all()
        now = datetime.utcnow()
        for row in rows:
            row.status = "processing"
            row.started_at = now
            row.completed_at = None
            row.error_message = ""
            row.updated_at = now
        if rows:
            db.commit()
        return [row.id for row in rows]


def run_review_ai_task(result_id: int) -> None:
    with SessionLocal() as db:
        ai_result = db.get(AssetReviewAiResult, result_id)
        if not ai_result or ai_result.status != "processing":
            return
        if not _review_workflow_config(db).ai_redline_enabled:
            ai_result.status = "disabled"
            ai_result.summary = "AI审核建议已关闭，不影响人工审核或推送门禁。"
            ai_result.error_message = ""
            ai_result.completed_at = datetime.utcnow()
            ai_result.updated_at = datetime.utcnow()
            db.commit()
            return
        submission = db.get(AssetReviewSubmission, ai_result.submission_id)
        asset = db.get(Asset, submission.asset_id) if submission else None
        if not submission or not asset or asset.deleted_at is not None or asset.purged_at is not None:
            ai_result.status = "error"
            ai_result.error_message = "提审素材已不存在或不可读取"
            ai_result.completed_at = datetime.utcnow()
            ai_result.updated_at = datetime.utcnow()
            db.commit()
            return
        video_url = oss_service.url_for(asset.object_key)
        configured_rules, rule_version = _review_ai_rule_snapshot(db)

    try:
        analysis = review_ai_service.analyze(video_url)
        raw_results = analysis.get("results")
        if not isinstance(raw_results, list) or not raw_results:
            raise RuntimeError("视频理解完成，但没有返回可审核的分段证据")
        evaluation = evaluate_redlines(raw_results, configured_rules)
    except Exception as error:
        with SessionLocal() as db:
            ai_result = db.get(AssetReviewAiResult, result_id)
            if not ai_result or ai_result.status != "processing":
                return
            ai_result.status = "error"
            ai_result.summary = "AI识别未完成，请审核人直接人工判断；不影响人工审核。"
            ai_result.error_message = str(error)[:1000]
            ai_result.completed_at = datetime.utcnow()
            ai_result.updated_at = datetime.utcnow()
            db.commit()
        return

    with SessionLocal() as db:
        ai_result = db.get(AssetReviewAiResult, result_id)
        if not ai_result or ai_result.status != "processing":
            return
        ai_result.status = evaluation["status"]
        ai_result.provider = "cutter_rules"
        ai_result.task_id = str(analysis.get("task_id") or "")[:160]
        ai_result.rule_version = rule_version
        ai_result.summary = evaluation["summary"]
        ai_result.category_counts = evaluation["category_counts"]
        ai_result.findings = evaluation["findings"]
        ai_result.segments = evaluation["segments"]
        ai_result.error_message = ""
        ai_result.completed_at = datetime.utcnow()
        ai_result.updated_at = datetime.utcnow()
        db.commit()


async def review_ai_loop() -> None:
    running: set[asyncio.Task] = set()
    while True:
        try:
            completed = {task for task in running if task.done()}
            running.difference_update(completed)
            for task in completed:
                with suppress(Exception):
                    task.result()
            if not running:
                claimed = await asyncio.to_thread(claim_review_ai_tasks, 1)
                for result_id in claimed:
                    running.add(asyncio.create_task(asyncio.to_thread(run_review_ai_task, result_id)))
        except Exception:
            logger.exception("Review AI worker failed")
        await asyncio.sleep(1 if running else 3)


_cover_retry_after: dict[int, float] = {}


def claim_cover_assets(limit: int) -> list[int]:
    """Prioritise recent missing covers while backing off unsupported media."""
    if limit <= 0 or not oss_service.configured:
        return []
    now = time.monotonic()
    with SessionLocal() as db:
        candidates = list(db.scalars(
            select(Asset.id)
            .where(
                Asset.media_type == "video",
                Asset.cover_url == "",
                Asset.deleted_at.is_(None),
                Asset.purged_at.is_(None),
            )
            .order_by(Asset.modified_at.desc())
            .limit(1000)
        ).all())
    return [asset_id for asset_id in candidates if _cover_retry_after.get(asset_id, 0) <= now][:limit]


def generate_asset_cover(asset_id: int) -> bool:
    """Create one cached 9:16 JPEG without changing the source video."""
    with SessionLocal() as db:
        asset = db.get(Asset, asset_id)
        if (
            not asset
            or asset.cover_url
            or asset.media_type != "video"
            or asset.deleted_at is not None
            or asset.purged_at is not None
        ):
            return False
        object_key = asset.object_key
        etag = (asset.etag or "noetag").replace('"', "")[:16]

    input_url = oss_service.url_for(object_key, expires_in=1800)
    if not input_url:
        _cover_retry_after[asset_id] = time.monotonic() + 3600
        return False
    cover_key = f"{settings.prefix.rstrip('/')}/covers/{asset_id}-{etag}.jpg"
    try:
        with tempfile.TemporaryDirectory(prefix="wis-cover-") as temp_dir:
            output_path = str(Path(temp_dir) / "cover.jpg")
            completed = None
            for seek_seconds in (1, 0):
                command = [
                    "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                    "-ss", str(seek_seconds), "-i", input_url,
                    "-frames:v", "1",
                    "-vf", "scale=480:854:force_original_aspect_ratio=decrease,pad=480:854:(ow-iw)/2:(oh-ih)/2:color=0x0b1820",
                    "-q:v", "5", output_path,
                ]
                completed = subprocess.run(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=120)
                if completed.returncode == 0 and Path(output_path).is_file() and Path(output_path).stat().st_size > 0:
                    break
            if not completed or completed.returncode != 0 or not Path(output_path).is_file():
                raise RuntimeError("ffmpeg could not decode a cover frame")
            oss_service.upload_file(output_path, cover_key, "image/jpeg")
        cover_url = oss_service.url_for(cover_key)
        with SessionLocal() as db:
            asset = db.get(Asset, asset_id)
            if asset and not asset.cover_url and asset.deleted_at is None and asset.purged_at is None:
                asset.cover_url = cover_url
                db.commit()
                _invalidate_catalog_cache()
        _cover_retry_after.pop(asset_id, None)
        return True
    except Exception:
        logger.warning("Cover generation deferred asset_id=%s", asset_id)
        _cover_retry_after[asset_id] = time.monotonic() + 3600
        return False


async def cover_generation_loop() -> None:
    """Generate derivatives in a low-priority bounded lane."""
    await asyncio.sleep(30)
    running: set[asyncio.Task] = set()
    while True:
        completed = {task for task in running if task.done()}
        running.difference_update(completed)
        for task in completed:
            with suppress(Exception):
                task.result()
        available = max(0, settings.cover_worker_limit - len(running))
        claimed = await asyncio.to_thread(claim_cover_assets, available) if available else []
        for asset_id in claimed:
            running.add(asyncio.create_task(asyncio.to_thread(generate_asset_cover, asset_id)))
        await asyncio.sleep(1 if running else 5)


@asynccontextmanager
async def business_lifespan(_: FastAPI):
    if os.getenv("CLOUD_MIGRATION_VALIDATION", "") == "1":
        logger.warning("Migration validation mode: background jobs and startup database writes are disabled")
        yield
        return
    ensure_asset_schema()
    with SessionLocal() as db:
        repaired_names = repair_upload_asset_filenames(db)
        if repaired_names:
            db.commit()
            logger.info("Repaired %s uploaded asset display filenames", repaired_names)
        _seed_environment_access_grants(db)
        _seed_protected_admin_grants(db)
        synced_review_people = _sync_review_organization_assignments(db)
        if synced_review_people:
            logger.info("Synchronized %s organization-scoped review assignments", synced_review_people)
        stalled = db.scalars(
            select(QianchuanDelivery).where(
                or_(
                    QianchuanDelivery.status.in_({"uploading", "binding"}),
                    (
                        (QianchuanDelivery.status == "uploaded")
                        & (QianchuanDelivery.plan_id != "")
                    ),
                )
            )
        ).all()
        for task in stalled:
            task.status = "pending"
            task.message = "服务恢复后已重新进入安全推送队列"
            task.updated_at = datetime.utcnow()
        if stalled:
            db.commit()
        adq_stalled = db.scalars(
            select(AdqDelivery).where(AdqDelivery.status.in_({"uploading", "binding"}))
        ).all()
        for task in adq_stalled:
            task.status = "pending"
            task.message = "服务恢复后已重新进入 ADQ 安全推送队列"
            task.updated_at = datetime.utcnow()
        if adq_stalled:
            db.commit()
        channels_stalled = db.scalars(
            select(ChannelsDelivery).where(ChannelsDelivery.status == "publishing")
        ).all()
        for task in channels_stalled:
            may_have_submitted = bool(
                task.publish_clicked_at
                or task.submitted_at
                or task.platform_content_id
                or task.platform_export_id
                or task.failure_stage in {"submit_publish", "confirm_publish"}
            )
            task.status = "submitted" if may_have_submitted else "pending"
            task.failure_stage = "confirm_publish" if may_have_submitted else ""
            task.submitted_at = task.submitted_at or (task.publish_clicked_at if may_have_submitted else None)
            task.message = (
                "服务恢复后发现已进入平台发布边界，已锁定重复上传并转作品回查"
                if may_have_submitted
                else "服务恢复后已重新进入视频号安全发布队列"
            )
            task.updated_at = datetime.utcnow()
        if channels_stalled:
            db.commit()
        notification_requeued = db.scalars(
            select(UserNotification).where(UserNotification.external_status == "sending")
        ).all()
        for notification in notification_requeued:
            notification.external_status = "pending"
        if notification_requeued:
            db.commit()
        review_ai_requeued = db.scalars(
            select(AssetReviewAiResult).where(AssetReviewAiResult.status == "processing")
        ).all()
        ai_review_enabled = bool(_review_workflow_config(db).ai_redline_enabled)
        for ai_result in review_ai_requeued:
            ai_result.status = "pending" if ai_review_enabled else "disabled"
            ai_result.summary = (
                ai_result.summary
                if ai_review_enabled
                else "AI审核建议已关闭，不影响人工审核或推送门禁。"
            )
            ai_result.error_message = "服务重启后已重新进入AI建议识别队列" if ai_review_enabled else ""
            ai_result.completed_at = None if ai_review_enabled else datetime.utcnow()
            ai_result.updated_at = datetime.utcnow()
        if review_ai_requeued:
            db.commit()
        if catalog_service.configured:
            try:
                bootstrap_catalog(db)
            except Exception:
                db.rollback()
    try:
        await asyncio.to_thread(ai_insight_service.refresh)
    except Exception:
        logger.exception("AI insight initial refresh failed")
    cleanup_task = asyncio.create_task(trash_cleanup_loop())
    delivery_task = asyncio.create_task(qianchuan_delivery_loop())
    metrics_task = asyncio.create_task(qianchuan_metrics_loop())
    adq_delivery_task = asyncio.create_task(adq_delivery_loop())
    adq_metrics_task = asyncio.create_task(adq_metrics_loop())
    channels_delivery_task = asyncio.create_task(channels_delivery_loop())
    channels_metrics_task = asyncio.create_task(channels_metrics_loop())
    personal_sales_task = asyncio.create_task(personal_sales_refresh_loop())
    business_intelligence_task = asyncio.create_task(business_intelligence_refresh_loop())
    creative_incentive_task = asyncio.create_task(creative_incentive_refresh_loop())
    long_term_work_task = asyncio.create_task(long_term_work_refresh_loop())
    feishu_notification_task = asyncio.create_task(feishu_notification_loop())
    review_ai_task = asyncio.create_task(review_ai_loop())
    cover_task = asyncio.create_task(cover_generation_loop())
    ai_insight_task = asyncio.create_task(ai_insight_loop())
    try:
        yield
    finally:
        cleanup_task.cancel()
        delivery_task.cancel()
        metrics_task.cancel()
        adq_delivery_task.cancel()
        adq_metrics_task.cancel()
        channels_delivery_task.cancel()
        channels_metrics_task.cancel()
        personal_sales_task.cancel()
        business_intelligence_task.cancel()
        creative_incentive_task.cancel()
        long_term_work_task.cancel()
        feishu_notification_task.cancel()
        review_ai_task.cancel()
        cover_task.cancel()
        ai_insight_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task
        with suppress(asyncio.CancelledError):
            await delivery_task
        with suppress(asyncio.CancelledError):
            await metrics_task
        with suppress(asyncio.CancelledError):
            await adq_delivery_task
        with suppress(asyncio.CancelledError):
            await adq_metrics_task
        with suppress(asyncio.CancelledError):
            await channels_delivery_task
        with suppress(asyncio.CancelledError):
            await channels_metrics_task
        with suppress(asyncio.CancelledError):
            await personal_sales_task
        with suppress(asyncio.CancelledError):
            await business_intelligence_task
        with suppress(asyncio.CancelledError):
            await creative_incentive_task
        with suppress(asyncio.CancelledError):
            await long_term_work_task
        await asyncio.to_thread(long_term_work_service.wait_for_active_run)
        with suppress(asyncio.CancelledError):
            await feishu_notification_task
        with suppress(asyncio.CancelledError):
            await review_ai_task
        with suppress(asyncio.CancelledError):
            await cover_task
        with suppress(asyncio.CancelledError):
            await ai_insight_task


@asynccontextmanager
async def lifespan(application: FastAPI):
    async with business_lifespan(application):
        from .sqlite_read_lifetime import sqlite_read_lifetime
        with sqlite_read_lifetime(engine.url.database if engine.url.get_backend_name() == "sqlite" else None):
            yield

app = FastAPI(title="WIS 营销素材中心 API", version="0.4.0", lifespan=lifespan)


@app.middleware("http")
async def migration_validation_read_only(request: Request, call_next):
    if os.getenv("CLOUD_MIGRATION_VALIDATION", "") == "1" and request.method not in {"GET", "HEAD", "OPTIONS"}:
        auth_preview = os.getenv("CLOUD_MIGRATION_AUTH_PREVIEW", "") == "1" and request.method == "POST" and request.url.path in {
            "/api/auth/login", "/api/auth/captcha", "/api/auth/logout",
        }
        if not auth_preview:
            return JSONResponse(status_code=423, content={"error": "migration_validation_read_only"})
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.cors_origins),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _operation_module(path: str) -> str:
    routes = (
        ("/api/qianchuan", "千川推送"),
        ("/api/adq", "腾讯ADQ推送"),
        ("/api/channels", "视频号主页推送"),
        ("/api/reviews", "视频审核"),
        ("/api/video-requests", "视频中心提需"),
        ("/api/notifications", "通知中心"),
        ("/api/jianying", "剪映互传"),
        ("/api/uploads", "素材上传"),
        ("/api/muse", "MUSE视频拆解"),
        ("/api/trash", "回收站"),
        ("/api/assets", "素材库"),
        ("/api/sync", "OSS同步"),
        ("/api/admin/access", "登录权限"),
        ("/api/admin/admin", "管理员权限"),
        ("/api/creative-incentives", "素材实时激励"),
        ("/api/business-intelligence", "经营智能"),
        ("/api/assistant", "AI中枢管家"),
    )
    return next((name for prefix, name in routes if path.startswith(prefix)), "系统")


_INTERNAL_UPLOAD_AUDIT_PATHS = frozenset({
    "/api/uploads/part-leases/acquire",
    "/api/uploads/part-leases/renew",
    "/api/uploads/part-leases/release",
})
_MULTIPART_PARTS_PATH = re.compile(r"^/api/uploads/multipart/sessions/[^/]+/parts$")
_MULTIPART_COMPLETE_PATH = re.compile(r"^/api/uploads/multipart/sessions/[^/]+/complete$")
_MULTIPART_SESSION_PATH = re.compile(r"^/api/uploads/multipart/sessions/[^/]+$")


def _is_operation_audit_noise(path: str, status_code: int) -> bool:
    """Hide transport chatter while preserving a failed multipart merge for diagnosis."""
    if path in _INTERNAL_UPLOAD_AUDIT_PATHS or _MULTIPART_PARTS_PATH.fullmatch(path):
        return True
    return bool(_MULTIPART_COMPLETE_PATH.fullmatch(path) and status_code < 400)


def _operation_action(method: str, path: str, result: str = "success") -> str:
    if path == "/api/reviews/config" and method == "PUT":
        return "审核流程保存失败" if result == "failed" else "保存审核流程"
    if path == "/api/reviews/roles" and method == "POST":
        return "角色配置失败" if result == "failed" else "配置审核角色"
    if path.endswith("/submit") and path.startswith("/api/reviews/assets/"):
        return "提交审核失败" if result == "failed" else "提交视频审核"
    if path.endswith("/approve") and path.startswith("/api/reviews/submissions/"):
        return "审核失败" if result == "failed" else "审核通过"
    if path.endswith("/reject") and path.startswith("/api/reviews/submissions/"):
        return "驳回失败" if result == "failed" else "驳回修改"
    if path in {"/api/uploads/presign", "/api/uploads/multipart/sessions"}:
        return "上传失败" if result == "failed" else "开始上传"
    if path in {"/api/uploads/complete", "/api/jianying/device/uploads/complete"}:
        return "上传失败" if result == "failed" else "上传完成"
    if path == "/api/jianying/device/uploads/presign":
        return "上传失败" if result == "failed" else "开始上传"
    if _MULTIPART_COMPLETE_PATH.fullmatch(path):
        return "上传失败" if result == "failed" else "分片合并完成"
    if method == "DELETE" and _MULTIPART_SESSION_PATH.fullmatch(path):
        return "取消上传"
    segments = [segment for segment in path.split("/") if segment]
    return f"{method} {segments[-1] if segments else 'root'}"[:80]


def _visible_operation_log_filters():
    """SQL filters shared by count, rows and module facets for historical audit noise."""
    multipart_parts = "/api/uploads/multipart/sessions/%/parts"
    multipart_complete = "/api/uploads/multipart/sessions/%/complete"
    return (
        ~OperationLog.path.in_(tuple(_INTERNAL_UPLOAD_AUDIT_PATHS)),
        ~OperationLog.path.like(multipart_parts),
        or_(~OperationLog.path.like(multipart_complete), OperationLog.result == "failed"),
    )


@app.middleware("http")
async def operation_audit_middleware(request: Request, call_next):
    response = await call_next(request)
    # Migration preview is not the business writer. Its auth probes and blocked
    # POSTs must not take operation_log IDs that belong to the old live writer.
    if os.getenv("CLOUD_MIGRATION_VALIDATION", "") == "1":
        return response
    path = request.url.path
    result = "success" if response.status_code < 400 else "failed"
    should_log = (
        request.method in {"POST", "PUT", "PATCH", "DELETE"}
        and not path.endswith("/captcha")
        and not _is_operation_audit_noise(path, response.status_code)
    )
    actor = getattr(request.state, "user", None)
    if should_log and isinstance(actor, dict):
        try:
            segments = [segment for segment in path.split("/") if segment]
            with SessionLocal() as db:
                db.add(OperationLog(
                    actor_number=str(actor.get("number") or actor.get("userId") or "")[:80],
                    actor_name=str(actor.get("realName") or actor.get("name") or "")[:120],
                    department=str(actor.get("groupName") or actor.get("department") or "")[:255],
                    module=_operation_module(path),
                    action=_operation_action(request.method, path, result),
                    method=request.method,
                    path=path[:512],
                    result=result,
                    status_code=response.status_code,
                    resource_type=segments[-2][:50] if len(segments) > 1 else "",
                    resource_id=segments[-1][:180] if segments else "",
                    detail="仅记录操作结果，不保存请求正文、密码、令牌或 Cookie",
                ))
                db.commit()
        except Exception:
            logger.exception("Could not persist operation audit log")
    return response


_CENTRAL_AUTH_EXEMPT_PATHS = {
    # The hub gateway uses this to establish identity for every module. The
    # endpoint still requires OA authentication; module grants belong to the
    # requested business API, not the shared identity lookup.
    "/api/auth/me",
    "/api/auth/captcha",
    "/api/auth/login",
    "/api/auth/logout",
    # The provider returns without a hub session; exchange_code validates the
    # short-lived OAuth state before persisting any new authorization.
    "/api/qianchuan/oauth/callback",
    # Tencent ADQ redirects outside the hub session too. The callback checks
    # its short-lived authorization state before storing an operator token.
    "/api/adq/user-authorization/callback",
    "/api/live",
    "/api/health",
    # The desktop helper exchanges a short-lived, single-use pairing code for
    # its own device token.  Requiring an OA browser cookie here makes every
    # helper claim fail with "未登录" before the pairing code is evaluated.
    "/api/jianying/pairings/claim",
}
_CENTRAL_AUTH_EXEMPT_PREFIXES = (
    "/api/admin/access-grants",
    "/api/admin/admin-grants",
    "/api/admin/permission-managers",
    "/api/admin/module-access",
    "/api/admin/workspace-profiles",
    "/api/central-auth/",
    "/api/assistant/",
    "/api/business-intelligence/",
    "/api/workstation/",
    # Device endpoints are not public: every route under this prefix validates
    # the independently issued WIS Jianying bearer token.  They must bypass the
    # OA/module gate so the desktop process can reconnect after the browser is
    # closed or Windows restarts.
    "/api/jianying/device/",
)


def _central_auth_exempt(path: str) -> bool:
    return path in _CENTRAL_AUTH_EXEMPT_PATHS or any(
        path.startswith(prefix) for prefix in _CENTRAL_AUTH_EXEMPT_PREFIXES
    )


@app.middleware("http")
async def cloud_manager_module_access_middleware(request: Request, call_next):
    """Block every business API when the central scope excludes Cloud Manager."""
    path = request.url.path
    is_business_api = path.startswith("/api/")
    is_exempt = _central_auth_exempt(path)
    if is_business_api and not is_exempt:
        try:
            # Permission checks may open SQLite connections or contact OA. Keep
            # them off the event loop so a busy catalog cannot freeze QR polling
            # and even liveness. This preserves both checks; it is not a bypass.
            user = await asyncio.to_thread(require_user, request)
            # Material incentives is an independently configurable business
            # module. Its users need no additional Cloud Manager grant.
            required_module = (
                "material-incentive"
                if path == "/api/creative-incentives" or path.startswith("/api/creative-incentives/")
                else "cloud-manager"
            )
            await asyncio.to_thread(require_module_access, user, required_module)
        except HTTPException as error:
            return JSONResponse(status_code=error.status_code, content={"detail": error.detail})
    return await call_next(request)


def source_name() -> str:
    return "公司 OSS · yxb/（实时对象）"


def user_number(user: dict) -> str:
    value = str(user.get("number") or user.get("userId") or user.get("id") or "").strip()
    if not value:
        raise HTTPException(403, "OA 账号缺少稳定工号，无法保存个人数据")
    return value[:80]


def user_name(user: dict) -> str:
    return str(user.get("realName") or user.get("name") or user_number(user))[:120]


EFFECTIVE_MARK_ALLOWED_CENTERS = ("品牌创意中心", "视频中心")


def can_mark_effective_asset(user: dict) -> bool:
    """Allow manual effective-material decisions only to the two owning centers."""
    organization = " ".join(
        str(user.get(field) or "").strip()
        for field in ("groupName", "parentDept", "deptName", "department", "center")
        if str(user.get(field) or "").strip()
    )
    return any(center in organization for center in EFFECTIVE_MARK_ALLOWED_CENTERS)


def require_effective_asset_marker(user: dict) -> None:
    if not can_mark_effective_asset(user):
        raise HTTPException(403, "仅品牌创意中心和视频中心同事可标记有效一创素材")


def is_asset_admin(user: dict) -> bool:
    if is_super_admin(user):
        return True
    number = user_number(user)
    if number in settings.asset_admin_numbers:
        return True
    role_text = " ".join(
        str(user.get(field) or "")
        for field in ("deptJobName", "jobName", "title", "roleName", "groupName")
    ).lower()
    return any(keyword in role_text for keyword in ("管理员", "负责人", "主管", "经理", "总监", "leader", "admin", "owner"))


ASSET_DELETE_MANAGER_NAMES = {"何雨庭"}


def is_asset_delete_manager(user: dict) -> bool:
    """Allow named asset stewards to perform recoverable deletion only."""
    allowed_names = {name.casefold() for name in ASSET_DELETE_MANAGER_NAMES}
    allowed_names.update({
        value.strip().casefold()
        for value in os.getenv("ASSET_DELETE_MANAGER_NAMES", "何雨庭").split(",")
        if value.strip()
    })
    return user_name(user).strip().casefold() in allowed_names


def is_super_admin(user: dict) -> bool:
    protected_names = {
        value.strip().casefold()
        for value in os.getenv("SUPER_ADMIN_NAMES", "舒豪,吴为").split(",")
        if value.strip()
    }
    allowed = {
        value.strip().upper()
        for value in os.getenv("SUPER_ADMIN_NUMBERS", "").split(",")
        if value.strip()
    }
    return user_name(user).strip().casefold() in protected_names or user_number(user).upper() in allowed


def is_operation_admin(user: dict) -> bool:
    if is_super_admin(user):
        return True
    number = user_number(user)
    name = user_name(user)
    identifiers = [_grant_identifier(number, "number"), _grant_identifier(name)]
    with SessionLocal() as db:
        return bool(db.scalar(select(func.count()).select_from(AdminGrant).where(
            AdminGrant.identifier.in_(identifiers),
            AdminGrant.active.is_(True),
            AdminGrant.role.in_(("operation_admin", "permission_manager", "super_admin")),
        )))


def is_permission_manager(user: dict) -> bool:
    if is_super_admin(user):
        return True
    number = user_number(user)
    name = user_name(user)
    identifiers = [_grant_identifier(number, "number"), _grant_identifier(name)]
    with SessionLocal() as db:
        return bool(db.scalar(select(func.count()).select_from(AdminGrant).where(
            AdminGrant.identifier.in_(identifiers),
            AdminGrant.active.is_(True),
            AdminGrant.role.in_(("permission_manager", "super_admin")),
        )))


def _review_workflow_config(db: Session) -> ReviewWorkflowConfig:
    config = db.get(ReviewWorkflowConfig, 1)
    if not config:
        config = ReviewWorkflowConfig(
            id=1,
            enabled=False,
            naming_enabled=False,
            ai_redline_enabled=False,
            required_roles=list(REVIEW_STAGE_ORDER),
        )
        db.add(config)
        db.flush()
    return config


REVIEW_AI_RULE_VERSION_KEY = "review_ai_rule_version"
REVIEW_AI_ADVISORY_RELAXATION_KEY = "review_ai_advisory_relaxation_20260901_v1"


def _review_ai_rule_version(db: Session) -> int:
    row = db.get(AppMeta, REVIEW_AI_RULE_VERSION_KEY)
    if not row:
        row = AppMeta(key=REVIEW_AI_RULE_VERSION_KEY, value="1")
        db.add(row)
        db.flush()
    try:
        return max(1, int(row.value or "1"))
    except ValueError:
        row.value = "1"
        return 1


def _review_ai_rule_out(row: ReviewAiRule) -> dict:
    return {
        "code": row.code,
        "category": row.category,
        "severity": row.severity,
        "title": row.title,
        "pattern": row.pattern,
        "enabled": bool(row.enabled),
        "sort_order": int(row.sort_order or 0),
        "updated_by_name": row.updated_by_name,
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _migrate_review_ai_results_to_advisory(db: Session, now: datetime) -> None:
    """Remove legacy AI-only rejections without changing human decisions."""
    legacy_rows = db.scalars(
        select(AssetReviewAiResult).where(AssetReviewAiResult.status == "rejected")
    ).all()
    for ai_result in legacy_rows:
        ai_result.status = "warning"
        ai_result.summary = (
            "历史AI高风险结果已改为人工审核建议；最终是否通过由审核人决定。"
        )
        ai_result.updated_at = now
        submission = db.get(AssetReviewSubmission, ai_result.submission_id)
        if not submission or submission.status != "rejected":
            continue
        has_human_rejection = db.scalar(
            select(func.count())
            .select_from(AssetReviewDecision)
            .where(
                AssetReviewDecision.submission_id == submission.id,
                AssetReviewDecision.status == "rejected",
            )
        ) or 0
        if not has_human_rejection:
            submission.status = "pending"
            submission.completed_at = None


def _review_ai_rules(db: Session) -> list[ReviewAiRule]:
    rows = db.scalars(
        select(ReviewAiRule).order_by(ReviewAiRule.sort_order, ReviewAiRule.id)
    ).all()
    if rows:
        migration = db.get(AppMeta, REVIEW_AI_ADVISORY_RELAXATION_KEY)
        if not migration:
            existing_codes = {row.code for row in rows}
            now = datetime.utcnow()
            next_sort_order = max((int(row.sort_order or 0) for row in rows), default=-1) + 1
            for rule in (item for item in DEFAULT_RULES if item.code.startswith("relax-")):
                if rule.code in existing_codes:
                    continue
                db.add(ReviewAiRule(
                    code=rule.code,
                    category=rule.category,
                    severity=rule.severity,
                    title=rule.title,
                    pattern=rule.pattern.pattern,
                    enabled=True,
                    sort_order=next_sort_order,
                    created_at=now,
                    updated_at=now,
                ))
                next_sort_order += 1
            current_version = _review_ai_rule_version(db)
            version_row = db.get(AppMeta, REVIEW_AI_RULE_VERSION_KEY)
            version_row.value = str(current_version + 1)
            db.add(AppMeta(
                key=REVIEW_AI_ADVISORY_RELAXATION_KEY,
                value=now.isoformat() + "Z",
            ))
            _migrate_review_ai_results_to_advisory(db, now)
            db.commit()
            rows = db.scalars(
                select(ReviewAiRule).order_by(ReviewAiRule.sort_order, ReviewAiRule.id)
            ).all()
        return list(rows)
    now = datetime.utcnow()
    for index, rule in enumerate(DEFAULT_RULES):
        db.add(ReviewAiRule(
            code=rule.code,
            category=rule.category,
            severity=rule.severity,
            title=rule.title,
            pattern=rule.pattern.pattern,
            enabled=True,
            sort_order=index,
            created_at=now,
            updated_at=now,
        ))
    _review_ai_rule_version(db)
    db.add(AppMeta(
        key=REVIEW_AI_ADVISORY_RELAXATION_KEY,
        value=now.isoformat() + "Z",
    ))
    _migrate_review_ai_results_to_advisory(db, now)
    db.commit()
    return list(db.scalars(
        select(ReviewAiRule).order_by(ReviewAiRule.sort_order, ReviewAiRule.id)
    ).all())


def _review_ai_rule_snapshot(db: Session) -> tuple[list[dict], str]:
    rules = [_review_ai_rule_out(row) for row in _review_ai_rules(db)]
    return rules, f"wis-redline-v{_review_ai_rule_version(db)}"


def _review_user_roles(db: Session, user: dict) -> list[str]:
    number = user_number(user).strip().upper()
    name = user_name(user).strip().casefold()
    rows = db.scalars(select(ReviewRoleAssignment).where(ReviewRoleAssignment.active.is_(True))).all()
    return [
        row.role_code
        for row in rows
        if (row.user_number and row.user_number.strip().upper() == number)
        or (row.user_name and row.user_name.strip().casefold() == name)
    ]


def user_permissions(user: dict) -> dict:
    from .private_assets import private_access_enabled
    super_admin = is_super_admin(user)
    permission_manager = super_admin or is_permission_manager(user)
    with SessionLocal() as db:
        reviewer_roles = _review_user_roles(db, user)
    return {
        "private_assets": private_access_enabled(user_number(user)),
        "asset_admin": is_asset_admin(user),
        "asset_delete_manager": is_asset_delete_manager(user),
        "super_admin": super_admin,
        "operation_admin": permission_manager or is_operation_admin(user),
        "manage_permissions": permission_manager,
        "video_request_assigner": is_video_request_assigner(user),
        "video_request_supervisor_viewer": super_admin or is_video_request_assigner(user),
        "review_config_admin": super_admin,
        "can_mark_effective": can_mark_effective_asset(user),
        "reviewer_roles": reviewer_roles,
    }


VIDEO_REQUEST_CORE_ASSIGNER_NAMES = {"何雨庭", "赵佳乐"}
VIDEO_REQUEST_CORE_ASSIGNER_NUMBERS = {"FD-026222"}
VIDEO_REQUEST_ASSIGNEE_ROSTER_PATH = Path(__file__).with_name("video_request_roster.json")
VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NUMBERS = {"FD-117110"}
VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NAMES = {"赵佳乐"}


def is_video_request_assigner(user: dict) -> bool:
    allowed_names = {
        value.casefold()
        for value in VIDEO_REQUEST_CORE_ASSIGNER_NAMES
    }
    allowed_names.update({
        value.strip().casefold()
        for value in os.getenv("VIDEO_REQUEST_ASSIGNER_NAMES", "何雨庭").split(",")
        if value.strip()
    })
    allowed_numbers = VIDEO_REQUEST_CORE_ASSIGNER_NUMBERS | {
        value.strip().upper()
        for value in os.getenv("VIDEO_REQUEST_ASSIGNER_NUMBERS", "").split(",")
        if value.strip()
    }
    return (
        user_name(user).strip().casefold() in allowed_names
        or user_number(user).strip().upper() in allowed_numbers
    )


def require_video_request_assigner(user: dict) -> None:
    if not is_video_request_assigner(user):
        raise HTTPException(403, "仅视频中心授权主管可分配制作人")


def require_super_admin(user: dict) -> None:
    if not is_super_admin(user):
        raise HTTPException(403, "仅超级管理员可管理登录白名单")


def require_permission_manager(user: dict) -> None:
    if not is_permission_manager(user):
        raise HTTPException(403, "仅已开通权限管理权限的成员可以配置权限")


def require_operation_admin(user: dict) -> None:
    if not is_operation_admin(user):
        raise HTTPException(403, "仅管理员可查看操作日志")


def _review_latest_submission(db: Session, asset_id: int) -> AssetReviewSubmission | None:
    return db.scalar(
        select(AssetReviewSubmission)
        .where(AssetReviewSubmission.asset_id == asset_id)
        .order_by(AssetReviewSubmission.version.desc())
        .limit(1)
    )


_REVIEW_NAMING_FILE_SUFFIXES = {
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".flv",
    ".jpg", ".jpeg", ".png", ".webp", ".gif",
}


def _review_naming_normalize(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", str(value or "").strip())
    suffix = Path(cleaned).suffix.casefold()
    if suffix in _REVIEW_NAMING_FILE_SUFFIXES:
        cleaned = cleaned[: -len(suffix)]
    return "".join(char.casefold() for char in cleaned if char.isalnum())


def _review_naming_item_requires_name(item: dict) -> bool:
    category = str(item.get("category") or "")
    usage = str(item.get("usage") or "")
    position = str(item.get("position") or "")
    if category in {"face", "mechanism"}:
        return usage == "complete" or position == "opening"
    if category in {"product_display", "ai_first_creation"}:
        return position == "opening"
    return False


def _review_naming_evaluate(
    asset: Asset,
    evidence: list[dict] | None,
    *,
    no_applicable_sources: bool = False,
) -> dict:
    items = [dict(item) for item in (evidence or [])]
    base = {
        "version": REVIEW_NAMING_RULE_VERSION,
        "document_url": REVIEW_NAMING_DOC_URL,
        "filename": asset.filename,
        "no_applicable_sources": bool(no_applicable_sources),
        "evidence_count": len(items),
        "required_names": [],
        "missing_names": [],
        "pending_categories": ["明星素材"],
    }
    if asset.library_type != "remix":
        return {
            **base,
            "status": "not_applicable",
            "message": "源素材不适用混剪成片命名规范。",
        }
    if items and no_applicable_sources:
        return {
            **base,
            "status": "failed",
            "message": "已填写来源素材时，不能同时选择“未使用前四类素材”。",
        }
    if not items:
        if no_applicable_sources:
            return {
                **base,
                "status": "passed",
                "message": "已确认该混剪未使用当前生效的前四类命名素材；明星素材章节待确认，暂不拦截。",
            }
        return {
            **base,
            "status": "failed",
            "message": "请补充混剪使用的素材名称、类别、使用方式和出现位置，或确认未使用前四类素材。",
        }

    invalid: list[str] = []
    required_names: list[str] = []
    for index, item in enumerate(items, start=1):
        item_invalid = False
        category = str(item.get("category") or "")
        material_name = str(item.get("material_name") or "").strip()
        usage = str(item.get("usage") or "")
        position = str(item.get("position") or "")
        if category not in REVIEW_NAMING_CATEGORY_LABELS:
            invalid.append(f"第 {index} 条素材类别无效")
            item_invalid = True
        if not _review_naming_normalize(material_name):
            invalid.append(f"第 {index} 条素材名称为空")
            item_invalid = True
        if usage not in {"complete", "clip"}:
            invalid.append(f"第 {index} 条使用方式无效")
            item_invalid = True
        if position not in {"opening", "middle", "ending"}:
            invalid.append(f"第 {index} 条出现位置无效")
            item_invalid = True
        if not item_invalid and _review_naming_item_requires_name(item):
            required_names.append(material_name)
    if invalid:
        return {
            **base,
            "status": "failed",
            "message": "；".join(invalid[:5]),
        }

    required_names = list(dict.fromkeys(required_names))
    normalized_filename = _review_naming_normalize(asset.filename)
    missing_names = [
        name for name in required_names
        if _review_naming_normalize(name) not in normalized_filename
    ]
    if missing_names:
        return {
            **base,
            "status": "failed",
            "message": f"素材名称缺少：{'、'.join(missing_names)}。请改名后重新提审。",
            "required_names": required_names,
            "missing_names": missing_names,
        }
    return {
        **base,
        "status": "passed",
        "message": (
            f"命名规范已通过；文件名已包含 {len(required_names)} 个必填素材名称。"
            if required_names
            else "命名规范已通过；本次使用方式和位置未触发前四章的素材名写入要求。"
        ),
        "required_names": required_names,
        "missing_names": [],
    }


def _review_naming_disabled(asset: Asset) -> dict:
    return {
        "version": REVIEW_NAMING_RULE_VERSION,
        "document_url": REVIEW_NAMING_DOC_URL,
        "filename": asset.filename,
        "status": "disabled",
        "message": "命名规范审核当前已关闭，不参与提交或推送拦截。",
        "no_applicable_sources": False,
        "evidence_count": 0,
        "required_names": [],
        "missing_names": [],
        "pending_categories": ["明星素材"],
    }


def _review_naming_suggestions(db: Session, asset: Asset) -> list[dict]:
    record = db.scalar(
        select(WorkstationReturn)
        .where(WorkstationReturn.asset_id == asset.id, WorkstationReturn.status == "completed")
        .order_by(WorkstationReturn.completed_at.desc(), WorkstationReturn.created_at.desc())
        .limit(1)
    )
    if not record:
        return []
    source_ids = [
        int(value) for value in (record.provenance or {}).get("source_asset_ids") or []
        if str(value).isdigit() and int(value) > 0
    ]
    if not source_ids:
        return []
    source_assets = db.scalars(select(Asset).where(Asset.id.in_(source_ids))).all()
    source_by_id = {item.id: item for item in source_assets}
    return [
        {
            "source_asset_id": source_id,
            "material_name": source_by_id[source_id].filename,
            "asset_subtype": source_by_id[source_id].asset_subtype,
        }
        for source_id in source_ids
        if source_id in source_by_id
    ]


def _review_latest_summaries(db: Session, asset_ids: list[int]) -> dict[int, dict]:
    """Return the latest review state for a bounded asset page without N+1 queries."""
    if not asset_ids:
        return {}
    latest_versions = (
        select(
            AssetReviewSubmission.asset_id.label("asset_id"),
            func.max(AssetReviewSubmission.version).label("latest_version"),
        )
        .where(AssetReviewSubmission.asset_id.in_(asset_ids))
        .group_by(AssetReviewSubmission.asset_id)
        .subquery("latest_asset_review_versions")
    )
    submissions = db.scalars(
        select(AssetReviewSubmission)
        .join(
            latest_versions,
            and_(
                latest_versions.c.asset_id == AssetReviewSubmission.asset_id,
                latest_versions.c.latest_version == AssetReviewSubmission.version,
            ),
        )
    ).all()
    rejected_notes = {
        submission_id: note or ""
        for submission_id, note in db.execute(
            select(AssetReviewDecision.submission_id, AssetReviewDecision.note).where(
                AssetReviewDecision.submission_id.in_([item.id for item in submissions]),
                AssetReviewDecision.status == "rejected",
            )
        ).all()
    }
    return {
        item.asset_id: {
            "status": item.status,
            "version": item.version,
            "note": (
                rejected_notes.get(item.id, "")
                or str((item.naming_check or {}).get("message") or "")
                if item.status == "rejected"
                else ""
            ),
            "updated_at": item.completed_at or item.submitted_at,
        }
        for item in submissions
    }


def _require_assets_review_approved(db: Session, assets: list[Asset]) -> None:
    config = _review_workflow_config(db)
    naming_blocked: list[str] = []
    review_blocked: list[str] = []
    for asset in assets:
        _recovery_require_asset_push(db, asset.id)
        latest = _review_latest_submission(db, asset.id)
        trusted_workstation_review = bool(
            latest
            and latest.assignment_mode == "automatic_workstation"
            and asset.source == "wis_remix_workstation"
            and "自动混剪" in asset.filename
            and (latest.naming_check or {}).get("status") == "passed"
        )
        if config.naming_enabled and asset.library_type == "remix":
            naming = (
                dict(latest.naming_check or {})
                if trusted_workstation_review
                else _review_naming_evaluate(
                    asset,
                    latest.naming_evidence if latest else [],
                    no_applicable_sources=bool((latest.naming_check or {}).get("no_applicable_sources")) if latest else False,
                )
            )
            if not latest or naming["status"] != "passed" or latest.filename_snapshot != asset.filename:
                reason = naming["message"] if latest else "尚未提交命名规范审核"
                naming_blocked.append(f"{asset.filename}（{reason}）")
        direct_review_required = bool(latest and latest.assignment_mode == "designated")
        if (config.enabled or direct_review_required) and (not latest or latest.status != "approved"):
            review_blocked.append(asset.filename)
    if naming_blocked:
        names = "、".join(naming_blocked[:3])
        suffix = f"等 {len(naming_blocked)} 条" if len(naming_blocked) > 3 else ""
        raise HTTPException(409, f"混剪素材命名规范未通过，暂不能推送：{names}{suffix}")
    if review_blocked:
        names = "、".join(review_blocked[:3])
        suffix = f"等 {len(review_blocked)} 条" if len(review_blocked) > 3 else ""
        raise HTTPException(409, f"素材尚未完成全部审核，暂不能推送上线：{names}{suffix}")


def _grant_identifier(value: str, identifier_type: str = "name") -> str:
    cleaned = value.strip()
    if identifier_type == "number":
        return f"number:{cleaned.upper()}"
    return f"name:{cleaned.casefold()}"


MODULE_CATALOG = (
    {"key": "data-dashboard", "label": "根数据看板", "purpose": "经营情况总览"},
    {"key": "material-incentive", "label": "素材实时激励", "purpose": "原创素材激励"},
    {"key": "creative-hub", "label": "创意中枢平台", "purpose": "创意来源"},
    {"key": "creative-radar", "label": "创意雷达", "purpose": "创意情报与素材洞察"},
    {"key": "ai-first-creation", "label": "AI一创工作台", "purpose": "一创创作"},
    {"key": "material-workbench", "label": "WIS素材工作台", "purpose": "二创混剪"},
    {"key": "cloud-manager", "label": "云管家项目", "purpose": "素材存储与推送回流"},
    {"key": "live-room-management", "label": "直播间管理", "purpose": "直播间"},
    {"key": "workflow-engine", "label": "流程引擎", "purpose": "流程与我的任务"},
)
MODULE_KEYS = tuple(item["key"] for item in MODULE_CATALOG)

ORGANIZATION_DIRECTOR_NAMES = {"赵佳乐", "舒豪"}
ORGANIZATION_LEADER_CENTERS = {
    "吴为": ("AI营销中心",),
    "宋睿凛": ("营销中心A", "品牌营销中心"),
    "彭聪": ("营销中心B",),
    "曾业高": ("营销中心C",),
    "练美好": ("营销中心D",),
    "张鑫露": ("营销中心J",),
    "李雨橦": ("品牌创意中心",),
    "何雨庭": ("视频中心",),
    "刘慧迅": ("直播中心",),
}
ORGANIZATION_CENTERS = tuple(dict.fromkeys(
    center
    for centers in ORGANIZATION_LEADER_CENTERS.values()
    for center in centers
))
REVIEW_ORGANIZATION_CENTERS = (
    "AI营销中心",
    "营销中心A",
    "营销中心B",
    "营销中心C",
    "营销中心D",
    "营销中心J",
)
REVIEW_ORGANIZATION_ROSTER_PATH = Path(__file__).with_name("review_organization_roster.json")
REVIEW_ORGANIZATION_ROSTER = json.loads(REVIEW_ORGANIZATION_ROSTER_PATH.read_text(encoding="utf-8"))


def _normalized_person_name(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().casefold()


def _canonical_organization_center(value: str) -> str:
    text_value = str(value or "").strip()
    return next((center for center in ORGANIZATION_CENTERS if center in text_value), "")


def _organization_dashboard_scope(user: dict) -> dict:
    # The existing “最高权限” screen grants permission_manager. Its business
    # view is department-wide; protected super-admin operations remain separate.
    if is_permission_manager(user):
        return {"scope": "department", "department": "品牌营销部",
                "centers": list(ORGANIZATION_CENTERS), "personName": user_name(user).strip()}
    from .workspace_roles import configured_scope
    configured = configured_scope(user)
    if configured:
        return configured
    name = user_name(user).strip()
    department, raw_oa_center = _organization_from_user(user)
    # Owner-approved business-only exception (2026-09-04, 舒豪). Keep the
    # colleague's OA identity intact; do not add any AdminGrant or roster role.
    organization_parts = re.split(r"[\s/·—-]+", " ".join(
        str(user.get(field) or "")
        for field in ("department", "parentDept", "deptName", "groupName", "center")
    ))
    if name == "丁小恬" and "总经办" in organization_parts:
        return {
            "scope": "department",
            "department": "品牌营销部",
            "centers": list(ORGANIZATION_CENTERS),
            "personName": name,
        }
    leader_centers = list(ORGANIZATION_LEADER_CENTERS.get(name, ()))
    if name in ORGANIZATION_DIRECTOR_NAMES:
        scope = "department"
        centers = list(ORGANIZATION_CENTERS)
    elif leader_centers:
        scope = "center"
        centers = leader_centers
    elif is_permission_manager(user):
        scope = "department"
        centers = list(ORGANIZATION_CENTERS)
    else:
        scope = "personal"
        centers = []
    return {
        "scope": scope,
        "department": department or "品牌营销部",
        "centers": centers,
        "personName": name,
    }


def _organization_members(db: Session, user: dict) -> list[dict]:
    access_rows = db.scalars(
        select(OaAccessGrant)
        .where(OaAccessGrant.active.is_(True))
        .order_by(OaAccessGrant.real_name, OaAccessGrant.user_number)
    ).all()
    module_rows = db.scalars(select(ModuleAccessGrant)).all()
    module_by_identifier = {row.identifier: row for row in module_rows}
    module_by_number = {
        row.user_number.strip().upper(): row
        for row in module_rows
        if row.user_number.strip()
    }
    module_by_name = {
        _normalized_person_name(row.real_name): row
        for row in module_rows
        if row.real_name.strip()
    }
    try:
        roster = json.loads(PEOPLE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        roster = []
    roster_by_number = {
        str(item.get("employeeId") or "").strip().upper(): item
        for item in roster
        if str(item.get("employeeId") or "").strip()
    }
    roster_by_name = {
        _normalized_person_name(item.get("name", "")): item
        for item in roster
        if str(item.get("name") or "").strip()
    }
    members: dict[str, dict] = {}
    for row in access_rows:
        module = (
            module_by_identifier.get(row.identifier)
            or module_by_number.get(row.user_number.strip().upper())
            or module_by_name.get(_normalized_person_name(row.real_name))
        )
        real_name = (row.real_name or (module.real_name if module else "")).strip()
        number = (row.user_number or (module.user_number if module else "")).strip()
        raw_department = (row.department or (module.department if module else "")).strip()
        raw_center = (row.center or (module.center if module else "")).strip()
        roster_item = roster_by_number.get(number.upper()) or roster_by_name.get(_normalized_person_name(real_name)) or {}
        roster_organization = str(roster_item.get("department") or "").strip()
        center = _canonical_organization_center(raw_center) or _canonical_organization_center(roster_organization)
        leader_centers = ORGANIZATION_LEADER_CENTERS.get(real_name, ())
        if leader_centers and center not in leader_centers:
            center = leader_centers[0]
        department = "品牌营销部" if "品牌营销" in f"{raw_department} {raw_center} {roster_organization}" or center else raw_department
        organization_text = f"{department} {center} {raw_center} {roster_organization}"
        if not real_name or not (
            "品牌营销" in organization_text
            or center in ORGANIZATION_CENTERS
            or real_name in ORGANIZATION_DIRECTOR_NAMES
            or real_name in ORGANIZATION_LEADER_CENTERS
        ):
            continue
        key = number.upper() or _normalized_person_name(real_name)
        members[key] = {
            "personName": real_name,
            "employeeId": number,
            "department": department or "品牌营销部",
            "center": center,
            "isLeader": real_name in ORGANIZATION_LEADER_CENTERS,
            "leaderCenters": list(ORGANIZATION_LEADER_CENTERS.get(real_name, ())),
        }

    # The governed creator roster is also an organization directory source. A
    # colleague must not disappear from a supervisor's center merely because an
    # OA access row has not yet been enriched with department metadata.
    for roster_item in roster:
        real_name = str(roster_item.get("name") or "").strip()
        number = str(roster_item.get("employeeId") or "").strip()
        center = _canonical_organization_center(roster_item.get("department", ""))
        if not real_name or not center:
            continue
        key = number.upper() or _normalized_person_name(real_name)
        existing = members.get(key, {})
        leader_centers = ORGANIZATION_LEADER_CENTERS.get(real_name, ())
        if leader_centers and center not in leader_centers:
            center = leader_centers[0]
        members[key] = {
            "personName": real_name,
            "employeeId": number,
            "department": existing.get("department") or "品牌营销部",
            "center": center,
            "isLeader": real_name in ORGANIZATION_LEADER_CENTERS,
            "leaderCenters": list(leader_centers),
        }

    current_name = user_name(user).strip()
    current_number = user_number(user).strip()
    current_department, raw_current_center = _organization_from_user(user)
    current_center = _canonical_organization_center(raw_current_center)
    current_leader_centers = ORGANIZATION_LEADER_CENTERS.get(current_name, ())
    if current_leader_centers and current_center not in current_leader_centers:
        current_center = current_leader_centers[0]
    current_key = current_number.upper() or _normalized_person_name(current_name)
    current = members.get(current_key, {})
    members[current_key] = {
        "personName": current_name,
        "employeeId": current_number,
        "department": current_department or current.get("department") or "品牌营销部",
        "center": current_center or current.get("center") or "",
        "isLeader": current_name in ORGANIZATION_LEADER_CENTERS,
        "leaderCenters": list(ORGANIZATION_LEADER_CENTERS.get(current_name, ())),
    }
    from .workspace_roles import apply_member_profiles
    return apply_member_profiles(db, list(members.values()))


def _scoped_organization_members(db: Session, user: dict) -> tuple[dict, list[dict]]:
    access = _organization_dashboard_scope(user)
    members = _organization_members(db, user)
    if access["scope"] == "center":
        allowed_centers = set(access["centers"])
        members = [
            item for item in members
            if item["center"] in allowed_centers
            or bool(allowed_centers.intersection(item["leaderCenters"]))
        ]
    elif access["scope"] == "personal":
        members = [item for item in members
                   if personal_identity_match(item, user_number(user), access["personName"])]
    members.sort(key=lambda item: (item["center"] or "未分中心", not item["isLeader"], item["personName"]))
    return access, members


def _review_assignment_identifier(number: str, name: str) -> str:
    return _grant_identifier(number, "number") if number.strip() else _grant_identifier(name)


def _sync_review_organization_assignments(db: Session) -> int:
    """Mirror the source whiteboard's group leads and center owners into review roles."""
    people = _organization_members(db, {"realName": "舒豪", "number": "FD-026222"})
    people_by_name = {
        str(person.get("personName") or "").strip(): person
        for person in people
        if str(person.get("personName") or "").strip()
    }
    access_rows = db.scalars(select(OaAccessGrant).where(OaAccessGrant.active.is_(True))).all()
    access_by_name = {row.real_name.strip(): row for row in access_rows if row.real_name.strip()}
    desired: dict[tuple[str, str], dict] = {}

    def add_assignment(role_code: str, name: str, center: str, group_name: str = "") -> None:
        if not name or center not in REVIEW_ORGANIZATION_CENTERS:
            return
        access = access_by_name.get(name)
        person = people_by_name.get(name, {})
        number = str(
            (access.user_number if access else "")
            or person.get("employeeId")
            or ""
        ).strip().upper()
        identifier = _review_assignment_identifier(number, name)
        desired[(role_code, identifier)] = {
            "role_code": role_code,
            "identifier": identifier,
            "user_name": name,
            "user_number": number,
            "department": f"{center}/{group_name}" if group_name else center,
            "center": center,
            "group_name": group_name,
        }

    for center_item in REVIEW_ORGANIZATION_ROSTER.get("centers", []):
        center = str(center_item.get("center") or "").strip()
        supervisor_names = list(center_item.get("supervisors", []))
        if center_item.get("supervisor"):
            supervisor_names.append(center_item.get("supervisor"))
        for supervisor_name in supervisor_names:
            add_assignment("supervisor", str(supervisor_name or "").strip(), center)
        for team_lead_name in center_item.get("team_leads", []):
            add_assignment("team_lead", str(team_lead_name or "").strip(), center)
        for group in center_item.get("groups", []):
            group_name = str(group.get("group_name") or "").strip()
            add_assignment("team_lead", str(group.get("team_lead") or "").strip(), center, group_name)
            for member_name in group.get("members", []):
                add_assignment("member", str(member_name or "").strip(), center, group_name)

    existing = {
        (row.role_code, row.identifier): row
        for row in db.scalars(select(ReviewRoleAssignment)).all()
    }
    now = datetime.utcnow()
    changed = 0
    desired_keys = set(desired)
    for key, values in desired.items():
        row = existing.get(key)
        if not row:
            row = ReviewRoleAssignment(role_code=values["role_code"], identifier=values["identifier"])
            db.add(row)
        before = (row.user_name, row.user_number, row.department, row.center, row.group_name, row.source, bool(row.active))
        row.user_name = values["user_name"]
        row.user_number = values["user_number"]
        row.department = values["department"]
        row.center = values["center"]
        row.group_name = values["group_name"]
        row.source = "organization_sync"
        row.active = True
        row.created_by_number = row.created_by_number or "SYSTEM"
        row.created_by_name = row.created_by_name or "营销端组织架构"
        row.updated_at = now
        after = (row.user_name, row.user_number, row.department, row.center, row.group_name, row.source, bool(row.active))
        changed += int(before != after)
    for key, row in existing.items():
        if row.source == "organization_sync" and key not in desired_keys and row.active:
            row.active = False
            row.updated_at = now
            changed += 1
    if changed:
        db.commit()
    return changed


def _review_user_matches_assignment(user: dict, row: ReviewRoleAssignment) -> bool:
    number = user_number(user).strip().upper()
    name = user_name(user).strip().casefold()
    return bool(
        (number and row.user_number.strip().upper() == number)
        or (name and row.user_name.strip().casefold() == name)
    )


def _review_route_scope(db: Session, user: dict) -> tuple[str, str]:
    rows = db.scalars(
        select(ReviewRoleAssignment).where(
            ReviewRoleAssignment.active.is_(True),
            ReviewRoleAssignment.role_code.in_(("member", "team_lead")),
        )
    ).all()
    match = next((row for row in rows if row.center in REVIEW_ORGANIZATION_CENTERS and _review_user_matches_assignment(user, row)), None)
    return (match.center, match.group_name) if match else ("", "")


def _review_role_candidates(db: Session, role_code: str, center: str, group_name: str = "") -> list[dict]:
    rows = db.scalars(
        select(ReviewRoleAssignment).where(
            ReviewRoleAssignment.active.is_(True),
            ReviewRoleAssignment.role_code == role_code,
        ).order_by(ReviewRoleAssignment.user_name)
    ).all()
    if role_code == "team_lead" and center:
        exact_group = [row for row in rows if row.center == center and group_name and row.group_name == group_name]
        scoped = exact_group or [row for row in rows if row.center == center]
    elif role_code == "supervisor" and center:
        scoped = [row for row in rows if row.center in {center, "ALL"}]
    else:
        scoped = []
    selected = scoped or [row for row in rows if not row.center]
    return [
        {
            "user_name": row.user_name,
            "user_number": row.user_number,
            "center": row.center,
            "group_name": row.group_name,
        }
        for row in selected
    ]


def _review_eligible_reviewers(db: Session) -> list[dict]:
    rows = db.scalars(
        select(ReviewRoleAssignment).where(
            ReviewRoleAssignment.active.is_(True),
            ReviewRoleAssignment.role_code.in_(REVIEW_STAGE_ORDER),
        ).order_by(ReviewRoleAssignment.center, ReviewRoleAssignment.group_name, ReviewRoleAssignment.user_name)
    ).all()
    reviewers: dict[str, dict] = {}
    for row in rows:
        key = row.user_number.strip().upper() or f"name:{row.user_name.strip().casefold()}"
        reviewer = reviewers.setdefault(key, {
            "user_name": row.user_name,
            "user_number": row.user_number,
            "center": row.center,
            "group_name": row.group_name,
            "role_codes": [],
            "role_labels": [],
        })
        if row.role_code not in reviewer["role_codes"]:
            reviewer["role_codes"].append(row.role_code)
            reviewer["role_labels"].append(REVIEW_ROLE_LABELS[row.role_code])
    return list(reviewers.values())


def _resolve_designated_reviewer(db: Session, number: str, name: str) -> dict:
    normalized_number = number.strip().upper()
    normalized_name = name.strip().casefold()
    matches = [
        reviewer for reviewer in _review_eligible_reviewers(db)
        if (
            normalized_number
            and str(reviewer.get("user_number") or "").strip().upper() == normalized_number
        ) or (
            not normalized_number
            and normalized_name
            and str(reviewer.get("user_name") or "").strip().casefold() == normalized_name
        )
    ]
    if len(matches) != 1:
        raise HTTPException(409, "指定审核人无效或存在同名人员，请从审核人名单中重新选择")
    reviewer = matches[0]
    if normalized_name and str(reviewer.get("user_name") or "").strip().casefold() != normalized_name:
        raise HTTPException(409, "指定审核人的姓名与工号不一致，请重新选择")
    return reviewer


def _review_decision_allows_user(row: AssetReviewDecision, user: dict, fallback_roles: set[str]) -> bool:
    candidates = row.candidate_reviewers or []
    if not candidates:
        return row.role_code in fallback_roles
    number = user_number(user).strip().upper()
    name = user_name(user).strip().casefold()
    return any(
        (number and str(item.get("user_number") or "").strip().upper() == number)
        or (name and str(item.get("user_name") or "").strip().casefold() == name)
        for item in candidates
    )


def _user_grant_identifiers(user: dict) -> list[str]:
    number = str(user.get("number") or user.get("userId") or user.get("id") or "").strip()
    name = str(user.get("realName") or user.get("name") or "").strip()
    identifiers = []
    if number:
        identifiers.append(_grant_identifier(number, "number"))
    if name:
        identifiers.append(_grant_identifier(name))
    return identifiers


def _module_access_grant(db: Session, user: dict) -> ModuleAccessGrant | None:
    identifiers = _user_grant_identifiers(user)
    if not identifiers:
        return None
    rows = db.scalars(select(ModuleAccessGrant).where(ModuleAccessGrant.identifier.in_(identifiers))).all()
    by_identifier = {row.identifier: row for row in rows}
    return next((by_identifier[identifier] for identifier in identifiers if identifier in by_identifier), None)


def _is_active_brand_marketing_member(user: dict) -> bool:
    if str(user.get("status") or "normal").strip().lower() != "normal":
        return False
    department_evidence = " ".join(
        str(user.get(field) or "")
        for field in ("groupName", "parentDept", "deptName", "department", "center")
    )
    return "品牌营销" in department_evidence


def module_access_for_user(user: dict, db: Session | None = None) -> dict:
    if confirmed_departure(user):
        return {"master_access": False, "access_mode": "selected", "configured": True,
                "highest_business_access": False, "allowed_modules": [],
                "modules": list(MODULE_CATALOG), "login_blocked": True,
                "login_blocked_reason": "已确认离职"}
    owns_session = db is None
    session = db or SessionLocal()
    try:
        from .workspace_roles import configured_profile
        workspace_profile = configured_profile(user, session)
        grant = _module_access_grant(session, user)
        highest_business_access = is_permission_manager(user)
        mode = grant.access_mode if grant and grant.access_mode == "selected" else "all"
        selected = [key for key in (grant.modules or []) if key in MODULE_KEYS] if grant else []
        allowed = list(MODULE_KEYS) if mode == "all" else [key for key in MODULE_KEYS if key in selected]
        if highest_business_access:
            mode, allowed = "all", list(MODULE_KEYS)
        return {
            "master_access": True,
            "workspace_profile": workspace_profile,
            "access_mode": mode,
            "configured": grant is not None,
            "highest_business_access": highest_business_access,
            "allowed_modules": allowed,
            "modules": list(MODULE_CATALOG),
        }
    finally:
        if owns_session:
            session.close()


def module_allowed(user: dict, module_key: str, db: Session | None = None) -> bool:
    if confirmed_departure(user):
        return False
    if module_key == "hub":
        return True
    if module_key not in MODULE_KEYS:
        return False
    access = module_access_for_user(user, db)
    return module_key in access["allowed_modules"]


def require_module_access(user: dict, module_key: str, db: Session | None = None) -> None:
    if not module_allowed(user, module_key, db):
        label = next((item["label"] for item in MODULE_CATALOG if item["key"] == module_key), "该界面")
        raise HTTPException(403, f"当前账号未开通{label}访问权限")


def _module_access_item(access_grant: OaAccessGrant, module_grant: ModuleAccessGrant | None,
                        permission_identifiers: set[str] | None = None) -> dict:
    member = {"number": access_grant.user_number or access_grant.identifier,
              "realName": access_grant.real_name}
    departed = confirmed_departure(member) is not None
    # An explicit request-local set avoids opening one DB session per member.
    # None retains the single-member mutation response behavior; an empty set
    # deliberately means no database administrators, not a cache miss.
    highest = False if departed else (
        is_permission_manager(member) if permission_identifiers is None else
        is_super_admin(member) or bool(permission_identifiers.intersection((
            _grant_identifier(user_number(member), "number"),
            _grant_identifier(user_name(member)),
        )))
    )
    mode = module_grant.access_mode if module_grant and module_grant.access_mode == "selected" else "all"
    selected = [key for key in (module_grant.modules or []) if key in MODULE_KEYS] if module_grant else []
    return {
        "identifier": access_grant.identifier,
        "identifier_type": access_grant.identifier_type,
        "real_name": access_grant.real_name,
        "user_number": access_grant.user_number,
        "department": access_grant.department,
        "center": access_grant.center,
        "login_active": bool(access_grant.active) and not departed,
        "configured_login_active": bool(access_grant.active),
        "login_blocked": departed,
        "login_blocked_reason": "已确认离职" if departed else "",
        "access_mode": mode,
        "configured": module_grant is not None,
        "highest_business_access": highest,
        "effective_modules": [] if departed else list(MODULE_KEYS) if highest or mode == "all" else [key for key in MODULE_KEYS if key in selected],
        "modules": list(MODULE_KEYS) if mode == "all" else [key for key in MODULE_KEYS if key in selected],
        "updated_by_name": module_grant.updated_by_name if module_grant else "",
        "updated_at": module_grant.updated_at.isoformat() + "Z" if module_grant else None,
    }


def _seed_environment_access_grants(db: Session) -> None:
    for value in re.split(r"[,，;\n\r]+", os.getenv("OA_ALLOWED_USERS", "")):
        cleaned = value.strip()
        if not cleaned:
            continue
        identifier_type = "number" if cleaned.upper().startswith("FD-") else "name"
        identifier = _grant_identifier(cleaned, identifier_type)
        if db.get(OaAccessGrant, identifier):
            continue
        grant = OaAccessGrant(
            identifier=identifier,
            identifier_type=identifier_type,
            real_name="" if identifier_type == "number" else cleaned[:120],
            user_number=cleaned[:80].upper() if identifier_type == "number" else "",
            active=True,
            source="environment",
            granted_by_name="系统迁移",
        )
        db.add(grant)
        db.add(OaAccessAuditLog(
            identifier=identifier,
            real_name=grant.real_name,
            action="grant",
            actor_name="系统迁移",
            detail="从原登录白名单迁移",
        ))
    db.commit()


def _seed_protected_admin_grants(db: Session) -> None:
    """The two permission owners are immutable; other admins only receive log access."""
    for real_name in ("舒豪", "吴为"):
        identifier = _grant_identifier(real_name)
        grant = db.get(AdminGrant, identifier)
        if not grant:
            grant = AdminGrant(identifier=identifier, identifier_type="name")
            db.add(grant)
        grant.real_name = real_name
        grant.role = "super_admin"
        grant.active = True
        grant.protected = True
        grant.granted_by_name = "系统内置"
        grant.revoked_at = None
    db.commit()


def can_manage_asset(user: dict, asset: Asset) -> bool:
    if is_asset_admin(user):
        return True
    return bool(
        asset.ingest_source == "oa_upload"
        and asset.uploaded_by_number
        and asset.uploaded_by_number == user_number(user)
    )


def can_delete_asset(user: dict, asset: Asset) -> bool:
    return can_manage_asset(user, asset) or is_asset_delete_manager(user)


def require_asset_manager(user: dict, asset: Asset) -> None:
    if not can_manage_asset(user, asset):
        raise HTTPException(403, "只能管理自己上传的素材；OSS 扫描素材由素材管理员维护")


def require_asset_deleter(user: dict, asset: Asset) -> None:
    if not can_delete_asset(user, asset):
        raise HTTPException(403, "只能删除自己上传的素材；何雨庭可执行可恢复删除")


def _shanghai_date_bounds(
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    """Translate inclusive Asia/Shanghai dates to naive UTC database bounds."""
    # Endpoint functions are also called directly by a number of service tests.
    # In that case FastAPI's ``Query`` default object is passed instead of None,
    # so normalize non-calendar values before doing date arithmetic.
    start_date = start_date if isinstance(start_date, date) and not isinstance(start_date, datetime) else None
    end_date = end_date if isinstance(end_date, date) and not isinstance(end_date, datetime) else None
    if start_date and end_date and start_date > end_date:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    if start_date and end_date and (end_date - start_date).days > 365:
        raise HTTPException(400, "单次最多查询 366 天")
    shanghai_offset = timedelta(hours=8)
    start_utc = datetime.combine(start_date, datetime.min.time()) - shanghai_offset if start_date else None
    end_utc = (
        datetime.combine(end_date + timedelta(days=1), datetime.min.time()) - shanghai_offset
        if end_date
        else None
    )
    return start_utc, end_utc


def _append_date_filters(
    filters: list,
    column,
    start_date: date | None,
    end_date: date | None,
) -> None:
    start_utc, end_utc = _shanghai_date_bounds(start_date, end_date)
    if start_utc is not None:
        filters.append(column >= start_utc)
    if end_utc is not None:
        filters.append(column < end_utc)


def favorite_asset_ids(db: Session, user: dict, asset_ids: list[int] | None = None) -> set[int]:
    query = select(AssetFavorite.asset_id).where(AssetFavorite.user_number == user_number(user))
    if asset_ids is not None:
        if not asset_ids:
            return set()
        query = query.where(AssetFavorite.asset_id.in_(asset_ids))
    return set(db.scalars(query).all())


def favorite_counts_for_assets(db: Session, asset_ids: list[int]) -> dict[int, int]:
    if not asset_ids:
        return {}
    return {
        int(asset_id): int(count or 0)
        for asset_id, count in db.execute(
            select(AssetFavorite.asset_id, func.count(AssetFavorite.user_number))
            .where(AssetFavorite.asset_id.in_(asset_ids))
            .group_by(AssetFavorite.asset_id)
        ).all()
    }


def effective_marks_for_assets(
    db: Session, asset_ids: list[int] | None = None
) -> dict[int, AssetEffectiveMark]:
    query = select(AssetEffectiveMark)
    if asset_ids is not None:
        if not asset_ids:
            return {}
        query = query.where(AssetEffectiveMark.asset_id.in_(asset_ids))
    return {row.asset_id: row for row in db.scalars(query).all()}


def _push_preference_out(item: PushPreference) -> dict:
    return {
        "id": item.id,
        "platform": item.platform,
        "account_id": item.account_id,
        "account_name": item.account_name,
        "target_id": item.target_id,
        "target_name": item.target_name,
        "target_type": item.target_type,
        "pinned": bool(item.pinned),
        "use_count": int(item.use_count or 0),
        "last_used_at": item.last_used_at.isoformat() + "Z" if item.last_used_at else None,
        "updated_at": item.updated_at.isoformat() + "Z",
    }


def _upsert_push_preference(
    db: Session,
    owner_number: str,
    platform: str,
    account_id: str,
    account_name: str,
    target_id: str = "",
    target_name: str = "",
    target_type: str = "",
    record_use: bool = False,
) -> PushPreference:
    item = db.scalar(
        select(PushPreference).where(
            PushPreference.owner_number == owner_number,
            PushPreference.platform == platform,
            PushPreference.account_id == account_id,
            PushPreference.target_id == target_id,
        )
    )
    now = datetime.utcnow()
    if not item:
        item = PushPreference(
            owner_number=owner_number,
            platform=platform,
            account_id=account_id,
            target_id=target_id,
            created_at=now,
        )
        db.add(item)
    item.account_name = account_name[:255]
    item.target_name = target_name[:255]
    item.target_type = target_type[:40]
    if record_use:
        item.use_count = int(item.use_count or 0) + 1
        item.last_used_at = now
    item.updated_at = now
    return item


def _record_push_preferences(
    db: Session,
    owner_number: str,
    platform: str,
    targets: list[dict],
) -> None:
    seen_accounts: set[str] = set()
    for target in targets:
        account_id = str(target.get("account_id") or "").strip()
        if not account_id:
            continue
        account_name = str(target.get("account_name") or "").strip()
        if account_id not in seen_accounts:
            _upsert_push_preference(
                db, owner_number, platform, account_id, account_name, record_use=True
            )
            seen_accounts.add(account_id)
        target_id = str(target.get("target_id") or "").strip()
        if target_id:
            _upsert_push_preference(
                db,
                owner_number,
                platform,
                account_id,
                account_name,
                target_id,
                str(target.get("target_name") or "").strip(),
                str(target.get("target_type") or "").strip(),
                record_use=True,
            )


def soft_delete_asset(db: Session, asset: Asset, user: dict, now: datetime | None = None) -> bool:
    if asset.purged_at is not None:
        raise HTTPException(410, "素材原文件已永久删除")
    if asset.deleted_at is not None:
        return False
    current = now or datetime.utcnow()
    asset.deleted_at = current
    asset.deleted_by_number = user_number(user)
    asset.deleted_by_name = user_name(user)
    asset.purge_error = ""
    _audit_asset(
        db,
        asset,
        "trash",
        actor_number=asset.deleted_by_number,
        actor_name=asset.deleted_by_name,
        detail=f"移入回收站；满 {settings.trash_retention_days} 天后进入定期永久删除范围",
        created_at=current,
    )
    return True


def restore_deleted_asset(db: Session, asset: Asset, user: dict, now: datetime | None = None) -> bool:
    if asset.purged_at is not None:
        raise HTTPException(410, "素材原文件已永久删除，无法恢复")
    if asset.deleted_at is None:
        return False
    current = now or datetime.utcnow()
    _audit_asset(
        db,
        asset,
        "restore",
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail="从回收站恢复",
        created_at=current,
    )
    asset.deleted_at = None
    asset.deleted_by_number = ""
    asset.deleted_by_name = ""
    asset.purge_error = ""
    return True


def permanently_delete_asset(db: Session, asset: Asset, user: dict, now: datetime | None = None) -> bool:
    if not is_asset_admin(user):
        raise HTTPException(403, "仅部门负责人或素材管理员可永久删除")
    if asset.purged_at is not None:
        return False
    if asset.deleted_at is None:
        raise HTTPException(409, "请先将素材移入回收站")
    if asset.id in _recovery_asset_hold_ids(db, "purge"):
        raise HTTPException(409, "历史素材恢复核验中，暂不能永久删除")
    current = now or datetime.utcnow()
    try:
        oss_service.delete_asset(asset.object_key)
    except Exception as error:
        asset.purge_error = str(error)[:1000]
        _audit_asset(
            db,
            asset,
            "purge_failed",
            actor_number=user_number(user),
            actor_name=user_name(user),
            detail=asset.purge_error,
            created_at=current,
        )
        db.commit()
        raise HTTPException(502, "OSS 原文件删除失败，请稍后重试") from error
    asset.purged_at = current
    asset.purge_error = ""
    _audit_asset(
        db,
        asset,
        "purge",
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail="负责人手动永久删除 OSS 原文件",
        created_at=current,
    )
    return True


def _asset_gmv_sort_subquery():
    """Return one verified GMV value per asset for server-side sorting and hit filtering."""
    daily = (
        select(
            QianchuanDelivery.asset_id.label("asset_id"),
            QianchuanDelivery.advertiser_id.label("advertiser_id"),
            QianchuanDelivery.plan_id.label("plan_id"),
            QianchuanMetricDaily.video_id.label("video_id"),
            QianchuanMetricDaily.stat_date.label("stat_date"),
            func.max(
                cast(func.json_extract(QianchuanMetricDaily.metrics, "$.pay_order_amount"), Float)
            ).label("daily_gmv"),
        )
        .join(QianchuanMetricDaily, QianchuanMetricDaily.task_id == QianchuanDelivery.id)
        .where(
            QianchuanMetricDaily.status == "success",
            QianchuanMetricDaily.has_data.is_(True),
            QianchuanMetricDaily.link_verified.is_(True),
        )
        .group_by(
            QianchuanDelivery.asset_id,
            QianchuanDelivery.advertiser_id,
            QianchuanDelivery.plan_id,
            QianchuanMetricDaily.video_id,
            QianchuanMetricDaily.stat_date,
        )
        .subquery("verified_asset_gmv_daily")
    )
    return (
        select(daily.c.asset_id, func.sum(daily.c.daily_gmv).label("gmv_yuan"))
        .group_by(daily.c.asset_id)
        .subquery("verified_asset_gmv")
    )


def _verified_asset_gmv_query():
    return (
        select(
            QianchuanDelivery.asset_id,
            QianchuanDelivery.advertiser_id,
            QianchuanDelivery.plan_id,
            QianchuanMetricDaily.video_id,
            QianchuanMetricDaily.stat_date,
            QianchuanMetricDaily.metrics,
            QianchuanMetricDaily.updated_at,
        )
        .join(QianchuanMetricDaily, QianchuanMetricDaily.task_id == QianchuanDelivery.id)
        .join(Asset, Asset.id == QianchuanDelivery.asset_id)
        .where(
            Asset.deleted_at.is_(None),
            Asset.purged_at.is_(None),
            QianchuanMetricDaily.status == "success",
            QianchuanMetricDaily.has_data.is_(True),
            QianchuanMetricDaily.link_verified.is_(True),
        )
    )


def _asset_hit_material_count(db: Session) -> int:
    query = _verified_asset_gmv_query().order_by(QianchuanDelivery.asset_id)
    rows = db.execute(query.execution_options(yield_per=500))
    try:
        return count_ordered_asset_hits(rows)
    finally:
        rows.close()


def _asset_gmv_summary(db: Session, asset_ids: list[int] | None = None) -> dict[int, dict]:
    """Aggregate verified daily Qianchuan GMV and deduplicate retry records."""
    query = _verified_asset_gmv_query()
    if asset_ids is not None:
        if not asset_ids:
            return {}
        query = query.where(QianchuanDelivery.asset_id.in_(asset_ids))
    rows = db.execute(query).all()
    deduped: dict[tuple, tuple[float, datetime]] = {}
    targets: dict[int, set[tuple[str, str, str]]] = {}
    for asset_id, advertiser_id, plan_id, video_id, stat_date, metrics, updated_at in rows:
        raw_gmv = (metrics or {}).get("pay_order_amount")
        if raw_gmv is None:
            continue
        try:
            gmv = max(0.0, float(raw_gmv))
        except (TypeError, ValueError):
            continue
        key = (asset_id, advertiser_id or "", plan_id or "", video_id or "", stat_date)
        previous = deduped.get(key)
        if previous is None or gmv > previous[0]:
            deduped[key] = (gmv, updated_at)
        targets.setdefault(asset_id, set()).add((advertiser_id or "", plan_id or "", video_id or ""))
    summary: dict[int, dict] = {}
    for key, (gmv, updated_at) in deduped.items():
        asset_id = int(key[0])
        item = summary.setdefault(asset_id, {"gmv_yuan": 0.0, "target_count": 0, "updated_at": None})
        item["gmv_yuan"] += gmv
        if not item["updated_at"] or updated_at > item["updated_at"]:
            item["updated_at"] = updated_at
    for asset_id, item in summary.items():
        item["gmv_yuan"] = round(item["gmv_yuan"], 2)
        item["target_count"] = len(targets.get(asset_id, set()))
    return summary


def _asset_platform_gmv_summary(
    db: Session,
    asset_ids: list[int] | None = None,
    qianchuan_summary: dict[int, dict] | None = None,
) -> dict[int, list[dict]]:
    """Return only real platform GMV fields; missing values never become zero."""
    if asset_ids is not None and not asset_ids:
        return {}
    qianchuan = qianchuan_summary if qianchuan_summary is not None else _asset_gmv_summary(db, asset_ids)
    result: dict[int, list[dict]] = {}
    for asset_id, item in qianchuan.items():
        result.setdefault(asset_id, []).append({
            "platform": "qianchuan",
            "label": "千川成交额",
            "gmv_yuan": round(float(item["gmv_yuan"]), 2),
            "updated_at": item.get("updated_at"),
        })

    adq_query = (
        select(
            AdqDelivery.id,
            AdqDelivery.asset_id,
            AdqDelivery.account_id,
            AdqDelivery.adgroup_id,
            AdqMetricDaily.video_id,
            AdqMetricDaily.stat_date,
            AdqMetricDaily.metrics,
            AdqMetricDaily.updated_at,
        )
        .join(AdqMetricDaily, AdqMetricDaily.task_id == AdqDelivery.id)
        .join(Asset, Asset.id == AdqDelivery.asset_id)
        .where(
            Asset.deleted_at.is_(None),
            Asset.purged_at.is_(None),
            AdqDelivery.deleted_at.is_(None),
            AdqMetricDaily.status == "success",
            AdqMetricDaily.has_data.is_(True),
        )
    )
    if asset_ids is not None:
        adq_query = adq_query.where(AdqDelivery.asset_id.in_(asset_ids))
    adq_daily: dict[tuple, tuple[float, datetime]] = {}
    adq_daily_tasks: set[str] = set()
    adq_daily_targets: set[tuple] = set()
    for task_id, asset_id, account_id, adgroup_id, video_id, stat_date, metrics, updated_at in db.execute(adq_query).all():
        raw_gmv = (metrics or {}).get("order_amount_yuan")
        if raw_gmv is None:
            continue
        try:
            gmv = max(0.0, float(raw_gmv))
        except (TypeError, ValueError):
            continue
        target = (int(asset_id), account_id or "", adgroup_id or "", video_id or "")
        key = (*target, stat_date)
        previous = adq_daily.get(key)
        if previous is None or gmv > previous[0]:
            adq_daily[key] = (gmv, updated_at)
        adq_daily_tasks.add(task_id)
        adq_daily_targets.add(target)

    adq_fallback_query = select(
        AdqDelivery.id,
        AdqDelivery.asset_id,
        AdqDelivery.account_id,
        AdqDelivery.adgroup_id,
        AdqDelivery.platform_asset_id,
        AdqDelivery.metrics,
        AdqDelivery.metrics_synced_at,
    ).where(
        AdqDelivery.deleted_at.is_(None),
        AdqDelivery.metrics_status == "success",
        AdqDelivery.metrics_synced_at.is_not(None),
    )
    if asset_ids is not None:
        adq_fallback_query = adq_fallback_query.where(AdqDelivery.asset_id.in_(asset_ids))
    adq_fallback: dict[tuple, tuple[float, datetime]] = {}
    for task_id, asset_id, account_id, adgroup_id, video_id, metrics, updated_at in db.execute(adq_fallback_query).all():
        target = (int(asset_id), account_id or "", adgroup_id or "", video_id or "")
        if task_id in adq_daily_tasks or target in adq_daily_targets:
            continue
        raw_gmv = (metrics or {}).get("order_amount_yuan")
        if raw_gmv is None:
            continue
        try:
            gmv = max(0.0, float(raw_gmv))
        except (TypeError, ValueError):
            continue
        previous = adq_fallback.get(target)
        if previous is None or gmv > previous[0]:
            adq_fallback[target] = (gmv, updated_at)

    adq_totals: dict[int, dict] = {}
    for key, (gmv, updated_at) in [*adq_daily.items(), *adq_fallback.items()]:
        asset_id = int(key[0])
        item = adq_totals.setdefault(asset_id, {"gmv_yuan": 0.0, "updated_at": None})
        item["gmv_yuan"] += gmv
        if updated_at and (not item["updated_at"] or updated_at > item["updated_at"]):
            item["updated_at"] = updated_at
    for asset_id, item in adq_totals.items():
        result.setdefault(asset_id, []).append({
            "platform": "adq",
            "label": "ADQ 成交额",
            "gmv_yuan": round(item["gmv_yuan"], 2),
            "updated_at": item["updated_at"],
        })

    channels_query = (
        select(
            ChannelsDelivery.id,
            ChannelsDelivery.asset_id,
            ChannelsDelivery.account_id,
            ChannelsDelivery.platform_export_id,
            ChannelsDelivery.platform_content_id,
            ChannelsMetricDaily.data_date,
            ChannelsMetricDaily.gmv_fen,
            ChannelsMetricDaily.collected_at,
        )
        .join(ChannelsMetricDaily, ChannelsMetricDaily.delivery_id == ChannelsDelivery.id)
        .join(Asset, Asset.id == ChannelsDelivery.asset_id)
        .where(
            Asset.deleted_at.is_(None),
            Asset.purged_at.is_(None),
            ChannelsDelivery.deleted_at.is_(None),
            ChannelsMetricDaily.gmv_fen.is_not(None),
        )
    )
    if asset_ids is not None:
        channels_query = channels_query.where(ChannelsDelivery.asset_id.in_(asset_ids))
    channel_daily: dict[tuple, tuple[int, datetime]] = {}
    channel_daily_tasks: set[str] = set()
    channel_daily_targets: set[tuple] = set()
    for task_id, asset_id, account_id, export_id, content_id, data_date, gmv_fen, updated_at in db.execute(channels_query).all():
        identity = export_id or content_id or task_id
        target = (int(asset_id), account_id or "", identity)
        key = (*target, data_date)
        value = max(0, int(gmv_fen))
        previous = channel_daily.get(key)
        if previous is None or value > previous[0]:
            channel_daily[key] = (value, updated_at)
        channel_daily_tasks.add(task_id)
        channel_daily_targets.add(target)

    channels_fallback_query = select(
        ChannelsDelivery.id,
        ChannelsDelivery.asset_id,
        ChannelsDelivery.account_id,
        ChannelsDelivery.platform_export_id,
        ChannelsDelivery.platform_content_id,
        ChannelsDelivery.gmv_fen,
        ChannelsDelivery.metrics_updated_at,
    ).where(
        ChannelsDelivery.deleted_at.is_(None),
        ChannelsDelivery.metrics_updated_at.is_not(None),
        ChannelsDelivery.gmv_fen.is_not(None),
    )
    if asset_ids is not None:
        channels_fallback_query = channels_fallback_query.where(ChannelsDelivery.asset_id.in_(asset_ids))
    channel_fallback: dict[tuple, tuple[int, datetime]] = {}
    for task_id, asset_id, account_id, export_id, content_id, gmv_fen, updated_at in db.execute(channels_fallback_query).all():
        target = (int(asset_id), account_id or "", export_id or content_id or task_id)
        if task_id in channel_daily_tasks or target in channel_daily_targets:
            continue
        value = max(0, int(gmv_fen))
        previous = channel_fallback.get(target)
        if previous is None or value > previous[0]:
            channel_fallback[target] = (value, updated_at)

    channel_totals: dict[int, dict] = {}
    for key, (gmv_fen, updated_at) in [*channel_daily.items(), *channel_fallback.items()]:
        asset_id = int(key[0])
        item = channel_totals.setdefault(asset_id, {"gmv_fen": 0, "updated_at": None})
        item["gmv_fen"] += gmv_fen
        if updated_at and (not item["updated_at"] or updated_at > item["updated_at"]):
            item["updated_at"] = updated_at
    for asset_id, item in channel_totals.items():
        result.setdefault(asset_id, []).append({
            "platform": "channels",
            "label": "视频号成交额",
            "gmv_yuan": round(item["gmv_fen"] / 100, 2),
            "updated_at": item["updated_at"],
        })
    return result


def to_asset_out(
    asset: Asset,
    favorite: bool = False,
    favorite_count: int = 0,
    can_purge: bool = False,
    can_manage: bool = False,
    can_delete: bool | None = None,
    gmv_summary: dict | None = None,
    platform_gmv: list[dict] | None = None,
    effective_mark: AssetEffectiveMark | None = None,
    review_summary: dict | None = None,
) -> AssetOut:
    purge_after = (
        asset.deleted_at + timedelta(days=settings.trash_retention_days)
        if asset.deleted_at and not asset.purged_at
        else None
    )
    return AssetOut(
        id=asset.id,
        filename=asset.filename,
        object_key=asset.object_key,
        media_type=asset.media_type,
        size=asset.size,
        modified_at=asset.modified_at,
        etag=asset.etag or "",
        uploaded_by_number=asset.uploaded_by_number or "",
        uploaded_by_name=asset.uploaded_by_name or "",
        category=asset.category,
        content_type=asset.content_type,
        status=asset.status,
        asset_scope=asset.asset_scope or "marketing_video",
        library_type=asset.library_type or "source",
        asset_subtype=asset.asset_subtype or "其他视频素材",
        folder_name=asset.folder_name or "",
        tags=asset.tags or [],
        favorite=favorite,
        favorite_count=favorite_count,
        preview_url=oss_service.url_for(asset.object_key) if not asset.purged_at else "",
        download_url=oss_service.url_for(asset.object_key, download=True) if not asset.purged_at else "",
        cover_url=asset.cover_url or "",
        source=asset.source or "",
        account_name=asset.account_name or "",
        ingest_source=asset.ingest_source or ("oa_upload" if asset.source == "oa_upload" else "oss_scan"),
        reference_url=asset.reference_url or "",
        reference_video_key=asset.reference_video_key or "",
        reference_video_name=asset.reference_video_name or "",
        reference_video_url=oss_service.url_for(asset.reference_video_key) if asset.reference_video_key and not asset.purged_at else "",
        material_description=asset.material_description or "",
        performance_screenshots=[
            {
                "object_key": str(reference.get("object_key") or ""),
                "filename": str(reference.get("filename") or "数据截图"),
                "url": oss_service.url_for(str(reference.get("object_key") or "")),
            }
            for reference in (asset.performance_screenshots or [])
            if str(reference.get("object_key") or "") and not asset.purged_at
        ],
        can_manage=can_manage,
        can_delete=can_manage if can_delete is None else can_delete,
        deleted_at=asset.deleted_at,
        deleted_by_name=asset.deleted_by_name or "",
        purge_after=purge_after,
        purged_at=asset.purged_at,
        purge_error=asset.purge_error or "",
        can_purge=can_purge,
        historical_gmv_yuan=(gmv_summary or {}).get("gmv_yuan"),
        gmv_target_count=int((gmv_summary or {}).get("target_count") or 0),
        gmv_updated_at=(gmv_summary or {}).get("updated_at").isoformat() + "Z" if (gmv_summary or {}).get("updated_at") else None,
        platform_gmv=[{
            "platform": str(item.get("platform") or ""),
            "label": str(item.get("label") or "成交额"),
            "gmv_yuan": round(float(item.get("gmv_yuan") or 0), 2),
            "updated_at": item.get("updated_at").isoformat() + "Z" if item.get("updated_at") else None,
        } for item in (platform_gmv or [])],
        effective=effective_mark is not None,
        effective_marked_by_name=effective_mark.marked_by_name if effective_mark else "",
        effective_marked_at=effective_mark.created_at.isoformat() + "Z" if effective_mark else None,
        review_status=str((review_summary or {}).get("status") or ""),
        review_version=int((review_summary or {}).get("version") or 0),
        review_note=str((review_summary or {}).get("note") or ""),
        review_updated_at=(review_summary or {}).get("updated_at").isoformat() + "Z" if (review_summary or {}).get("updated_at") else None,
    )


def delivery_out(
    task: QianchuanDelivery,
    asset: Asset | None = None,
    daily_summary: dict | None = None,
    can_manage: bool = True,
    can_cancel: bool = False,
) -> dict:
    summary = daily_summary or {}
    effective_metrics_link_status = task.metrics_link_status or "pending"
    if summary.get("link_verified"):
        effective_metrics_link_status = "verified"
    return {
        "id": task.id,
        "batch_id": task.batch_id,
        "asset_id": task.asset_id,
        "asset_name": asset.filename if asset else "",
        "created_by_number": task.created_by_number,
        "created_by_name": task.created_by_name,
        "can_manage": can_manage,
        "can_cancel": can_cancel,
        "advertiser_id": task.advertiser_id,
        "advertiser_name": task.advertiser_name,
        "plan_id": task.plan_id,
        "plan_name": task.plan_name,
        "plan_type": task.plan_type,
        "platform_asset_id": task.platform_asset_id,
        "upload_task_id": task.upload_task_id,
        "idempotency_key": task.idempotency_key,
        "delivery_entity_type": task.delivery_entity_type,
        "delivery_entity_id": task.delivery_entity_id,
        "binding_evidence": task.binding_evidence or {},
        "platform_audit": (task.binding_evidence or {}).get('platform_audit', {}),
        "binding_verified_at": task.binding_verified_at.isoformat() + "Z" if task.binding_verified_at else None,
        "status": task.status,
        "message": task.message,
        "request_id": task.request_id,
        "attempt_count": task.attempt_count,
        "last_error_category": task.last_error_category,
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "error_advice": task.error_advice,
        "metrics": summary.get("metrics", task.metrics or {}),
        "related_ad_ids": task.related_ad_ids or [],
        "related_creative_ids": task.related_creative_ids or [],
        "metrics_link_status": effective_metrics_link_status,
        "metrics_start_date": task.metrics_start_date,
        "metrics_end_date": task.metrics_end_date,
        "metrics_synced_at": task.metrics_synced_at.isoformat() + "Z" if task.metrics_synced_at else None,
        "metrics_message": task.metrics_message,
        "metrics_data_status": summary.get("status", "legacy"),
        "metrics_fresh_through": summary.get("fresh_through"),
        "metrics_coverage": summary.get("coverage", {"completed": 0, "expected": 0}),
        "daily_metrics": summary.get("daily_metrics", []),
        "created_at": task.created_at.isoformat() + "Z",
        "updated_at": task.updated_at.isoformat() + "Z",
    }


def adq_delivery_out(db: Session, task: AdqDelivery, asset: Asset | None = None, can_manage: bool = True) -> dict:
    daily_rows = db.scalars(
        select(AdqMetricDaily)
        .where(AdqMetricDaily.task_id == task.id)
        .order_by(AdqMetricDaily.stat_date.desc())
        .limit(31)
    ).all()
    evidence = task.binding_evidence or {}
    library_readback = evidence.get("library_readback") if isinstance(evidence, dict) else {}
    library_verified = bool(isinstance(library_readback, dict) and library_readback.get("verified"))
    return {
        "id": task.id,
        "batch_id": task.batch_id,
        "asset_id": task.asset_id,
        "asset_name": asset.filename if asset else "",
        "created_by_number": task.created_by_number,
        "created_by_name": task.created_by_name,
        "can_manage": can_manage,
        "account_id": task.account_id,
        "account_name": task.account_name,
        "adgroup_id": task.adgroup_id,
        "adgroup_name": task.adgroup_name,
        "source_dynamic_creative_id": task.source_dynamic_creative_id,
        "dynamic_creative_id": task.dynamic_creative_id,
        "platform_asset_id": task.platform_asset_id,
        "root_material_id": task.root_material_id,
        "cover_id": task.cover_id,
        "binding_evidence": evidence,
        "binding_verified_at": task.binding_verified_at.isoformat() + "Z" if task.binding_verified_at else None,
        "library_readback_status": "verified" if library_verified else (
            "pending" if task.platform_asset_id else "unavailable"
        ),
        "library_readback_message": (
            "已按视频 ID 在统一素材源账户回读确认"
            if library_verified else
            ("平台已返回视频 ID，尚未完成素材库回读确认" if task.platform_asset_id else "等待平台返回视频 ID")
        ),
        "library_readback_at": library_readback.get("checked_at") if isinstance(library_readback, dict) else None,
        "status": task.status,
        "message": task.message,
        "request_id": task.request_id,
        "attempt_count": task.attempt_count,
        "last_error_category": task.last_error_category,
        "failure_stage": task.failure_stage,
        "error_code": task.error_code,
        "error_message": task.error_message,
        "error_advice": task.error_advice,
        "metrics": task.metrics or {},
        "metrics_status": task.metrics_status or "pending",
        "metrics_start_date": task.metrics_start_date,
        "metrics_end_date": task.metrics_end_date,
        "metrics_synced_at": task.metrics_synced_at.isoformat() + "Z" if task.metrics_synced_at else None,
        "metrics_message": task.metrics_message,
        "daily_metrics": [
            {
                "date": row.stat_date,
                "metrics": row.metrics or {},
                "status": row.status,
                "has_data": row.has_data,
                "message": row.message,
                "synced_at": row.synced_at.isoformat() + "Z",
            }
            for row in daily_rows
        ],
        "created_at": task.created_at.isoformat() + "Z",
        "updated_at": task.updated_at.isoformat() + "Z",
    }


class LoginPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)
    password: str = Field(min_length=1, max_length=300)
    captcha: str = Field(default="", max_length=20)


class CaptchaPayload(BaseModel):
    username: str = Field(min_length=1, max_length=100)


class AccessGrantCreate(BaseModel):
    real_name: str = Field(min_length=2, max_length=120)
    department: str = Field(default="", max_length=255)
    center: str = Field(default="", max_length=255)


class AdminGrantCreate(BaseModel):
    real_name: str = Field(min_length=2, max_length=120)
    department: str = Field(default="", max_length=255)
    center: str = Field(default="", max_length=255)


class ModuleAccessUpdate(BaseModel):
    access_mode: str = Field(pattern="^(all|selected)$")
    modules: list[str] = Field(default_factory=list, max_length=10)


class ModuleAccessBatchUpdate(ModuleAccessUpdate):
    identifiers: list[str] = Field(min_length=1, max_length=200)


class AssistantMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=6000)


class CreativeIncentiveDirectionCreate(BaseModel):
    direction_name: str = Field(min_length=2, max_length=180)
    material_id: str = Field(min_length=1, max_length=120)
    material_name: str = Field(default="", max_length=255)
    creator_name: str = Field(default="", max_length=120)
    creator_number: str = Field(default="", max_length=80)
    center: str = Field(default="", max_length=255)
    online_date: date
    originality_note: str = Field(default="", max_length=2000)
    copy_judgement: str = Field(default="", max_length=2000)
    visual_judgement: str = Field(default="", max_length=2000)
    voice_judgement: str = Field(default="", max_length=2000)
    remix_plan: str = Field(default="", max_length=2000)
    reference_url: str = Field(default="", max_length=2048)


class CreativeIncentiveConfirm(BaseModel):
    confirmed: bool = True
    note: str = Field(default="", max_length=2000)


class CreativeIncentiveMilestonePublish(BaseModel):
    mode: Literal["feishu", "manual"] = "manual"


class AssistantConversationCreate(BaseModel):
    title: str = Field(default="新会话", max_length=160)


class AssistantConversationUpdate(BaseModel):
    title: str = Field(min_length=1, max_length=160)


class AssistantAttachmentCreate(BaseModel):
    conversation_id: str = Field(min_length=1, max_length=36)
    filename: str = Field(min_length=1, max_length=255)
    mime_type: str = Field(min_length=1, max_length=80)
    data_base64: str = Field(min_length=1, max_length=17_000_000)


class AssistantChatPayload(BaseModel):
    messages: list[AssistantMessage] = Field(default_factory=list, max_length=14)
    conversation_id: str = Field(default="", max_length=36)
    content: str = Field(default="", max_length=6000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=4)
    reuse_last_user_message: bool = False
    current_view: str = Field(default="AI管家", max_length=80)


class ReviewWorkflowUpdate(BaseModel):
    enabled: bool = False
    naming_enabled: bool = False
    ai_redline_enabled: bool = False
    required_roles: list[str] = Field(default_factory=lambda: list(REVIEW_STAGE_ORDER), min_length=1, max_length=2)


class ReviewAiRuleUpdate(BaseModel):
    code: str = Field(default="", max_length=80)
    category: Literal["platform", "internal", "artist", "relaxation"]
    severity: Literal["hard", "warning"]
    title: str = Field(min_length=2, max_length=160)
    pattern: str = Field(min_length=1, max_length=2000)
    enabled: bool = True


class ReviewAiRulesUpdate(BaseModel):
    rules: list[ReviewAiRuleUpdate] = Field(min_length=1, max_length=100)


class ReviewRoleCreate(BaseModel):
    role_code: str = Field(pattern="^(member|team_lead|supervisor|brand_tone|director|internal_control)$")
    user_name: str = Field(min_length=2, max_length=120)
    user_number: str = Field(default="", max_length=80)
    department: str = Field(default="", max_length=255)
    center: str = Field(default="", max_length=120)
    group_name: str = Field(default="", max_length=120)


class ReviewNamingEvidence(BaseModel):
    category: Literal["face", "mechanism", "product_display", "ai_first_creation"]
    material_name: str = Field(min_length=1, max_length=512)
    usage: Literal["complete", "clip"]
    position: Literal["opening", "middle", "ending"]
    source_asset_id: int | None = Field(default=None, gt=0)


class AssetReviewSubmit(BaseModel):
    note: str = Field(default="", max_length=1000)
    naming_evidence: list[ReviewNamingEvidence] = Field(default_factory=list, max_length=100)
    no_applicable_sources: bool = False
    assignment_mode: Literal["organization", "designated"] = "organization"
    designated_reviewer_number: str = Field(default="", max_length=80)
    designated_reviewer_name: str = Field(default="", max_length=120)


class AssetReviewBatchSubmitItem(AssetReviewSubmit):
    asset_id: int = Field(gt=0)


class AssetReviewBatchSubmit(BaseModel):
    items: list[AssetReviewBatchSubmitItem] = Field(min_length=1, max_length=100)


class AssetReviewAct(BaseModel):
    role_code: str = Field(pattern="^(team_lead|supervisor|designated_reviewer|director|internal_control)$")
    note: str = Field(default="", max_length=1000)
    quality_scores: dict[str, int] = Field(default_factory=dict)


class AssetReviewBatchActItem(BaseModel):
    submission_id: str = Field(min_length=1, max_length=36)
    role_code: str = Field(pattern="^(team_lead|supervisor|designated_reviewer|director|internal_control)$")
    note: str = Field(default="", max_length=1000)
    quality_scores: dict[str, int] = Field(default_factory=dict)


class AssetReviewBatchAct(BaseModel):
    decision: Literal["approve", "reject"]
    items: list[AssetReviewBatchActItem] = Field(min_length=1, max_length=100)


class BatchDeletePayload(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=100)


class BatchTagsPayload(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=100)
    tags: list[str] = Field(min_length=1, max_length=30)
    mode: str = Field(default="add", pattern="^(add|remove|replace)$")


class BatchFolderPayload(BaseModel):
    asset_ids: list[int] = Field(min_length=1, max_length=100)
    folder_name: str = Field(default="", max_length=160)


class TrashClearPayload(BaseModel):
    confirm_text: str = Field(min_length=1, max_length=30)


class PushPreferenceToggle(BaseModel):
    account_id: str = Field(min_length=1, max_length=80)
    account_name: str = Field(default="", max_length=255)
    target_id: str = Field(default="", max_length=80)
    target_name: str = Field(default="", max_length=255)
    target_type: str = Field(default="", max_length=40)


def normalize_asset_folder_name(value: str) -> str:
    cleaned = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", (value or "").strip())
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    return cleaned[:160]


class ChannelsInteraction(BaseModel):
    action: str = Field(default="click", pattern="^(click|scroll|select|select_account|scroll_accounts)$")
    x: float = Field(default=0.5, ge=0, le=1)
    y: float = Field(default=0.5, ge=0, le=1)
    delta_y: float = Field(default=0, ge=-3000, le=3000)
    choice_id: str = Field(default="", max_length=160)


class VideoRequestCreate(BaseModel):
    client_request_id: str = Field(default="", max_length=80, pattern=r"^[a-zA-Z0-9_-]*$")
    product: str = Field(default="通用", min_length=1, max_length=100)
    description: str = Field(default="", max_length=10000)
    reference_url: str = Field(default="", max_length=2048)
    reference_video_key: str = Field(default="", max_length=1024)
    reference_video_name: str = Field(default="", max_length=512)
    reference_videos: list[dict[str, str]] = Field(default_factory=list, max_length=9)
    reference_images: list[dict[str, str]] = Field(default_factory=list, max_length=9)


class VideoRequestAssign(BaseModel):
    assignee_number: str = Field(default="", max_length=80)
    assignee_name: str = Field(min_length=2, max_length=120)


class VideoRequestDeliver(BaseModel):
    asset_id: int | None = Field(default=None, gt=0)
    asset_ids: list[int] = Field(default_factory=list, max_length=10)
    note: str = Field(default="", max_length=2000)


class VideoRequestFeedback(BaseModel):
    feedback: str = Field(min_length=2, max_length=5000)


class VideoRequestReturn(BaseModel):
    reason: str = Field(min_length=2, max_length=2000)


def access_grant_out(grant: OaAccessGrant) -> dict:
    departed = confirmed_departure({"number": grant.user_number or grant.identifier}) is not None
    return {
        "identifier": grant.identifier,
        "identifier_type": grant.identifier_type,
        "real_name": grant.real_name,
        "user_number": grant.user_number,
        "department": grant.department,
        "center": grant.center,
        "active": bool(grant.active) and not departed,
        "configured_active": bool(grant.active),
        "login_blocked": departed,
        "login_blocked_reason": "已确认离职" if departed else "",
        "source": grant.source,
        "granted_by_number": grant.granted_by_number,
        "granted_by_name": grant.granted_by_name,
        "granted_at": grant.granted_at.isoformat() + "Z",
        "revoked_by_name": grant.revoked_by_name,
        "revoked_at": grant.revoked_at.isoformat() + "Z" if grant.revoked_at else None,
        "last_verified_at": grant.last_verified_at.isoformat() + "Z" if grant.last_verified_at else None,
    }


def access_audit_out(item: OaAccessAuditLog) -> dict:
    return {
        "id": item.id,
        "identifier": item.identifier,
        "real_name": item.real_name,
        "action": item.action,
        "actor_number": item.actor_number,
        "actor_name": item.actor_name,
        "detail": item.detail,
        "created_at": item.created_at.isoformat() + "Z",
    }


def admin_grant_out(grant: AdminGrant, access_grant: OaAccessGrant | None = None) -> dict:
    return {
        "identifier": grant.identifier,
        "identifier_type": grant.identifier_type,
        "real_name": grant.real_name,
        "user_number": grant.user_number or (access_grant.user_number if access_grant else ""),
        "department": grant.department or (access_grant.department if access_grant else ""),
        "center": grant.center or (access_grant.center if access_grant else ""),
        "role": grant.role,
        "active": grant.active,
        "protected": grant.protected,
        "granted_by_name": grant.granted_by_name,
        "granted_at": grant.granted_at.isoformat() + "Z",
        "revoked_by_name": grant.revoked_by_name,
        "revoked_at": grant.revoked_at.isoformat() + "Z" if grant.revoked_at else None,
    }


def operation_log_out(item: OperationLog) -> dict:
    return {
        "id": item.id,
        "actor_number": item.actor_number,
        "actor_name": item.actor_name,
        "department": item.department,
        "module": item.module,
        "action": _operation_action(item.method, item.path, item.result),
        "method": item.method,
        "path": item.path,
        "result": item.result,
        "status_code": item.status_code,
        "resource_type": item.resource_type,
        "resource_id": item.resource_id,
        "detail": item.detail,
        "created_at": item.created_at.isoformat() + "Z",
    }


@app.get("/api/live", include_in_schema=False)
async def liveness():
    """Cheap process liveness probe; it must not wait on SQLite aggregate queries."""
    return {"status": "ok"}


@app.get("/api/health")
def health():
    with SessionLocal() as db:
        total = db.scalar(
            select(func.count()).select_from(Asset).where(Asset.deleted_at.is_(None), Asset.purged_at.is_(None))
        ) or 0
        oss_total = db.scalar(
            select(func.count()).select_from(Asset).where(Asset.purged_at.is_(None))
        ) or 0
        trash = db.scalar(
            select(func.count()).select_from(Asset).where(Asset.deleted_at.is_not(None), Asset.purged_at.is_(None))
        ) or 0
        updated_at = _source_updated_at(db)
    return {
        "status": "ok",
        "oss_configured": oss_service.configured,
        "workstation_api_configured": len(settings.workstation_api_token) >= 32,
        "source": source_name(),
        "source_updated_at": updated_at,
        "oss_records": oss_total,
        "active_records": total,
        "trash_records": trash,
        "indexed_records": int(catalog_service.meta.get("record_count", 0) or 0),
        "catalog_records": int(catalog_service.meta.get("record_count", 0) or 0),
        "sync": _sync_snapshot(),
        "personal_sales": personal_sales_service.status(),
        "ai_assistant": ai_assistant_service.status(),
        "ai_insights": ai_insight_service.status(),
    }


@app.get("/api/personal-sales")
def personal_sales(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    _: dict = Depends(require_user),
):
    """Return the last complete personal GMV snapshot; never replace missing data with zero."""
    try:
        return personal_sales_service.get_snapshot(start_date, end_date)
    except PersonalSalesError as error:
        raise HTTPException(409, str(error)) from error


def _analytics_date_range(start_date: date | None, end_date: date | None) -> tuple[date, date]:
    today = datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).date()
    resolved_end = end_date or today
    resolved_start = start_date or (resolved_end - timedelta(days=6))
    if resolved_start > resolved_end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    if (resolved_end - resolved_start).days > 365:
        raise HTTPException(400, "单次最多查询 366 天")
    return resolved_start, resolved_end


def _safe_gmv(metrics: dict | None) -> float:
    try:
        return max(0.0, float((metrics or {}).get("pay_order_amount") or 0))
    except (TypeError, ValueError):
        return 0.0


@app.get("/api/analytics/uploads")
def upload_analytics(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    _: dict = Depends(require_user),
):
    """Team upload volume and verified material GMV for the selected Shanghai date range."""
    range_start, range_end = _analytics_date_range(start_date, end_date)
    shanghai_offset = timedelta(hours=8)
    utc_start = datetime.combine(range_start, datetime.min.time()) - shanghai_offset
    utc_end = datetime.combine(range_end + timedelta(days=1), datetime.min.time()) - shanghai_offset
    day_keys = [
        (range_start + timedelta(days=offset)).isoformat()
        for offset in range((range_end - range_start).days + 1)
    ]

    upload_rows = db.execute(
        select(
            Asset.id,
            Asset.modified_at,
            Asset.library_type,
            Asset.uploaded_by_number,
            Asset.uploaded_by_name,
        ).where(
            Asset.asset_scope == "marketing_video",
            Asset.ingest_source == "oa_upload",
            Asset.source != "jianying_export",
            Asset.modified_at >= utc_start,
            Asset.modified_at < utc_end,
        )
    ).all()

    asset_owner_rows = db.execute(
        select(
            Asset.id,
            Asset.library_type,
            Asset.uploaded_by_number,
            Asset.uploaded_by_name,
        ).where(Asset.asset_scope == "marketing_video")
    ).all()
    asset_owners = {
        int(asset_id): {
            "library_type": library_type if library_type in {"source", "remix"} else "source",
            "number": str(number or ""),
            "name": str(name or number or "未识别上传人"),
        }
        for asset_id, library_type, number, name in asset_owner_rows
    }

    daily = {
        key: {"date": key, "total": 0, "source": 0, "remix": 0, "transacted": 0, "hits": 0}
        for key in day_keys
    }
    contributors: dict[str, dict] = {}

    def contributor(number: str, name: str) -> dict:
        identity = number or f"name:{name}"
        if identity not in contributors:
            contributors[identity] = {
                "number": number,
                "name": name or number or "未识别上传人",
                "total": 0,
                "source": 0,
                "remix": 0,
                "transacted": 0,
                "hits": 0,
                "daily": {
                    key: {"date": key, "total": 0, "source": 0, "remix": 0, "transacted": 0, "hits": 0}
                    for key in day_keys
                },
            }
        return contributors[identity]

    for asset_id, modified_at, library_type, number, name in upload_rows:
        local_date = (modified_at + shanghai_offset).date().isoformat()
        if local_date not in daily:
            continue
        kind = library_type if library_type in {"source", "remix"} else "source"
        number_text = str(number or "")
        name_text = str(name or number or "未识别上传人")
        daily[local_date]["total"] += 1
        daily[local_date][kind] += 1
        person = contributor(number_text, name_text)
        person["total"] += 1
        person[kind] += 1
        person["daily"][local_date]["total"] += 1
        person["daily"][local_date][kind] += 1

    metric_rows = db.execute(
        select(
            QianchuanDelivery.asset_id,
            QianchuanDelivery.advertiser_id,
            QianchuanDelivery.plan_id,
            QianchuanMetricDaily.video_id,
            QianchuanMetricDaily.stat_date,
            QianchuanMetricDaily.metrics,
            QianchuanMetricDaily.updated_at,
        )
        .join(QianchuanMetricDaily, QianchuanMetricDaily.task_id == QianchuanDelivery.id)
        .where(
            QianchuanMetricDaily.stat_date >= range_start.isoformat(),
            QianchuanMetricDaily.stat_date <= range_end.isoformat(),
            QianchuanMetricDaily.status == "success",
            QianchuanMetricDaily.has_data.is_(True),
            QianchuanMetricDaily.link_verified.is_(True),
        )
    ).all()
    deduped: dict[tuple, tuple[float, datetime]] = {}
    for asset_id, advertiser_id, plan_id, video_id, stat_day, metrics, updated_at in metric_rows:
        key = (int(asset_id), advertiser_id or "", plan_id or "", video_id or "", stat_day)
        gmv = _safe_gmv(metrics)
        previous = deduped.get(key)
        if previous is None or gmv > previous[0]:
            deduped[key] = (gmv, updated_at)

    asset_day_gmv: dict[tuple[int, str], float] = defaultdict(float)
    latest_metric_at: datetime | None = None
    for key, (gmv, updated_at) in deduped.items():
        asset_day_gmv[(int(key[0]), str(key[4]))] += gmv
        if updated_at and (latest_metric_at is None or updated_at > latest_metric_at):
            latest_metric_at = updated_at

    asset_period_gmv: dict[int, float] = defaultdict(float)
    for (asset_id, stat_day), gmv in asset_day_gmv.items():
        asset_period_gmv[asset_id] += gmv
        if stat_day in daily and gmv > 0:
            daily[stat_day]["transacted"] += 1
            if gmv > 50000:
                daily[stat_day]["hits"] += 1
        owner = asset_owners.get(asset_id)
        if owner and gmv > 0:
            person = contributor(owner["number"], owner["name"])
            person["daily"][stat_day]["transacted"] += 1
            if gmv > 50000:
                person["daily"][stat_day]["hits"] += 1

    for asset_id, gmv in asset_period_gmv.items():
        if gmv <= 0:
            continue
        owner = asset_owners.get(asset_id)
        if not owner:
            continue
        person = contributor(owner["number"], owner["name"])
        person["transacted"] += 1
        if gmv > 50000:
            person["hits"] += 1

    contributor_items = []
    for item in contributors.values():
        item["daily"] = [item["daily"][key] for key in day_keys]
        contributor_items.append(item)
    contributor_items.sort(key=lambda item: (-item["total"], -item["hits"], item["name"]))

    upload_total = len(upload_rows)
    source_total = sum(1 for row in upload_rows if row.library_type != "remix")
    remix_total = upload_total - source_total
    transacted_assets = sum(1 for value in asset_period_gmv.values() if value > 0)
    hit_assets = sum(1 for value in asset_period_gmv.values() if value > 50000)
    metric_days = len({day for _, day in asset_day_gmv})
    metric_state = "available" if deduped else "unavailable"
    metric_message = (
        f"已读取 {metric_days} 天千川已核验日级数据；无回流的素材不会按成交 0 处理"
        if deduped
        else "所选周期暂无已核验千川日级回流，成交与爆款显示为待回流"
    )
    return {
        "range": {"start_date": range_start.isoformat(), "end_date": range_end.isoformat(), "days": len(day_keys)},
        "summary": {
            "total": upload_total,
            "source": source_total,
            "remix": remix_total,
            "transacted": transacted_assets if deduped else None,
            "hits": hit_assets if deduped else None,
        },
        "daily": [daily[key] for key in day_keys],
        "contributors": contributor_items,
        "coverage": {
            "state": metric_state,
            "message": metric_message,
            "verified_rows": len(deduped),
            "metric_days": metric_days,
            "latest_metric_at": latest_metric_at.isoformat() + "Z" if latest_metric_at else None,
            "gmv_source": "千川已核验日级素材回流",
            "upload_time_basis": "OSS 原文件完成写入时间（Asia/Shanghai）",
        },
    }


@app.post("/api/auth/login")
def auth_login(payload: LoginPayload, request: Request, response: Response):
    token, user = login_with_password(payload.username.strip(), payload.password, payload.captcha)
    require_module_access(user, "cloud-manager")
    request.state.user = user
    response.set_cookie(
        SESSION_COOKIE,
        cookie_value(token),
        httponly=True,
        samesite="lax",
        secure=request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https",
        path="/",
        max_age=60 * 60 * 8,
    )
    return {"user": user, "permissions": user_permissions(user)}


@app.post("/api/auth/captcha")
def auth_captcha(payload: CaptchaPayload):
    result = send_login_captcha(payload.username)
    return {"ok": True, "message": str(result.get("msg") or "验证码已发送")}


@app.get("/api/auth/me")
def auth_me(user: dict = Depends(require_user)):
    return {"user": user, "permissions": user_permissions(user), "access": module_access_for_user(user)}


@app.post("/api/auth/logout")
def auth_logout(response: Response):
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"ok": True}


@app.get("/api/central-auth/me")
def central_auth_me(user: dict = Depends(require_user)):
    department, center = _organization_from_user(user)

    def first_text(*fields: str) -> str:
        for field in fields:
            value = user.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    def avatar_url() -> str:
        for field in ("avatarUrl", "avatar_url", "headImg", "head_img", "photo", "photoUrl", "profilePhotoUrl", "picture"):
            value = user.get(field)
            if isinstance(value, str) and value.strip().startswith("https://"):
                return value.strip()
        avatar = user.get("avatar")
        if isinstance(avatar, str) and avatar.strip().startswith("https://"):
            return avatar.strip()
        if isinstance(avatar, dict):
            for field in ("avatar_origin", "avatar_640", "avatar_240", "avatar_72", "url"):
                value = avatar.get(field)
                if isinstance(value, str) and value.strip().startswith("https://"):
                    return value.strip()
        return ""

    safe_user = {
        field: user.get(field)
        for field in (
            "number", "userId", "id", "realName", "name", "groupName", "parentDept", "deptName",
            "status"
        )
        if user.get(field) is not None
    }
    safe_user["department"] = department
    safe_user["center"] = center
    safe_user["jobTitle"] = first_text(
        "jobTitle", "job_title", "positionName", "position_name", "position", "postName", "post_name", "jobName", "title"
    )
    safe_user["avatarUrl"] = avatar_url()
    return {
        "user": safe_user,
        "permissions": user_permissions(user),
        "access": module_access_for_user(user),
    }


@app.get("/api/organization/member-dashboard")
def organization_member_dashboard(
    end_date: date | None = Query(default=None),
    days: int = Query(default=7, ge=1, le=31),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Return a server-filtered member directory plus attributable root-data outputs."""
    require_module_access(user, "data-dashboard", db)
    shanghai_today = datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).date()
    selected_end = end_date or (shanghai_today - timedelta(days=1))
    if selected_end >= shanghai_today:
        raise HTTPException(400, "人员明细只能查询已结束的完整自然日")
    selected_start = selected_end - timedelta(days=days - 1)
    access, members = _scoped_organization_members(db, user)

    timesheet_snapshot = None
    timesheet_state = "pending"
    timesheet_note = "飞书工时数据待接入"
    try:
        timesheet_snapshot = timesheet_service.get_snapshot(selected_end)
        timesheet_state = str(timesheet_snapshot.get("state") or "partial")
        timesheet_note = str((timesheet_snapshot.get("source") or {}).get("note") or "飞书工时数据已读取")
    except TimesheetError as error:
        timesheet_note = str(error)

    def timesheet_member_visible(item: dict) -> bool:
        if access["scope"] == "department":
            return True
        if access["scope"] == "center":
            return str(item.get("center") or "") in set(access["centers"])
        return personal_identity_match(item, user_number(user), access["personName"])

    # The governed Base is also a complete department directory for the selected
    # month. Merge only rows inside the already-computed server-side scope so a
    # colleague cannot disappear merely because the OA access roster is sparse.
    members = canonical_members(members)
    existing_numbers = {employee_key(item.get("employeeId")) for item in members if item.get("employeeId")}
    existing_names = {_normalized_person_name(item.get("personName", "")) for item in members if item.get("personName")}
    for item in (timesheet_snapshot or {}).get("members") or []:
        employee_id = str(item.get("employeeId") or "").strip()
        person_name = str(item.get("personName") or "").strip()
        if not person_name or not timesheet_member_visible(item):
            continue
        # Known, distinct numbers may share a name; keep both identities. A
        # nameless-number alias must not suppress the authoritative source row.
        if (employee_key(employee_id) in existing_numbers if employee_id else
                _normalized_person_name(person_name) in existing_names):
            continue
        leader_centers = list(ORGANIZATION_LEADER_CENTERS.get(person_name, ()))
        members.append({
            "personName": person_name,
            "employeeId": employee_id,
            "department": "品牌营销部",
            "center": str(item.get("center") or "未分中心"),
            "isLeader": bool(leader_centers),
            "leaderCenters": leader_centers,
        })
        if employee_id:
            existing_numbers.add(employee_key(employee_id))
        existing_names.add(_normalized_person_name(person_name))
    members.sort(key=lambda item: (item["center"] or "未分中心", not item["isLeader"], item["personName"]))

    matched_timesheets = match_timesheets(members, (timesheet_snapshot or {}).get("members") or [])

    rankings_by_name: dict[str, dict] = {}
    performance_state = "ready"
    performance_note = "FanDo 根数据按作者关键词可靠映射；未映射成员不按 0 展示。"
    complete_through = None
    performance_available_end: date | None = None
    try:
        performance_available_end = personal_sales_service.complete_through_date()
        effective_end = min(selected_end, performance_available_end)
        if effective_end < selected_start:
            raise SourcePendingError(
                f"所选日期尚未完整回补，当前完整至 {performance_available_end.isoformat()}；不会将缺失值显示为 0"
            )
        performance = personal_sales_service.get_snapshot(selected_start, effective_end)
        complete_through = (performance.get("meta") or {}).get("completeThroughDate")
        rankings_by_name = {
            _normalized_person_name(item.get("personName", "")): item
            for item in performance.get("rankings") or []
            if item.get("personName")
        }
        if effective_end < selected_end:
            performance_state = "partial"
            performance_note = (
                f"当前展示 {selected_start.isoformat()} 至 {effective_end.isoformat()} 的最近成功个人根数据；"
                f"{effective_end + timedelta(days=1)} 至 {selected_end.isoformat()} 保持待回补，不按 0 处理。"
            )
        if (performance.get("quality") or {}).get("mappedGmvRate") not in (None, 1, 1.0):
            performance_state = "partial"
            performance_note += " 部分根数据素材未能可靠映射到人员；未映射部分保留待核验。"
    except PersonalSalesError as error:
        performance_state = "pending"
        performance_note = str(error)

    daily_by_name: dict[str, list[dict]] = {
        _normalized_person_name(item["personName"]): [] for item in members
    }
    for offset in range(days):
        day = selected_start + timedelta(days=offset)
        day_rankings: dict[str, dict] = {}
        day_state = "pending"
        if performance_available_end is not None and day <= performance_available_end:
            try:
                day_snapshot = personal_sales_service.get_snapshot(day, day)
                day_rankings = {
                    _normalized_person_name(item.get("personName", "")): item
                    for item in day_snapshot.get("rankings") or []
                    if item.get("personName")
                }
                day_state = "ready"
            except PersonalSalesError:
                day_state = "pending"
        for name in daily_by_name:
            ranking = day_rankings.get(name)
            daily_by_name[name].append({
                "date": day.isoformat(),
                "materialCount": int(ranking["materialCount"]) if ranking else None,
                "gmvYuan": float(ranking["totalGmvYuan"]) if ranking else None,
                "state": "ready" if ranking else day_state if day_state == "pending" else "unmapped",
            })

    items = []
    for member, timesheet in zip(members, matched_timesheets):
        normalized_name = _normalized_person_name(member["personName"])
        ranking = rankings_by_name.get(normalized_name)
        items.append({
            **member,
            "performance": {
                "state": "ready" if ranking else performance_state if performance_state == "pending" else "unmapped",
                "materialCount": int(ranking["materialCount"]) if ranking else None,
                "qianchuanMaterialCount": int(ranking["qianchuanMaterialCount"]) if ranking else None,
                "videoMaterialCount": int(ranking["videoMaterialCount"]) if ranking else None,
                "totalGmvYuan": float(ranking["totalGmvYuan"]) if ranking else None,
                "qianchuanGmvYuan": float(ranking["qianchuanGmvYuan"]) if ranking else None,
                "videoGmvYuan": float(ranking["videoGmvYuan"]) if ranking else None,
            },
            "signals": {
                "workloadScore": None,
                "vitalityScore": None,
                "heatScore": None,
                "saturationScore": None,
                "state": "pending",
                "note": "个人会议、任务与工时证据尚未可靠归因，不以中心均值代替本人。",
            },
            "timesheet": {
                "state": timesheet_state if timesheet else "unmapped" if timesheet_snapshot else "pending",
                "monthLabel": (timesheet or {}).get("monthLabel") or (timesheet_snapshot or {}).get("monthLabel"),
                "averageEffectiveHours": (timesheet or {}).get("averageEffectiveHours"),
                "totalEffectiveHours": (timesheet or {}).get("totalEffectiveHours"),
                "scheduledDays": (timesheet or {}).get("scheduledDays"),
                "punchDays": (timesheet or {}).get("punchDays"),
                "averageAttendanceHours": (timesheet or {}).get("averageAttendanceHours"),
            },
            "daily": daily_by_name.get(normalized_name, []),
        })

    visible_timesheets = [item["timesheet"] for item in items if item["timesheet"]["averageEffectiveHours"] is not None]

    def average_timesheet(field: str) -> float | None:
        values = [float(item[field]) for item in visible_timesheets if item.get(field) is not None]
        return round(sum(values) / len(values), 2) if values else None

    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone(timedelta(hours=8), "Asia/Shanghai")).isoformat(),
        "range": {"startDate": selected_start.isoformat(), "endDate": selected_end.isoformat(), "days": days},
        "access": access,
        "summary": {
            "visibleMembers": len(items),
            "mappedMembers": sum(1 for item in items if item["performance"]["state"] == "ready"),
            "unmappedMembers": sum(1 for item in items if item["performance"]["state"] != "ready"),
            "timesheetMappedMembers": len(visible_timesheets),
            "averageEffectiveHours": average_timesheet("averageEffectiveHours"),
            "averageAttendanceHours": average_timesheet("averageAttendanceHours"),
            "averageScheduledDays": average_timesheet("scheduledDays"),
            "averagePunchDays": average_timesheet("punchDays"),
        },
        "source": {
            "directory": "OA 登录授权成员目录",
            "performance": "FanDo 根数据个人素材成交快照",
            "performanceState": performance_state,
            "performanceNote": performance_note,
            "completeThroughDate": complete_through,
            "timesheet": (timesheet_snapshot or {}).get("source"),
            "timesheetState": timesheet_state,
            "timesheetMode": (timesheet_snapshot or {}).get("sourceMode"),
            "timesheetMonth": (timesheet_snapshot or {}).get("monthLabel"),
            "timesheetGeneratedAt": (timesheet_snapshot or {}).get("generatedAt"),
            "timesheetNote": timesheet_note,
        },
        "members": items,
    }


def _business_overview_with_lineage(snapshot: dict, db: Session) -> dict:
    """Attach only verified local asset links; unmatched root rows remain visibly unmatched."""
    result = json.loads(json.dumps(snapshot, ensure_ascii=False))
    materials = result.get("topMaterials") or []
    material_ids = [str(item.get("materialId") or "") for item in materials if item.get("materialId")]
    if not material_ids:
        return result
    rows = db.execute(
        select(QianchuanDelivery, Asset)
        .join(Asset, Asset.id == QianchuanDelivery.asset_id)
        .where(
            QianchuanDelivery.platform_asset_id.in_(material_ids),
            QianchuanDelivery.deleted_at.is_(None),
        )
        .order_by(QianchuanDelivery.updated_at.desc())
    ).all()
    asset_ids = {asset.id for _delivery, asset in rows}
    provenance_by_asset: dict[int, dict] = {}
    if asset_ids:
        return_rows = db.scalars(
            select(WorkstationReturn).where(
                WorkstationReturn.asset_id.in_(asset_ids),
                WorkstationReturn.status == "completed",
            ).order_by(WorkstationReturn.completed_at.desc())
        ).all()
        for item in return_rows:
            if item.asset_id is not None and item.asset_id not in provenance_by_asset:
                provenance_by_asset[item.asset_id] = item.provenance or {}

    by_pair: dict[tuple[str, str], tuple[QianchuanDelivery, Asset]] = {}
    by_material: dict[str, tuple[QianchuanDelivery, Asset]] = {}
    for delivery, asset in rows:
        material_id = str(delivery.platform_asset_id or "")
        advertiser_id = str(delivery.advertiser_id or "")
        by_pair.setdefault((material_id, advertiser_id), (delivery, asset))
        by_material.setdefault(material_id, (delivery, asset))

    linked = 0
    for item in materials:
        material_id = str(item.get("materialId") or "")
        match = next(
            (
                by_pair[(material_id, str(advertiser_id))]
                for advertiser_id in item.get("advertiserIds") or []
                if (material_id, str(advertiser_id)) in by_pair
            ),
            by_material.get(material_id),
        )
        if not match:
            item["lineage"] = {
                "state": "unlinked",
                "note": "根数据已识别该素材，但尚未与当前中枢推送记录匹配",
            }
            continue
        delivery, asset = match
        provenance = provenance_by_asset.get(asset.id) or {}
        linked += 1
        item["lineage"] = {
            "state": "linked",
            "assetId": asset.id,
            "assetName": asset.filename,
            "libraryType": asset.library_type,
            "businessStage": "二创混剪" if asset.library_type == "remix" else "素材存储与推送回流",
            "source": asset.source or asset.ingest_source or "WIS素材库",
            "uploadedByName": asset.uploaded_by_name or None,
            "deliveryStatus": delivery.status,
            "linkedAt": delivery.updated_at.isoformat() + "Z" if delivery.updated_at else None,
            "workstationProvenance": provenance or None,
        }
    quality = result.setdefault("quality", {})
    quality["topMaterialLineageLinkedCount"] = linked
    quality["topMaterialLineageCoverageRate"] = round(linked / len(materials), 4) if materials else None
    return result


class LongTermWorkSchedulerUpdate(BaseModel):
    enabled: bool
    hour: int = Field(default=10, ge=0, le=23)
    minute: int = Field(default=15, ge=0, le=59)


def _long_term_work_for_user(snapshot: dict, user: dict) -> dict:
    access = _organization_dashboard_scope(user)
    items = list(snapshot.get("items") or [])
    if access["scope"] == "center":
        allowed_centers = set(access.get("centers") or [])
        items = [item for item in items if item.get("center") == "ALL" or item.get("center") in allowed_centers]
        visibility_note = f"仅展示部门公共及 {'、'.join(access.get('centers') or [])} 长期工作。"
    elif access["scope"] == "personal":
        person_name = access.get("personName") or ""
        items = [item for item in items if person_name and person_name in (item.get("owners") or [])]
        visibility_note = "仅展示负责人明确匹配本人的长期工作；未匹配不展示他人事项。"
    else:
        visibility_note = "展示部门公共和各中心长期工作。"
    return {**snapshot, "items": items, "visibilityNote": visibility_note}


@app.get("/api/business-intelligence/long-term-work")
def business_intelligence_long_term_work(
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "data-dashboard", db)
    return _long_term_work_for_user(long_term_work_service.snapshot(), user)


@app.get("/api/business-intelligence/long-term-work/scheduler")
def business_intelligence_long_term_work_scheduler(request_id: str | None = Query(default=None, max_length=96), user: dict = Depends(require_user)):
    require_operation_admin(user)
    try:
        return long_term_work_service.status(request_id=request_id)
    except ValueError:
        raise HTTPException(400, "刷新请求编号或记录不合法") from None


@app.put("/api/business-intelligence/long-term-work/scheduler")
def business_intelligence_long_term_work_scheduler_update(
    payload: LongTermWorkSchedulerUpdate,
    user: dict = Depends(require_user),
):
    require_operation_admin(user)
    try:
        return long_term_work_service.configure(
            enabled=payload.enabled,
            hour=payload.hour,
            minute=payload.minute,
        )
    except LongTermWorkError as error:
        raise HTTPException(400, str(error)) from error


class LongTermWorkRunRequest(BaseModel):
    requestId: str = Field(default_factory=lambda: "manual-" + uuid4().hex, min_length=1, max_length=96, pattern=r"^[A-Za-z0-9_.:-]+$")


@app.post("/api/business-intelligence/long-term-work/scheduler/run")
def business_intelligence_long_term_work_scheduler_run(payload: LongTermWorkRunRequest | None = None, user: dict = Depends(require_user)):
    require_operation_admin(user)
    try:
        return JSONResponse(status_code=202, content=long_term_work_service.start_run((payload or LongTermWorkRunRequest()).requestId))
    except LongTermWorkError as error:
        status_code = 409 if error.state == "running" else 503
        if error.state == "running":
            return JSONResponse(status_code=409, content={"detail": str(error), "accepted": False, "current": long_term_work_service.status()})
        raise HTTPException(status_code, str(error)) from error


@app.get("/api/business-intelligence/overview")
def business_intelligence_overview(
    target_date: date | None = Query(default=None, alias="date"),
    days: int = Query(default=7, ge=1, le=30),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "data-dashboard", db)
    try:
        snapshot = business_intelligence_service.get_overview(target_date, days)
        result = _business_overview_with_lineage(snapshot, db)
        result.pop("_materialCandidates", None)
        return result
    except BusinessIntelligenceError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        logger.exception("Business intelligence overview failed")
        raise HTTPException(503, "经营数据暂时不可用，且没有可展示的成功快照") from error


def _creative_incentive_milestone_out(item: CreativeIncentiveMilestone) -> dict:
    return {
        "id": item.id,
        "directionId": item.direction_id,
        "pointNumber": item.point_number,
        "thresholdGmvYuan": item.threshold_gmv_yuan,
        "observedGmvYuan": item.observed_gmv_yuan,
        "observedCostYuan": item.observed_cost_yuan,
        "observedRoi": item.observed_roi,
        "sourceCutoffAt": item.source_cutoff_at or None,
        "shareType": item.share_type,
        "shareText": item.share_text,
        "shareStatus": item.share_status,
        "publishedByName": item.published_by_name,
        "publishedAt": item.published_at.isoformat() + "Z" if item.published_at else None,
        "feishuMessageId": item.feishu_message_id,
        "deliveryError": item.delivery_error,
        "createdAt": item.created_at.isoformat() + "Z",
    }


def _creative_incentive_direction_out(
    item: CreativeIncentiveDirection,
    milestones: list[CreativeIncentiveMilestone],
) -> dict:
    points = sorted(milestones, key=lambda row: row.point_number)
    return {
        "id": item.id,
        "directionName": item.direction_name,
        "materialId": item.material_id,
        "materialName": item.material_name,
        "creatorNumber": item.creator_number,
        "creatorName": item.creator_name,
        "department": item.department,
        "center": item.center,
        "onlineDate": item.online_date,
        "originalityStatus": item.originality_status,
        "originalityNote": item.originality_note,
        "confirmedByName": item.confirmed_by_name,
        "confirmedAt": item.confirmed_at.isoformat() + "Z" if item.confirmed_at else None,
        "copyJudgement": item.copy_judgement,
        "visualJudgement": item.visual_judgement,
        "voiceJudgement": item.voice_judgement,
        "remixPlan": item.remix_plan,
        "referenceUrl": item.reference_url,
        "metricStatus": item.metric_status,
        "latestGmvYuan": item.latest_gmv_yuan,
        "latestCostYuan": item.latest_cost_yuan,
        "latestRoi": item.latest_roi,
        "sourceCutoffAt": item.source_cutoff_at or None,
        "sourceUpdatedAt": item.source_updated_at or None,
        "syncedAt": item.synced_at.isoformat() + "Z" if item.synced_at else None,
        "earnedPoints": len(points),
        "nextThresholdGmvYuan": (len(points) + 1) * POINT_GMV_YUAN,
        "milestones": [_creative_incentive_milestone_out(row) for row in points],
        "createdByName": item.created_by_name,
        "createdAt": item.created_at.isoformat() + "Z",
        "updatedAt": item.updated_at.isoformat() + "Z",
    }


def _visible_creative_incentive_directions(
    db: Session,
    user: dict,
) -> tuple[dict, list[CreativeIncentiveDirection]]:
    access = _organization_dashboard_scope(user)
    rows = db.scalars(
        select(CreativeIncentiveDirection)
        .order_by(CreativeIncentiveDirection.created_at.desc())
    ).all()
    if access["scope"] == "department":
        return access, list(rows)
    if access["scope"] == "center":
        allowed_centers = set(access["centers"])
        return access, [row for row in rows if row.center in allowed_centers]
    current_number = str(user.get("number") or user.get("userId") or user.get("id") or "").strip().upper()
    current_name = _normalized_person_name(user_name(user))
    return access, [
        row for row in rows
        if (current_number and row.creator_number.strip().upper() == current_number)
        or _normalized_person_name(row.creator_name) == current_name
    ]


def _creative_incentive_overview_payload(db: Session, user: dict) -> dict:
    access, directions = _visible_creative_incentive_directions(db, user)
    direction_ids = [row.id for row in directions]
    milestone_rows = db.scalars(
        select(CreativeIncentiveMilestone)
        .where(CreativeIncentiveMilestone.direction_id.in_(direction_ids))
        .order_by(CreativeIncentiveMilestone.created_at.desc())
    ).all() if direction_ids else []
    milestones_by_direction: dict[str, list[CreativeIncentiveMilestone]] = defaultdict(list)
    for milestone in milestone_rows:
        milestones_by_direction[milestone.direction_id].append(milestone)
    pending_drafts = sum(1 for item in milestone_rows if item.share_status != "published")
    qualified = sum(
        1 for item in directions
        if item.originality_status == "confirmed"
        and item.latest_gmv_yuan is not None
        and item.latest_gmv_yuan >= POINT_GMV_YUAN
    )
    return {
        "schemaVersion": 1,
        "generatedAt": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
        "access": access,
        "rule": {
            "originality": "仅统计经人工确认为全新原创方向的素材；系统不自动判断原创性",
            "pointGmvYuan": POINT_GMV_YUAN,
            "pointRule": "每满2万元素材归因GMV计1分",
            "firstShare": "首次达到2万元生成完整分享",
            "followupShare": "4万、6万及后续节点只更新数据与积分",
            "metric": "千川 ROI2 素材归因 GMV；缺失、未匹配或未返回保持待核验，不按0处理",
        },
        "summary": {
            "directionCount": len(directions),
            "confirmedCount": sum(1 for item in directions if item.originality_status == "confirmed"),
            "qualifiedCount": qualified,
            "totalPoints": len(milestone_rows),
            "pendingDraftCount": pending_drafts,
        },
        "groupDelivery": {
            "configured": bool(os.getenv("FEISHU_CREATIVE_INCENTIVE_WEBHOOK_URL", "").strip()),
            "target": "创意突破分享群",
            "fallback": "未配置时保留草稿，可复制到群后人工标记已发布",
        },
        "directions": [
            _creative_incentive_direction_out(item, milestones_by_direction[item.id])
            for item in directions
        ],
    }


@app.get("/api/creative-incentives/overview")
def creative_incentive_overview(
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "material-incentive", db)
    return _creative_incentive_overview_payload(db, user)


@app.post("/api/creative-incentives/directions")
def creative_incentive_create(
    payload: CreativeIncentiveDirectionCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "material-incentive", db)
    material_id = payload.material_id.strip()
    if db.scalar(select(CreativeIncentiveDirection.id).where(CreativeIncentiveDirection.material_id == material_id)):
        raise HTTPException(409, "该素材ID已登记，请直接查看现有方向")
    shanghai_today = datetime.now(timezone(timedelta(hours=8))).date()
    if payload.online_date > shanghai_today:
        raise HTTPException(422, "首次上线日期不能晚于今天")
    access = _organization_dashboard_scope(user)
    current_name = user_name(user)
    current_number = user_number(user)
    creator_name = payload.creator_name.strip() or current_name
    creator_number = payload.creator_number.strip()
    center = _canonical_organization_center(payload.center) or payload.center.strip()
    if access["scope"] == "personal":
        if _normalized_person_name(creator_name) != _normalized_person_name(current_name):
            raise HTTPException(403, "专员级权限只能登记本人原创方向")
        creator_name = current_name
        creator_number = current_number
        _, raw_center = _organization_from_user(user)
        center = _canonical_organization_center(raw_center) or center
    elif access["scope"] == "center":
        if center and center not in set(access["centers"]):
            raise HTTPException(403, "主管级权限只能登记本中心原创方向")
        center = center or access["centers"][0]
    department, _ = _organization_from_user(user)
    row = CreativeIncentiveDirection(
        id=str(uuid4()),
        direction_name=payload.direction_name.strip(),
        material_id=material_id,
        material_name=payload.material_name.strip(),
        creator_number=creator_number,
        creator_name=creator_name,
        department=department or "品牌营销部",
        center=center,
        online_date=payload.online_date.isoformat(),
        originality_note=payload.originality_note.strip(),
        copy_judgement=payload.copy_judgement.strip(),
        visual_judgement=payload.visual_judgement.strip(),
        voice_judgement=payload.voice_judgement.strip(),
        remix_plan=payload.remix_plan.strip(),
        reference_url=payload.reference_url.strip(),
        created_by_number=current_number,
        created_by_name=current_name,
    )
    db.add(row)
    try:
        db.commit()
    except IntegrityError as error:
        db.rollback()
        raise HTTPException(409, "该素材ID已登记，请直接查看现有方向") from error
    db.refresh(row)
    return _creative_incentive_direction_out(row, [])


@app.patch("/api/creative-incentives/directions/{direction_id}/originality")
def creative_incentive_confirm_originality(
    direction_id: str,
    payload: CreativeIncentiveConfirm,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "material-incentive", db)
    _, visible = _visible_creative_incentive_directions(db, user)
    row = next((item for item in visible if item.id == direction_id), None)
    if not row:
        raise HTTPException(404, "原创方向不存在或不在当前可见范围")
    milestones = db.scalars(
        select(CreativeIncentiveMilestone)
        .where(CreativeIncentiveMilestone.direction_id == row.id)
    ).all()
    if not payload.confirmed and milestones:
        raise HTTPException(409, "该方向已产生积分记录，不能直接取消原创确认")
    row.originality_status = "confirmed" if payload.confirmed else "rejected"
    if payload.note.strip():
        row.originality_note = payload.note.strip()
    row.confirmed_by_number = user_number(user)
    row.confirmed_by_name = user_name(user)
    row.confirmed_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _creative_incentive_direction_out(row, list(milestones))


@app.post("/api/creative-incentives/sync")
def creative_incentive_sync(
    target_date: date | None = Query(default=None, alias="date"),
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "material-incentive", db)
    _, directions = _visible_creative_incentive_directions(db, user)
    selected_date = target_date or business_intelligence_service.expected_target_date()
    try:
        snapshot = business_intelligence_service.get_overview(selected_date, 7, force=force)
        if directions and not snapshot.get("_materialCandidates") and not force:
            snapshot = business_intelligence_service.get_overview(selected_date, 7, force=True)
        sync_result = sync_directions(db, directions, snapshot)
    except BusinessIntelligenceError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        logger.exception("Creative incentive sync failed")
        raise HTTPException(503, "千川素材激励数据暂时不可用，已保留上次成功结果") from error
    result = _creative_incentive_overview_payload(db, user)
    result["sync"] = {
        **sync_result,
        "requestedDate": selected_date.isoformat(),
        "actualDate": (snapshot.get("query") or {}).get("productDate"),
        "sourceStatus": snapshot.get("status"),
        "candidateBoundary": "仅匹配当前根数据高成交候选；未匹配保持待核验",
    }
    return result


@app.post("/api/creative-incentives/milestones/{milestone_id}/publish")
def creative_incentive_publish(
    milestone_id: str,
    payload: CreativeIncentiveMilestonePublish,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "material-incentive", db)
    milestone = db.get(CreativeIncentiveMilestone, milestone_id)
    if not milestone:
        raise HTTPException(404, "激励分享草稿不存在")
    _, visible = _visible_creative_incentive_directions(db, user)
    direction = next((item for item in visible if item.id == milestone.direction_id), None)
    if not direction:
        raise HTTPException(404, "激励分享草稿不存在或不在当前可见范围")
    if milestone.share_status == "published":
        return _creative_incentive_milestone_out(milestone)
    if payload.mode == "feishu":
        status, message_id, error = deliver_share_text(milestone.share_text)
        milestone.delivery_error = error
        if status == "sent":
            milestone.share_status = "published"
            milestone.feishu_message_id = message_id
        else:
            milestone.share_status = "draft"
    else:
        milestone.share_status = "published"
        milestone.delivery_error = ""
    if milestone.share_status == "published":
        milestone.published_by_number = user_number(user)
        milestone.published_by_name = user_name(user)
        milestone.published_at = datetime.utcnow()
    db.commit()
    db.refresh(milestone)
    return _creative_incentive_milestone_out(milestone)


@app.get("/api/business-intelligence/omnichannel-realtime")
def business_intelligence_omnichannel_realtime(
    force: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_module_access(user, "data-dashboard", db)
    try:
        return omnichannel_intelligence_service.get_realtime(force=force)
    except OmnichannelIntelligenceError as error:
        raise HTTPException(503, str(error)) from error
    except Exception as error:
        logger.exception("Omnichannel business intelligence failed")
        raise HTTPException(503, "抖店与视频号实时经营数据暂时不可用") from error


@app.get("/api/business-intelligence/material-uploads")
def business_intelligence_material_uploads(
    target_date: date | None = Query(default=None, alias="date"),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Return complete-day root-data material online/publish counts."""
    require_module_access(user, "data-dashboard", db)
    shanghai_tz = timezone(timedelta(hours=8))
    yesterday = datetime.now(shanghai_tz).date() - timedelta(days=1)
    selected = target_date or yesterday
    if selected > yesterday:
        raise HTTPException(400, "素材上传数量只能查询截至昨天的完整自然日")
    try:
        return root_material_upload_service.get_snapshot(selected)
    except RootMaterialUploadError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/assistant/status")
def assistant_status(user: dict = Depends(require_user)):
    access = module_access_for_user(user)
    return {
        **ai_assistant_service.status(),
        "allowed_modules": access["allowed_modules"],
    }


def _assistant_conversation_out(row: AssistantConversation) -> dict:
    return {
        "id": row.id,
        "title": row.title,
        "last_message_preview": row.last_message_preview,
        "message_count": int(row.message_count or 0),
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _assistant_attachment_out(row: AssistantAttachment) -> dict:
    return {
        "id": row.id,
        "filename": row.filename,
        "mime_type": row.mime_type,
        "file_size": int(row.file_size or 0),
        "preview_url": f"/api/assistant/attachments/{row.id}/content",
        "created_at": row.created_at.isoformat() + "Z",
    }


def _assistant_messages_out(db: Session, rows: list[AssistantConversationMessage]) -> list[dict]:
    message_ids = [row.id for row in rows]
    attachments = db.scalars(
        select(AssistantAttachment)
        .where(AssistantAttachment.message_id.in_(message_ids))
        .order_by(AssistantAttachment.created_at)
    ).all() if message_ids else []
    by_message: dict[str, list[dict]] = defaultdict(list)
    for attachment in attachments:
        by_message[attachment.message_id].append(_assistant_attachment_out(attachment))
    return [
        {
            "id": row.id,
            "role": row.role,
            "content": row.content,
            "actions": list(row.actions or []),
            "meta": dict(row.message_meta or {}),
            "attachments": by_message.get(row.id, []),
            "created_at": row.created_at.isoformat() + "Z",
        }
        for row in rows
    ]


def _assistant_owned_conversation(db: Session, conversation_id: str, user: dict) -> AssistantConversation:
    row = db.scalar(select(AssistantConversation).where(
        AssistantConversation.id == conversation_id,
        AssistantConversation.owner_number == user_number(user),
    ))
    if not row:
        raise HTTPException(404, "未找到该会话")
    return row


def _assistant_refresh_conversation(db: Session, row: AssistantConversation, preview: str = "") -> None:
    row.message_count = int(db.scalar(
        select(func.count()).select_from(AssistantConversationMessage).where(
            AssistantConversationMessage.conversation_id == row.id,
            AssistantConversationMessage.owner_number == row.owner_number,
        )
    ) or 0)
    if preview.strip():
        row.last_message_preview = re.sub(r"\s+", " ", preview).strip()[:300]
    row.updated_at = datetime.utcnow()


@app.get("/api/assistant/conversations")
def assistant_conversations(
    q: str = "",
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    query = select(AssistantConversation).where(
        AssistantConversation.owner_number == user_number(user)
    )
    if q.strip():
        query = query.where(AssistantConversation.title.like(f"%{q.strip()}%"))
    rows = db.scalars(query.order_by(AssistantConversation.updated_at.desc())).all()
    return {"items": [_assistant_conversation_out(row) for row in rows], "total": len(rows)}


@app.post("/api/assistant/conversations")
def assistant_conversation_create(
    payload: AssistantConversationCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    title = re.sub(r"\s+", " ", payload.title).strip() or "新会话"
    row = AssistantConversation(
        id=str(uuid4()),
        owner_number=user_number(user),
        owner_name=user_name(user),
        title=title[:160],
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _assistant_conversation_out(row)


@app.get("/api/assistant/conversations/{conversation_id}/messages")
def assistant_conversation_messages(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    conversation = _assistant_owned_conversation(db, conversation_id, user)
    rows = db.scalars(
        select(AssistantConversationMessage).where(
            AssistantConversationMessage.conversation_id == conversation.id,
            AssistantConversationMessage.owner_number == user_number(user),
        ).order_by(AssistantConversationMessage.created_at, AssistantConversationMessage.id)
    ).all()
    return {
        "conversation": _assistant_conversation_out(conversation),
        "items": _assistant_messages_out(db, list(rows)),
    }


@app.patch("/api/assistant/conversations/{conversation_id}")
def assistant_conversation_update(
    conversation_id: str,
    payload: AssistantConversationUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    row = _assistant_owned_conversation(db, conversation_id, user)
    title = re.sub(r"\s+", " ", payload.title).strip()
    if not title:
        raise HTTPException(400, "会话名称不能为空")
    row.title = title[:160]
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _assistant_conversation_out(row)


@app.delete("/api/assistant/conversations/{conversation_id}")
def assistant_conversation_delete(
    conversation_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    row = _assistant_owned_conversation(db, conversation_id, user)
    attachments = db.scalars(select(AssistantAttachment).where(
        AssistantAttachment.conversation_id == row.id,
        AssistantAttachment.owner_number == user_number(user),
    )).all()
    for attachment in attachments:
        try:
            Path(attachment.storage_path).unlink(missing_ok=True)
        except OSError:
            logger.warning("Could not remove assistant attachment id=%s", attachment.id)
    db.execute(delete(AssistantAttachment).where(AssistantAttachment.conversation_id == row.id))
    db.execute(delete(AssistantConversationMessage).where(AssistantConversationMessage.conversation_id == row.id))
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": conversation_id}


_ASSISTANT_IMAGE_SIGNATURES = {
    "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda value: len(value) >= 12 and value.startswith(b"RIFF") and value[8:12] == b"WEBP",
}
_ASSISTANT_IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


@app.post("/api/assistant/attachments")
def assistant_attachment_create(
    payload: AssistantAttachmentCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    conversation = _assistant_owned_conversation(db, payload.conversation_id, user)
    mime_type = payload.mime_type.strip().lower()
    validator = _ASSISTANT_IMAGE_SIGNATURES.get(mime_type)
    if not validator:
        raise HTTPException(415, "仅支持 PNG、JPG、WEBP 或 GIF 图片")
    encoded = payload.data_base64.split(",", 1)[-1]
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(400, "图片数据无效，请重新选择") from error
    if not content or len(content) > settings.ai_assistant_image_max_bytes:
        max_mb = settings.ai_assistant_image_max_bytes // (1024 * 1024)
        raise HTTPException(413, f"单张图片不能超过 {max_mb} MB")
    if not validator(content):
        raise HTTPException(415, "图片格式与文件内容不一致")
    owner = user_number(user)
    owner_dir = Path(settings.ai_assistant_upload_dir) / hashlib.sha256(owner.encode("utf-8")).hexdigest()[:16]
    owner_dir.mkdir(parents=True, exist_ok=True)
    attachment_id = str(uuid4())
    target = owner_dir / f"{attachment_id}{_ASSISTANT_IMAGE_EXTENSIONS[mime_type]}"
    target.write_bytes(content)
    safe_name = Path(payload.filename).name.strip()[:255] or f"图片{_ASSISTANT_IMAGE_EXTENSIONS[mime_type]}"
    row = AssistantAttachment(
        id=attachment_id,
        conversation_id=conversation.id,
        owner_number=owner,
        owner_name=user_name(user),
        filename=safe_name,
        mime_type=mime_type,
        file_size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        storage_path=str(target),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _assistant_attachment_out(row)


@app.get("/api/assistant/attachments/{attachment_id}/content")
def assistant_attachment_content(
    attachment_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    row = db.scalar(select(AssistantAttachment).where(
        AssistantAttachment.id == attachment_id,
        AssistantAttachment.owner_number == user_number(user),
    ))
    if not row or not Path(row.storage_path).is_file():
        raise HTTPException(404, "未找到该图片")
    return FileResponse(
        row.storage_path,
        media_type=row.mime_type,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )


@app.delete("/api/assistant/attachments/{attachment_id}")
def assistant_attachment_delete(
    attachment_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    row = db.scalar(select(AssistantAttachment).where(
        AssistantAttachment.id == attachment_id,
        AssistantAttachment.owner_number == user_number(user),
    ))
    if not row:
        raise HTTPException(404, "未找到该图片")
    if row.message_id:
        raise HTTPException(409, "已发送消息中的图片不能单独删除")
    try:
        Path(row.storage_path).unlink(missing_ok=True)
    except OSError:
        logger.warning("Could not remove pending assistant attachment id=%s", row.id)
    db.delete(row)
    db.commit()
    return {"deleted": True, "id": attachment_id}


def _assistant_context(access: dict, can_manage_permissions: bool) -> dict:
    view = ai_insight_service.view(access["allowed_modules"], can_manage_permissions)
    return {
        "scanned_at": view["snapshot"].get("scanned_at"),
        "sources": view["snapshot"].get("sources", []),
        "summary": view["summary"],
        "snapshot": view["snapshot"],
        "insights": [
            {
                "title": item["title"],
                "severity": item["severity"],
                "evidence": item["evidence"],
                "suggestion": item["suggestion"],
            }
            for item in view["insights"][:8]
        ],
    }


def _assistant_needs_business_data(messages: list[dict[str, str]]) -> bool:
    latest = (messages[-1].get("content") if messages else "") or ""
    keywords = (
        "昨天", "前天", "成交", "gmv", "销售", "单品", "经营", "业绩",
        "有效素材", "素材有效率", "roi", "top", "排名", "业务链路", "素材来源",
    )
    normalized = latest.lower()
    return any(keyword in normalized for keyword in keywords)


def _compact_business_context(business: dict) -> dict:
    return {
        "status": business.get("status"),
        "generatedAt": business.get("generatedAt"),
        "query": business.get("query"),
        "summary": business.get("summary"),
        "products": [
            {
                key: item.get(key)
                for key in (
                    "standardProductName", "effectiveSalesYuan", "orderCount",
                    "productQuantity", "salesSharePct", "mapped",
                )
            }
            for item in (business.get("products") or [])
        ],
        "topMaterials": [
            {
                key: item.get(key)
                for key in (
                    "materialId", "materialName", "gmvYuan", "costYuan", "roi",
                    "orderCount", "activeDayCount", "sourcePlatforms",
                )
            }
            for item in (business.get("topMaterials") or [])
        ],
        "quality": business.get("quality"),
        "coverage": business.get("coverage"),
        "warnings": ((business.get("coverage") or {}).get("warnings") or []),
        "definitions": business.get("definitions"),
    }


def _assistant_number(value, decimals: int = 2, multiplier: float = 1.0) -> str:
    import math
    try:
        if value is None or isinstance(value, bool):
            return "待核验"
        number = float(value) * multiplier
        if not math.isfinite(number):
            return "待核验"
        if decimals == 0 and not number.is_integer():
            return "待核验"
        return f"{number:,.{decimals}f}"
    except (TypeError, ValueError, OverflowError):
        return "待核验"


def _assistant_is_exact_business_lookup(question: str) -> bool:
    normalized = question.lower()
    lookup_terms = ("多少", "分别", "查询", "列出", "排名", "top", "明细")
    analysis_terms = ("分析", "为什么", "原因", "建议", "优化", "规律", "诊断")
    return any(term in normalized for term in lookup_terms) and not any(term in normalized for term in analysis_terms)


def _business_readback_fallback(business: dict, question: str, model_unavailable: bool = True) -> dict:
    query = business.get("query") or {}
    summary = business.get("summary") or {}
    products = business.get("products") or []
    coverage = business.get("coverage") or {}
    product_coverage = coverage.get("product") or {}
    product_date = str(product_coverage.get("realBusinessDate") or query.get("productDate") or "日期待核验")
    product_lines = [
        f"{index}. {item.get('standardProductName') or '未命名分类'}："
        f"{_assistant_number(item.get('effectiveSalesYuan'))} 元，"
        f"{_assistant_number(item.get('orderCount'), 0)} 单"
        for index, item in enumerate(products, 1)
    ]
    answer_parts = []
    if business.get("status") != "ready":
        answer_parts.append("数据状态：来源尚未全部就绪，以下仅为已有证据，不代表最新完整数据。")
    if query.get("productDate") and product_date != str(query["productDate"]):
        answer_parts.append(f"请求日期为 {query['productDate']}，当前商品事实日期为 {product_date}。")
    for key, label in (("product", "商品"), ("material", "素材")):
        source = coverage.get(key) or {}
        if source:
            answer_parts.append(f"{label}来源：状态 {source.get('state') or '待核验'}；事实日期 {source.get('realBusinessDate') or '待核验'}；读取时间 {source.get('readAt') or '待核验'}；口径 {source.get('sourceMode') or '待核验'}。")
    warnings = business.get("warnings") or coverage.get("warnings") or []
    if warnings:
        answer_parts.append("来源提示：" + "；".join(str(item) for item in warnings))
    if products:
        answer_parts.append(
            f"**结论：{product_date} 单品有效成交额合计 "
            f"{_assistant_number(summary.get('productEffectiveSalesYuan'))} 元。**"
        )
        answer_parts.append("成交额前三名：\n" + "\n".join(product_lines[:3]))
        if "各单品" in question or "分别" in question:
            answer_parts.append("全部单品：\n" + "\n".join(product_lines))
    if any(keyword in question.lower() for keyword in ("素材", "roi", "gmv", "有效率")):
        rate = summary.get("effectiveMaterialRate")
        rate_value = _assistant_number(rate, 1, 100)
        rate_text = rate_value + "%" if rate_value != "待核验" else rate_value
        answer_parts.append(
            f"近{query.get('materialDays') or 7}日投放素材有效率为 {rate_text}："
            f"{summary.get('effectiveMaterialCount') if summary.get('effectiveMaterialCount') is not None else '待核验'} 条有效，"
            f"分母为 {summary.get('spentMaterialCount') if summary.get('spentMaterialCount') is not None else '待核验'} 条有消耗“账户素材-天”记录。"
        )
    answer_parts.append(
        "口径区别：有效成交额来自 WIS 抖店有效订单的 SKU 实付金额，并排除取消单；"
        "千川归因 GMV 来自 ROI2 广告归因口径。两者不能相加，也不能互相替代。"
    )
    answer_parts.append(
        (
            "本次模型深度解读暂未返回，以上仅为中枢现有已授权来源的读回；"
            "精确查询仍可用，策略分析可稍后重试。"
        ) if model_unavailable else (
            "以上是中枢现有已授权来源的直接精确查询结果，完整性和时效以来源状态为准；如需原因诊断、素材规律或行动建议，"
            "可以继续让我进行模型分析。"
        )
    )
    return {
        "answer": "\n\n".join(answer_parts),
        "actions": [],
        "meta": {
            "provider": "FanDo 根数据",
            "model": "经营数据直接读回",
            "mode": "read_only",
            "latency_ms": 0,
            "context_sources": ["FanDo 根数据 · 经营智能"],
        },
    }


@app.get("/api/assistant/insights")
def assistant_insights(user: dict = Depends(require_user)):
    access = module_access_for_user(user)
    try:
        return ai_insight_service.view(access["allowed_modules"], is_permission_manager(user))
    except Exception as error:
        logger.exception("AI insight view failed")
        raise HTTPException(503, "系统巡检结果暂时不可用，请稍后重试") from error


@app.post("/api/assistant/insights/refresh")
def assistant_insights_refresh(user: dict = Depends(require_user)):
    require_operation_admin(user)
    access = module_access_for_user(user)
    try:
        ai_insight_service.refresh()
        return ai_insight_service.view(access["allowed_modules"], is_permission_manager(user))
    except Exception as error:
        logger.exception("AI insight manual refresh failed")
        raise HTTPException(503, "系统巡检暂时无法完成，请稍后重试") from error


def _assistant_model_messages(
    db: Session,
    conversation: AssistantConversation,
) -> tuple[list[dict[str, str]], list[dict]]:
    newest = db.scalars(
        select(AssistantConversationMessage).where(
            AssistantConversationMessage.conversation_id == conversation.id,
            AssistantConversationMessage.owner_number == conversation.owner_number,
        ).order_by(
            AssistantConversationMessage.created_at.desc(),
            AssistantConversationMessage.id.desc(),
        ).limit(12)
    ).all()
    rows = list(reversed(newest))
    message_ids = [row.id for row in rows]
    attachments = db.scalars(
        select(AssistantAttachment).where(
            AssistantAttachment.message_id.in_(message_ids),
            AssistantAttachment.owner_number == conversation.owner_number,
        ).order_by(AssistantAttachment.created_at.desc())
    ).all() if message_ids else []
    recent_image_ids = {row.id for row in list(attachments)[:4]}
    by_message: dict[str, list[AssistantAttachment]] = defaultdict(list)
    for attachment in reversed(list(attachments)):
        if attachment.id in recent_image_ids:
            by_message[attachment.message_id].append(attachment)
    text_messages: list[dict[str, str]] = []
    model_messages: list[dict] = []
    for row in rows:
        content = row.content.strip()
        text_messages.append({"role": row.role, "content": content})
        image_rows = by_message.get(row.id, []) if row.role == "user" else []
        if not image_rows:
            model_messages.append({"role": row.role, "content": content})
            continue
        parts: list[dict] = [{"type": "text", "text": content or "请分析我上传的图片。"}]
        for attachment in image_rows:
            path = Path(attachment.storage_path)
            if not path.is_file():
                continue
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            parts.append({
                "type": "image_url",
                "image_url": {"url": f"data:{attachment.mime_type};base64,{encoded}"},
            })
        model_messages.append({"role": row.role, "content": parts if len(parts) > 1 else content})
    return text_messages, model_messages


def _assistant_generate(
    text_messages: list[dict[str, str]],
    model_messages: list[dict],
    current_view: str,
    user: dict,
) -> dict:
    if not text_messages or text_messages[-1]["role"] != "user":
        raise HTTPException(400, "最后一条消息必须由用户发送")
    if sum(len(item["content"]) for item in text_messages) > 24000:
        raise HTTPException(413, "本次对话内容过长，请新建会话后重试")
    access = module_access_for_user(user)
    try:
        try:
            can_manage_permissions = is_permission_manager(user)
        except Exception:
            logger.warning("AI assistant permission context unavailable; using least privilege")
            can_manage_permissions = False
        try:
            context = _assistant_context(access, can_manage_permissions)
        except Exception:
            logger.exception("AI assistant context collection failed")
            context = {
                "sources": [],
                "status": "unavailable",
                "note": "本次系统巡检证据暂不可用，相关状态需标记待核验",
            }
        business_context = None
        if "data-dashboard" in access["allowed_modules"] and _assistant_needs_business_data(text_messages):
            try:
                business = business_intelligence_service.get_overview()
                business_context = _compact_business_context(business)
                sources = list(context.get("sources") or [])
                if "FanDo 根数据 · 经营智能" not in sources:
                    sources.append("FanDo 根数据 · 经营智能")
                context = {
                    **context,
                    "sources": sources,
                    "business_intelligence": business_context,
                    "system_summary": context.get("summary"),
                }
            except Exception:
                logger.exception("AI assistant business context collection failed")
                context["business_intelligence"] = {
                    "status": "unavailable",
                    "note": "经营数据本次未能返回，单品成交与素材效率需标记待核验",
                }
        question = text_messages[-1]["content"]
        if business_context and _assistant_is_exact_business_lookup(question):
            result = _business_readback_fallback(
                business_context,
                question,
                model_unavailable=False,
            )
        else:
            try:
                result = ai_assistant_service.chat(
                    model_messages[-12:],
                    access["allowed_modules"],
                    current_view.strip(),
                    context,
                )
            except AiAssistantError:
                if business_context:
                    result = _business_readback_fallback(
                        business_context,
                        question,
                    )
                else:
                    raise
    except AiAssistantError as error:
        raise HTTPException(error.status_code, str(error)) from error
    return result


@app.post("/api/assistant/chat")
def assistant_chat(
    payload: AssistantChatPayload,
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
):
    if not payload.conversation_id:
        normalized_messages = [
            {"role": item.role, "content": item.content.strip()}
            for item in payload.messages
            if item.content.strip()
        ]
        result = _assistant_generate(
            normalized_messages,
            normalized_messages,
            payload.current_view,
            user,
        )
        return {**result, "request_id": uuid4().hex}

    conversation = _assistant_owned_conversation(db, payload.conversation_id, user)
    owner = user_number(user)
    if payload.reuse_last_user_message:
        user_message = db.scalar(
            select(AssistantConversationMessage).where(
                AssistantConversationMessage.conversation_id == conversation.id,
                AssistantConversationMessage.owner_number == owner,
                AssistantConversationMessage.role == "user",
            ).order_by(
                AssistantConversationMessage.created_at.desc(),
                AssistantConversationMessage.id.desc(),
            ).limit(1)
        )
        if not user_message:
            raise HTTPException(400, "当前会话没有可重试的问题")
    else:
        content = payload.content.strip()
        attachment_ids = list(dict.fromkeys(item.strip() for item in payload.attachment_ids if item.strip()))
        if not content and not attachment_ids:
            raise HTTPException(400, "请输入问题或上传图片")
        attachments = db.scalars(select(AssistantAttachment).where(
            AssistantAttachment.id.in_(attachment_ids),
            AssistantAttachment.conversation_id == conversation.id,
            AssistantAttachment.owner_number == owner,
            AssistantAttachment.message_id == "",
        )).all() if attachment_ids else []
        if len(attachments) != len(attachment_ids):
            raise HTTPException(400, "部分图片已失效，请重新上传")
        if sum(int(row.file_size or 0) for row in attachments) > 16 * 1024 * 1024:
            raise HTTPException(413, "本次图片总大小不能超过 16 MB")
        user_message = AssistantConversationMessage(
            id=str(uuid4()),
            conversation_id=conversation.id,
            owner_number=owner,
            role="user",
            content=content or "请分析我上传的图片。",
        )
        db.add(user_message)
        for attachment in attachments:
            attachment.message_id = user_message.id
        if conversation.title == "新会话":
            conversation.title = re.sub(r"\s+", " ", user_message.content).strip()[:28] or "图片分析"
        db.flush()
        _assistant_refresh_conversation(db, conversation, user_message.content)
        db.commit()
        db.refresh(user_message)

    text_messages, model_messages = _assistant_model_messages(db, conversation)
    result = _assistant_generate(text_messages, model_messages, payload.current_view, user)
    assistant_message = AssistantConversationMessage(
        id=str(uuid4()),
        conversation_id=conversation.id,
        owner_number=owner,
        role="assistant",
        content=result["answer"],
        actions=list(result.get("actions") or []),
        message_meta=dict(result.get("meta") or {}),
    )
    db.add(assistant_message)
    db.flush()
    _assistant_refresh_conversation(db, conversation, result["answer"])
    db.commit()
    db.refresh(conversation)
    response_messages = _assistant_messages_out(db, [user_message, assistant_message])
    return {
        **result,
        "request_id": uuid4().hex,
        "conversation": _assistant_conversation_out(conversation),
        "user_message": response_messages[0],
        "assistant_message": response_messages[1],
    }


@app.get("/api/admin/access-grants")
def admin_access_grants(
    q: str = "",
    include_revoked: bool = True,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    query = select(OaAccessGrant)
    filters = []
    if q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(or_(
            OaAccessGrant.real_name.like(pattern),
            OaAccessGrant.user_number.like(pattern),
            OaAccessGrant.department.like(pattern),
            OaAccessGrant.center.like(pattern),
        ))
    if not include_revoked:
        filters.append(OaAccessGrant.active.is_(True))
    if filters:
        query = query.where(*filters)
    grants = db.scalars(query.order_by(OaAccessGrant.active.desc(), OaAccessGrant.granted_at.desc())).all()
    audits = db.scalars(
        select(OaAccessAuditLog).order_by(OaAccessAuditLog.created_at.desc()).limit(50)
    ).all()
    return {
        "items": [access_grant_out(item) for item in grants],
        "total": len(grants),
        "audits": [access_audit_out(item) for item in audits],
        "department_policy": (
            os.getenv("OA_ALLOWED_DEPARTMENTS", "").strip()
            or os.getenv("OA_ALLOWED_DEPARTMENT", "品牌营销").strip()
            or "全部部门"
        ),
    }


@app.post("/api/admin/access-grants")
def admin_access_grant_create(
    payload: AccessGrantCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    real_name = re.sub(r"\s+", "", payload.real_name).strip()
    if len(real_name) < 2 or any(character in real_name for character in ",，;\n\r"):
        raise HTTPException(400, "请输入一位同事的完整 OA 真实姓名")
    identifier = _grant_identifier(real_name)
    now = datetime.utcnow()
    grant = db.get(OaAccessGrant, identifier)
    if not grant:
        grant = OaAccessGrant(identifier=identifier, identifier_type="name")
        db.add(grant)
    grant.real_name = real_name[:120]
    grant.department = payload.department.strip()[:255]
    grant.center = payload.center.strip()[:255]
    grant.active = True
    grant.source = "admin"
    grant.granted_by_number = user_number(user)
    grant.granted_by_name = user_name(user)
    grant.granted_at = now
    grant.revoked_by_number = ""
    grant.revoked_by_name = ""
    grant.revoked_at = None
    db.add(OaAccessAuditLog(
        identifier=identifier,
        real_name=grant.real_name,
        action="grant",
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail=f"权限管理员授权登录；备注部门：{grant.department or '未填写'}",
        created_at=now,
    ))
    db.commit()
    db.refresh(grant)
    return access_grant_out(grant)


@app.delete("/api/admin/access-grants/{identifier:path}")
def admin_access_grant_revoke(
    identifier: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    grant = db.get(OaAccessGrant, identifier)
    if not grant:
        raise HTTPException(404, "未找到该白名单成员")
    if grant.user_number and grant.user_number.upper() == user_number(user).upper():
        raise HTTPException(409, "不能取消自己的中枢登录权限")
    if grant.real_name and grant.real_name == user_name(user):
        raise HTTPException(409, "不能取消自己的中枢登录权限")
    if not grant.active:
        return access_grant_out(grant)
    now = datetime.utcnow()
    grant.active = False
    grant.revoked_by_number = user_number(user)
    grant.revoked_by_name = user_name(user)
    grant.revoked_at = now
    db.add(OaAccessAuditLog(
        identifier=grant.identifier,
        real_name=grant.real_name,
        action="revoke",
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail="权限管理员取消登录权限",
        created_at=now,
    ))
    db.commit()
    db.refresh(grant)
    return access_grant_out(grant)


@app.get("/api/admin/module-access")
def admin_module_access(
    q: str = "",
    include_revoked: bool = True,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    query = select(OaAccessGrant)
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(or_(
            OaAccessGrant.real_name.like(pattern),
            OaAccessGrant.user_number.like(pattern),
            OaAccessGrant.department.like(pattern),
            OaAccessGrant.center.like(pattern),
        ))
    if not include_revoked:
        query = query.where(OaAccessGrant.active.is_(True))
    access_grants = db.scalars(
        query.order_by(OaAccessGrant.active.desc(), OaAccessGrant.granted_at.desc())
    ).all()
    module_rows = {
        row.identifier: row
        for row in db.scalars(select(ModuleAccessGrant).where(
            ModuleAccessGrant.identifier.in_([item.identifier for item in access_grants])
        )).all()
    } if access_grants else {}
    # Read current grants once per request; revocations take effect on the next
    # request without a global permission cache or a separate DB snapshot per row.
    permission_identifiers = set(db.scalars(select(AdminGrant.identifier).where(
        AdminGrant.active.is_(True),
        AdminGrant.role.in_(("permission_manager", "super_admin")),
    )).all()) if access_grants else set()
    return {
        "items": [_module_access_item(item, module_rows.get(item.identifier), permission_identifiers)
                  for item in access_grants],
        "total": len(access_grants),
        "modules": list(MODULE_CATALOG),
        "default_mode": "all",
    }


def _validated_module_selection(payload: ModuleAccessUpdate) -> list[str]:
    invalid = sorted(set(payload.modules) - set(MODULE_KEYS))
    if invalid:
        raise HTTPException(400, f"包含未识别的界面权限：{', '.join(invalid)}")
    return [key for key in MODULE_KEYS if key in set(payload.modules)]


def _require_configurable_module_member(access_grant: OaAccessGrant) -> None:
    if is_permission_manager({"number": access_grant.user_number or access_grant.identifier,
                              "realName": access_grant.real_name}):
        raise HTTPException(409, "该成员的最高权限统一开放全部界面；请先取消最高权限，再修改普通界面配置")


def _set_module_access(
    db: Session,
    access_grant: OaAccessGrant,
    payload: ModuleAccessUpdate,
    selected: list[str],
    user: dict,
    *,
    batch: bool = False,
) -> ModuleAccessGrant:
    row = db.get(ModuleAccessGrant, access_grant.identifier)
    if not row:
        row = ModuleAccessGrant(identifier=access_grant.identifier, identifier_type=access_grant.identifier_type)
        db.add(row)
    row.real_name = access_grant.real_name
    row.user_number = access_grant.user_number
    row.department = access_grant.department
    row.center = access_grant.center
    row.access_mode = payload.access_mode
    row.modules = [] if payload.access_mode == "all" else selected
    row.updated_by_number = user_number(user)
    row.updated_by_name = user_name(user)
    row.updated_at = datetime.utcnow()
    db.add(OaAccessAuditLog(
        identifier=access_grant.identifier,
        real_name=access_grant.real_name,
        action="module_scope",
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail=(
            ("批量" if batch else "") + "界面权限更新为全部模块"
            if payload.access_mode == "all"
            else f"{'批量' if batch else ''}界面权限更新为指定模块：{', '.join(selected) if selected else '无'}"
        ),
    ))
    return row


@app.post("/api/admin/module-access/batch")
def admin_module_access_batch_update(
    payload: ModuleAccessBatchUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    identifiers = list(dict.fromkeys(item.strip() for item in payload.identifiers if item.strip()))
    access_grants = db.scalars(select(OaAccessGrant).where(OaAccessGrant.identifier.in_(identifiers))).all()
    by_identifier = {row.identifier: row for row in access_grants}
    missing = [identifier for identifier in identifiers if identifier not in by_identifier]
    if missing:
        raise HTTPException(404, f"有 {len(missing)} 位成员未找到登录授权，未执行任何修改")
    inactive = [row for row in access_grants if not row.active]
    if inactive:
        raise HTTPException(409, f"有 {len(inactive)} 位成员的登录权限已取消，未执行任何修改")
    for row in access_grants:
        _require_configurable_module_member(row)
    selected = _validated_module_selection(payload)
    rows = [
        _set_module_access(db, by_identifier[identifier], payload, selected, user, batch=True)
        for identifier in identifiers
    ]
    db.commit()
    return {
        "items": [_module_access_item(by_identifier[row.identifier], row) for row in rows],
        "updated": len(rows),
    }


@app.put("/api/admin/module-access/{identifier:path}")
def admin_module_access_update(
    identifier: str,
    payload: ModuleAccessUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    access_grant = db.get(OaAccessGrant, identifier)
    if not access_grant:
        raise HTTPException(404, "未找到对应的登录授权成员")
    if not access_grant.active:
        raise HTTPException(409, "该成员的登录权限已取消，未修改界面配置")
    _require_configurable_module_member(access_grant)
    selected = _validated_module_selection(payload)
    row = _set_module_access(db, access_grant, payload, selected, user)
    db.commit()
    db.refresh(row)
    return _module_access_item(access_grant, row)


@app.get("/api/admin/admin-grants")
def admin_grants(
    q: str = "",
    include_revoked: bool = True,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    query = select(AdminGrant).where(AdminGrant.role == "operation_admin")
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(or_(
            AdminGrant.real_name.like(pattern),
            AdminGrant.user_number.like(pattern),
            AdminGrant.department.like(pattern),
            AdminGrant.center.like(pattern),
        ))
    if not include_revoked:
        query = query.where(AdminGrant.active.is_(True))
    rows = db.scalars(query.order_by(AdminGrant.protected.desc(), AdminGrant.active.desc(), AdminGrant.granted_at.desc())).all()
    access_rows = db.scalars(select(OaAccessGrant)).all()
    access_by_identifier = {row.identifier: row for row in access_rows}
    access_by_name = {row.real_name.casefold(): row for row in access_rows if row.real_name}
    access_by_number = {row.user_number.upper(): row for row in access_rows if row.user_number}

    def related_access(row: AdminGrant) -> OaAccessGrant | None:
        return (
            access_by_identifier.get(row.identifier)
            or (access_by_number.get(row.user_number.upper()) if row.user_number else None)
            or (access_by_name.get(row.real_name.casefold()) if row.real_name else None)
        )

    return {"items": [admin_grant_out(row, related_access(row)) for row in rows], "total": len(rows)}


@app.post("/api/admin/admin-grants")
def admin_grant_create(
    payload: AdminGrantCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    real_name = re.sub(r"\s+", "", payload.real_name).strip()
    if len(real_name) < 2 or any(character in real_name for character in ",，;\n\r"):
        raise HTTPException(400, "请输入一位同事的完整 OA 真实姓名")
    identifier = _grant_identifier(real_name)
    row = db.get(AdminGrant, identifier)
    if not row:
        row = AdminGrant(identifier=identifier, identifier_type="name")
        db.add(row)
    if row.protected:
        return admin_grant_out(row)
    row.real_name = real_name[:120]
    row.department = payload.department.strip()[:255]
    row.center = payload.center.strip()[:255]
    row.role = "operation_admin"
    row.active = True
    row.granted_by_number = user_number(user)
    row.granted_by_name = user_name(user)
    row.granted_at = datetime.utcnow()
    row.revoked_by_number = ""
    row.revoked_by_name = ""
    row.revoked_at = None
    db.commit()
    db.refresh(row)
    return admin_grant_out(row)


@app.delete("/api/admin/admin-grants/{identifier:path}")
def admin_grant_revoke(
    identifier: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    row = db.get(AdminGrant, identifier)
    if not row:
        raise HTTPException(404, "未找到该管理员")
    if row.protected:
        raise HTTPException(409, "受保护的权限负责人不能取消")
    if row.active:
        row.active = False
        row.revoked_by_number = user_number(user)
        row.revoked_by_name = user_name(user)
        row.revoked_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
    return admin_grant_out(row)


@app.get("/api/admin/permission-managers")
def permission_manager_grants(
    q: str = "",
    include_revoked: bool = True,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    query = select(AdminGrant).where(
        AdminGrant.role.in_(("permission_manager", "super_admin"))
    )
    if q.strip():
        pattern = f"%{q.strip()}%"
        query = query.where(or_(
            AdminGrant.real_name.like(pattern),
            AdminGrant.user_number.like(pattern),
            AdminGrant.department.like(pattern),
            AdminGrant.center.like(pattern),
        ))
    if not include_revoked:
        query = query.where(AdminGrant.active.is_(True))
    rows = db.scalars(
        query.order_by(
            AdminGrant.protected.desc(),
            AdminGrant.active.desc(),
            AdminGrant.granted_at.desc(),
        )
    ).all()
    access_rows = db.scalars(select(OaAccessGrant)).all()
    access_by_identifier = {row.identifier: row for row in access_rows}
    access_by_name = {row.real_name.casefold(): row for row in access_rows if row.real_name}
    access_by_number = {row.user_number.upper(): row for row in access_rows if row.user_number}

    def related_access(row: AdminGrant) -> OaAccessGrant | None:
        return (
            access_by_identifier.get(row.identifier)
            or (access_by_number.get(row.user_number.upper()) if row.user_number else None)
            or (access_by_name.get(row.real_name.casefold()) if row.real_name else None)
        )

    return {"items": [admin_grant_out(row, related_access(row)) for row in rows], "total": len(rows)}


@app.post("/api/admin/permission-managers")
def permission_manager_grant_create(
    payload: AdminGrantCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    real_name = re.sub(r"\s+", "", payload.real_name).strip()
    if len(real_name) < 2 or any(character in real_name for character in ",，;\n\r"):
        raise HTTPException(400, "请输入一位同事的完整 OA 真实姓名")
    identifier = _grant_identifier(real_name)
    row = db.get(AdminGrant, identifier)
    if not row:
        row = AdminGrant(identifier=identifier, identifier_type="name")
        db.add(row)
    if row.protected:
        return admin_grant_out(row)
    row.real_name = real_name[:120]
    row.department = payload.department.strip()[:255]
    row.center = payload.center.strip()[:255]
    row.role = "permission_manager"
    row.active = True
    row.granted_by_number = user_number(user)
    row.granted_by_name = user_name(user)
    row.granted_at = datetime.utcnow()
    row.revoked_by_number = ""
    row.revoked_by_name = ""
    row.revoked_at = None
    db.commit()
    db.refresh(row)
    return admin_grant_out(row)


@app.delete("/api/admin/permission-managers/{identifier:path}")
def permission_manager_grant_revoke(
    identifier: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_permission_manager(user)
    row = db.get(AdminGrant, identifier)
    if not row or row.role not in {"permission_manager", "super_admin"}:
        raise HTTPException(404, "未找到该权限管理员")
    if row.protected:
        raise HTTPException(409, "系统保护的最高权限负责人不能取消")
    own_identifiers = {
        _grant_identifier(user_number(user), "number"),
        _grant_identifier(user_name(user)),
    }
    if row.identifier in own_identifiers or (
        row.user_number and row.user_number.upper() == user_number(user).upper()
    ) or (row.real_name and row.real_name == user_name(user)):
        raise HTTPException(409, "不能取消自己的权限管理权限")
    if row.active:
        row.active = False
        row.revoked_by_number = user_number(user)
        row.revoked_by_name = user_name(user)
        row.revoked_at = datetime.utcnow()
        db.commit()
        db.refresh(row)
    return admin_grant_out(row)


def _review_assignment_out(row: ReviewRoleAssignment) -> dict:
    return {
        "id": row.id,
        "role_code": row.role_code,
        "role_label": REVIEW_ROLE_LABELS.get(row.role_code, row.role_code),
        "user_name": row.user_name,
        "user_number": row.user_number,
        "department": row.department,
        "center": row.center,
        "group_name": row.group_name,
        "source": row.source,
        "active": bool(row.active),
        "created_by_name": row.created_by_name,
        "created_at": row.created_at.isoformat() + "Z",
    }


def _review_config_out(db: Session, user: dict) -> dict:
    config = _review_workflow_config(db)
    ai_rules, ai_rule_version = _review_ai_rule_snapshot(db)
    assignments = db.scalars(
        select(ReviewRoleAssignment)
        .where(ReviewRoleAssignment.active.is_(True))
        .order_by(ReviewRoleAssignment.role_code, ReviewRoleAssignment.user_name)
    ).all()
    members_by_role = {code: [] for code, _label, _stage in REVIEW_ROLE_DEFINITIONS}
    for row in assignments:
        members_by_role.setdefault(row.role_code, []).append(_review_assignment_out(row))
    required_roles = [role for role in (config.required_roles or []) if role in REVIEW_STAGE_ORDER]
    missing = [role for role in required_roles if not members_by_role.get(role)]
    return {
        "enabled": bool(config.enabled),
        "required_roles": required_roles,
        "roles": [
            {
                "code": code,
                "label": label,
                "is_stage": is_stage,
                "legacy": code in REVIEW_LEGACY_STAGE_CODES,
                "required": code in required_roles,
                "members": members_by_role.get(code, []),
            }
            for code, label, is_stage in REVIEW_ROLE_DEFINITIONS
        ],
        "missing_required_roles": missing,
        "can_enable": not missing,
        "updated_by_name": config.updated_by_name,
        "updated_at": config.updated_at.isoformat() + "Z",
        "viewer_roles": _review_user_roles(db, user),
        "can_configure": is_super_admin(user),
        "eligible_reviewers": _review_eligible_reviewers(db),
        "organization_centers": [
            {
                "center": center,
                "member_count": sum(1 for row in assignments if row.role_code == "member" and row.center == center),
                "team_leads": [row.user_name for row in assignments if row.role_code == "team_lead" and row.center == center],
                "supervisors": [row.user_name for row in assignments if row.role_code == "supervisor" and row.center in {center, "ALL"}],
                "groups": [
                    {
                        "group_name": group_name,
                        "member_count": sum(
                            1 for row in assignments
                            if row.role_code == "member" and row.center == center and row.group_name == group_name
                        ),
                        "team_leads": [
                            row.user_name for row in assignments
                            if row.role_code == "team_lead" and row.center == center and row.group_name == group_name
                        ],
                    }
                    for group_name in dict.fromkeys(
                        str(group.get("group_name") or "").strip()
                        for item in REVIEW_ORGANIZATION_ROSTER.get("centers", [])
                        if item.get("center") == center
                        for group in item.get("groups", [])
                    )
                    if group_name
                ],
            }
            for center in REVIEW_ORGANIZATION_CENTERS
        ],
        "organization_source": {
            "title": REVIEW_ORGANIZATION_ROSTER.get("source_title", ""),
            "whiteboard_label": REVIEW_ORGANIZATION_ROSTER.get("whiteboard_label", ""),
            "document_url": REVIEW_ORGANIZATION_ROSTER.get("source_document", ""),
            "document_revision": REVIEW_ORGANIZATION_ROSTER.get("document_revision"),
        },
        "fixed_stages": ["naming_check", "ai_review", *REVIEW_STAGE_ORDER],
        "naming_standard": {
            "enabled": bool(config.naming_enabled),
            "blocking": bool(config.naming_enabled),
            "version": REVIEW_NAMING_RULE_VERSION,
            "label": "自产混剪命名规范",
            "document_url": REVIEW_NAMING_DOC_URL,
            "categories": [
                {
                    "code": "face",
                    "label": "上脸素材",
                    "rule": "完整使用在任意位置，或切片用于片头时，名称必须包含对应素材名。",
                },
                {
                    "code": "mechanism",
                    "label": "机制素材",
                    "rule": "完整使用在任意位置，或切片用于片头时，名称必须包含对应素材名。",
                },
                {
                    "code": "product_display",
                    "label": "产品展示素材",
                    "rule": "用于片头时，名称必须包含对应素材名。",
                },
                {
                    "code": "ai_first_creation",
                    "label": "AI一创素材",
                    "rule": "用于片头时，名称必须包含对应素材名。",
                },
            ],
            "pending_categories": ["明星素材"],
        },
        "ai_review_enabled": bool(config.ai_redline_enabled),
        "ai_service_configured": review_ai_service.configured,
        "ai_redlines": {
            "version": ai_rule_version,
            "rules": ai_rules,
            "mode": "advisory",
            "blocking": False,
            "enabled_count": sum(1 for rule in ai_rules if rule["enabled"]),
            "hard_count": sum(1 for rule in ai_rules if rule["enabled"] and rule["severity"] == "hard"),
            "warning_count": sum(1 for rule in ai_rules if rule["enabled"] and rule["severity"] == "warning"),
            "policy_source": {
                "title": "本土美妆素材审核放宽规则（常态 8 类）",
                "url": RELAXATION_SOURCE_URL,
                "case_count": 273,
            },
        },
        "brand_tone": {
            "enabled": False,
            "blocking": False,
            "label": "品牌调性审核（预留）",
            "document_url": REVIEW_BRAND_TONE_DOC_URL,
        },
    }


def _review_ai_out(row: AssetReviewAiResult | None) -> dict:
    if not row:
        return {
            "status": "legacy_skipped",
            "summary": "该任务创建于AI建议上线前，继续由审核人按人工流程处理。",
            "provider": "",
            "task_id": "",
            "rule_version": "",
            "category_counts": {"platform": 0, "internal": 0, "artist": 0, "relaxation": 0},
            "findings": [],
            "segments": [],
            "error_message": "",
            "retry_count": 0,
            "started_at": None,
            "completed_at": None,
        }
    return {
        "status": row.status,
        "summary": row.summary,
        "provider": row.provider,
        "task_id": row.task_id,
        "rule_version": row.rule_version,
        "category_counts": row.category_counts or {"platform": 0, "internal": 0, "artist": 0, "relaxation": 0},
        "findings": row.findings or [],
        "segments": row.segments or [],
        "error_message": row.error_message,
        "retry_count": int(row.retry_count or 0),
        "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
        "completed_at": row.completed_at.isoformat() + "Z" if row.completed_at else None,
    }


def _review_naming_check_out(
    item: AssetReviewSubmission,
    asset: Asset,
    config: ReviewWorkflowConfig,
) -> dict:
    """Return a complete naming payload even for rows created by older releases."""
    if not config.naming_enabled:
        return _review_naming_disabled(asset)
    fallback = _review_naming_evaluate(asset, item.naming_evidence or [])
    stored = item.naming_check if isinstance(item.naming_check, dict) else {}
    merged = {**fallback, **stored}
    for key in ("required_names", "missing_names", "pending_categories"):
        if not isinstance(merged.get(key), list):
            merged[key] = list(fallback.get(key) or [])
    merged["message"] = str(merged.get("message") or fallback["message"])
    merged["status"] = str(merged.get("status") or fallback["status"])
    merged["evidence_count"] = int(merged.get("evidence_count") or 0)
    merged["no_applicable_sources"] = bool(merged.get("no_applicable_sources"))
    return merged


def _review_submission_out(db: Session, item: AssetReviewSubmission, asset: Asset, user: dict) -> dict:
    config = _review_workflow_config(db)
    decisions = db.scalars(
        select(AssetReviewDecision)
        .where(AssetReviewDecision.submission_id == item.id)
        .order_by(AssetReviewDecision.id)
    ).all()
    ai_result = db.scalar(
        select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == item.id)
    )
    viewer_roles = set(_review_user_roles(db, user))
    human_review_enabled = bool(config.enabled or item.assignment_mode == "designated")
    current = (
        next((row for row in decisions if row.status == "pending"), None)
        if human_review_enabled
        else None
    )
    current_role = current.role_code if current else ""
    current_role_label = REVIEW_ROLE_LABELS.get(current.role_code, "") if current else ""
    owner_number = item.submitted_by_number.strip().upper()
    owner_name = item.submitted_by_name.strip().casefold()
    is_owner = bool(
        (owner_number and owner_number == user_number(user).strip().upper())
        or (not owner_number and owner_name and owner_name == user_name(user).strip().casefold())
    )
    return {
        "id": item.id,
        "asset_id": asset.id,
        "asset_name": asset.filename,
        "preview_url": oss_service.url_for(asset.object_key) if not asset.purged_at else "",
        "version": item.version,
        "status": item.status,
        "submitted_by_number": item.submitted_by_number,
        "submitted_by_name": item.submitted_by_name,
        "note": item.note,
        "filename_snapshot": item.filename_snapshot,
        "naming_evidence": item.naming_evidence or [],
        "naming_check": _review_naming_check_out(item, asset, config),
        "submitted_at": item.submitted_at.isoformat() + "Z",
        "completed_at": item.completed_at.isoformat() + "Z" if item.completed_at else None,
        "current_role": current_role,
        "current_role_label": current_role_label,
        "route_center": item.route_center,
        "route_group": item.route_group,
        "assignment_mode": item.assignment_mode or "organization",
        "designated_reviewer_number": item.designated_reviewer_number,
        "designated_reviewer_name": item.designated_reviewer_name,
        "current_reviewers": (
            (current.candidate_reviewers or _review_role_candidates(db, current.role_code, item.route_center, item.route_group))
            if current else []
        ),
        "can_review": bool(human_review_enabled and current and _review_decision_allows_user(current, user, viewer_roles)),
        "can_retry_ai": bool(
            config.ai_redline_enabled
            and ai_result
            and ai_result.status == "error"
            and (is_owner or is_super_admin(user))
        ),
        "ai_review": _review_ai_out(ai_result),
        "brand_tone": {
            "status": "reserved",
            "label": "品牌调性审核（预留）",
            "blocking": False,
            "document_url": REVIEW_BRAND_TONE_DOC_URL,
        },
        "decisions": [
            {
                "role_code": row.role_code,
                "role_label": REVIEW_ROLE_LABELS.get(row.role_code, row.role_code),
                "status": row.status,
                "reviewer_number": row.reviewer_number,
                "reviewer_name": row.reviewer_name,
                "candidate_reviewers": row.candidate_reviewers or _review_role_candidates(db, row.role_code, item.route_center, item.route_group),
                "note": row.note,
                "quality_scores": row.quality_scores or {},
                "quality_total": row.quality_total,
                "quality_grade": row.quality_grade,
                "decided_at": row.decided_at.isoformat() + "Z" if row.decided_at else None,
            }
            for row in decisions
        ],
    }


@app.get("/api/reviews/pending-count")
def review_pending_count(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    config = _review_workflow_config(db)
    viewer_roles = set(_review_user_roles(db, user))
    latest_versions = (
        select(
            AssetReviewSubmission.asset_id.label("asset_id"),
            func.max(AssetReviewSubmission.version).label("latest_version"),
        )
        .group_by(AssetReviewSubmission.asset_id)
        .subquery("latest_review_versions_for_owner")
    )
    owner_number = user_number(user).strip()
    owner_name = user_name(user).strip()
    owner_conditions = []
    if owner_number:
        owner_conditions.append(AssetReviewSubmission.submitted_by_number == owner_number)
    if owner_name:
        owner_conditions.append(and_(
            AssetReviewSubmission.submitted_by_number == "",
            AssetReviewSubmission.submitted_by_name == owner_name,
        ))
    owner_filter = or_(*owner_conditions) if owner_conditions else AssetReviewSubmission.id == ""
    rejected_count = db.scalar(
        select(func.count())
        .select_from(AssetReviewSubmission)
        .join(
            latest_versions,
            and_(
                latest_versions.c.asset_id == AssetReviewSubmission.asset_id,
                latest_versions.c.latest_version == AssetReviewSubmission.version,
            ),
        )
        .where(AssetReviewSubmission.status == "rejected", owner_filter)
    ) or 0

    ai_attention_count = (
        db.scalar(
            select(func.count())
            .select_from(AssetReviewSubmission)
            .join(
                latest_versions,
                and_(
                    latest_versions.c.asset_id == AssetReviewSubmission.asset_id,
                    latest_versions.c.latest_version == AssetReviewSubmission.version,
                ),
            )
            .join(AssetReviewAiResult, AssetReviewAiResult.submission_id == AssetReviewSubmission.id)
            .where(
                AssetReviewSubmission.status == "pending",
                AssetReviewAiResult.status == "error",
                owner_filter,
            )
        ) or 0
        if config.ai_redline_enabled
        else 0
    )

    if not viewer_roles:
        return {
            "count": 0,
            "roles": [],
            "rejected_count": rejected_count,
            "ai_attention_count": ai_attention_count,
        }

    pending_rows = db.scalars(
        select(AssetReviewDecision)
        .join(AssetReviewSubmission, AssetReviewSubmission.id == AssetReviewDecision.submission_id)
        .where(
            AssetReviewSubmission.status == "pending",
            AssetReviewDecision.status == "pending",
        )
        .order_by(AssetReviewDecision.submission_id, AssetReviewDecision.id)
    ).all()
    if not config.enabled:
        designated_submission_ids = set(db.scalars(
            select(AssetReviewSubmission.id).where(
                AssetReviewSubmission.status == "pending",
                AssetReviewSubmission.assignment_mode == "designated",
            )
        ).all())
        pending_rows = [row for row in pending_rows if row.submission_id in designated_submission_ids]
    first_pending_by_submission: dict[str, AssetReviewDecision] = {}
    for decision in pending_rows:
        first_pending_by_submission.setdefault(decision.submission_id, decision)

    count = sum(
        1
        for _submission_id, decision in first_pending_by_submission.items()
        if _review_decision_allows_user(decision, user, viewer_roles)
    )
    return {
        "count": count,
        "roles": sorted(viewer_roles),
        "rejected_count": rejected_count,
        "ai_attention_count": ai_attention_count,
    }


@app.get("/api/reviews/config")
def review_config(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    return _review_config_out(db, user)


@app.put("/api/reviews/config")
def review_config_update(
    payload: ReviewWorkflowUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_super_admin(user)
    requested = set(payload.required_roles)
    unknown = requested.difference(REVIEW_STAGE_ORDER)
    if unknown:
        raise HTTPException(400, "审核流程包含不支持的角色")
    required_roles = list(REVIEW_STAGE_ORDER)
    active_roles = set(db.scalars(
        select(ReviewRoleAssignment.role_code).where(ReviewRoleAssignment.active.is_(True)).distinct()
    ).all())
    missing = [role for role in required_roles if role not in active_roles]
    if payload.enabled and missing:
        labels = "、".join(REVIEW_ROLE_LABELS[role] for role in missing)
        raise HTTPException(409, f"请先为以下必审角色配置成员：{labels}")
    config = _review_workflow_config(db)
    previous_ai_enabled = bool(config.ai_redline_enabled)
    config.enabled = payload.enabled
    config.naming_enabled = payload.naming_enabled
    config.ai_redline_enabled = payload.ai_redline_enabled
    config.required_roles = required_roles
    config.updated_by_number = user_number(user)
    config.updated_by_name = user_name(user)
    config.updated_at = datetime.utcnow()
    if previous_ai_enabled != payload.ai_redline_enabled:
        pending_submission_ids = select(AssetReviewSubmission.id).where(
            AssetReviewSubmission.status == "pending"
        )
        ai_rows = db.scalars(
            select(AssetReviewAiResult).where(
                AssetReviewAiResult.submission_id.in_(pending_submission_ids),
                AssetReviewAiResult.status.in_(
                    {"disabled"} if payload.ai_redline_enabled else {"pending", "processing", "error"}
                ),
            )
        ).all()
        now = datetime.utcnow()
        for ai_result in ai_rows:
            ai_result.status = "pending" if payload.ai_redline_enabled else "disabled"
            ai_result.summary = (
                "AI审核建议已开启，任务已进入识别队列；审核人可同时开始人工审核。"
                if payload.ai_redline_enabled
                else "AI审核建议已关闭，不影响人工审核或推送门禁。"
            )
            ai_result.error_message = ""
            ai_result.started_at = None
            ai_result.completed_at = None if payload.ai_redline_enabled else now
            ai_result.updated_at = now
    db.commit()
    return _review_config_out(db, user)


@app.put("/api/reviews/redline-rules")
def review_ai_rules_update(
    payload: ReviewAiRulesUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_super_admin(user)
    if not any(rule.enabled for rule in payload.rules):
        raise HTTPException(400, "至少保留一条启用的AI审核建议规则")

    existing_rows = _review_ai_rules(db)
    existing = {row.code: row for row in existing_rows}
    submitted_codes: set[str] = set()
    now = datetime.utcnow()
    editor_number = user_number(user)
    editor_name = user_name(user)

    for index, item in enumerate(payload.rules):
        code = item.code.strip().lower()
        if not code:
            code = f"custom-{uuid4().hex[:12]}"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,79}", code):
            raise HTTPException(400, f"规则“{item.title}”的编号格式无效")
        if code in submitted_codes:
            raise HTTPException(400, f"规则编号重复：{code}")
        submitted_codes.add(code)
        pattern = item.pattern.strip()
        try:
            re.compile(pattern, re.I)
        except re.error as error:
            raise HTTPException(400, f"规则“{item.title}”的命中表达式无效：{error}") from error
        row = existing.get(code)
        if not row:
            row = ReviewAiRule(code=code, created_at=now)
            db.add(row)
        row.category = item.category
        row.severity = item.severity
        row.title = re.sub(r"\s+", " ", item.title).strip()
        row.pattern = pattern
        row.enabled = item.enabled
        row.sort_order = index
        row.updated_by_number = editor_number
        row.updated_by_name = editor_name
        row.updated_at = now

    for code, row in existing.items():
        if code not in submitted_codes:
            db.delete(row)

    version_row = db.get(AppMeta, REVIEW_AI_RULE_VERSION_KEY)
    if not version_row:
        version_row = AppMeta(key=REVIEW_AI_RULE_VERSION_KEY, value="1")
        db.add(version_row)
    try:
        current_version = max(1, int(version_row.value or "1"))
    except ValueError:
        current_version = 1
    version_row.value = str(current_version + 1)
    db.commit()
    return _review_config_out(db, user)


@app.post("/api/reviews/roles")
def review_role_create(
    payload: ReviewRoleCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_super_admin(user)
    if payload.role_code in REVIEW_LEGACY_STAGE_CODES:
        raise HTTPException(400, "总监与内控只保留历史审核记录，不能再新增为必审角色")
    clean_name = re.sub(r"\s+", "", payload.user_name).strip()
    clean_number = payload.user_number.strip().upper()
    identifier = _grant_identifier(clean_number, "number") if clean_number else _grant_identifier(clean_name)
    row = db.scalar(select(ReviewRoleAssignment).where(
        ReviewRoleAssignment.role_code == payload.role_code,
        ReviewRoleAssignment.identifier == identifier,
    ))
    if not row:
        row = ReviewRoleAssignment(role_code=payload.role_code, identifier=identifier)
        db.add(row)
    row.user_name = clean_name
    row.user_number = clean_number
    row.department = payload.department.strip()
    row.center = payload.center.strip()
    row.group_name = payload.group_name.strip()
    row.source = "manual"
    row.active = True
    row.created_by_number = user_number(user)
    row.created_by_name = user_name(user)
    row.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(row)
    return _review_assignment_out(row)


@app.delete("/api/reviews/roles/{assignment_id}")
def review_role_delete(
    assignment_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_super_admin(user)
    row = db.get(ReviewRoleAssignment, assignment_id)
    if not row:
        raise HTTPException(404, "未找到该审核角色成员")
    row.active = False
    row.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "id": row.id}


@app.get("/api/reviews/submissions")
def review_submissions(
    q: str = Query(default="", max_length=120),
    reviewer: str = Query(default="", max_length=120),
    status: str = Query(default="all", pattern="^(all|pending|approved|rejected)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=5, le=50),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    filters = []
    if q.strip():
        filters.append(or_(
            Asset.filename.contains(q.strip()),
            AssetReviewSubmission.submitted_by_name.contains(q.strip()),
        ))
    reviewer_term = reviewer.strip() if isinstance(reviewer, str) else ""
    if reviewer_term:
        reviewer_roles = list(db.scalars(
            select(ReviewRoleAssignment.role_code).where(
                ReviewRoleAssignment.active.is_(True),
                ReviewRoleAssignment.user_name.contains(reviewer_term),
            ).distinct()
        ).all())
        decision_filters = [
            AssetReviewDecision.reviewer_name.contains(reviewer_term),
            AssetReviewDecision.candidate_search.contains(reviewer_term),
        ]
        if reviewer_roles:
            decision_filters.append(and_(
                AssetReviewDecision.status == "pending",
                AssetReviewDecision.role_code.in_(reviewer_roles),
                AssetReviewDecision.candidate_search == "",
            ))
        filters.append(AssetReviewSubmission.id.in_(
            select(AssetReviewDecision.submission_id).where(or_(*decision_filters))
        ))
    if status != "all":
        filters.append(AssetReviewSubmission.status == status)
    base = select(AssetReviewSubmission, Asset).join(Asset, Asset.id == AssetReviewSubmission.asset_id)
    count_query = select(func.count()).select_from(AssetReviewSubmission).join(Asset, Asset.id == AssetReviewSubmission.asset_id)
    if filters:
        base = base.where(*filters)
        count_query = count_query.where(*filters)
    total = db.scalar(count_query) or 0
    rows = db.execute(
        base.order_by(AssetReviewSubmission.submitted_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {
        "items": [_review_submission_out(db, item, asset, user) for item, asset in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "workflow": _review_config_out(db, user),
    }


@app.get("/api/reviews/assets/{asset_id}/naming-context")
def review_asset_naming_context(
    asset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "未找到可审核的素材")
    if asset.media_type != "video":
        raise HTTPException(400, "视频审核仅支持视频素材")
    return {
        "asset_id": asset.id,
        "filename": asset.filename,
        "library_type": asset.library_type,
        "source_suggestions": _review_naming_suggestions(db, asset),
        "standard": _review_config_out(db, user)["naming_standard"],
    }


def _notify_asset_review(db, item, asset, action, decision=None):
    """Persist reviewer and submitter notices in the business transaction."""
    recipients = {}
    def add(number, name, detail):
        key = number or ('unmapped:' + name)
        entry = recipients.setdefault(key, {'number': number, 'name': name, 'details': []})
        if detail not in entry['details']:
            entry['details'].append(detail)
    label = REVIEW_ROLE_LABELS.get(decision.role_code, '') if decision else ''
    if action in {'submitted', 'reassigned'}:
        title = '素材已提交审核' if action == 'submitted' else '素材审核人已调整'
        add(item.submitted_by_number, item.submitted_by_name, '你的素材已进入审核，请关注审核进度。')
    elif action == 'rejected':
        title = '素材审核已驳回'
        add(item.submitted_by_number, item.submitted_by_name, label + '驳回：' + (decision.note or '请查看审核意见'))
    elif item.status == 'approved':
        title = '素材审核已通过'
        add(item.submitted_by_number, item.submitted_by_name, '全部人工审核已通过，可按授权继续分发。审核通过不代表已发布。')
    else:
        title = label + '审核通过'
        add(item.submitted_by_number, item.submitted_by_name, label + '审核已通过，进入下一审核环节。')
    if item.status == 'pending':
        db.flush()
        pending = db.scalar(select(AssetReviewDecision).where(AssetReviewDecision.submission_id == item.id, AssetReviewDecision.status == 'pending').order_by(AssetReviewDecision.id))
        if pending:
            for candidate in pending.candidate_reviewers or []:
                add(candidate.get('user_number',''), candidate.get('user_name',''), '轮到你进行' + REVIEW_ROLE_LABELS.get(pending.role_code, pending.role_code) + '审核，请预览视频后处理。')
    for entry in recipients.values():
        db.add(UserNotification(recipient_number=entry['number'],recipient_name=entry['name'],kind='asset_review',title=title,
            message='\n'.join(['视频：' + asset.filename, '产品：' + asset.category, '审核版本：' + str(item.version), *entry['details']]),
            resource_type='asset_review',resource_id=item.id))


@app.post("/api/reviews/assets/{asset_id}/submit")
def review_asset_submit(
    asset_id: int,
    payload: AssetReviewSubmit,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "未找到可审核的素材")
    if asset.media_type != "video":
        raise HTTPException(400, "视频审核仅支持视频素材")
    config = _review_workflow_config(db)
    latest = _review_latest_submission(db, asset.id)
    evidence = [item.model_dump() for item in payload.naming_evidence]
    no_applicable_sources = payload.no_applicable_sources
    # The material-hall batch entry does not carry the detailed naming form.
    # Reuse a still-valid prior naming snapshot instead of making an already
    # verified asset fail solely because it was submitted from another entry.
    if (
        config.naming_enabled
        and not evidence
        and not no_applicable_sources
        and latest
        and latest.filename_snapshot == asset.filename
        and (latest.naming_check or {}).get("status") == "passed"
    ):
        evidence = list(latest.naming_evidence or [])
        no_applicable_sources = bool((latest.naming_check or {}).get("no_applicable_sources"))
    naming_check = (
        _review_naming_evaluate(
            asset,
            evidence,
            no_applicable_sources=no_applicable_sources,
        )
        if config.naming_enabled
        else _review_naming_disabled(asset)
    )
    if config.naming_enabled and naming_check["status"] == "failed":
        raise HTTPException(409, f"命名规范未通过：{naming_check['message']}")
    # Snapshot the submitter's organization route and exact reviewer candidates.
    # This prevents a reviewer from another marketing center taking the task.
    route_center, route_group = _review_route_scope(db, user)
    designated_reviewer = None
    if payload.assignment_mode == "designated":
        designated_reviewer = _resolve_designated_reviewer(
            db,
            payload.designated_reviewer_number,
            payload.designated_reviewer_name,
        )
        review_steps = [(REVIEW_DESIGNATED_ROLE_CODE, [designated_reviewer])]
    else:
        candidates_by_role = {
            role: _review_role_candidates(db, role, route_center, route_group)
            for role in REVIEW_STAGE_ORDER
        }
        missing = [role for role in REVIEW_STAGE_ORDER if not candidates_by_role[role]]
        if config.enabled and missing:
            labels = "、".join(REVIEW_ROLE_LABELS[role] for role in missing)
            route_label = " / ".join(part for part in (route_center, route_group) if part)
            route_hint = f"（提审人所属：{route_label}）" if route_label else "（提审人尚未匹配营销中心 A/B/C/D/J 或 AI 营销中心）"
            raise HTTPException(409, f"审核组织链路尚未配置完整：{labels}{route_hint}")
        review_steps = [(role, candidates_by_role[role]) for role in REVIEW_STAGE_ORDER]
    if latest and latest.status == "pending":
        assignment_changed = (
            (latest.assignment_mode or "organization") != payload.assignment_mode
            or latest.designated_reviewer_number != (designated_reviewer or {}).get("user_number", "")
            or latest.designated_reviewer_name != (designated_reviewer or {}).get("user_name", "")
        )
        if assignment_changed:
            existing_decisions = db.scalars(
                select(AssetReviewDecision).where(AssetReviewDecision.submission_id == latest.id)
            ).all()
            if any(decision.status != "pending" for decision in existing_decisions):
                raise HTTPException(409, "该素材的人工审核已开始，不能再更换指定审核人")
            for decision in existing_decisions:
                db.delete(decision)
            db.flush()
            for role_code, candidates in review_steps:
                db.add(AssetReviewDecision(
                    submission_id=latest.id,
                    role_code=role_code,
                    status="pending",
                    candidate_reviewers=candidates,
                    candidate_search=" | ".join(
                        part
                        for candidate in candidates
                        for part in (candidate.get("user_name", ""), candidate.get("user_number", ""))
                        if part
                    ),
                ))
            latest.assignment_mode = payload.assignment_mode
            latest.designated_reviewer_number = (designated_reviewer or {}).get("user_number", "")
            latest.designated_reviewer_name = (designated_reviewer or {}).get("user_name", "")
            latest.route_center = route_center
            latest.route_group = route_group
            latest.note = payload.note.strip()
            latest.filename_snapshot = asset.filename
            latest.naming_evidence = evidence
            latest.naming_check = naming_check
            _notify_asset_review(db, latest, asset, "reassigned")
            db.commit()
        return _review_submission_out(db, latest, asset, user)
    version = (latest.version + 1) if latest else 1
    item = AssetReviewSubmission(
        id=str(uuid4()),
        asset_id=asset.id,
        version=version,
        status="pending",
        submitted_by_number=user_number(user),
        submitted_by_name=user_name(user),
        note=payload.note.strip(),
        filename_snapshot=asset.filename,
        naming_evidence=evidence,
        naming_check=naming_check,
        route_center=route_center,
        route_group=route_group,
        assignment_mode=payload.assignment_mode,
        designated_reviewer_number=(designated_reviewer or {}).get("user_number", ""),
        designated_reviewer_name=(designated_reviewer or {}).get("user_name", ""),
    )
    db.add(item)
    db.flush()
    for role_code, candidates in review_steps:
        db.add(AssetReviewDecision(
            submission_id=item.id,
            role_code=role_code,
            status="pending",
            candidate_reviewers=candidates,
            candidate_search=" | ".join(
                part
                for candidate in candidates
                for part in (candidate.get("user_name", ""), candidate.get("user_number", ""))
                if part
            ),
        ))
    db.add(
        AssetReviewAiResult(
            submission_id=item.id,
            status="pending" if config.ai_redline_enabled else "disabled",
            provider="cutter_rules",
            rule_version="wis-redline-v1",
            summary=(
                "已进入AI审核建议队列；审核人可同时开始人工审核，最终决定不受AI状态限制。"
                if config.ai_redline_enabled
                else "AI审核建议已关闭，不影响人工审核或推送门禁。"
            ),
            completed_at=None if config.ai_redline_enabled else datetime.utcnow(),
        )
    )
    _notify_asset_review(db, item, asset, "submitted")
    db.commit()
    return _review_submission_out(db, item, asset, user)


@app.post("/api/reviews/assets/batch-submit")
def review_assets_batch_submit(
    payload: AssetReviewBatchSubmit,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    succeeded: list[dict] = []
    failed: list[dict] = []
    seen: set[int] = set()
    for request_item in payload.items:
        if request_item.asset_id in seen:
            failed.append({"asset_id": request_item.asset_id, "detail": "同一批次中素材重复"})
            continue
        seen.add(request_item.asset_id)
        submit_payload = AssetReviewSubmit(
            note=request_item.note,
            naming_evidence=request_item.naming_evidence,
            no_applicable_sources=request_item.no_applicable_sources,
            assignment_mode=request_item.assignment_mode,
            designated_reviewer_number=request_item.designated_reviewer_number,
            designated_reviewer_name=request_item.designated_reviewer_name,
        )
        try:
            result = review_asset_submit(request_item.asset_id, submit_payload, db=db, user=user)
            succeeded.append({
                "asset_id": request_item.asset_id,
                "submission_id": result["id"],
                "asset_name": result["asset_name"],
                "status": result["status"],
                "route_center": result["route_center"],
                "route_group": result["route_group"],
                "assignment_mode": result["assignment_mode"],
                "designated_reviewer_number": result["designated_reviewer_number"],
                "designated_reviewer_name": result["designated_reviewer_name"],
                "current_reviewers": result["current_reviewers"],
            })
        except HTTPException as exc:
            db.rollback()
            failed.append({"asset_id": request_item.asset_id, "detail": str(exc.detail)})
        except Exception:
            db.rollback()
            logger.exception("Batch review submit failed asset_id=%s", request_item.asset_id)
            failed.append({"asset_id": request_item.asset_id, "detail": "提交失败，请稍后重试"})
    return {
        "ok": not failed,
        "total": len(payload.items),
        "succeeded": succeeded,
        "failed": failed,
    }


@app.post("/api/reviews/submissions/{submission_id}/retry-ai")
def review_ai_retry(
    submission_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if not _review_workflow_config(db).ai_redline_enabled:
        raise HTTPException(409, "AI审核建议当前已关闭")
    item = db.get(AssetReviewSubmission, submission_id)
    if not item:
        raise HTTPException(404, "未找到该审核任务")
    ai_result = db.scalar(
        select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == submission_id)
    )
    if not ai_result:
        raise HTTPException(409, "该任务属于历史人工审核流程")
    owner_number = item.submitted_by_number.strip().upper()
    owner_name = item.submitted_by_name.strip().casefold()
    is_owner = bool(
        (owner_number and owner_number == user_number(user).strip().upper())
        or (not owner_number and owner_name and owner_name == user_name(user).strip().casefold())
    )
    if not is_owner and not is_super_admin(user):
        raise HTTPException(403, "仅提审人或管理员可以重试AI建议识别")
    if ai_result.status != "error":
        raise HTTPException(409, "只有识别失败的AI建议任务可以重试")
    if item.status != "pending":
        raise HTTPException(409, "该审核任务已经结束")
    ai_result.status = "pending"
    ai_result.summary = "AI建议已重新进入识别队列；审核人可继续人工审核。"
    ai_result.error_message = ""
    ai_result.retry_count = int(ai_result.retry_count or 0) + 1
    ai_result.started_at = None
    ai_result.completed_at = None
    ai_result.updated_at = datetime.utcnow()
    db.commit()
    asset = db.get(Asset, item.asset_id)
    return _review_submission_out(db, item, asset, user)


def _review_act(
    submission_id: str,
    payload: AssetReviewAct,
    decision_status: str,
    db: Session,
    user: dict,
) -> dict:
    item = db.get(AssetReviewSubmission, submission_id)
    if not item:
        raise HTTPException(404, "未找到该审核任务")
    if item.status != "pending":
        raise HTTPException(409, "该审核任务已经结束")
    ai_result = db.scalar(
        select(AssetReviewAiResult).where(AssetReviewAiResult.submission_id == item.id)
    )
    config = _review_workflow_config(db)
    if not config.enabled and item.assignment_mode != "designated":
        raise HTTPException(409, "人工审核当前已关闭")
    roles = set(_review_user_roles(db, user))
    decisions = db.scalars(
        select(AssetReviewDecision)
        .where(AssetReviewDecision.submission_id == item.id)
        .order_by(AssetReviewDecision.id)
    ).all()
    current = next((row for row in decisions if row.status == "pending"), None)
    if not current or current.role_code != payload.role_code:
        current_label = REVIEW_ROLE_LABELS.get(current.role_code, "下一节点") if current else "无"
        raise HTTPException(409, f"当前应由{current_label}审核")
    if not _review_decision_allows_user(current, user, roles):
        candidates = current.candidate_reviewers or []
        reviewer_names = "、".join(str(item.get("user_name") or "") for item in candidates if item.get("user_name"))
        suffix = f"，本任务指定审核人：{reviewer_names}" if reviewer_names else ""
        raise HTTPException(403, f"当前账号不是本任务的{REVIEW_ROLE_LABELS[payload.role_code]}审核人{suffix}")
    quality_scores: dict[str, int] = {}
    quality_total: int | None = None
    quality_grade = ""
    if decision_status == "approved" and payload.role_code in REVIEW_HUMAN_DECISION_CODES and ai_result:
        for key in REVIEW_QUALITY_KEYS:
            if key not in payload.quality_scores:
                raise HTTPException(400, "通过审核前请完成四项效果评分")
            value = int(payload.quality_scores[key])
            if value < 0 or value > 25:
                raise HTTPException(400, "每项效果评分需在0到25分之间")
            quality_scores[key] = value
        quality_total = sum(quality_scores.values())
        if quality_total < 60:
            raise HTTPException(400, "效果评分低于60分，请驳回修改后再提审")
        quality_grade = (
            "priority" if quality_total >= 90 else "excellent" if quality_total >= 80 else "qualified"
        )
    elif payload.quality_scores:
        quality_scores = {
            key: max(0, min(25, int(payload.quality_scores.get(key, 0))))
            for key in REVIEW_QUALITY_KEYS
        }
        quality_total = sum(quality_scores.values())
        quality_grade = "revise" if decision_status == "rejected" else ""
    current.status = decision_status
    current.reviewer_number = user_number(user)
    current.reviewer_name = user_name(user)
    current.note = payload.note.strip()
    if quality_scores:
        current.quality_scores = quality_scores
        current.quality_total = quality_total
        current.quality_grade = quality_grade
    current.decided_at = datetime.utcnow()
    if decision_status == "rejected":
        item.status = "rejected"
        item.completed_at = datetime.utcnow()
    elif all(row.status == "approved" for row in decisions):
        item.status = "approved"
        item.completed_at = datetime.utcnow()
    asset = db.get(Asset, item.asset_id)
    _notify_asset_review(db, item, asset, decision_status, current)
    db.commit()
    asset = db.get(Asset, item.asset_id)
    return _review_submission_out(db, item, asset, user)


@app.post("/api/reviews/submissions/{submission_id}/approve")
def review_approve(
    submission_id: str,
    payload: AssetReviewAct,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    return _review_act(submission_id, payload, "approved", db, user)


@app.post("/api/reviews/submissions/{submission_id}/reject")
def review_reject(
    submission_id: str,
    payload: AssetReviewAct,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if not payload.note.strip():
        raise HTTPException(400, "驳回时请填写修改意见")
    return _review_act(submission_id, payload, "rejected", db, user)


@app.post("/api/reviews/submissions/batch-act")
def review_batch_act(
    payload: AssetReviewBatchAct,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    succeeded: list[dict] = []
    failed: list[dict] = []
    seen: set[str] = set()
    for batch_item in payload.items:
        if batch_item.submission_id in seen:
            continue
        seen.add(batch_item.submission_id)
        if payload.decision == "reject" and not batch_item.note.strip():
            failed.append({"submission_id": batch_item.submission_id, "detail": "批量驳回必须填写修改意见"})
            continue
        try:
            result = _review_act(
                batch_item.submission_id,
                AssetReviewAct(
                    role_code=batch_item.role_code,
                    note=batch_item.note,
                    quality_scores=batch_item.quality_scores,
                ),
                "approved" if payload.decision == "approve" else "rejected",
                db,
                user,
            )
            succeeded.append({
                "submission_id": batch_item.submission_id,
                "asset_name": result["asset_name"],
                "status": result["status"],
            })
        except HTTPException as error:
            failed.append({
                "submission_id": batch_item.submission_id,
                "detail": str(error.detail),
            })
    return {
        "ok": not failed,
        "decision": payload.decision,
        "succeeded": succeeded,
        "failed": failed,
    }


@app.get("/api/admin/operation-logs")
def operation_logs(
    q: str = "",
    module: str = "",
    result: str = Query(default="", pattern="^(|success|failed)$"),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=5, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_operation_admin(user)
    visibility_filters = list(_visible_operation_log_filters())
    filters = list(visibility_filters)
    if date_from and date_to and date_from > date_to:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    if date_from and date_to and (date_to - date_from).days > 90:
        raise HTTPException(400, "单次最多查询 91 天操作日志")
    if date_from:
        filters.append(
            OperationLog.created_at
            >= datetime.combine(date_from, datetime.min.time()) - timedelta(hours=8)
        )
    if date_to:
        filters.append(
            OperationLog.created_at
            < datetime.combine(date_to + timedelta(days=1), datetime.min.time()) - timedelta(hours=8)
        )
    if q.strip():
        pattern = f"%{q.strip()}%"
        filters.append(or_(
            OperationLog.actor_name.like(pattern),
            OperationLog.actor_number.like(pattern),
            OperationLog.path.like(pattern),
            OperationLog.resource_id.like(pattern),
        ))
    if module.strip():
        filters.append(OperationLog.module == module.strip())
    if result:
        filters.append(OperationLog.result == result)
    total = db.scalar(select(func.count()).select_from(OperationLog).where(*filters)) or 0
    rows = db.scalars(
        select(OperationLog).where(*filters).order_by(OperationLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    modules = db.scalars(
        select(OperationLog.module).where(*visibility_filters).distinct().order_by(OperationLog.module)
    ).all()
    return {
        "items": [operation_log_out(row) for row in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "modules": [value for value in modules if value],
    }


@app.get("/api/assets", response_model=AssetPage)
def list_assets(
    q: str = "",
    category: str = "",
    content_type: str = "",
    status: str = "",
    media_type: str = "",
    asset_scope: str = Query(default="", pattern="^(|marketing_video|product_image)$"),
    library_type: str = Query(default="", pattern="^(|source|remix)$"),
    asset_subtype: str = "",
    folder_name: str = "",
    ingest_source: str = Query(default="", pattern="^(|oa_upload|oss_scan)$"),
    mine_only: bool = False,
    directory: str = "",
    favorite: bool | None = None,
    hot_only: bool = False,
    effective_only: bool = False,
    upload_start_date: date | None = Query(default=None),
    upload_end_date: date | None = Query(default=None),
    sort: str = Query(
        default="newest",
        pattern="^(newest|oldest|name|size|favorites|gmv_desc|gmv_asc)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
    include_performance: bool = True,
):
    filters = [Asset.deleted_at.is_(None), Asset.purged_at.is_(None),
               _recovery_asset_allowed(db, Asset.id, "visibility")]
    _append_date_filters(filters, Asset.modified_at, upload_start_date, upload_end_date)
    gmv_sort = _asset_gmv_sort_subquery() if hot_only or sort in {"gmv_desc", "gmv_asc"} else None
    if hot_only:
        filters.append(
            Asset.id.in_(select(gmv_sort.c.asset_id).where(gmv_sort.c.gmv_yuan > 50000))
        )
    if not isinstance(asset_scope, str):
        asset_scope = ""
    if q:
        term = q.strip()
        filters.append(
            or_(
                Asset.filename.contains(term),
                Asset.category.contains(term),
                Asset.content_type.contains(term),
                Asset.asset_subtype.contains(term),
                Asset.source.contains(term),
                Asset.account_name.contains(term),
                Asset.uploaded_by_name.contains(term),
                Asset.uploaded_by_number.contains(term),
                Asset.material_description.contains(term),
                asset_tags_contain(term),
            )
        )
    if category:
        filters.append(Asset.category == canonical_product_category(category))
    if content_type:
        filters.append(Asset.content_type == content_type)
    if status:
        filters.append(Asset.status == status)
    if media_type:
        filters.append(Asset.media_type == media_type)
    if asset_scope:
        filters.append(Asset.asset_scope == asset_scope)
    if library_type:
        filters.append(Asset.library_type == library_type)
    if asset_subtype:
        filters.append(Asset.asset_subtype == asset_subtype)
    if folder_name:
        filters.append(Asset.folder_name == folder_name.strip())
    if ingest_source:
        filters.append(Asset.ingest_source == ingest_source)
    if mine_only:
        mine_conditions = []
        owner_number = user_number(user).strip()
        owner_name = user_name(user).strip()
        if owner_number:
            mine_conditions.append(Asset.uploaded_by_number == owner_number)
        if owner_name:
            mine_conditions.append(and_(Asset.uploaded_by_number == "", Asset.uploaded_by_name == owner_name))
        filters.append(or_(*mine_conditions) if mine_conditions else Asset.id == -1)
    if directory:
        safe_directory = directory.strip().lstrip("/")
        if not safe_directory.startswith(settings.prefix):
            raise HTTPException(400, "目录不在 yxb/ 范围内")
        filters.append(Asset.object_key.startswith(safe_directory))
    if favorite is not None:
        personal_favorites = select(AssetFavorite.asset_id).where(AssetFavorite.user_number == user_number(user))
        filters.append(Asset.id.in_(personal_favorites) if favorite else Asset.id.not_in(personal_favorites))
    if effective_only:
        filters.extend([
            Asset.media_type == "video",
            Asset.asset_scope == "marketing_video",
            Asset.id.in_(select(AssetEffectiveMark.asset_id)),
        ])

    favorite_count_subquery = (
        select(
            AssetFavorite.asset_id.label("asset_id"),
            func.count(AssetFavorite.user_number).label("favorite_count"),
        )
        .group_by(AssetFavorite.asset_id)
        .subquery("asset_favorite_counts")
    )
    favorite_count_column = func.coalesce(favorite_count_subquery.c.favorite_count, 0)
    if sort == "favorites":
        order_by = [favorite_count_column.desc(), Asset.modified_at.desc()]
    elif sort in {"gmv_desc", "gmv_asc"} and gmv_sort is not None:
        gmv_column = gmv_sort.c.gmv_yuan
        order_by = [
            gmv_column.is_(None).asc(),
            gmv_column.desc() if sort == "gmv_desc" else gmv_column.asc(),
            Asset.modified_at.desc(),
        ]
    else:
        order_by = [{
            "newest": Asset.modified_at.desc(),
            "oldest": Asset.modified_at.asc(),
            "name": Asset.filename.asc(),
            "size": Asset.size.desc(),
        }[sort]]
    total = db.scalar(select(func.count()).select_from(Asset).where(*filters)) or 0
    query = (
        select(Asset, favorite_count_column.label("favorite_count"))
        .outerjoin(favorite_count_subquery, favorite_count_subquery.c.asset_id == Asset.id)
        .where(*filters)
    )
    if gmv_sort is not None:
        query = query.outerjoin(gmv_sort, gmv_sort.c.asset_id == Asset.id)
    rows = db.execute(
        query.order_by(*order_by).offset((page - 1) * page_size).limit(page_size)
    ).all()
    asset_ids = [row.id for row, _favorite_count in rows]
    favorites = favorite_asset_ids(db, user, asset_ids)
    effective_marks = effective_marks_for_assets(db, asset_ids)
    # Delivery pickers need the original asset and review fields, not a full
    # lifetime metric history. Existing library clients keep their default data.
    gmv_summary = _asset_gmv_summary(db, asset_ids) if include_performance else {}
    platform_gmv = _asset_platform_gmv_summary(db, asset_ids, gmv_summary) if include_performance else {}
    review_summaries = _review_latest_summaries(db, asset_ids) if mine_only and media_type == "video" else {}
    return AssetPage(
        items=[to_asset_out(
            row,
            row.id in favorites,
            int(favorite_count or 0),
            can_manage=can_manage_asset(user, row),
            can_delete=can_delete_asset(user, row),
            gmv_summary=gmv_summary.get(row.id),
            platform_gmv=platform_gmv.get(row.id),
            effective_mark=effective_marks.get(row.id),
            review_summary=review_summaries.get(row.id),
        ) for row, favorite_count in rows],
        total=total,
        page=page,
        page_size=page_size,
        source=source_name(),
        source_updated_at=_source_updated_at(db),
    )


def facets(
    library_type: str = Query(default="", pattern="^(|source|remix)$"),
    asset_scope: str = Query(default="", pattern="^(|marketing_video|product_image)$"),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
    categories_only: bool = False,
):
    if categories_only:
        scope_filters = [Asset.deleted_at.is_(None), Asset.purged_at.is_(None),
                         _recovery_asset_allowed(db, Asset.id, "visibility")]
        if asset_scope:
            scope_filters.append(Asset.asset_scope == asset_scope)
        if library_type:
            scope_filters.append(Asset.library_type == library_type)
        # Keep category names/counts identical to full facets without loading
        # every asset's tags, directory, or other unused dimensions into Python.
        category = func.coalesce(func.nullif(Asset.category, ""), "未填写")
        rows = db.execute(
            select(category, func.count(Asset.id))
            .where(*scope_filters).group_by(category).order_by(func.min(Asset.id))
        ).all()
        counts = Counter({str(name): int(count) for name, count in rows})
        for name in ADDITIONAL_PRODUCT_CATEGORIES:
            counts.setdefault(name, 0)
        return {
            "categories": [{"name": name, "count": count} for name, count in counts.most_common()],
            "source": source_name(),
            "source_updated_at": _source_updated_at(db),
        }

    cache_key = f"facets:{asset_scope or 'all'}:{library_type or 'all'}"
    cached = _catalog_cache_get(cache_key)
    if cached is not None:
        return cached

    scope_filters = [Asset.deleted_at.is_(None), Asset.purged_at.is_(None),
                         _recovery_asset_allowed(db, Asset.id, "visibility")]
    if asset_scope:
        scope_filters.append(Asset.asset_scope == asset_scope)
    if library_type:
        scope_filters.append(Asset.library_type == library_type)

    rows = db.execute(
        select(
            Asset.object_key,
            Asset.tags,
            Asset.folder_name,
            Asset.category,
            Asset.content_type,
            Asset.status,
            Asset.ingest_source,
            Asset.library_type,
            Asset.asset_subtype,
        ).where(*scope_filters).execution_options(yield_per=500)
    )
    try:
        result = _facets_from_rows(rows, db)
    finally:
        rows.close()
    return _catalog_cache_set(cache_key, result)


def _new_facet_counts():
    fields = {
        "categories": "category",
        "content_types": "content_type",
        "statuses": "status",
        "ingest_sources": "ingest_source",
        "library_types": "library_type",
        "asset_subtypes": "asset_subtype",
    }
    counters = {key: Counter() for key in fields}
    tags: Counter[str] = Counter()
    business_folders: Counter[str] = Counter()
    directories: Counter[str] = Counter()
    return counters, tags, business_folders, directories


def _accumulate_facet_row(state, row):
    counters, tags, business_folders, directories = state
    fields = {
        "categories": "category",
        "content_types": "content_type",
        "statuses": "status",
        "ingest_sources": "ingest_source",
        "library_types": "library_type",
        "asset_subtypes": "asset_subtype",
    }
    item = row._mapping
    for output_key, field in fields.items():
        counters[output_key][str(item.get(field) or "未填写")] += 1
    tags.update(item.get("tags") or [])
    if item.get("folder_name"):
        business_folders[str(item["folder_name"])] += 1
    parts = [part for part in str(item.get("object_key") or "").split("/") if part]
    if len(parts) >= 2:
        directories["/".join(parts[:2]) + "/"] += 1


def _finish_facet_counts(state, db: Session):
    counters, tags, business_folders, directories = state
    # New products remain selectable before the first material is uploaded.
    # Do not reclassify historical assets or inflate their counts.
    for name in ADDITIONAL_PRODUCT_CATEGORIES:
        counters["categories"].setdefault(name, 0)
    result = {
        key: [{"name": name, "count": count} for name, count in counter.most_common()]
        for key, counter in counters.items()
    }
    result.update({
        "folders": [{"name": name, "count": count} for name, count in business_folders.most_common()],
        "tags": [{"name": name, "count": count} for name, count in tags.most_common(80)],
        "directories": [{"name": name, "count": count} for name, count in directories.most_common()],
        "source": source_name(),
        "source_updated_at": _source_updated_at(db),
    })
    return result


def _facets_from_rows(rows, db: Session) -> dict:
    state = _new_facet_counts()
    for row in rows:
        _accumulate_facet_row(state, row)
    return _finish_facet_counts(state, db)


def facets_bundle(db: Session = Depends(get_db), _user: dict = Depends(require_user)):
    """Return all marketing-library facets from one catalog pass."""
    keys = {
        "all": "facets:marketing_video:all",
        "source": "facets:marketing_video:source",
        "remix": "facets:marketing_video:remix",
    }
    cached = {name: _catalog_cache_get(key) for name, key in keys.items()}
    if all(cached.values()):
        return cached
    rows = db.execute(
        select(
            Asset.object_key,
            Asset.tags,
            Asset.folder_name,
            Asset.category,
            Asset.content_type,
            Asset.status,
            Asset.ingest_source,
            Asset.library_type,
            Asset.asset_subtype,
        ).where(
            Asset.deleted_at.is_(None),
            Asset.purged_at.is_(None),
            _recovery_asset_allowed(db, Asset.id, "visibility"),
            Asset.asset_scope == "marketing_video",
        ).execution_options(yield_per=500)
    )
    counts = {name: _new_facet_counts() for name in keys}
    try:
        for row in rows:
            _accumulate_facet_row(counts["all"], row)
            library = row._mapping.get("library_type")
            if library in {"source", "remix"}:
                _accumulate_facet_row(counts[library], row)
    finally:
        rows.close()
    result = {name: _finish_facet_counts(state, db) for name, state in counts.items()}
    for name, value in result.items():
        _catalog_cache_set(keys[name], value)
    return result


@app.get("/api/product-images/categories")
def product_image_categories(
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    cache_key = "product-image-categories"
    cached = _catalog_cache_get(cache_key)
    if cached is not None:
        return cached
    filters = [
        Asset.asset_scope == "product_image",
        Asset.media_type == "image",
        Asset.deleted_at.is_(None),
        Asset.purged_at.is_(None),
    ]
    counts = dict(
        db.execute(select(Asset.category, func.count()).where(*filters).group_by(Asset.category)).all()
    )
    preferred = ["通用", "隐形水润面膜", "晶润眼膜", "深海次抛", "黑晶面膜", "燕窝面膜", "肌活蛋白喷雾", "颈膜", "黄金面膜", "美白针", *ADDITIONAL_PRODUCT_CATEGORIES, "其他 WIS 素材"]
    names = preferred + sorted(name for name in counts if name not in preferred)
    items = []
    for name in names:
        cover = db.scalar(select(Asset).where(*filters, Asset.category == name).order_by(Asset.modified_at.desc()))
        items.append({
            "name": name,
            "count": int(counts.get(name, 0)),
            "cover_url": oss_service.url_for(cover.object_key) if cover else "",
        })
    return _catalog_cache_set(cache_key, {"items": items, "total": sum(counts.values())})


@app.post("/api/sync/oss", response_model=SyncStatusOut)
def sync_oss(background_tasks: BackgroundTasks, user: dict = Depends(require_user)):
    if not is_asset_admin(user):
        raise HTTPException(403, "仅素材管理员可扫描 OSS")
    return start_oss_sync(background_tasks)


@app.get("/api/sync/oss/status", response_model=SyncStatusOut)
def sync_oss_status(_user: dict = Depends(require_user)):
    return _sync_snapshot()


@app.post("/api/assets/refresh", response_model=SyncStatusOut)
def refresh_assets(background_tasks: BackgroundTasks, user: dict = Depends(require_user)):
    if not is_asset_admin(user):
        raise HTTPException(403, "仅素材管理员可扫描 OSS")
    return start_oss_sync(background_tasks)


def _multipart_session_for_user(db: Session, session_id: str, user: dict) -> UploadSession:
    session = db.get(UploadSession, session_id)
    if not session or session.owner_number != user_number(user):
        raise HTTPException(404, "上传会话不存在")
    return session


def _multipart_session_out(session: UploadSession, uploaded_parts: list[dict] | None = None) -> dict:
    total_parts = (session.file_size + session.part_size - 1) // session.part_size
    return {
        "session_id": session.id,
        "object_key": session.object_key,
        "filename": session.filename,
        "file_size": session.file_size,
        "part_size": session.part_size,
        "total_parts": total_parts,
        "status": session.status,
        "uploaded_parts": uploaded_parts or [],
        "expires_at": _utc_iso(session.expires_at),
        "public_url": oss_service.url_for(session.object_key),
    }


def _recover_completed_multipart_session(session: UploadSession, db: Session) -> bool:
    """Reconcile a lost completion receipt against this owner's unique OSS key.

    HEAD proves a finalized object exists, not just uploaded parts. An unavailable
    HEAD is inconclusive: keep the session and never create another upload for it.
    This retains the existing key/size completion contract; it is not a SHA audit.
    """
    try:
        remote = oss_service.head_asset(session.object_key)
    except Exception as error:
        raise HTTPException(503, "上传结果暂时无法核验，已保留进度，请稍后重试") from error
    if not remote:
        return False
    if (remote.get("object_key") != session.object_key
            or int(remote.get("size") or 0) != session.file_size
            or not str(remote.get("etag") or "").strip()):
        raise HTTPException(409, "OSS 文件与上传会话不一致，已保留记录，请联系管理员核验")
    session.status = "completed"
    session.completed_at = session.completed_at or datetime.utcnow()
    session.updated_at = datetime.utcnow()
    db.commit()
    return True


def _multipart_parts_best_effort(session: UploadSession, db: Session) -> list[dict]:
    """Return server-visible parts when the OSS account permits ListParts.

    Some OSS roles intentionally allow create/upload/complete but deny ListParts.
    Upload correctness is still enforced at completion using the client-provided
    ETags and a final HEAD size check, so a denied inventory lookup must not
    invalidate an otherwise resumable session.
    """
    try:
        return oss_service.list_multipart_parts(session.object_key, session.multipart_upload_id)
    except Exception as error:
        response = getattr(error, "response", {}) or {}
        code = str((response.get("Error") or {}).get("Code") or type(error).__name__)
        if code == "NoSuchUpload":
            if _recover_completed_multipart_session(session, db):
                return []
            # Both the multipart handle and final object are confirmed absent.
            # Retrying may now create a new session instead of looping forever.
            session.status = "expired"
            session.updated_at = datetime.utcnow()
            db.commit()
            raise HTTPException(409, "OSS 分片会话已失效且未发现完整文件，请重试该文件") from error
        logger.warning(
            "Multipart part inventory unavailable session_id=%s code=%s; using client receipts",
            session.id,
            code,
        )
        return []


def _multipart_lookup_completed_object(session: UploadSession) -> bool:
    """Read the original object without changing the session or OSS state."""
    try:
        remote = oss_service.head_asset(session.object_key)
    except Exception as error:
        raise HTTPException(503, "原上传文件暂时无法核验，已保留会话，请稍后恢复") from error
    if remote is None:
        return False
    try:
        valid = (
            remote.get("object_key") == session.object_key
            and int(remote.get("size") or 0) == session.file_size
            and bool(str(remote.get("etag") or "").strip())
        )
    except (AttributeError, TypeError, ValueError):
        valid = False
    if not valid:
        raise HTTPException(409, "原文件与上传会话不一致，已保留记录，请核验原文件")
    return True


@app.post("/api/uploads/multipart/lookup")
def lookup_multipart_upload_session(
    payload: MultipartUploadLookup,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Find this owner's existing session; never create, complete, or expire it.

    A lost completion receipt is reported explicitly. The existing status route
    can then reconcile that same session, without lookup mutating its state.
    """
    actual_sha256 = payload.sha256.lower()
    digests = {actual_sha256}
    if payload.legacy_sha256:
        digests.add(payload.legacy_sha256.lower())
    with db.no_autoflush:
        session = db.scalar(
            select(UploadSession).where(
                UploadSession.owner_number == user_number(user),
                UploadSession.file_size == payload.size,
                UploadSession.asset_scope == payload.asset_scope,
                UploadSession.category == payload.category,
                UploadSession.sha256.in_(digests),
                UploadSession.status.in_({"active", "completed"}),
            ).order_by(
                (UploadSession.sha256 == actual_sha256).desc(),
                UploadSession.created_at.desc(), UploadSession.id.desc(),
            ).limit(1)
        )
    if session is None:
        return {"session": None}
    if not isinstance(session.part_size, int) or session.part_size <= 0:
        raise HTTPException(409, "原上传会话的分片信息异常，已保留记录，请核验")
    if session.status == "completed":
        if not _multipart_lookup_completed_object(session):
            raise HTTPException(409, "原完成记录对应的文件未找到，已保留会话，请核验原文件")
        return {"session": _multipart_session_out(session)}
    if session.expires_at is None or session.expires_at <= datetime.utcnow():
        raise HTTPException(409, "原上传会话已过期，已保留记录，请恢复或核验原会话")
    try:
        parts = oss_service.list_multipart_parts(session.object_key, session.multipart_upload_id)
    except Exception as error:
        response = getattr(error, "response", {}) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        if code == "NoSuchUpload":
            if _multipart_lookup_completed_object(session):
                return {"session": _multipart_session_out(session), "recovery_required": True}
            raise HTTPException(409, "原分片会话和完整文件均未找到，已保留记录，请核验原会话") from error
        # Cross-entry recovery has no reliable local receipts. A denied or
        # timed-out inventory cannot be reported as an empty upload.
        raise HTTPException(503, "原上传分片暂时无法核验，已保留会话，请稍后恢复") from error
    total_parts = (session.file_size + session.part_size - 1) // session.part_size
    seen = set()
    if not isinstance(parts, list):
        raise HTTPException(503, "原上传分片返回信息异常，已保留会话，请稍后恢复")
    for part in parts:
        number = part.get("part_number") if isinstance(part, dict) else None
        if (not isinstance(number, int) or number < 1 or number > total_parts
                or number in seen
                or part.get("size") != min(session.part_size, session.file_size - (number - 1) * session.part_size)
                or not isinstance(part.get("etag"), str) or not part["etag"].strip()):
            raise HTTPException(409, "原上传分片与会话不一致，已保留回执，请核验原文件")
        seen.add(number)
    return {"session": _multipart_session_out(session, parts)}


@app.post("/api/uploads/multipart/sessions")
def create_multipart_upload_session(
    payload: MultipartUploadCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner_number = user_number(user)
    owner_name = user_name(user)
    sha256 = payload.sha256.lower()
    now = datetime.utcnow()
    existing = db.scalar(
        select(UploadSession)
        .where(
            UploadSession.owner_number == owner_number,
            UploadSession.sha256 == sha256,
            UploadSession.file_size == payload.size,
            UploadSession.asset_scope == payload.asset_scope,
            UploadSession.category == payload.category,
            UploadSession.status.in_({"active", "completed"}),
        )
        .order_by(UploadSession.created_at.desc())
    )
    if existing and existing.status == "active" and existing.expires_at <= now:
        if _recover_completed_multipart_session(existing, db):
            return _multipart_session_out(existing)
        try:
            oss_service.abort_multipart_upload(existing.object_key, existing.multipart_upload_id)
        except Exception:
            logger.warning("Unable to abort expired multipart session id=%s", existing.id, exc_info=True)
        existing.status = "expired"
        existing.updated_at = now
        db.commit()
        existing = None
    if existing and existing.status == "completed":
        try:
            remote = oss_service.head_asset(existing.object_key)
        except Exception as error:
            raise HTTPException(503, "已上传文件暂时无法核验，已保留完成记录，请稍后重试") from error
        if not remote or int(remote.get("size") or 0) != payload.size:
            existing.status = "invalid"
            existing.updated_at = now
            db.commit()
            existing = None
    if existing:
        parts = [] if existing.status == "completed" else _multipart_parts_best_effort(existing, db)
        return _multipart_session_out(existing, parts)

    try:
        object_key = oss_service.create_object_key(
            payload.filename,
            owner_name or owner_number,
            asset_scope=payload.asset_scope,
            category=payload.category,
        )
        upload_id = oss_service.create_multipart_upload(object_key, payload.content_type)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, "无法创建分片上传会话") from error
    part_size = payload.part_size or (
        64 * 1024 * 1024 if payload.size >= 1024 * 1024 * 1024 else UPLOAD_PART_BYTES
    )
    session = UploadSession(
        id=str(uuid4()),
        owner_number=owner_number,
        owner_name=owner_name,
        sha256=sha256,
        file_size=payload.size,
        filename=normalize_upload_filename(payload.filename),
        content_type=payload.content_type,
        asset_scope=payload.asset_scope,
        category=payload.category,
        object_key=object_key,
        multipart_upload_id=upload_id,
        part_size=part_size,
        status="active",
        expires_at=now + timedelta(hours=settings.upload_session_hours),
        created_at=now,
        updated_at=now,
    )
    db.add(session)
    db.commit()
    return _multipart_session_out(session)


@app.get("/api/uploads/multipart/sessions/{session_id}")
def multipart_upload_session_status(
    session_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    session = _multipart_session_for_user(db, session_id, user)
    if session.status == "active" and session.expires_at <= datetime.utcnow():
        if _recover_completed_multipart_session(session, db):
            return _multipart_session_out(session)
        session.status = "expired"
        session.updated_at = datetime.utcnow()
        db.commit()
    parts = [] if session.status != "active" else _multipart_parts_best_effort(session, db)
    return _multipart_session_out(session, parts)


@app.post("/api/uploads/multipart/sessions/{session_id}/parts")
def multipart_upload_part_urls(
    session_id: str,
    payload: MultipartPartUrlsCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    session = _multipart_session_for_user(db, session_id, user)
    if session.status != "active" or session.expires_at <= datetime.utcnow():
        raise HTTPException(409, "上传会话已结束，请重新选择文件")
    total_parts = (session.file_size + session.part_size - 1) // session.part_size
    numbers = sorted(set(payload.part_numbers))
    if any(number < 1 or number > total_parts for number in numbers):
        raise HTTPException(400, "分片编号超出文件范围")
    try:
        items = [
            {
                "part_number": number,
                "upload_url": oss_service.presign_upload_part(
                    session.object_key, session.multipart_upload_id, number
                ),
                "headers": {},
                "expires_in": 3600,
            }
            for number in numbers
        ]
    except Exception as error:
        raise HTTPException(502, "无法创建分片上传地址") from error
    return {"items": items, "total": len(items)}


@app.put("/api/uploads/multipart/sessions/{session_id}/relay-parts/{part_number}")
async def multipart_upload_part_relay(
    session_id: str,
    part_number: int,
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Durable same-origin fallback when a browser cannot reach OSS directly."""
    session = _multipart_session_for_user(db, session_id, user)
    if session.status != "active" or session.expires_at <= datetime.utcnow():
        raise HTTPException(409, "上传会话已结束，请重新选择文件")
    total_parts = (session.file_size + session.part_size - 1) // session.part_size
    if part_number < 1 or part_number > total_parts:
        raise HTTPException(400, "分片编号超出文件范围")
    expected_size = min(
        session.part_size,
        session.file_size - (part_number - 1) * session.part_size,
    )
    supplied_md5 = request.headers.get("x-upload-part-md5", "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{32}", supplied_md5):
        raise HTTPException(400, "分片校验值格式不正确")
    content_length = request.headers.get("content-length", "").strip()
    if content_length:
        try:
            if int(content_length) != expected_size:
                raise HTTPException(400, "分片大小不正确，请重试该文件")
        except ValueError as error:
            raise HTTPException(400, "分片大小格式不正确") from error

    received = 0
    digest = hashlib.md5(usedforsecurity=False)
    with tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024) as staged:
        async for chunk in request.stream():
            if not chunk:
                continue
            received += len(chunk)
            if received > expected_size:
                raise HTTPException(413, "分片超过预期大小，请重试该文件")
            digest.update(chunk)
            staged.write(chunk)
        if received != expected_size:
            raise HTTPException(400, f"分片接收不完整（{received}/{expected_size}）")
        if digest.hexdigest() != supplied_md5:
            raise HTTPException(409, "分片校验失败，请重试该分片")
        staged.seek(0)
        try:
            etag = await run_in_threadpool(
                oss_service.upload_multipart_part,
                session.object_key,
                session.multipart_upload_id,
                part_number,
                staged,
                expected_size,
                base64.b64encode(digest.digest()).decode("ascii"),
            )
        except Exception as error:
            response = getattr(error, "response", {}) or {}
            code = str((response.get("Error") or {}).get("Code") or type(error).__name__)
            logger.warning(
                "Multipart relay failed session_id=%s part=%s code=%s",
                session.id,
                part_number,
                code,
            )
            raise HTTPException(502, "云管家中转分片失败，系统将自动重试") from error
    return {"part_number": part_number, "etag": etag, "size": received, "relayed": True}


@app.post("/api/uploads/multipart/sessions/{session_id}/complete")
def complete_multipart_upload_session(
    session_id: str,
    payload: dict,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    session = _multipart_session_for_user(db, session_id, user)
    if session.status == "completed":
        return _multipart_session_out(session)
    if session.status == "active" and session.expires_at <= datetime.utcnow():
        if _recover_completed_multipart_session(session, db):
            return _multipart_session_out(session)
    if session.status != "active" or session.expires_at <= datetime.utcnow():
        raise HTTPException(409, "上传会话已结束，请重新选择文件")
    total_parts = (session.file_size + session.part_size - 1) // session.part_size
    # Colleagues may keep the uploader open across a production release. Accept
    # both the current receipt shape and older/cross-browser aliases so an
    # already transferred original is not rejected with FastAPI's generic 422.
    if isinstance(payload, BaseModel):
        payload = payload.model_dump()
    if not isinstance(payload, dict):
        raise HTTPException(400, "上传合并信息格式不正确，请刷新页面后重试")
    raw_parts = payload.get("parts") or payload.get("uploaded_parts") or payload.get("uploadedParts") or []
    if isinstance(raw_parts, dict):
        raw_parts = list(raw_parts.values())
    if not isinstance(raw_parts, list) or not raw_parts or len(raw_parts) > 10000:
        raise HTTPException(409, f"仍有分片未上传完成（0/{total_parts}）")
    normalized_parts = []
    for index, item in enumerate(raw_parts):
        if isinstance(item, str):
            item = {"part_number": index + 1, "etag": item}
        if not isinstance(item, dict):
            raise HTTPException(400, f"第 {index + 1} 个分片回执格式不正确")
        number = item.get("part_number", item.get("partNumber", item.get("PartNumber", index + 1)))
        etag = item.get("etag", item.get("ETag", item.get("eTag", "")))
        try:
            number = int(number)
        except (TypeError, ValueError) as error:
            raise HTTPException(400, f"第 {index + 1} 个分片编号不正确") from error
        expected_size = min(session.part_size, session.file_size - (number - 1) * session.part_size)
        size = item.get("size", item.get("Size", expected_size))
        try:
            size = int(size)
        except (TypeError, ValueError) as error:
            raise HTTPException(400, f"第 {number} 个分片大小不正确") from error
        etag = str(etag or "").strip()
        if not etag:
            raise HTTPException(409, f"第 {number} 个分片缺少 OSS 校验值，请重试该文件")
        if len(etag) > 2048 or "\r" in etag or "\n" in etag:
            raise HTTPException(400, f"第 {number} 个分片校验值格式不正确")
        normalized_parts.append({"part_number": number, "etag": etag, "size": size})
    ordered = sorted(
        normalized_parts,
        key=lambda row: int(row["part_number"]),
    )
    if [int(row["part_number"]) for row in ordered] != list(range(1, total_parts + 1)):
        raise HTTPException(409, f"仍有分片未上传完成（{len(ordered)}/{total_parts}）")
    for index, row in enumerate(ordered):
        expected_size = min(session.part_size, session.file_size - index * session.part_size)
        if int(row["size"]) != expected_size:
            raise HTTPException(409, f"第 {index + 1} 个分片大小不正确，请重试该文件")
        etag = str(row["etag"])
        if "\r" in etag or "\n" in etag:
            raise HTTPException(400, "分片校验值格式不正确")
    try:
        oss_service.complete_multipart_upload(session.object_key, session.multipart_upload_id, ordered)
        remote = oss_service.head_asset(session.object_key)
    except Exception as error:
        response = getattr(error, "response", {}) or {}
        code = str((response.get("Error") or {}).get("Code") or "")
        logger.warning(
            "Multipart completion failed session_id=%s code=%s",
            session.id,
            code or type(error).__name__,
        )
        # CompleteMultipartUpload may have succeeded before the connection was
        # lost or our database commit failed. Do not force retransmission then.
        if code not in {"InvalidPart", "InvalidPartOrder", "AccessDenied"}:
            if _recover_completed_multipart_session(session, db):
                return _multipart_session_out(session, ordered)
            if code == "NoSuchUpload":
                session.status = "expired"
                session.updated_at = datetime.utcnow()
                db.commit()
        if code in {"InvalidPart", "InvalidPartOrder", "NoSuchUpload"}:
            raise HTTPException(409, "分片记录已失效，请点击重试；系统会自动补传缺失分片") from error
        if code == "AccessDenied":
            raise HTTPException(502, "OSS 合并权限不足，请联系管理员检查分片上传权限") from error
        raise HTTPException(502, "OSS 合并分片失败，请稍后重试") from error
    if not remote or int(remote.get("size") or 0) != session.file_size:
        raise HTTPException(409, "OSS 合并结果与原文件大小不一致")
    sha256 = str(payload.get("sha256") or payload.get("hash") or session.sha256 or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        sha256 = session.sha256.lower()
    session.sha256 = sha256
    session.status = "completed"
    session.completed_at = datetime.utcnow()
    session.updated_at = session.completed_at
    db.commit()
    return _multipart_session_out(session, ordered)


@app.delete("/api/uploads/multipart/sessions/{session_id}")
def cancel_multipart_upload_session(
    session_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    session = _multipart_session_for_user(db, session_id, user)
    if session.status == "active":
        try:
            oss_service.abort_multipart_upload(session.object_key, session.multipart_upload_id)
        except Exception as error:
            raise HTTPException(502, "无法取消 OSS 分片上传") from error
        session.status = "cancelled"
        session.updated_at = datetime.utcnow()
        db.commit()
    return {"ok": True, "session_id": session.id, "status": session.status}


@app.post("/api/uploads/part-leases/acquire")
def upload_part_lease_acquire(payload: UploadLeaseCreate, user: dict = Depends(require_user)):
    return acquire_upload_lease(user_number(user), payload.session_id, payload.request_id)


@app.post("/api/uploads/part-leases/renew")
def upload_part_lease_renew(payload: UploadLeaseRenew, user: dict = Depends(require_user)):
    if not renew_upload_lease(user_number(user), payload.lease_id):
        raise HTTPException(409, "上传并发凭证已过期")
    return {"ok": True, "expires_in": settings.upload_lease_seconds}


@app.post("/api/uploads/part-leases/release")
def upload_part_lease_release(payload: UploadLeaseRenew, user: dict = Depends(require_user)):
    return {"ok": release_upload_lease(user_number(user), payload.lease_id)}


@app.post("/api/uploads/presign", response_model=UploadTicket)
def create_upload(payload: UploadCreate, user: dict = Depends(require_user), db: Session = Depends(get_db)):
    uploader = str(user.get("realName") or user.get("name") or user.get("number") or "OA用户")
    try:
        ticket = oss_service.create_upload(
            payload.filename,
            payload.content_type,
            uploader,
            asset_scope=payload.asset_scope,
            category=payload.category,
        )
        return remember_direct_ticket(
            db, ticket, filename=normalize_upload_filename(payload.filename), size=payload.size,
            content_type=payload.content_type, owner_number=user_number(user), owner_name=user_name(user),
            asset_scope=payload.asset_scope, category=canonical_product_category(payload.category),
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, "无法创建安全上传地址") from error


def _normalized_reference_url(value: str) -> str:
    normalized = (value or "").strip()
    # Douyin/Xiaohongshu share copies often contain a title, account name and
    # short link in the same text. Preserve that original reference verbatim;
    # the UI extracts a clickable http(s) address when one exists.
    return normalized[:2048]


def _validated_reference_video(key: str, name: str) -> tuple[str, str]:
    normalized = (key or "").strip()
    if not normalized:
        return "", ""
    reference_prefix = settings.prefix.rstrip("/") + "/references/"
    # Historical objects use lowercase yxb/ while some deployments configured
    # the same managed prefix as YXB/. OSS keys remain unchanged for the real
    # HEAD check; only the managed-directory boundary comparison is casefolded.
    if not normalized.casefold().startswith(reference_prefix.casefold()):
        raise HTTPException(400, "参考视频路径不在受管 references 目录内")
    try:
        remote = oss_service.head_asset(normalized)
    except Exception as error:
        raise HTTPException(502, "参考视频验证失败") from error
    if not remote or remote.get("media_type") != "video" or int(remote.get("size") or 0) <= 0:
        raise HTTPException(400, "参考视频尚未上传完成或文件格式不可用")
    return normalized, Path(name or normalized).name


def _validated_managed_images(
    items: list[dict] | None,
    *,
    label: str,
) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    reference_prefix = settings.prefix.rstrip("/") + "/references/"
    for item in (items or [])[:9]:
        normalized = str(item.get("object_key") or "").strip()
        if not normalized or not normalized.casefold().startswith(reference_prefix.casefold()):
            raise HTTPException(400, f"{label}路径不在受管 references 目录内")
        try:
            remote = oss_service.head_asset(normalized)
        except Exception as error:
            raise HTTPException(502, f"{label}验证失败") from error
        if not remote or remote.get("media_type") != "image" or int(remote.get("size") or 0) <= 0:
            raise HTTPException(400, f"{label}尚未上传完成或文件格式不可用")
        if int(remote.get("size") or 0) > 20 * 1024 * 1024:
            raise HTTPException(400, f"单张{label}不能超过 20MB")
        validated.append({
            "object_key": normalized,
            "filename": Path(str(item.get("filename") or normalized)).name[:512],
        })
    return validated


def _validated_reference_images(items: list[dict] | None) -> list[dict[str, str]]:
    return _validated_managed_images(items, label="参考图片")


def _validated_performance_screenshots(items: list[dict] | None) -> list[dict[str, str]]:
    return _validated_managed_images(items, label="数据截图")


def _validated_video_request_references(items: list[dict] | None) -> list[dict[str, str]]:
    validated: list[dict[str, str]] = []
    seen: set[str] = set()
    reference_prefix = settings.prefix.rstrip("/") + "/references/"
    for item in (items or [])[:9]:
        normalized = str(item.get("object_key") or "").strip()
        if normalized in seen:
            continue
        if not normalized or not normalized.casefold().startswith(reference_prefix.casefold()):
            raise HTTPException(400, "参考视频路径不在受管 references 目录内")
        try:
            remote = oss_service.head_asset(normalized)
        except Exception as error:
            raise HTTPException(502, "参考视频验证失败") from error
        size = int((remote or {}).get("size") or 0)
        if not remote or remote.get("media_type") != "video" or size <= 0:
            raise HTTPException(400, "参考视频尚未上传完成或文件格式不可用")
        if size > 500 * 1024 * 1024:
            raise HTTPException(400, "单条参考视频不能超过 500MB")
        validated.append({
            "object_key": normalized,
            "filename": Path(str(item.get("filename") or normalized)).name[:512],
        })
        seen.add(normalized)
    return validated


def _complete_upload_for_user(
    payload: UploadComplete,
    user: dict,
    db: Session,
    *,
    source: str = "oa_upload",
    extra_tags: tuple[str, ...] = (),
    workstation_record: WorkstationReturn | None = None,
):
    scope_folder = "product-images" if payload.asset_scope == "product_image" else "uploads"
    upload_prefix = settings.prefix.rstrip("/") + f"/{scope_folder}/"
    if not payload.object_key.startswith(upload_prefix):
        raise HTTPException(400, f"上传路径不在 yxb/{scope_folder}/ 范围内")
    try:
        remote = oss_service.head_asset(payload.object_key)
    except Exception as error:
        raise HTTPException(502, "上传验证失败") from error
    if not remote or remote["size"] <= 0:
        raise HTTPException(400, "OSS 中尚未找到已上传文件")
    if payload.asset_scope == "product_image" and remote["media_type"] != "image":
        raise HTTPException(400, "产品图片资产库仅支持图片文件")
    asset, upload_session, replay = registration_proof(
        db, payload, user_number(user), remote, canonical_product_category,
        workstation_record=workstation_record,
    )
    if replay:
        # Read back the original asset; retries do not rewrite ownership,
        # classification, annotations or any review version.
        return to_asset_out(
            asset, asset.id in favorite_asset_ids(db, user, [asset.id]),
            favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
            can_manage=can_manage_asset(user, asset), can_delete=can_delete_asset(user, asset),
            review_summary=_review_latest_summaries(db, [asset.id]).get(asset.id),
        )
    original_filename = normalize_upload_filename(payload.filename)
    remote["filename"] = original_filename
    reference_url = _normalized_reference_url(payload.reference_url)
    reference_video_key, reference_video_name = _validated_reference_video(
        payload.reference_video_key, payload.reference_video_name
    )
    material_description = payload.material_description.strip()[:10000]
    performance_screenshots = _validated_performance_screenshots(payload.performance_screenshots)

    uploader = str(user.get("realName") or user.get("name") or user.get("number") or "OA用户")
    tags = list(dict.fromkeys(
        [tag.strip() for tag in payload.tags if tag.strip()]
        + list(extra_tags)
        + ["产品图片" if payload.asset_scope == "product_image" else "上传素材"]
    ))[:30]
    resolved_content_type = (
        infer_product_image_type(original_filename)
        if payload.asset_scope == "product_image" and payload.content_type in {"", "自动识别", "其他"}
        else payload.content_type.strip() or "其他"
    )
    if not asset:
        asset = Asset(
            **remote,
            category=canonical_product_category(payload.category),
            content_type=resolved_content_type,
            status="待整理",
            asset_scope=payload.asset_scope,
            library_type="source" if payload.asset_scope == "product_image" else payload.library_type,
            asset_subtype="产品图片" if payload.asset_scope == "product_image" else payload.asset_subtype.strip() or ("其他混剪成片" if payload.library_type == "remix" else "其他视频素材"),
            folder_name=normalize_asset_folder_name(payload.folder_name) if payload.library_type == "source" else "",
            tags=tags,
            favorite=False,
            cover_url="",
            source=source,
            account_name=uploader,
            ingest_source="oa_upload",
            uploaded_by_number=user_number(user),
            uploaded_by_name=user_name(user),
            reference_url=reference_url,
            reference_video_key=reference_video_key,
            reference_video_name=reference_video_name,
            material_description=material_description,
            performance_screenshots=performance_screenshots,
        )
        db.add(asset)
    else:
        for field, value in remote.items():
            setattr(asset, field, value)
        asset.category = canonical_product_category(payload.category, asset.category or "待分类")
        asset.content_type = resolved_content_type
        asset.asset_scope = payload.asset_scope
        asset.library_type = "source" if payload.asset_scope == "product_image" else payload.library_type
        asset.asset_subtype = "产品图片" if payload.asset_scope == "product_image" else payload.asset_subtype.strip() or ("其他混剪成片" if payload.library_type == "remix" else "其他视频素材")
        asset.folder_name = normalize_asset_folder_name(payload.folder_name) if payload.library_type == "source" else ""
        asset.tags = tags
        asset.source = source
        asset.account_name = uploader
        asset.ingest_source = "oa_upload"
        asset.uploaded_by_number = user_number(user)
        asset.uploaded_by_name = user_name(user)
        asset.reference_url = reference_url
        asset.reference_video_key = reference_video_key
        asset.reference_video_name = reference_video_name
        asset.material_description = material_description
        asset.performance_screenshots = performance_screenshots
    finish_registration(upload_session, canonical_product_category(payload.category))
    db.commit()
    _invalidate_catalog_cache()
    db.refresh(asset)
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
    )


@app.post("/api/uploads/complete", response_model=AssetOut)
def complete_upload(
    payload: UploadComplete,
    user: dict = Depends(require_user),
    db: Session = Depends(get_db),
):
    return _complete_upload_for_user(payload, user, db)


def _workstation_asset_payload(
    asset: Asset, effective_mark: AssetEffectiveMark | None = None
) -> dict:
    return {
        "id": asset.id,
        "filename": asset.filename,
        "object_key": asset.object_key,
        "size": asset.size,
        "modified_at": asset.modified_at.isoformat(timespec="seconds") + "Z",
        "category": asset.category,
        "content_type": asset.content_type,
        "asset_subtype": asset.asset_subtype,
        "library_type": asset.library_type,
        "folder_name": asset.folder_name,
        "tags": list(asset.tags or []),
        "cover_url": asset.cover_url or "",
        "preview_url": oss_service.url_for(asset.object_key),
        "download_url": oss_service.url_for(asset.object_key, download=True, expires_in=3600),
        "uploaded_by_name": asset.uploaded_by_name,
        "source": asset.source,
        "reference_url": asset.reference_url,
        "effective": effective_mark is not None,
        "effective_marked_by_name": effective_mark.marked_by_name if effective_mark else "",
        "effective_marked_at": (
            effective_mark.created_at.isoformat(timespec="seconds") + "Z"
            if effective_mark
            else None
        ),
    }


def _workstation_return_payload(record: WorkstationReturn, db: Session) -> dict:
    asset = db.get(Asset, record.asset_id) if record.asset_id else None
    asset_available = bool(asset and asset.deleted_at is None and asset.purged_at is None)
    return {
        "idempotency_key": record.idempotency_key,
        "status": record.status,
        "asset_id": record.asset_id,
        "asset_available": asset_available,
        "asset": _workstation_asset_payload(asset) if asset_available else None,
        "filename": record.filename,
        "object_key": record.object_key,
        "sha256": record.sha256,
        "provenance": dict(record.provenance or {}),
        "error_message": record.error_message,
        "created_at": record.created_at.isoformat(timespec="seconds") + "Z",
        "updated_at": record.updated_at.isoformat(timespec="seconds") + "Z",
        "completed_at": (
            record.completed_at.isoformat(timespec="seconds") + "Z"
            if record.completed_at
            else None
        ),
    }


def _workstation_assessment_is_trusted(provenance: dict) -> bool:
    if not provenance.get("automatic_review_enabled"):
        return False
    assessment = provenance.get("automatic_assessment")
    if not isinstance(assessment, dict):
        return False
    checks = assessment.get("checks")
    return bool(
        assessment.get("status") == "passed"
        and assessment.get("recommendation") == "approve"
        and assessment.get("autoApproved") is True
        and float(assessment.get("score") or 0) >= 90
        and isinstance(checks, list)
        and checks
        and all(isinstance(check, dict) and check.get("passed") is True for check in checks)
    )


def _register_workstation_automatic_review(
    db: Session,
    asset: Asset,
    provenance: dict,
    *,
    actor_number: str,
    actor_name: str,
) -> None:
    if not _workstation_assessment_is_trusted(provenance):
        return
    if "自动混剪" not in asset.filename:
        return
    latest = _review_latest_submission(db, asset.id)
    if latest:
        return
    assessment = dict(provenance.get("automatic_assessment") or {})
    now = datetime.utcnow()
    submission = AssetReviewSubmission(
        id=str(uuid4()),
        asset_id=asset.id,
        version=1,
        status="approved",
        submitted_by_number=actor_number,
        submitted_by_name=actor_name,
        note=(
            "混剪工作台已完成自动成片审核：来源切片均已审核通过，且画面、音轨、"
            "完整切片边界、产品分类、叙事结构与重复性检查全部通过。"
        ),
        filename_snapshot=asset.filename,
        naming_evidence=[],
        naming_check={
            "version": "wis-auto-remix-v1",
            "status": "passed",
            "message": "自动混剪统一命名已通过。",
            "filename": asset.filename,
            "no_applicable_sources": False,
        },
        route_center="品牌营销部",
        route_group="WIS混剪工作台",
        assignment_mode="automatic_workstation",
        submitted_at=now,
        completed_at=now,
    )
    db.add(submission)
    db.flush()
    db.add(
        AssetReviewAiResult(
            submission_id=submission.id,
            status="passed",
            provider="wis_remix_output_review",
            rule_version="wis-auto-remix-v1",
            summary=(
                f"自动成片审核通过，规则得分 {int(float(assessment.get('score') or 0))}。"
            ),
            category_counts={"passed": len(assessment.get("checks") or [])},
            findings=[],
            segments=[],
            completed_at=now,
            updated_at=now,
        )
    )


@app.get("/api/workstation/access-authorize")
def workstation_access_authorize(
    request: Request,
    module: str = "material-workbench",
    _service: dict = Depends(require_workstation),
):
    """Use the material center's live OA policy as the workstation authority."""
    raw_token = str(request.headers.get("x-oa-token") or "").strip()
    if raw_token.lower().startswith("bearer "):
        raw_token = raw_token[7:].strip()
    if not raw_token:
        raise HTTPException(401, "OA 登录状态无效，请重新登录")

    user = user_from_token(raw_token)
    if str(user.get("status") or "").strip().lower() != "normal":
        raise HTTPException(403, "当前 OA 账号已冻结或状态异常，无法进入")
    if not user_allowed(user):
        raise HTTPException(403, "当前 OA 账号未获云管家登录权限")
    require_module_access(user, module)

    return {
        "allowed": True,
        "authority": "wis-video-center",
        "module": module,
        "access": module_access_for_user(user),
        "user": {
            field: user.get(field)
            for field in (
                "number",
                "userId",
                "id",
                "username",
                "userName",
                "realName",
                "name",
                "open_id",
                "groupName",
                "parentDept",
                "deptName",
                "department",
                "center",
                "status",
            )
            if user.get(field) is not None
        },
    }


class WorkstationIdentityAuthorize(BaseModel):
    """Verified user identity forwarded by a trusted WIS workstation service."""

    number: str = Field(default="", max_length=128)
    name: str = Field(default="", max_length=160)
    lark_user_id: str = Field(default="", max_length=160)


@app.post("/api/workstation/access-authorize-identity")
def workstation_access_authorize_identity(
    payload: WorkstationIdentityAuthorize,
    request: Request,
    module: str = "material-workbench",
    _service: dict = Depends(require_workstation),
):
    """Recheck live login and module grants for a workstation-verified identity.

    The workstation must first verify its own signed login ticket.  This endpoint
    then performs the authoritative database lookup on every check, so a login or
    module revocation takes effect without issuing a new workstation session.
    """
    number = str(payload.number or payload.lark_user_id or "").strip()
    name = str(payload.name or "").strip()
    if not number and not name:
        raise HTTPException(400, "工作台登录身份缺少工号和姓名")
    user = {
        "number": number,
        "userId": number,
        "id": number,
        "realName": name,
        "name": name,
        "open_id": str(payload.lark_user_id or "").strip(),
        "status": "normal",
    }
    if not user_allowed(user):
        raise HTTPException(403, "当前 OA 账号未获中枢登录权限")
    require_module_access(user, module)
    request.state.user = user
    return {
        "allowed": True,
        "authority": "wis-video-center",
        "module": module,
        "access": module_access_for_user(user),
        "user": {
            "number": number,
            "realName": name,
            "open_id": str(payload.lark_user_id or "").strip(),
            "status": "normal",
        },
    }


@app.get("/api/workstation/assets")
def workstation_assets(
    q: str = "",
    category: str = "",
    folder_name: str = "",
    library_type: str = "source",
    effective_only: bool = False,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=24, ge=1, le=100),
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    normalized_library_type = (library_type or "source").strip().lower()
    if normalized_library_type not in {"all", "source", "remix"}:
        raise HTTPException(422, "library_type 仅支持 all、source 或 remix")
    filters = [
        Asset.deleted_at.is_(None),
        Asset.purged_at.is_(None),
        Asset.asset_scope == "marketing_video",
        Asset.media_type == "video",
    ]
    if normalized_library_type != "all":
        filters.append(Asset.library_type == normalized_library_type)
    if effective_only:
        filters.append(
            Asset.id.in_(select(AssetEffectiveMark.asset_id))
        )
    if q.strip():
        term = q.strip()
        filters.append(
            or_(
                Asset.filename.contains(term),
                Asset.category.contains(term),
                Asset.asset_subtype.contains(term),
                Asset.folder_name.contains(term),
                asset_tags_contain(term),
            )
        )
    if category.strip():
        filters.append(Asset.category == canonical_product_category(category))
    if folder_name.strip():
        filters.append(Asset.folder_name == normalize_asset_folder_name(folder_name))
    total = db.scalar(select(func.count()).select_from(Asset).where(*filters)) or 0
    rows = db.scalars(
        select(Asset)
        .where(*filters)
        .order_by(Asset.modified_at.desc(), Asset.id.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    effective_marks = effective_marks_for_assets(db, [asset.id for asset in rows])
    return {
        "items": [
            _workstation_asset_payload(asset, effective_marks.get(asset.id))
            for asset in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "library_type": normalized_library_type,
        "source": source_name(),
        "source_updated_at": _source_updated_at(db),
    }


@app.get("/api/workstation/assets/{asset_id}")
def workstation_asset_detail(
    asset_id: int,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    asset = db.get(Asset, asset_id)
    if (
        not asset
        or asset.deleted_at is not None
        or asset.purged_at is not None
        or asset.asset_scope != "marketing_video"
        or asset.library_type not in {"source", "remix"}
        or asset.media_type != "video"
    ):
        raise HTTPException(404, "素材不存在、已回收或不是可用视频")
    return _workstation_asset_payload(asset, db.get(AssetEffectiveMark, asset.id))


@app.post("/api/workstation/returns/presign")
def workstation_return_presign(
    payload: WorkstationReturnCreate,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    if any(asset_id <= 0 for asset_id in payload.source_asset_ids):
        raise HTTPException(422, "来源素材 ID 无效")
    normalized_sha256 = payload.sha256.lower()
    with _workstation_return_lock:
        record = db.get(WorkstationReturn, payload.idempotency_key)
        initial_ticket = None
        if record:
            if record.sha256 != normalized_sha256 or record.file_size != payload.size:
                raise HTTPException(409, "相同幂等键对应的文件内容不一致")
        else:
            try:
                initial_ticket = oss_service.create_upload(
                    payload.filename,
                    payload.mime_type,
                    "WIS混剪工作台",
                    asset_scope="marketing_video",
                    category=payload.category,
                )
            except ValueError as error:
                raise HTTPException(400, str(error)) from error
            except Exception as error:
                raise HTTPException(502, "无法创建素材中心上传地址") from error
            record = WorkstationReturn(
                idempotency_key=payload.idempotency_key,
                object_key=initial_ticket["object_key"],
                filename=normalize_upload_filename(payload.filename),
                file_size=payload.size,
                sha256=normalized_sha256,
                mime_type=payload.mime_type,
                status="pending",
                provenance=payload.model_dump(),
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(record)
            db.commit()

    if record.status == "completed":
        return {
            **_workstation_return_payload(record, db),
            "upload_required": False,
            "upload_url": "",
            "headers": {},
            "expires_in": 0,
        }

    try:
        remote = oss_service.head_asset(record.object_key)
    except Exception:
        remote = None
    if remote and int(remote.get("size") or 0) != record.file_size:
        raise HTTPException(409, "幂等上传路径已有不同大小的文件，请联系管理员处理")
    upload_required = not remote
    ticket = initial_ticket
    if upload_required and not ticket:
        try:
            ticket = oss_service.presign_upload(record.object_key, record.mime_type)
        except Exception as error:
            raise HTTPException(502, "无法续签素材中心上传地址") from error
    return {
        **_workstation_return_payload(record, db),
        "upload_required": upload_required,
        "upload_url": ticket["upload_url"] if ticket else "",
        "headers": ticket["headers"] if ticket else {},
        "expires_in": ticket["expires_in"] if ticket else 0,
    }


@app.post("/api/workstation/returns/complete")
def workstation_return_complete(
    payload: WorkstationReturnComplete,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    record = db.get(WorkstationReturn, payload.idempotency_key)
    if not record:
        raise HTTPException(404, "未找到对应的回传任务")
    if record.status == "completed":
        return _workstation_return_payload(record, db)
    try:
        remote = oss_service.head_asset(record.object_key)
    except Exception as error:
        raise HTTPException(502, "素材中心无法核验已上传成片") from error
    if not remote or int(remote.get("size") or 0) != record.file_size:
        raise HTTPException(409, "成片尚未完整上传到 OSS")

    provenance = dict(record.provenance or {})
    maker_id = str(provenance.get("maker_id") or "SERVICE-WIS-REMIX")[:80]
    maker_name = str(provenance.get("maker_name") or "WIS混剪工作台")[:120]
    actor = {"number": maker_id, "realName": maker_name, "status": "normal"}
    reference_url = (
        f"wis-remix://render/{provenance.get('render_id', '')}/"
        f"variant/{provenance.get('variant_id', '')}"
    )[:2048]
    tags = list(
        dict.fromkeys(
            [
                *[str(tag).strip() for tag in provenance.get("tags") or [] if str(tag).strip()],
                "二创混剪",
                "WIS混剪工作台",
                "审核通过",
            ]
        )
    )[:30]
    upload = UploadComplete(
        object_key=record.object_key,
        filename=record.filename,
        category=str(provenance.get("category") or "待分类"),
        content_type=str(provenance.get("content_type") or "其他"),
        asset_scope="marketing_video",
        library_type="remix",
        asset_subtype=str(provenance.get("asset_subtype") or "AI混剪成片"),
        folder_name="",
        tags=tags,
        reference_url=reference_url,
    )
    asset_out = _complete_upload_for_user(
        upload,
        actor,
        db,
        source="wis_remix_workstation",
        extra_tags=("二创混剪", "WIS混剪工作台", "审核通过"),
        workstation_record=record,
    )
    asset = db.get(Asset, asset_out.id)
    record.asset_id = asset_out.id
    record.status = "completed"
    record.error_message = ""
    record.completed_at = datetime.utcnow()
    record.updated_at = record.completed_at
    if asset:
        _register_workstation_automatic_review(
            db,
            asset,
            provenance,
            actor_number=maker_id,
            actor_name=maker_name,
        )
        _audit_asset(
            db,
            asset,
            "workstation_return",
            actor_number=maker_id,
            actor_name=maker_name,
            detail=(
                f"混剪成片回传；render={provenance.get('render_id', '')}；"
                f"variant={provenance.get('variant_id', '')}；"
                f"framework={provenance.get('framework_id', '')}"
            )[:1000],
        )
    db.commit()
    return _workstation_return_payload(record, db)


@app.get("/api/workstation/returns/{idempotency_key}")
def workstation_return_status(
    idempotency_key: str,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    record = db.get(WorkstationReturn, idempotency_key)
    if not record:
        raise HTTPException(404, "未找到对应的回传任务")
    return _workstation_return_payload(record, db)


def _workstation_qianchuan_target(
    db: Session,
    *,
    advertiser_id: str,
    plan_id: str,
    plan_type: str,
) -> dict:
    status = qianchuan_service.validated_status(db)
    if not status["authorized"]:
        raise HTTPException(503, status["message"])
    try:
        account = next(
            (
                item
                for item in qianchuan_service.accounts(db)
                if str(item.get("id") or item.get("advertiser_id") or "") == advertiser_id
            ),
            None,
        )
        if not account:
            raise HTTPException(409, "指定千川账户不在当前真实授权账户目录中")
        plan = qianchuan_service.resolve_plan_target(
            db,
            advertiser_id=advertiser_id,
            plan_id=plan_id,
            plan_type=plan_type,
        )
    except QianchuanError as error:
        raise HTTPException(409, str(error)) from error
    if not plan.get("can_attach_video"):
        raise HTTPException(409, "所选千川计划当前不支持接口直接添加视频")
    return {
        "verified": True,
        "authorized": True,
        "account": {
            "id": advertiser_id,
            "name": str(account.get("name") or account.get("advertiser_name") or ""),
        },
        "plan": {
            "id": plan_id,
            "name": str(plan.get("name") or ""),
            "plan_type": str(plan.get("plan_type") or plan_type),
            "status": str(plan.get("status") or ""),
            "status_label": str(plan.get("status_label") or ""),
            "marketing_goal": str(plan.get("marketing_goal") or ""),
            "can_attach_video": bool(plan.get("can_attach_video")),
        },
        "verified_at": _utc_iso(datetime.utcnow()),
        "source": "qianchuan live account directory and plan resolution",
    }


@app.get("/api/workstation/qianchuan/targets/verify")
def workstation_qianchuan_target_verify(
    advertiser_id: str = Query(min_length=1, max_length=80, pattern=r"^\d+$"),
    plan_id: str = Query(min_length=1, max_length=80, pattern=r"^\d+$"),
    plan_type: str = Query(pattern="^(multiplication|full_domain|standard)$"),
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    return _workstation_qianchuan_target(
        db,
        advertiser_id=advertiser_id,
        plan_id=plan_id,
        plan_type=plan_type,
    )


@app.get("/api/workstation/qianchuan/accounts")
def workstation_qianchuan_accounts(
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    """Expose the same live authorized account catalog to the remix workstation."""

    status = qianchuan_service.validated_status(db)
    if not status["authorized"]:
        raise HTTPException(503, status["message"])
    try:
        items = qianchuan_service.accounts(db)
        return {
            "items": items,
            "total": len(items),
            "source": "qianchuan live authorized account directory",
            "source_read_at": _utc_iso(datetime.utcnow()),
        }
    except QianchuanError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/workstation/qianchuan/product-plan-map")
def workstation_qianchuan_product_plan_map(
    _service: dict = Depends(require_workstation),
):
    """Expose Cloud Manager's reviewed product-to-plan map to the workstation."""

    return product_plan_map_snapshot()


@app.get("/api/workstation/qianchuan/plans")
def workstation_qianchuan_plans(
    advertiser_id: str = Query(min_length=1, max_length=80, pattern=r"^\d+$"),
    q: str = Query(default="", max_length=100),
    refresh: bool = Query(default=False),
    cached_only: bool = Query(default=False),
    scope: str = Query(
        default="all",
        pattern="^(all|multiplication|full_domain|standard)$",
    ),
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    """Expose Cloud Manager's live plan picker catalog to the remix workstation."""

    status = qianchuan_service.validated_status(db)
    if not status["authorized"]:
        raise HTTPException(503, status["message"])
    try:
        accounts = qianchuan_service.accounts(db)
        if not any(
            str(item.get("id") or item.get("advertiser_id") or "") == advertiser_id
            for item in accounts
        ):
            raise HTTPException(409, "指定千川账户不在当前真实授权账户目录中")
        source_result = qianchuan_service.plans(
            db,
            advertiser_id,
            q.strip(),
            scope=scope,
            force_refresh=refresh,
            cache_only=cached_only,
        )
        result = {**source_result, "counts": dict(source_result.get("counts") or {})}
        full_evidence = _qianchuan_full_plan_evidence(db, advertiser_id)
        items: list[dict] = []
        for raw_item in result.get("items") or []:
            item = dict(raw_item)
            evidence = full_evidence.get(str(item.get("id") or ""))
            is_full = evidence is not None
            item.update(
                {
                    "is_full": is_full,
                    "capacity_status": "full" if is_full else "available",
                    "capacity_message": (
                        "计划已满：千川最近一次推送明确返回素材数量超过上限，请改选其他计划"
                        if is_full
                        else ""
                    ),
                    "capacity_checked_at": evidence["checked_at"] if evidence else "",
                    "can_attach_video": bool(item.get("can_attach_video")) and not is_full,
                }
            )
            items.append(item)
        result["items"] = items
        result.setdefault("counts", {})["full"] = sum(1 for item in items if item["is_full"])
        result["source"] = "qianchuan live plan directory"
        return result
    except QianchuanError as error:
        raise HTTPException(502, str(error)) from error


def _workstation_qianchuan_day_start_utc() -> datetime:
    local_now = _metrics_local_now()
    local_start = datetime.combine(local_now.date(), datetime.min.time(), tzinfo=_QIANCHUAN_DAILY_TIMEZONE)
    return local_start.astimezone(timezone.utc).replace(tzinfo=None)


@app.post("/api/workstation/qianchuan/push")
def workstation_qianchuan_push(
    payload: WorkstationQianchuanPushCreate,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    target = _workstation_qianchuan_target(
        db,
        advertiser_id=payload.target.advertiser_id,
        plan_id=payload.target.plan_id,
        plan_type=payload.target.plan_type,
    )
    if not payload.enabled:
        return {
            "status": "preflight_only",
            "message": "真实千川推送保持关闭；目标已完成实时核验。",
            "target": target,
        }
    if not payload.confirmed:
        raise HTTPException(409, "真实千川投放尚未明确确认")
    if payload.daily_spend_guard_yuan is None:
        raise HTTPException(409, "启用真实投放前必须配置每日消耗护栏")
    asset = db.get(Asset, payload.asset_id)
    if (
        not asset
        or asset.deleted_at is not None
        or asset.purged_at is not None
        or asset.media_type != "video"
        or asset.library_type != "remix"
    ):
        raise HTTPException(404, "自动混剪成片不存在、已回收或不是可投视频")
    category = canonical_product_category(payload.product_category)
    if asset.category != category:
        raise HTTPException(409, "成片产品分类与本次投放产品不一致")
    if asset.source != "wis_remix_workstation" or "自动混剪" not in asset.filename:
        raise HTTPException(409, "仅允许投放工作台回传且统一带有“自动混剪”的成片")
    return_record = db.scalar(
        select(WorkstationReturn).where(
            WorkstationReturn.asset_id == asset.id,
            WorkstationReturn.status == "completed",
        )
    )
    if not return_record or not _workstation_assessment_is_trusted(dict(return_record.provenance or {})):
        raise HTTPException(409, "成片缺少可信的自动审核通过证据")
    _require_assets_review_approved(db, [asset])

    existing = db.scalar(
        select(QianchuanDelivery)
        .where(QianchuanDelivery.idempotency_key == payload.idempotency_key)
        .order_by(QianchuanDelivery.created_at.desc())
        .limit(1)
    )
    if existing:
        return {
            "status": "already_queued",
            "task": delivery_out(existing, asset, _task_daily_summary(db, existing)),
            "target": target,
        }

    day_start = _workstation_qianchuan_day_start_utc()
    daily_tasks = list(
        db.scalars(
            select(QianchuanDelivery).where(
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.advertiser_id == payload.target.advertiser_id,
                QianchuanDelivery.plan_id == payload.target.plan_id,
                QianchuanDelivery.created_at >= day_start,
                QianchuanDelivery.idempotency_key.like("wis-remix:%"),
            )
        ).all()
    )
    if len(daily_tasks) >= payload.daily_material_limit:
        raise HTTPException(409, "已达到该计划今日自动混剪投放数量上限")
    verified_spend = 0.0
    unverified_success_count = 0
    for task in daily_tasks:
        summary = _task_daily_summary(db, task)
        if summary.get("link_verified"):
            verified_spend += float((summary.get("metrics") or {}).get("stat_cost") or 0)
        elif task.status == "success":
            unverified_success_count += 1
    if verified_spend >= payload.daily_spend_guard_yuan:
        raise HTTPException(409, "该计划今日已核验消耗达到配置护栏，自动投放已停止")

    now = datetime.utcnow()
    task = QianchuanDelivery(
        id=str(uuid4()),
        batch_id=str(uuid4()),
        asset_id=asset.id,
        created_by_number=payload.actor_number.strip(),
        created_by_name=payload.actor_name.strip(),
        advertiser_id=payload.target.advertiser_id,
        advertiser_name=str(target["account"]["name"] or payload.target.advertiser_name),
        plan_id=payload.target.plan_id,
        plan_name=str(target["plan"]["name"] or payload.target.plan_name),
        plan_type=str(target["plan"]["plan_type"]),
        idempotency_key=payload.idempotency_key,
        status="pending",
        message="自动混剪成片已通过安全门禁，等待投放到千川计划",
        created_at=now,
        updated_at=now,
    )
    db.add(task)
    db.commit()
    return {
        "status": "queued",
        "task": delivery_out(task, asset, _task_daily_summary(db, task)),
        "target": target,
        "guard": {
            "daily_material_limit": payload.daily_material_limit,
            "daily_material_count_before_push": len(daily_tasks),
            "daily_spend_guard_yuan": payload.daily_spend_guard_yuan,
            "verified_spend_yuan": round(verified_spend, 2),
            "spend_verification_status": "pending" if unverified_success_count else "verified",
            "unverified_success_count": unverified_success_count,
        },
    }


@app.get("/api/workstation/qianchuan/deliveries/{idempotency_key}")
def workstation_qianchuan_delivery(
    idempotency_key: str,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    task = db.scalar(
        select(QianchuanDelivery)
        .where(
            QianchuanDelivery.idempotency_key == idempotency_key,
            QianchuanDelivery.deleted_at.is_(None),
        )
        .order_by(QianchuanDelivery.created_at.desc())
        .limit(1)
    )
    if not task:
        raise HTTPException(404, "未找到对应的自动千川投放任务")
    asset = db.get(Asset, task.asset_id)
    return {
        "status": "found",
        "task": delivery_out(task, asset, _task_daily_summary(db, task)),
    }


@app.get("/api/workstation/returns/{idempotency_key}/feedback")
def workstation_return_feedback(
    idempotency_key: str,
    actor_number: str,
    render_id: str,
    variant_id: str,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    """Read stored receipts only; never submit/retry ads or synchronously fetch reports."""
    record = db.get(WorkstationReturn, idempotency_key)
    provenance = dict(record.provenance or {}) if record else {}
    if not record or not actor_number.strip() or any(
        str(provenance.get(key) or "") != expected
        for key, expected in (
            ("maker_id", actor_number.strip()),
            ("render_id", render_id),
            ("variant_id", variant_id),
        )
    ):
        raise HTTPException(404, "未找到当前制作人的对应回传成片")
    asset = db.get(Asset, record.asset_id) if record.asset_id else None
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        return {"asset_id": record.asset_id, "asset_available": False, "items": [], "complete": True}
    tasks = list(db.scalars(
        select(QianchuanDelivery).where(
            QianchuanDelivery.asset_id == asset.id,
            QianchuanDelivery.deleted_at.is_(None),
        ).order_by(QianchuanDelivery.updated_at.desc(), QianchuanDelivery.id).limit(101)
    ).all())
    return {
        "asset_id": asset.id,
        "asset_available": True,
        "items": [delivery_out(task, asset, _task_daily_summary(db, task), can_manage=False) for task in tasks[:100]],
        "complete": len(tasks) <= 100,
        "checked_at": datetime.utcnow().isoformat() + "Z",
    }


@app.post("/api/workstation/qianchuan/deliveries/{idempotency_key}/retry")
def workstation_qianchuan_delivery_retry(
    idempotency_key: str,
    db: Session = Depends(get_db),
    _service: dict = Depends(require_workstation),
):
    """Resume only the failed stage for a workstation-owned delivery."""
    with _qianchuan_submission_lock:
        task = db.scalar(
            select(QianchuanDelivery)
            .where(
                QianchuanDelivery.idempotency_key == idempotency_key,
                QianchuanDelivery.deleted_at.is_(None),
            )
            .order_by(QianchuanDelivery.created_at.desc())
            .limit(1)
        )
        if not task or not str(task.idempotency_key or "").startswith(
            "wis-remix:"
        ):
            raise HTTPException(404, "未找到对应的自动千川投放任务")
        if task.status not in {"failed", "partial"}:
            raise HTTPException(409, "只有失败或部分完成的自动投放任务可以续跑")
        if task.status == "partial" and task.plan_type == "standard":
            raise HTTPException(409, "普通计划不支持接口自动续绑")
        asset = db.get(Asset, task.asset_id)
        if (
            not asset
            or asset.deleted_at is not None
            or asset.purged_at is not None
        ):
            raise HTTPException(404, "原素材已不存在，无法续跑")
        _require_assets_review_approved(db, [asset])
        _prepare_qianchuan_retry(task)
        db.commit()
        db.refresh(task)
    return {
        "status": "queued",
        "task": delivery_out(task, asset, _task_daily_summary(db, task)),
    }


def require_jianying_device(request: Request, db: Session = Depends(get_db)) -> JianyingDevice:
    return device_from_request(request, db)


@app.post("/api/jianying/pairings")
def jianying_pairing_create(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    return create_pairing(db, user, external_app_url(request))


@app.get("/api/jianying/pairings/{pairing_id}")
def jianying_pairing_status(
    pairing_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    pairing = db.get(JianyingPairing, pairing_id)
    if not pairing or pairing.owner_number != user_number(user):
        raise HTTPException(404, "配对记录不存在")
    status = pairing.status
    if status == "pending" and pairing.expires_at <= datetime.utcnow():
        status = "expired"
        pairing.status = status
        pairing.updated_at = datetime.utcnow()
        db.commit()
    return {
        "id": pairing.id,
        "status": status,
        "device_id": pairing.device_id,
        "expires_at": pairing.expires_at.isoformat(timespec="seconds") + "Z",
        "claimed_at": pairing.claimed_at.isoformat(timespec="seconds") + "Z" if pairing.claimed_at else None,
    }


@app.post("/api/jianying/pairings/claim")
def jianying_pairing_claim(payload: JianyingPairingClaim, db: Session = Depends(get_db)):
    return claim_pairing(db, payload.code, payload.device_name)


@app.get("/api/jianying/devices")
def jianying_devices(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.scalars(
        select(JianyingDevice).where(
            JianyingDevice.owner_number == user_number(user),
            JianyingDevice.active.is_(True),
        ).order_by(JianyingDevice.updated_at.desc())
    ).all()
    return {"items": [device_out(row) for row in rows], "total": len(rows)}


@app.delete("/api/jianying/devices/{device_id}")
def jianying_device_revoke(
    device_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    device = db.get(JianyingDevice, device_id)
    if not device or device.owner_number != user_number(user):
        raise HTTPException(404, "剪映助手设备不存在")
    device.active = False
    device.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "device_id": device.id}


@app.get("/api/jianying/device/status")
def jianying_device_status(device: JianyingDevice = Depends(require_jianying_device)):
    return {
        "configured": True,
        "device": device_out(device),
        "owner": {"number": device.owner_number, "name": device.owner_name},
        "capabilities": {
            "watch_export_folder": True,
            "original_file_upload": True,
            "favorite_import": True,
            "jianying_deep_link": True,
        },
    }


@app.post("/api/jianying/device/uploads/presign", response_model=UploadTicket)
def jianying_device_upload_create(
    payload: JianyingDeviceUploadCreate,
    device: JianyingDevice = Depends(require_jianying_device),
    db: Session = Depends(get_db),
):
    try:
        ticket = oss_service.create_upload(
            payload.filename,
            payload.content_type,
            device.owner_name or device.owner_number,
            asset_scope="marketing_video",
            category="待分类",
        )
        return remember_direct_ticket(
            db, ticket, filename=normalize_upload_filename(payload.filename), size=payload.size,
            content_type=payload.content_type, owner_number=device.owner_number, owner_name=device.owner_name,
            asset_scope="marketing_video", category="",
        )
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    except Exception as error:
        raise HTTPException(502, "无法创建剪映素材上传地址") from error


@app.post("/api/jianying/device/uploads/complete", response_model=AssetOut)
def jianying_device_upload_complete(
    payload: JianyingDeviceUploadComplete,
    db: Session = Depends(get_db),
    device: JianyingDevice = Depends(require_jianying_device),
):
    complete_payload = UploadComplete(
        object_key=payload.object_key,
        filename=payload.filename,
        category=payload.category,
        content_type=payload.content_type,
        asset_scope="marketing_video",
        library_type="remix",
        asset_subtype=payload.asset_subtype,
        tags=payload.tags,
    )
    return _complete_upload_for_user(
        complete_payload,
        device_user(device),
        db,
        source="jianying_export",
        extra_tags=("剪映导出",),
    )


@app.post("/api/assets/{asset_id}/jianying-import")
def jianying_import_create(
    asset_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在")
    if asset.media_type != "video":
        raise HTTPException(400, "剪映仅支持导入视频素材")
    owner_number = user_number(user)
    if not db.get(AssetFavorite, (owner_number, asset.id)):
        raise HTTPException(403, "请先收藏该视频，再导入剪映")
    now = datetime.utcnow()
    ticket = JianyingImportTicket(
        id=str(uuid4()),
        owner_number=owner_number,
        asset_id=asset.id,
        status="waiting",
        progress=0,
        message="等待桌面助手接收",
        downloaded_bytes=0,
        total_bytes=asset.size,
        speed_bps=0,
        eta_seconds=0,
        helper_version="",
        cache_hit=False,
        expires_at=now + timedelta(minutes=10),
        created_at=now,
        updated_at=now,
    )
    db.add(ticket)
    db.commit()
    api_base = external_app_url(request).rstrip("/") + "/api"
    from urllib.parse import urlencode

    scheme_url = "wis-jianying://import?" + urlencode({"ticket": ticket.id, "server": api_base})
    return {
        "ticket_id": ticket.id,
        "asset_id": asset.id,
        "filename": asset.filename,
        "status": ticket.status,
        "progress": ticket.progress,
        "message": ticket.message,
        "downloaded_bytes": ticket.downloaded_bytes,
        "total_bytes": ticket.total_bytes,
        "speed_bps": ticket.speed_bps,
        "eta_seconds": ticket.eta_seconds,
        "helper_version": ticket.helper_version,
        "cache_hit": ticket.cache_hit,
        "updated_at": ticket.updated_at.isoformat(timespec="seconds") + "Z",
        "expires_at": ticket.expires_at.isoformat(timespec="seconds") + "Z",
        "scheme_url": scheme_url,
    }


def jianying_import_out(ticket: JianyingImportTicket, asset: Asset | None = None) -> dict:
    expired = ticket.expires_at <= datetime.utcnow() and ticket.status not in {"completed", "failed"}
    return {
        "ticket_id": ticket.id,
        "asset_id": ticket.asset_id,
        "filename": asset.filename if asset else "",
        "status": "expired" if expired else ticket.status,
        "progress": ticket.progress,
        "message": "导入任务已过期，请重新点击导入" if expired else ticket.message,
        "downloaded_bytes": ticket.downloaded_bytes,
        "total_bytes": ticket.total_bytes,
        "speed_bps": ticket.speed_bps,
        "eta_seconds": ticket.eta_seconds,
        "helper_version": ticket.helper_version,
        "cache_hit": ticket.cache_hit,
        "created_at": ticket.created_at.isoformat(timespec="seconds") + "Z",
        "updated_at": (ticket.updated_at or ticket.created_at).isoformat(timespec="seconds") + "Z",
        "completed_at": ticket.completed_at.isoformat(timespec="seconds") + "Z" if ticket.completed_at else None,
        "expires_at": ticket.expires_at.isoformat(timespec="seconds") + "Z",
    }


@app.get("/api/jianying/imports/{ticket_id}")
def jianying_import_status(
    ticket_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    ticket = db.get(JianyingImportTicket, ticket_id)
    if not ticket or ticket.owner_number != user_number(user):
        raise HTTPException(404, "导入任务不存在")
    return jianying_import_out(ticket, db.get(Asset, ticket.asset_id))


@app.get("/api/jianying/device/imports/{ticket_id}")
def jianying_import_resolve(
    ticket_id: str,
    db: Session = Depends(get_db),
    device: JianyingDevice = Depends(require_jianying_device),
):
    ticket = db.get(JianyingImportTicket, ticket_id)
    if not ticket or ticket.owner_number != device.owner_number:
        raise HTTPException(404, "导入任务不存在")
    if ticket.expires_at <= datetime.utcnow():
        raise HTTPException(410, "导入任务已过期，请在素材库重新点击导入")
    asset = db.get(Asset, ticket.asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在或已进入回收站")
    if not db.get(AssetFavorite, (device.owner_number, asset.id)):
        raise HTTPException(403, "该视频已不在你的收藏中")
    now = datetime.utcnow()
    ticket.delivered_at = now
    if ticket.status == "waiting":
        ticket.status = "claimed"
        ticket.progress = max(ticket.progress, 5)
        ticket.message = "桌面助手已接收，准备下载原视频"
    ticket.updated_at = now
    db.commit()
    return {
        "asset_id": asset.id,
        "filename": Path(asset.filename).name,
        "size": asset.size,
        "content_type": "video/mp4" if Path(asset.filename).suffix.lower() == ".mp4" else "application/octet-stream",
        "download_url": oss_service.url_for(asset.object_key, download=True, expires_in=3600),
        "expires_in": 3600,
    }


@app.post("/api/jianying/device/imports/{ticket_id}/progress")
def jianying_import_progress(
    ticket_id: str,
    payload: JianyingImportProgress,
    db: Session = Depends(get_db),
    device: JianyingDevice = Depends(require_jianying_device),
):
    ticket = db.get(JianyingImportTicket, ticket_id)
    if not ticket or ticket.owner_number != device.owner_number:
        raise HTTPException(404, "导入任务不存在")
    if ticket.expires_at <= datetime.utcnow() and ticket.status not in {"completed", "failed"}:
        raise HTTPException(410, "导入任务已过期，请在素材库重新点击导入")
    if ticket.status in {"completed", "failed"} and payload.status not in {"completed", "failed"}:
        return jianying_import_out(ticket, db.get(Asset, ticket.asset_id))
    now = datetime.utcnow()
    ticket.status = payload.status
    ticket.progress = 100 if payload.status == "completed" else max(ticket.progress, payload.progress)
    ticket.message = payload.message.strip() or ticket.message
    ticket.downloaded_bytes = payload.downloaded_bytes
    ticket.total_bytes = payload.total_bytes or ticket.total_bytes
    ticket.speed_bps = payload.speed_bps
    ticket.eta_seconds = payload.eta_seconds
    ticket.helper_version = payload.helper_version.strip() or ticket.helper_version
    ticket.cache_hit = payload.cache_hit
    ticket.updated_at = now
    if payload.status == "completed":
        ticket.completed_at = now
    db.commit()
    return jianying_import_out(ticket, db.get(Asset, ticket.asset_id))


@app.get("/api/trash", response_model=AssetPage)
def list_trash(
    q: str = "",
    sort: str = Query(default="deleted", pattern="^(deleted|name|size)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=60, ge=1, le=100),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    filters = [Asset.deleted_at.is_not(None), Asset.purged_at.is_(None)]
    if not (is_asset_admin(user) or is_asset_delete_manager(user)):
        filters.append(Asset.uploaded_by_number == user_number(user))
    if q.strip():
        term = q.strip()
        filters.append(
            or_(
                Asset.filename.contains(term),
                Asset.category.contains(term),
                Asset.content_type.contains(term),
                Asset.deleted_by_name.contains(term),
                asset_tags_contain(term),
            )
        )
    order = {
        "deleted": Asset.deleted_at.desc(),
        "name": Asset.filename.asc(),
        "size": Asset.size.desc(),
    }[sort]
    total = db.scalar(select(func.count()).select_from(Asset).where(*filters)) or 0
    rows = db.scalars(
        select(Asset).where(*filters).order_by(order).offset((page - 1) * page_size).limit(page_size)
    ).all()
    favorites = favorite_asset_ids(db, user, [row.id for row in rows])
    favorite_counts = favorite_counts_for_assets(db, [row.id for row in rows])
    can_purge = is_asset_admin(user)
    return AssetPage(
        items=[
            to_asset_out(
                row,
                row.id in favorites,
                favorite_counts.get(row.id, 0),
                can_purge=can_purge,
                can_manage=can_manage_asset(user, row),
                can_delete=can_delete_asset(user, row),
            )
            for row in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
        source=source_name(),
        source_updated_at=_source_updated_at(db),
    )


@app.post("/api/assets/batch-delete")
def batch_delete_assets(
    payload: BatchDeletePayload,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset_ids = list(dict.fromkeys(payload.asset_ids))
    if any(asset_id <= 0 for asset_id in asset_ids):
        raise HTTPException(400, "素材 ID 无效")
    assets = db.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    if len(assets) != len(asset_ids):
        raise HTTPException(404, "部分素材不存在，请刷新列表后重试")
    for asset in assets:
        require_asset_deleter(user, asset)
        if asset.deleted_at is not None or asset.purged_at is not None:
            raise HTTPException(409, f"{asset.filename} 已在回收站或已永久删除")
    for asset in assets:
        soft_delete_asset(db, asset, user)
    db.commit()
    _invalidate_catalog_cache()
    return {"ok": True, "count": len(assets), "asset_ids": asset_ids}


def _batch_managed_assets(db: Session, user: dict, asset_ids: list[int]) -> tuple[list[int], list[Asset]]:
    unique_ids = list(dict.fromkeys(asset_ids))
    if any(asset_id <= 0 for asset_id in unique_ids):
        raise HTTPException(400, "素材 ID 无效")
    assets = db.scalars(select(Asset).where(Asset.id.in_(unique_ids))).all()
    if len(assets) != len(unique_ids):
        raise HTTPException(404, "部分素材不存在，请刷新列表后重试")
    by_id = {asset.id: asset for asset in assets}
    ordered = [by_id[asset_id] for asset_id in unique_ids]
    for asset in ordered:
        require_asset_manager(user, asset)
        if asset.deleted_at is not None or asset.purged_at is not None:
            raise HTTPException(409, f"{asset.filename} 已在回收站或已永久删除")
    return unique_ids, ordered


@app.post("/api/assets/batch-tags")
def batch_update_asset_tags(
    payload: BatchTagsPayload,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset_ids, assets = _batch_managed_assets(db, user, payload.asset_ids)
    requested = list(dict.fromkeys(tag.strip()[:40] for tag in payload.tags if tag.strip()))[:30]
    if not requested:
        raise HTTPException(422, "请至少输入一个有效标签")
    for asset in assets:
        current = list(dict.fromkeys(asset.tags or []))
        if payload.mode == "replace":
            updated = requested
        elif payload.mode == "remove":
            updated = [tag for tag in current if tag not in requested]
        else:
            updated = list(dict.fromkeys([*current, *requested]))[:30]
        asset.tags = updated
        _audit_asset(
            db,
            asset,
            "batch_tags",
            actor_number=user_number(user),
            actor_name=user_name(user),
            detail=f"批量{payload.mode}标签：{'、'.join(requested)}"[:1000],
        )
    db.commit()
    _invalidate_catalog_cache()
    return {"ok": True, "count": len(assets), "asset_ids": asset_ids, "mode": payload.mode, "tags": requested}


@app.post("/api/assets/batch-folder")
def batch_update_asset_folder(
    payload: BatchFolderPayload,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset_ids, assets = _batch_managed_assets(db, user, payload.asset_ids)
    folder_name = normalize_asset_folder_name(payload.folder_name)
    for asset in assets:
        if asset.asset_scope != "marketing_video" or asset.library_type != "source" or asset.media_type != "video":
            raise HTTPException(422, f"{asset.filename} 不是视频素材，不能放入视频素材二级文件夹")
    for asset in assets:
        asset.folder_name = folder_name
        _audit_asset(
            db,
            asset,
            "batch_folder",
            actor_number=user_number(user),
            actor_name=user_name(user),
            detail=f"批量整理到文件夹：{folder_name or '未归档'}"[:1000],
        )
    db.commit()
    _invalidate_catalog_cache()
    return {"ok": True, "count": len(assets), "asset_ids": asset_ids, "folder_name": folder_name}


@app.delete("/api/assets/{asset_id}", response_model=AssetOut)
def delete_asset(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    require_asset_deleter(user, asset)
    soft_delete_asset(db, asset, user)
    db.commit()
    _invalidate_catalog_cache()
    db.refresh(asset)
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        can_purge=is_asset_admin(user),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
    )


@app.post("/api/trash/{asset_id}/restore", response_model=AssetOut)
def restore_asset(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    require_asset_deleter(user, asset)
    restore_deleted_asset(db, asset, user)
    db.commit()
    _invalidate_catalog_cache()
    db.refresh(asset)
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
    )


@app.delete("/api/trash/{asset_id}")
def purge_asset(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset = db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(404, "素材不存在")
    permanently_delete_asset(db, asset, user)
    db.commit()
    _invalidate_catalog_cache()
    return {"ok": True, "asset_id": asset_id}


@app.post("/api/trash/cleanup")
def cleanup_trash(user: dict = Depends(require_user)):
    if not is_asset_admin(user):
        raise HTTPException(403, "仅部门负责人或素材管理员可运行清理")
    return run_trash_cleanup_if_due(force=True)


@app.post("/api/trash/purge-all")
def purge_all_trash(
    payload: TrashClearPayload,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if not is_super_admin(user):
        raise HTTPException(403, "仅超级管理员可一键永久清空回收站")
    if payload.confirm_text.strip() != "永久清空回收站":
        raise HTTPException(422, "请输入“永久清空回收站”完成二次确认")
    assets = db.scalars(
        select(Asset)
        .where(Asset.deleted_at.is_not(None), Asset.purged_at.is_(None))
        .order_by(Asset.deleted_at.asc(), Asset.id.asc())
    ).all()
    purged_ids: list[int] = []
    failed: list[dict[str, object]] = []
    for asset in assets:
        try:
            if permanently_delete_asset(db, asset, user):
                purged_ids.append(asset.id)
        except HTTPException as error:
            failed.append({"asset_id": asset.id, "filename": asset.filename, "detail": str(error.detail)})
    db.commit()
    _invalidate_catalog_cache()
    return {
        "ok": not failed,
        "checked": len(assets),
        "purged": len(purged_ids),
        "purged_ids": purged_ids,
        "failed": failed,
    }


@app.get("/api/assets/{asset_id}", response_model=AssetOut)
def get_asset(
    asset_id: int,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
    include_performance: bool = True,
):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在")
    gmv_summary = _asset_gmv_summary(db, [asset.id]) if include_performance else {}
    platform_gmv = _asset_platform_gmv_summary(db, [asset.id], gmv_summary) if include_performance else {}
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        gmv_summary=gmv_summary.get(asset.id),
        platform_gmv=platform_gmv.get(asset.id),
        effective_mark=effective_marks_for_assets(db, [asset.id]).get(asset.id),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
        review_summary=_review_latest_summaries(db, [asset.id]).get(asset.id),
    )


@app.patch("/api/assets/{asset_id}", response_model=AssetOut)
def update_asset(asset_id: int, payload: AssetUpdate, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在")
    require_asset_manager(user, asset)
    updates = payload.model_dump(exclude_unset=True)
    original_filename = asset.filename
    if "filename" in updates:
        proposed = normalize_upload_filename(updates.get("filename") or "")
        original_suffix = Path(asset.filename).suffix.lower()
        proposed_suffix = Path(proposed).suffix.lower()
        if proposed_suffix and proposed_suffix != original_suffix:
            raise HTTPException(422, "修改名称不能改变原文件格式")
        stem = Path(proposed).stem if proposed_suffix else proposed
        stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "-", stem).strip(" .-")
        if not stem:
            raise HTTPException(422, "素材名称不能为空")
        updates["filename"] = f"{stem[:480]}{original_suffix}"
    if "reference_url" in updates:
        updates["reference_url"] = _normalized_reference_url(updates.get("reference_url") or "")
    if "category" in updates:
        updates["category"] = canonical_product_category(updates.get("category"))
    if "folder_name" in updates:
        updates["folder_name"] = normalize_asset_folder_name(updates.get("folder_name") or "")
    if updates.get("library_type") == "remix":
        updates["folder_name"] = ""
    if "reference_video_key" in updates:
        key, name = _validated_reference_video(
            updates.get("reference_video_key") or "", updates.get("reference_video_name") or ""
        )
        updates["reference_video_key"] = key
        updates["reference_video_name"] = name
    if "material_description" in updates:
        updates["material_description"] = str(updates.get("material_description") or "").strip()[:10000]
    if "performance_screenshots" in updates:
        updates["performance_screenshots"] = _validated_performance_screenshots(
            updates.get("performance_screenshots") or []
        )
    for field, value in updates.items():
        if field == "tags" and value is not None:
            value = list(dict.fromkeys(tag.strip() for tag in value if tag.strip()))[:30]
        setattr(asset, field, value)
    if asset.filename != original_filename and _review_workflow_config(db).naming_enabled:
        latest_review = _review_latest_submission(db, asset.id)
        if latest_review:
            latest_review.status = "rejected"
            latest_review.completed_at = datetime.utcnow()
            previous_check = dict(latest_review.naming_check or {})
            latest_review.naming_check = {
                **previous_check,
                "status": "stale",
                "filename": asset.filename,
                "message": (
                    f"素材名称已从“{original_filename}”修改为“{asset.filename}”，"
                    "原命名与审核结果已失效，请重新提审。"
                ),
            }
    db.commit()
    _invalidate_catalog_cache()
    db.refresh(asset)
    gmv_summary = _asset_gmv_summary(db, [asset.id])
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        gmv_summary=gmv_summary.get(asset.id),
        platform_gmv=_asset_platform_gmv_summary(db, [asset.id], gmv_summary).get(asset.id),
        effective_mark=effective_marks_for_assets(db, [asset.id]).get(asset.id),
        can_manage=can_manage_asset(user, asset),
    )


@app.post("/api/assets/{asset_id}/favorite", response_model=AssetOut)
def toggle_favorite(asset_id: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在")
    key = user_number(user)
    favorite = db.get(AssetFavorite, (key, asset_id))
    if favorite:
        db.delete(favorite)
        is_favorite = False
    else:
        db.add(AssetFavorite(user_number=key, asset_id=asset_id))
        is_favorite = True
    db.commit()
    gmv_summary = _asset_gmv_summary(db, [asset.id])
    return to_asset_out(
        asset,
        is_favorite,
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
        gmv_summary=gmv_summary.get(asset.id),
        platform_gmv=_asset_platform_gmv_summary(db, [asset.id], gmv_summary).get(asset.id),
        effective_mark=effective_marks_for_assets(db, [asset.id]).get(asset.id),
    )


@app.put("/api/assets/{asset_id}/effective", response_model=AssetOut)
def set_asset_effective(
    asset_id: int,
    payload: AssetEffectiveUpdate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_effective_asset_marker(user)
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在")
    if asset.media_type != "video" or asset.asset_scope != "marketing_video":
        raise HTTPException(422, "只有视频素材可以标记为有效一创素材")
    mark = db.get(AssetEffectiveMark, asset_id)
    changed = False
    if payload.effective and not mark:
        mark = AssetEffectiveMark(
            asset_id=asset.id,
            marked_by_number=user_number(user),
            marked_by_name=user_name(user),
        )
        db.add(mark)
        changed = True
    elif not payload.effective and mark:
        db.delete(mark)
        changed = True
    if changed:
        _audit_asset(
            db,
            asset,
            "mark_effective" if payload.effective else "unmark_effective",
            actor_number=user_number(user),
            actor_name=user_name(user),
            detail="人工确认进入有效一创素材" if payload.effective else "人工取消有效一创素材标记",
        )
    db.commit()
    _invalidate_catalog_cache()
    mark = db.get(AssetEffectiveMark, asset_id)
    gmv_summary = _asset_gmv_summary(db, [asset.id])
    return to_asset_out(
        asset,
        asset.id in favorite_asset_ids(db, user, [asset.id]),
        favorite_counts_for_assets(db, [asset.id]).get(asset.id, 0),
        can_manage=can_manage_asset(user, asset),
        can_delete=can_delete_asset(user, asset),
        gmv_summary=gmv_summary.get(asset.id),
        platform_gmv=_asset_platform_gmv_summary(db, [asset.id], gmv_summary).get(asset.id),
        effective_mark=mark,
    )


def _remix_worker_request(
    path_name: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_seconds: int = 30,
) -> dict:
    if (
        not settings.remix_worker_base_url
        or len(settings.remix_worker_token) < 32
    ):
        raise HTTPException(503, "二创混剪工作台联动尚未完成服务器配置")
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
    request = urllib.request.Request(
        f"{settings.remix_worker_base_url}/{path_name.lstrip('/')}",
        data=body,
        method=method,
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "X-WIS-Workstation-Token": settings.remix_worker_token,
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            raw = response.read(2 * 1024 * 1024)
    except urllib.error.HTTPError as error:
        raw = error.read(64 * 1024)
        try:
            failure = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            failure = {}
        message = str(failure.get("message") or failure.get("detail") or "").strip()
        raise HTTPException(error.code, message or "二创混剪工作台拒绝了本次联动") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise HTTPException(503, "二创混剪工作台暂时不可用，请稍后重试") from error
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HTTPException(502, "二创混剪工作台返回格式异常") from error
    if not isinstance(result, dict):
        raise HTTPException(502, "二创混剪工作台返回格式异常")
    return result


def _dispatch_effective_clip_import(asset_id: int, actor_number: str, actor_name: str) -> None:
    try:
        _remix_worker_request(
            "/api/integrations/material-center/effective-clips/import",
            method="POST",
            payload={
                "assetId": asset_id,
                "actorNumber": actor_number,
                "actorName": actor_name,
            },
            timeout_seconds=15 * 60,
        )
    except HTTPException:
        logger.exception("Effective material clip import failed for asset %s", asset_id)


def _effective_clip_import_asset(db: Session, asset_id: int) -> Asset:
    asset = db.get(Asset, asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "素材不存在或已进入回收站")
    if asset.media_type != "video" or asset.asset_scope != "marketing_video":
        raise HTTPException(422, "只有营销视频可以加入切片库")
    if asset.library_type != "source":
        raise HTTPException(422, "只有原始有效一创素材可以直接加入切片库")
    if not db.get(AssetEffectiveMark, asset.id):
        raise HTTPException(409, "该素材不是真实标记的有效一创素材")
    category = canonical_product_category(asset.category)
    if category in {"", "待分类", "其他 WIS 素材"}:
        raise HTTPException(409, "请先分类为“通用”或具体产品，再加入切片库")
    return asset


@app.post("/api/assets/{asset_id}/clip-library/import", status_code=202)
def import_effective_asset_to_clip_library(
    asset_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    asset = _effective_clip_import_asset(db, asset_id)
    actor_number = user_number(user)
    actor_name = user_name(user)
    if not settings.remix_worker_base_url or len(settings.remix_worker_token) < 32:
        raise HTTPException(503, "二创混剪工作台联动尚未完成服务器配置")
    _audit_asset(
        db,
        asset,
        "queue_effective_clip_import",
        actor_number=actor_number,
        actor_name=actor_name,
        detail=f"有效一创免二次内容审核加入切片库；产品分类：{asset.category}",
    )
    db.commit()
    background_tasks.add_task(
        _dispatch_effective_clip_import,
        asset.id,
        actor_number,
        actor_name,
    )
    return {
        "asset_id": asset.id,
        "status": "queued",
        "message": "已进入切片识别与技术校验队列；内容审核继承云管家有效一创结论。",
    }


@app.get("/api/assets/{asset_id}/clip-library/status")
def effective_asset_clip_library_status(
    asset_id: int,
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    _effective_clip_import_asset(db, asset_id)
    return _remix_worker_request(
        f"/api/integrations/material-center/effective-clips/status?asset_id={asset_id}",
        timeout_seconds=15,
    )


def stats(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    shared = _catalog_cache_get("stats")
    if shared is None:
        week_ago = datetime.utcnow() - timedelta(days=7)

        def scalar(*conditions):
            query = select(func.count()).select_from(Asset).where(
                Asset.deleted_at.is_(None), Asset.purged_at.is_(None),
                        _recovery_asset_allowed(db, Asset.id, "visibility")
            )
            return db.scalar(query.where(*conditions) if conditions else query) or 0

        shared = _catalog_cache_set(
            "stats",
            {
                "total": scalar(),
                "oss_total": db.scalar(
                    select(func.count()).select_from(Asset).where(Asset.purged_at.is_(None))
                ) or 0,
                "videos": scalar(Asset.media_type == "video"),
                "images": scalar(Asset.media_type == "image"),
                "source_materials": scalar(Asset.asset_scope == "marketing_video", Asset.library_type == "source"),
                "remix_outputs": scalar(Asset.asset_scope == "marketing_video", Asset.library_type == "remix"),
                "product_images": scalar(Asset.asset_scope == "product_image"),
                "trash": db.scalar(
                    select(func.count()).select_from(Asset).where(
                        Asset.deleted_at.is_not(None), Asset.purged_at.is_(None)
                    )
                ) or 0,
                "new_this_week": scalar(Asset.modified_at >= week_ago),
                "storage_bytes": db.scalar(
                    select(func.coalesce(func.sum(Asset.size), 0)).where(
                        Asset.deleted_at.is_(None), Asset.purged_at.is_(None),
                        _recovery_asset_allowed(db, Asset.id, "visibility")
                    )
                ) or 0,
                "indexed_records": int(catalog_service.meta.get("record_count", 0) or 0),
                "source": source_name(),
                "source_updated_at": _source_updated_at(db),
                "hit_materials": _asset_hit_material_count(db),
                "effective_materials": scalar(
                    Asset.media_type == "video",
                    Asset.asset_scope == "marketing_video",
                    Asset.id.in_(select(AssetEffectiveMark.asset_id)),
                ),
            },
        )

    favorites = db.scalar(
            select(func.count())
            .select_from(AssetFavorite)
            .join(Asset, Asset.id == AssetFavorite.asset_id)
            .where(
                AssetFavorite.user_number == user_number(user),
                Asset.deleted_at.is_(None),
                Asset.purged_at.is_(None),
            )
        ) or 0
    trash = shared["trash"] if is_asset_admin(user) else (
        db.scalar(
            select(func.count()).select_from(Asset).where(
                Asset.deleted_at.is_not(None),
                Asset.purged_at.is_(None),
                Asset.uploaded_by_number == user_number(user),
            )
        ) or 0
    )
    return StatsOut(**{**shared, "favorites": favorites, "trash": trash})


def _catalog_read_session(bind, callback):
    # The dependency only supplies the configured bind. The worker owns the
    # actual session, including when its HTTP requester disconnects mid-read.
    with Session(bind=bind, autoflush=False) as db:
        return callback(db)


@app.get("/api/facets")
async def facets_route(
    library_type: str = Query(default="", pattern="^(|source|remix)$"),
    asset_scope: str = Query(default="", pattern="^(|marketing_video|product_image)$"),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
    categories_only: bool = False,
):
    # Category-only lookup is cheap and must not wait behind full facet rebuilds.
    group = "category-only" if categories_only else "facets"
    bind = db.get_bind()
    return await _catalog_read_queue.run(group, lambda: _catalog_read_session(bind,
        lambda db: facets(library_type, asset_scope, db, _user, categories_only)
    ))


@app.get("/api/facets/bundle")
async def facets_bundle_route(db: Session = Depends(get_db), _user: dict = Depends(require_user)):
    bind = db.get_bind()
    return await _catalog_read_queue.run("facets", lambda: _catalog_read_session(bind,
        lambda db: facets_bundle(db, _user)
    ))


@app.get("/api/stats", response_model=StatsOut)
async def stats_route(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    bind = db.get_bind()
    return await _catalog_read_queue.run("stats", lambda: _catalog_read_session(bind,
        lambda db: stats(db, user)
    ))


_QIANCHUAN_WORKER_LIMIT = settings.qianchuan_worker_limit
_qianchuan_job_slots = BoundedSemaphore(_QIANCHUAN_WORKER_LIMIT)
_qianchuan_submission_lock = Lock()
_qianchuan_claim_pattern = ("fresh", "fresh", "fresh", "retry")
_qianchuan_claim_cursor = 0
_qianchuan_last_owner = ""
_qianchuan_metrics_slot = BoundedSemaphore(1)
_qianchuan_metrics_schedule_lock = Lock()
_QIANCHUAN_METRICS_AUTO_LAST_RUN_KEY = "qianchuan.metrics.auto.last_run_at"
_QIANCHUAN_METRICS_AUTO_STATE_KEY = "qianchuan.metrics.auto.state"
_QIANCHUAN_METRICS_ELIGIBLE_STATUSES = {"success", "partial", "uploaded"}
_QIANCHUAN_DAILY_LAST_DATE_KEY = "qianchuan.metrics.daily.last_completed_date"
_QIANCHUAN_DAILY_STATE_KEY = "qianchuan.metrics.daily.state"
_QIANCHUAN_DAILY_TARGET_KEY = "qianchuan.metrics.daily.target_date"
_QIANCHUAN_DAILY_TIMEZONE = timezone(timedelta(hours=8), "Asia/Shanghai")
_QIANCHUAN_DAILY_SUCCESS_STATES = {"success", "no_data"}
_QIANCHUAN_DAILY_ADDITIVE_FIELDS = {
    "stat_cost",
    "pay_order_amount",
    "pay_order_count",
    "show_cnt",
    "click_cnt",
}
_QIANCHUAN_DAILY_CORE_FIELDS = [
    field
    for field in PLAN_MATERIAL_REPORT_FIELDS
    if field not in {"product_show_count_for_roi2", "product_click_count_for_roi2"}
]


def run_qianchuan_push(task_ids: list[str]) -> None:
    """Use a small number of lanes so one slow plan cannot block every colleague."""
    with _qianchuan_job_slots:
        _run_qianchuan_push_serial(task_ids)


def claim_qianchuan_tasks(limit: int) -> list[str]:
    """Lease work fairly while reserving capacity for historical retries.

    New submissions use three of every four claim opportunities.  The fourth
    remains available to retries so recovery continues without letting one old
    batch monopolise the whole team queue.  Within each class we rotate by OA
    owner and still serialize the same asset/account pair.
    """
    if limit <= 0:
        return []
    global _qianchuan_claim_cursor, _qianchuan_last_owner
    with _qianchuan_submission_lock, SessionLocal() as db:
        # A process restart can leave a cooperative cancellation without its
        # original worker.  Settle those requests before leasing new work.
        cancellation_rows = db.scalars(
            select(QianchuanDelivery).where(
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.status == "cancel_requested",
            )
        ).all()
        for cancelled in cancellation_rows:
            if cancelled.failure_stage == "plan_binding":
                cancelled.status = "partial"
                cancelled.message = (
                    "取消请求已停止本地后续动作；平台绑定请求可能已经发出，"
                    "保留为部分完成并等待真实回读，不显示虚假取消"
                )
            else:
                cancelled.status = "cancelled"
                cancelled.failure_stage = ""
                cancelled.error_message = ""
                cancelled.message = (
                    "已停止后续计划绑定；原视频已进入千川素材库并保留，未删除平台素材"
                    if cancelled.platform_asset_id or cancelled.upload_task_id
                    else "已停止千川推送；尚未取得平台素材标识"
                )
            cancelled.updated_at = datetime.utcnow()
        if cancellation_rows:
            db.commit()
        # Auth outages are queue-level conditions, not failures of every asset.
        # Keep pending IDs, attempts and remote checkpoints untouched.
        if qianchuan_service.dispatch_block_reason(db):
            return []
        active_pairs = set(
            db.execute(
                select(QianchuanDelivery.asset_id, QianchuanDelivery.advertiser_id).where(
                    QianchuanDelivery.deleted_at.is_(None),
                    or_(
                        QianchuanDelivery.status.in_({"uploading", "binding", "maintenance_hold"}),
                        (
                            (QianchuanDelivery.status == "uploaded")
                            & (QianchuanDelivery.plan_id != "")
                        ),
                    ),
                )
            ).all()
        )
        candidates = list(
            db.scalars(
                select(QianchuanDelivery)
                .where(
                    QianchuanDelivery.deleted_at.is_(None),
                    QianchuanDelivery.status == "pending",
                    _recovery_asset_allowed(db, QianchuanDelivery.asset_id, "push"),
                )
                .order_by(QianchuanDelivery.created_at.asc())
                .limit(2000)
            ).all()
        )

        pools = {
            "fresh": [task for task in candidates if int(task.attempt_count or 0) == 0],
            "retry": [task for task in candidates if int(task.attempt_count or 0) > 0],
        }

        def pick_fair(pool: list[QianchuanDelivery]) -> QianchuanDelivery | None:
            global _qianchuan_last_owner
            available = [
                task for task in pool
                if (task.asset_id, task.advertiser_id) not in active_pairs
            ]
            if not available:
                return None
            grouped: dict[str, list[QianchuanDelivery]] = defaultdict(list)
            for task in available:
                grouped[task.created_by_number or task.created_by_name or "unknown"].append(task)
            owners = sorted(grouped, key=lambda owner: grouped[owner][0].created_at)
            if _qianchuan_last_owner in owners and len(owners) > 1:
                start = (owners.index(_qianchuan_last_owner) + 1) % len(owners)
                owners = owners[start:] + owners[:start]
            owner = owners[0]
            _qianchuan_last_owner = owner
            return grouped[owner][0]

        claimed: list[str] = []
        now = datetime.utcnow()
        for _ in range(limit):
            preferred = _qianchuan_claim_pattern[_qianchuan_claim_cursor % len(_qianchuan_claim_pattern)]
            _qianchuan_claim_cursor += 1
            task = pick_fair(pools[preferred]) or pick_fair(pools["retry" if preferred == "fresh" else "fresh"])
            if not task:
                break
            pair = (task.asset_id, task.advertiser_id)
            task.status = "uploading"
            task.message = (
                "新提交已进入公平处理通道，正在准备原视频"
                if int(task.attempt_count or 0) == 0
                else "历史重试已进入保留通道，正在复用原视频"
            )
            task.updated_at = now
            active_pairs.add(pair)
            claimed.append(task.id)
            pools["fresh"] = [row for row in pools["fresh"] if row.id != task.id]
            pools["retry"] = [row for row in pools["retry"] if row.id != task.id]
        if claimed:
            db.commit()
        return claimed


def _qianchuan_error_advice(error: Exception, stage: str) -> str:
    message = str(error)
    category = str(getattr(error, "category", "") or "")
    code = str(getattr(error, "code", "") or "")
    if category in {"authorization_required", "authorization_temporary"}:
        return "原任务和已上传素材保留；授权恢复后接续并先核验平台结果，无需重新上传。"
    if "合作达人" in message:
        return "计划包含未启用素材的合作达人身份；系统会改为只向已有有效素材的创意身份追加，请重试。"
    if "素材数量超过上限" in message:
        return "系统会改为只向素材数量较少的一个有效创意身份追加；请直接重试，无需重新上传视频。"
    if category == "plan_verification_pending":
        return "千川已受理但回读有延迟；等待 1—3 分钟后点击重试，系统会先查计划并避免重复添加。"
    if category == "plan_identity_unavailable":
        return "计划目标账号的创意身份仍在同步；稍后直接重试即可，系统不会误用合作达人身份。"
    if category == "plan_material_missing":
        return "目标计划当前未保留该视频；点击重试会复用账户素材并重新加入计划。"
    if category == "plan_material_removed":
        return "千川在受理后又自动移除了该视频；请在千川后台检查该计划的创意身份、达人素材限制或素材容量，无需重新上传原视频。"
    if category in {"connection_interrupted", "video_upload", "async_upload_timeout"}:
        return "原视频不会压缩或转码；请直接点击重试，系统会从已有上传任务继续并自动查重。"
    if code == "40002" or "无权限" in message or "权限" in message:
        return "请由千川应用管理员补充对应接口权限或账户授权后重试。"
    if category == "rate_limit" or code in {"40100", "4028", "429"} or "频率" in message:
        return "千川接口触发限频；稍等 1—3 分钟后重试，无需重新上传素材。"
    if stage == "metrics_sync":
        return "计划投放结果不会因此撤销；稍后使用“同步近 7 天数据”再次回流即可。"
    if stage == "plan_binding":
        return "素材已保留在账户素材库；修正计划配置或权限后点击重试即可，无需重新上传。"
    return "请点击重试；若仍失败，请将错误码和请求 ID 提供给千川管理员排查。"


def _record_qianchuan_error(task: QianchuanDelivery, error: Exception, stage: str) -> None:
    task.failure_stage = stage
    task.error_code = str(getattr(error, "code", "") or "")[:80]
    task.error_message = str(error)[:2000]
    task.error_advice = _qianchuan_error_advice(error, stage)[:2000]
    task.last_error_category = str(getattr(error, "category", "internal") or "internal")[:40]
    request_id = str(getattr(error, "request_id", "") or "")
    if request_id:
        task.request_id = request_id[:120]


def _clear_qianchuan_error(task: QianchuanDelivery) -> None:
    task.failure_stage = ""
    task.error_code = ""
    task.error_message = ""
    task.error_advice = ""
    task.last_error_category = ""


def _sync_qianchuan_task_metrics(
    db: Session,
    task: QianchuanDelivery,
    start_date: str | None,
    end_date: str | None,
) -> None:
    if task.plan_id and task.plan_type in {"multiplication", "full_domain"}:
        result = qianchuan_service.plan_material_metrics(
            db,
            advertiser_id=task.advertiser_id,
            plan_id=task.plan_id,
            video_id=task.platform_asset_id,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        result = qianchuan_service.material_metrics(
            db,
            advertiser_id=task.advertiser_id,
            material_id=task.platform_asset_id,
            start_date=start_date,
            end_date=end_date,
        )

    from .platform_audit import retain_material_audit
    retain_material_audit(task, result)
    task.related_ad_ids = result.get("related_ad_ids") or []
    task.related_creative_ids = result.get("related_creative_ids") or []
    exact_plan_link = bool(
        task.plan_id
        and task.plan_type in {"multiplication", "full_domain"}
        and result.get("link_verified")
    )
    if exact_plan_link:
        task.delivery_entity_type = task.delivery_entity_type or "uni_promotion_ad"
        task.delivery_entity_id = task.delivery_entity_id or task.plan_id
        task.binding_verified_at = task.binding_verified_at or datetime.utcnow()
        task.binding_evidence = task.binding_evidence or {
            "advertiser_id": task.advertiser_id,
            "plan_id": task.plan_id,
            "video_id": task.platform_asset_id,
            "matches": result.get("matches") or [],
            "matched_count": len(result.get("matches") or []),
            "readback_source": result.get("source") or "qianchuan/uni_promotion/ad/material/get",
            "request_id": result.get("request_id") or "",
        }
        if task.status == "partial":
            task.status = "success"
            task.message = "已回读目标计划确认视频 ID，计划投放关联成功"
            _clear_qianchuan_error(task)

    if exact_plan_link and result["has_data"]:
        task.metrics_link_status = "verified"
        task.metrics_message = "已按计划 ID 与视频 ID 回流真实数据（数值为 0 时代表千川实际返回 0）"
    elif exact_plan_link:
        task.metrics_link_status = "linked_pending"
        task.metrics_message = "计划与视频关联已确认；所选周期内千川暂未返回素材数据"
    elif task.plan_id and task.binding_verified_at:
        task.metrics_link_status = "missing"
        was_removed = int(result.get("found_count") or 0) > 0
        task.metrics_message = (
            "千川已将该计划中的视频标记为 DELETED，数据不再视为有效关联"
            if was_removed
            else "此前曾完成计划绑定，但本次回读未发现该视频；请展开记录查看并重试核验"
        )
        task.status = "partial"
        task.message = (
            "千川受理后又将该视频标记为 DELETED，当前计划未保持有效投放关联"
            if was_removed
            else "此前曾完成计划绑定，但当前回读未发现有效视频，需重新加入计划"
        )
        missing_error = QianchuanError(
            (
                "千川受理后自动将该视频标记为 DELETED，目标计划当前没有有效投放素材"
                if was_removed
                else "目标计划当前未回读到有效视频（素材可能已被移除或标记为不可投放）"
            ),
            category="plan_material_removed" if was_removed else "plan_material_missing",
            retryable=True,
        )
        _record_qianchuan_error(task, missing_error, "plan_binding")
    elif task.plan_id:
        task.metrics_link_status = "unverified"
        task.metrics_message = "尚未取得目标计划与该视频的有效回读证据，暂不展示投放数据"
    elif result["has_data"]:
        task.metrics_link_status = "account_material"
        task.metrics_message = "已回流账户素材级数据；该记录未选择目标计划"
    else:
        task.metrics_link_status = "pending"
        task.metrics_message = "所选周期内千川暂未返回素材数据"

    task.metrics = result["metrics"]
    task.metrics_start_date = result["start_date"]
    task.metrics_end_date = result["end_date"]
    task.metrics_synced_at = datetime.utcnow()
    task.request_id = result.get("request_id") or task.request_id
    if task.failure_stage == "metrics_sync":
        _clear_qianchuan_error(task)


def _qianchuan_cancel_checkpoint(
    db: Session,
    task: QianchuanDelivery,
    *,
    remote_updates: dict[str, object] | None = None,
) -> bool:
    """Persist remote evidence and stop only at a reversible boundary."""
    with _qianchuan_submission_lock:
        db.refresh(task)
        for field, value in (remote_updates or {}).items():
            setattr(task, field, value)
        if task.status == "cancelled":
            if remote_updates:
                task.updated_at = datetime.utcnow()
                db.commit()
            return True
        if task.status != "cancel_requested":
            if remote_updates:
                task.updated_at = datetime.utcnow()
                db.commit()
            return False
        kept_remote = bool(task.platform_asset_id or task.upload_task_id)
        task.status = "cancelled"
        task.failure_stage = ""
        task.error_message = ""
        task.message = (
            "已停止后续计划绑定；原视频已进入千川素材库并保留，未删除平台素材"
            if kept_remote
            else "已停止千川推送；尚未取得平台素材标识"
        )
        task.updated_at = datetime.utcnow()
        db.commit()
        return True


def _run_qianchuan_push_serial(task_ids: list[str]) -> None:
    for task_id in task_ids:
        with SessionLocal() as db:
            task = db.get(QianchuanDelivery, task_id)
            if not task or task.deleted_at is not None:
                continue
            if task.status == "cancel_requested":
                _qianchuan_cancel_checkpoint(db, task)
                continue
            if task.status not in {"pending", "failed", "uploading"}:
                continue
            if task.asset_id in _recovery_asset_hold_ids(db, "push"):
                continue
            asset = db.get(Asset, task.asset_id)
            if not asset:
                task.status = "failed"
                task.message = "素材不存在"
                missing_error = QianchuanError("原素材已不存在，无法继续推送", category="asset_missing")
                _record_qianchuan_error(task, missing_error, "upload")
                task.updated_at = datetime.utcnow()
                db.commit()
                continue
            current_stage = "upload"
            try:
                blocked = qianchuan_service.dispatch_block_reason(db)
                if blocked:
                    raise QianchuanError(blocked, category="authorization_required")
                with _qianchuan_submission_lock:
                    db.refresh(task)
                    if task.status == "cancel_requested":
                        task.status = "cancelled"
                        task.message = "已停止千川推送；尚未向平台继续传输"
                        task.updated_at = datetime.utcnow()
                        db.commit()
                        continue
                    task.attempt_count = int(task.attempt_count or 0) + 1
                    _clear_qianchuan_error(task)
                    task.status = "uploading"
                    task.message = (
                        f"正在以零压缩方式传输 {asset.size / 1024 / 1024:.1f}MB 原视频（不转码、不改变画质）"
                    )
                    task.updated_at = datetime.utcnow()
                    db.commit()
                if not task.platform_asset_id:
                    if not task.upload_task_id:
                        started = qianchuan_service.begin_video_upload(
                            db,
                            advertiser_id=task.advertiser_id,
                            object_key=asset.object_key,
                            filename=asset.filename,
                        )
                        if _qianchuan_cancel_checkpoint(db, task, remote_updates={
                            "platform_asset_id": started.get("platform_asset_id", ""),
                            "upload_task_id": started.get("upload_task_id", ""),
                            "request_id": started.get("request_id", ""),
                            "message": (
                                "已复用千川素材库中的同一原视频，未重复上传"
                                if started.get("reused")
                                else "千川正在处理原视频（不压缩、不转码、不改变画质）"
                            ),
                        }):
                            continue
                    if not task.platform_asset_id:
                        uploaded = qianchuan_service.wait_video_upload(
                            db,
                            advertiser_id=task.advertiser_id,
                            upload_task_id=task.upload_task_id,
                        )
                        if _qianchuan_cancel_checkpoint(db, task, remote_updates={
                            "platform_asset_id": uploaded["platform_asset_id"],
                            "request_id": uploaded.get("request_id", "") or task.request_id,
                            "message": "千川已完成原视频异步入库，文件未压缩、未转码",
                        }):
                            continue
                elif _qianchuan_cancel_checkpoint(db, task):
                    continue

                # One source video may target many plans.  Propagate the
                # verified account material ID locally so sibling rows skip a
                # repeated remote upload/search and move directly to binding.
                with _qianchuan_submission_lock:
                    db.refresh(task)
                    if task.status == "cancel_requested":
                        task.status = "cancelled"
                        task.message = "已停止后续计划绑定；原视频已保留在千川账户素材库"
                        task.updated_at = datetime.utcnow()
                        db.commit()
                        continue
                    if task.platform_asset_id:
                        siblings = db.scalars(
                            select(QianchuanDelivery).where(
                                QianchuanDelivery.id != task.id,
                                QianchuanDelivery.deleted_at.is_(None),
                                QianchuanDelivery.status == "pending",
                                QianchuanDelivery.asset_id == task.asset_id,
                                QianchuanDelivery.advertiser_id == task.advertiser_id,
                                QianchuanDelivery.platform_asset_id == "",
                            )
                        ).all()
                        for sibling in siblings:
                            sibling.platform_asset_id = task.platform_asset_id
                            sibling.upload_task_id = task.upload_task_id
                            sibling.message = "已复用同批次原视频，等待公平绑定通道"
                            sibling.updated_at = datetime.utcnow()
                    task.status = "uploaded"
                    task.message = "素材已在千川账户素材库，正在准备后续计划绑定" if task.plan_id else "原视频已进入千川账户素材库"
                    task.updated_at = datetime.utcnow()
                    db.commit()

                if task.plan_id:
                    if task.plan_type == "standard":
                        standard_error = QianchuanError(
                            "千川官方接口不支持只向普通计划追加素材",
                            category="standard_plan_manual",
                        )
                        task.status = "partial"
                        task.message = (
                            "素材已进入账户素材库；普通计划可以查询和选择，但千川官方接口不支持安全地仅追加素材，"
                            "请在千川后台把该素材加入所选普通计划。"
                        )
                        _record_qianchuan_error(task, standard_error, "plan_binding")
                        task.updated_at = datetime.utcnow()
                        db.commit()
                        continue
                    current_stage = "plan_binding"
                    with _qianchuan_submission_lock:
                        db.refresh(task)
                        if task.status in {"cancel_requested", "cancelled"}:
                            if task.status == "cancel_requested":
                                task.status = "cancelled"
                                task.message = "已停止后续计划绑定；原视频已保留在千川账户素材库"
                                task.updated_at = datetime.utcnow()
                                db.commit()
                            continue
                        task.status = "binding"
                        task.idempotency_key = task.idempotency_key or (
                            f"{task.created_by_number}|{task.asset_id}|{task.advertiser_id}|{task.plan_id}"
                        )
                        plan_type_label = "乘方计划" if task.plan_type == "multiplication" else "全域推广计划"
                        task.message = f"正在将素材加入{plan_type_label}，并回读目标计划确认视频 ID"
                        task.updated_at = datetime.utcnow()
                        db.commit()
                    try:
                        bound = qianchuan_service.add_to_plan(
                            db,
                            advertiser_id=task.advertiser_id,
                            plan_id=task.plan_id,
                            video_id=task.platform_asset_id,
                        )
                        with _qianchuan_submission_lock:
                            db.refresh(task)
                            cancel_arrived_late = task.status == "cancel_requested"
                            task.status = "success"
                            task.last_error_category = ""
                            task.request_id = bound.get("request_id") or task.request_id
                            task.delivery_entity_type = bound.get("delivery_entity_type", "uni_promotion_ad")
                            task.delivery_entity_id = bound.get("delivery_entity_id", task.plan_id)
                            task.binding_evidence = bound.get("binding_evidence") or {}
                            task.binding_verified_at = datetime.utcnow()
                            task.metrics_link_status = "pending"
                            action = "目标计划原已存在同一视频，未重复添加" if bound.get("already_present") else "已直接加入目标计划"
                            task.message = (
                                f"{action}；已回读{plan_type_label}确认视频 ID {task.platform_asset_id}，等待投放数据回流"
                                + ("；取消请求到达时平台动作已完成，未伪装为已取消" if cancel_arrived_late else "")
                            )[:1000]
                            task.updated_at = datetime.utcnow()
                            db.commit()
                    except QianchuanError as error:
                        with _qianchuan_submission_lock:
                            db.refresh(task)
                            cancel_arrived_late = task.status == "cancel_requested"
                            auth_wait = error.category in {"authorization_required", "authorization_temporary"}
                            task.status = "pending" if auth_wait and not cancel_arrived_late else "partial"
                            _record_qianchuan_error(task, error, "plan_binding")
                            task.message = (
                                ("取消请求到达时平台绑定动作已发出，结果需继续核验：" if cancel_arrived_late else "素材已进入账户素材库，但该计划未完成绑定：")
                                + str(error)
                            )[:1000]
                            task.updated_at = datetime.utcnow()
                            db.commit()
            except QianchuanError as error:
                with _qianchuan_submission_lock:
                    db.refresh(task)
                    if task.status == "cancel_requested" and current_stage == "upload":
                        task.status = "cancelled"
                        task.failure_stage = ""
                        task.error_message = ""
                        task.message = "已停止千川推送；平台上传未取得可核验完成结果"
                    else:
                        auth_wait = error.category in {"authorization_required", "authorization_temporary"}
                        task.status = "pending" if auth_wait and task.status != "cancel_requested" else ("partial" if current_stage == "plan_binding" and task.status == "cancel_requested" else "failed")
                        task.message = str(error)[:1000]
                        _record_qianchuan_error(task, error, current_stage)
                    task.updated_at = datetime.utcnow()
                    db.commit()
            except Exception as error:
                with _qianchuan_submission_lock:
                    db.refresh(task)
                    if task.status == "cancel_requested" and current_stage == "upload":
                        task.status = "cancelled"
                        task.failure_stage = ""
                        task.error_message = ""
                        task.message = "已停止千川推送；平台上传未取得可核验完成结果"
                    else:
                        task.status = "partial" if current_stage == "plan_binding" and task.status == "cancel_requested" else "failed"
                        task.message = "推送过程发生异常，请稍后重试"
                        _record_qianchuan_error(task, error, current_stage)
                    task.updated_at = datetime.utcnow()
                    db.commit()


def _utc_iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds") + "Z"


def _parse_utc_iso(value: str) -> datetime | None:
    if not value:
        return None
    with suppress(ValueError):
        return datetime.fromisoformat(value.removesuffix("Z"))
    return None


def _metrics_local_now(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(_QIANCHUAN_DAILY_TIMEZONE)


def _daily_metric_tasks(
    db: Session,
    *,
    owner_number: str = "",
    requested_task_ids: list[str] | None = None,
    limit: int | None = None,
    stat_date: date | None = None,
) -> list[QianchuanDelivery]:
    query = select(QianchuanDelivery).where(
        QianchuanDelivery.deleted_at.is_(None),
        QianchuanDelivery.status == "success",
        QianchuanDelivery.plan_id != "",
        QianchuanDelivery.platform_asset_id != "",
        QianchuanDelivery.plan_type.in_({"multiplication", "full_domain"}),
        QianchuanDelivery.binding_verified_at.is_not(None),
    )
    if owner_number:
        query = query.where(QianchuanDelivery.created_by_number == owner_number)
    if requested_task_ids:
        query = query.where(QianchuanDelivery.id.in_(requested_task_ids))
    if stat_date:
        query = query.where(
            QianchuanDelivery.created_at < datetime.combine(stat_date + timedelta(days=1), datetime.min.time())
        )
    query = query.order_by(
        QianchuanDelivery.advertiser_id,
        QianchuanDelivery.plan_id,
        QianchuanDelivery.platform_asset_id,
        QianchuanDelivery.created_at,
    )
    if limit:
        query = query.limit(limit)
    return list(db.scalars(query).all())


def _daily_plan_groups(
    tasks: list[QianchuanDelivery],
) -> dict[tuple[str, str], list[QianchuanDelivery]]:
    groups: dict[tuple[str, str], list[QianchuanDelivery]] = {}
    for task in tasks:
        groups.setdefault((task.advertiser_id, task.plan_id), []).append(task)
    return groups


def _daily_row_out(row: QianchuanMetricDaily) -> dict:
    return {
        "stat_date": row.stat_date,
        "metrics": row.metrics or {},
        "status": row.status,
        "has_data": bool(row.has_data),
        "link_verified": bool(row.link_verified),
        "message": row.message,
        "error_code": row.error_code,
        "request_id": row.request_id,
        "synced_at": _utc_iso(row.synced_at),
    }


def _task_daily_summary(
    db: Session,
    task: QianchuanDelivery,
    *,
    window_end: date | None = None,
) -> dict:
    local_today = _metrics_local_now().date()
    latest_row_date = db.scalar(
        select(func.max(QianchuanMetricDaily.stat_date)).where(
            QianchuanMetricDaily.task_id == task.id,
            QianchuanMetricDaily.stat_date <= local_today.isoformat(),
        )
    )
    end = window_end or (
        local_today if latest_row_date == local_today.isoformat() else local_today - timedelta(days=1)
    )
    start = end - timedelta(days=settings.qianchuan_metrics_daily_lookback_days - 1)
    rows = list(
        db.scalars(
            select(QianchuanMetricDaily)
            .where(
                QianchuanMetricDaily.task_id == task.id,
                QianchuanMetricDaily.stat_date >= start.isoformat(),
                QianchuanMetricDaily.stat_date <= end.isoformat(),
            )
            .order_by(QianchuanMetricDaily.stat_date.desc())
        ).all()
    )
    task_date = task.created_at.replace(tzinfo=timezone.utc).astimezone(_QIANCHUAN_DAILY_TIMEZONE).date()
    expected_start = max(start, task_date)
    expected = max(0, (end - expected_start).days + 1)
    expected_dates = {
        (expected_start + timedelta(days=offset)).isoformat()
        for offset in range(expected)
    }
    valid_rows = [
        row for row in rows
        if row.stat_date in expected_dates and row.status in _QIANCHUAN_DAILY_SUCCESS_STATES
    ]
    data_rows = [row for row in valid_rows if row.status == "success" and row.has_data]
    errors = [row for row in rows if row.stat_date in expected_dates and row.status == "error"]
    totals: dict[str, float] = {}
    for row in data_rows:
        for field in _QIANCHUAN_DAILY_ADDITIVE_FIELDS:
            value = (row.metrics or {}).get(field)
            if value is None:
                continue
            try:
                totals[field] = totals.get(field, 0.0) + float(value)
            except (TypeError, ValueError):
                continue
    metrics: dict[str, object] = {}
    if data_rows:
        for field, value in totals.items():
            metrics[field] = int(value) if value.is_integer() else round(value, 4)
        cost = float(metrics.get("stat_cost", 0) or 0)
        gmv = float(metrics.get("pay_order_amount", 0) or 0)
        metrics["prepay_and_pay_order_roi"] = round(gmv / cost, 4) if cost > 0 else 0
        metrics["metric_scope"] = "daily_plan_video_roi2"

    completed = len({row.stat_date for row in valid_rows})
    if errors and completed == 0:
        status = "error"
    elif errors:
        status = "partial_error"
    elif expected and completed >= expected:
        status = "fresh" if data_rows else "no_data"
    elif completed:
        status = "partial"
    else:
        status = "pending"
    fresh_dates = [row.stat_date for row in valid_rows]
    return {
        "metrics": metrics,
        "status": status,
        "fresh_through": max(fresh_dates) if fresh_dates else None,
        "coverage": {"completed": completed, "expected": expected},
        "link_verified": any(row.link_verified for row in valid_rows),
        "daily_metrics": [_daily_row_out(row) for row in rows],
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
    }


def _upsert_daily_metric(
    db: Session,
    task: QianchuanDelivery,
    stat_date: date,
    *,
    status: str,
    metrics: dict | None = None,
    has_data: bool = False,
    link_verified: bool = False,
    message: str = "",
    error_code: str = "",
    request_id: str = "",
) -> QianchuanMetricDaily:
    row = db.scalar(
        select(QianchuanMetricDaily).where(
            QianchuanMetricDaily.task_id == task.id,
            QianchuanMetricDaily.stat_date == stat_date.isoformat(),
        )
    )
    if not row:
        row = QianchuanMetricDaily(
            task_id=task.id,
            advertiser_id=task.advertiser_id,
            plan_id=task.plan_id,
            video_id=task.platform_asset_id,
            stat_date=stat_date.isoformat(),
        )
        db.add(row)
    keep_success = status == "error" and row.status in _QIANCHUAN_DAILY_SUCCESS_STATES
    if not keep_success:
        row.status = status
        row.metrics = metrics or {}
        row.has_data = has_data
        row.link_verified = link_verified
        row.synced_at = datetime.utcnow()
    row.message = message[:1000]
    row.error_code = error_code[:80]
    row.request_id = request_id[:120]
    row.updated_at = datetime.utcnow()
    return row


def _retryable_daily_error(error: QianchuanError) -> bool:
    return bool(error.retryable or str(error.code) in {"40000", "40100"})


def _read_plan_daily_with_retry(
    db: Session,
    advertiser_id: str,
    plan_id: str,
    stat_date: date,
) -> dict:
    last_error: QianchuanError | None = None
    fields = list(PLAN_MATERIAL_REPORT_FIELDS)
    field_warning = ""
    for attempt in range(settings.qianchuan_metrics_retry_count):
        try:
            result = qianchuan_service.plan_material_videos(
                db,
                advertiser_id=advertiser_id,
                plan_id=plan_id,
                start_date=stat_date.isoformat(),
                end_date=stat_date.isoformat(),
                fields=fields,
            )
            if field_warning:
                result["field_warning"] = field_warning
            return result
        except QianchuanError as error:
            last_error = error
            if (
                str(error.code) in {"40000", "400153"}
                and fields != _QIANCHUAN_DAILY_CORE_FIELDS
            ):
                fields = list(_QIANCHUAN_DAILY_CORE_FIELDS)
                field_warning = "该计划暂不支持展示量与点击量字段，核心经营数据已正常回流"
                continue
            if not _retryable_daily_error(error) or attempt + 1 >= settings.qianchuan_metrics_retry_count:
                raise
            time.sleep(min(12, 2 ** (attempt + 1)))
    raise last_error or QianchuanError("千川日数据读取失败")


def _refresh_task_daily_cache(db: Session, task: QianchuanDelivery) -> None:
    summary = _task_daily_summary(db, task)
    if summary["metrics"]:
        task.metrics = summary["metrics"]
    if summary.get("link_verified"):
        task.metrics_link_status = "verified"
    task.metrics_start_date = summary["start_date"]
    task.metrics_end_date = summary["end_date"]
    latest = db.scalar(
        select(func.max(QianchuanMetricDaily.synced_at)).where(
            QianchuanMetricDaily.task_id == task.id
        )
    )
    if latest:
        task.metrics_synced_at = latest
    coverage = summary["coverage"]
    if summary["status"] == "fresh":
        task.metrics_message = (
            f"每日数据已刷新至 {summary['fresh_through']}，"
            f"近 {settings.qianchuan_metrics_daily_lookback_days} 日按计划 ID 与视频 ID 汇总"
        )
    elif summary["status"] == "no_data":
        task.metrics_message = f"已刷新至 {summary['fresh_through']}；千川返回真实 0 或暂无投放数据"
    elif summary["status"] in {"error", "partial_error"}:
        task.metrics_message = "部分日期回流失败；已保留最近一次成功数据，请展开查看每日明细"
    else:
        task.metrics_message = (
            f"每日数据正在补齐（已完成 {coverage['completed']}/{coverage['expected']} 天）"
        )
    task.updated_at = datetime.utcnow()


def _run_daily_metric_date(
    stat_date: date,
    tasks: list[QianchuanDelivery],
    *,
    update_progress: bool = False,
) -> dict:
    groups = _daily_plan_groups(tasks)
    error_groups = 0
    processed_groups = 0
    for group_index, ((advertiser_id, plan_id), grouped_tasks) in enumerate(groups.items(), start=1):
        with SessionLocal() as db:
            current_tasks = [db.get(QianchuanDelivery, task.id) for task in grouped_tasks]
            current_tasks = [task for task in current_tasks if task and task.deleted_at is None]
            try:
                plan_result = _read_plan_daily_with_retry(db, advertiser_id, plan_id, stat_date)
                by_video: dict[str, dict] = {}
                for task in current_tasks:
                    result = by_video.setdefault(
                        task.platform_asset_id,
                        qianchuan_service.plan_material_metrics_from_result(
                            plan_result,
                            plan_id=plan_id,
                            video_id=task.platform_asset_id,
                            start_date=stat_date.isoformat(),
                            end_date=stat_date.isoformat(),
                        ),
                    )
                    from .platform_audit import retain_material_audit
                    retain_material_audit(task, result)
                    if result["link_verified"] and result["has_data"]:
                        row_status = "success"
                        message = plan_result.get("field_warning") or "千川已返回该日计划素材数据（数值为 0 时代表真实 0）"
                    elif result["link_verified"]:
                        row_status = "no_data"
                        message = "计划关联有效，但千川该日暂未返回投放数据"
                    else:
                        row_status = "missing"
                        message = "该日回读未发现有效计划素材关联"
                    _upsert_daily_metric(
                        db,
                        task,
                        stat_date,
                        status=row_status,
                        metrics=result["metrics"],
                        has_data=result["has_data"],
                        link_verified=result["link_verified"],
                        message=message,
                        request_id=result.get("request_id") or "",
                    )
                db.flush()
                for task in current_tasks:
                    _refresh_task_daily_cache(db, task)
                db.commit()
            except QianchuanError as error:
                error_groups += 1
                for task in current_tasks:
                    _upsert_daily_metric(
                        db,
                        task,
                        stat_date,
                        status="error",
                        message=f"每日数据回流失败：{str(error)}",
                        error_code=str(error.code or ""),
                        request_id=error.request_id,
                    )
                    _refresh_task_daily_cache(db, task)
                db.commit()
        processed_groups += 1
        if update_progress:
            with SessionLocal() as progress_db:
                _set_meta(progress_db, "qianchuan.metrics.daily.processed_groups", str(processed_groups))
                _set_meta(progress_db, "qianchuan.metrics.daily.error_groups", str(error_groups))
                progress_db.commit()
        if group_index < len(groups):
            time.sleep(settings.qianchuan_metrics_request_interval_seconds)
    return {
        "task_count": len(tasks),
        "group_count": len(groups),
        "processed_groups": processed_groups,
        "error_groups": error_groups,
    }


def run_qianchuan_daily_metrics_if_due(now: datetime | None = None) -> dict:
    current_local = _metrics_local_now(now)
    target_date = current_local.date() - timedelta(days=1)
    if current_local.hour < settings.qianchuan_metrics_daily_hour:
        return {"status": "waiting", "target_date": target_date.isoformat()}
    if not _qianchuan_metrics_slot.acquire(blocking=False):
        return {"status": "busy", "target_date": target_date.isoformat()}
    try:
        with SessionLocal() as db:
            last_completed = _meta(db, _QIANCHUAN_DAILY_LAST_DATE_KEY)
            daily_count = db.scalar(select(func.count(QianchuanMetricDaily.id))) or 0
            retry_start = target_date - timedelta(days=settings.qianchuan_metrics_daily_lookback_days - 1)
            retry_date_values = list(db.scalars(
                select(QianchuanMetricDaily.stat_date)
                .where(
                    QianchuanMetricDaily.status == "error",
                    QianchuanMetricDaily.stat_date >= retry_start.isoformat(),
                    QianchuanMetricDaily.stat_date <= target_date.isoformat(),
                )
                .distinct()
                .order_by(QianchuanMetricDaily.stat_date)
            ).all())
            if last_completed >= target_date.isoformat() and daily_count and not retry_date_values:
                return {"status": "waiting", "target_date": target_date.isoformat()}
            bootstrap = daily_count == 0
            full_dates = [
                target_date - timedelta(days=offset)
                for offset in reversed(range(settings.qianchuan_metrics_daily_lookback_days if bootstrap else 1))
            ] if last_completed < target_date.isoformat() or bootstrap else []
            retry_dates = [
                date.fromisoformat(value)
                for value in retry_date_values
                if value not in {item.isoformat() for item in full_dates}
            ]
            dates = sorted({*full_dates, *retry_dates})
            _set_meta(db, _QIANCHUAN_DAILY_STATE_KEY, "running")
            _set_meta(db, _QIANCHUAN_DAILY_TARGET_KEY, target_date.isoformat())
            _set_meta(db, "qianchuan.metrics.daily.started_at", _utc_iso(datetime.utcnow()))
            _set_meta(db, "qianchuan.metrics.daily.processed_groups", "0")
            _set_meta(db, "qianchuan.metrics.daily.error_groups", "0")
            db.commit()

        total_tasks = 0
        total_groups = 0
        total_errors = 0
        for stat_date in dates:
            with SessionLocal() as db:
                if stat_date in retry_dates:
                    failed_task_ids = list(db.scalars(
                        select(QianchuanMetricDaily.task_id).where(
                            QianchuanMetricDaily.stat_date == stat_date.isoformat(),
                            QianchuanMetricDaily.status == "error",
                        )
                    ).all())
                    tasks = _daily_metric_tasks(
                        db,
                        requested_task_ids=failed_task_ids,
                        stat_date=stat_date,
                    )
                else:
                    tasks = _daily_metric_tasks(db, stat_date=stat_date)
            outcome = _run_daily_metric_date(stat_date, tasks, update_progress=True)
            total_tasks += outcome["task_count"]
            total_groups += outcome["group_count"]
            total_errors += outcome["error_groups"]
        with SessionLocal() as db:
            _set_meta(db, _QIANCHUAN_DAILY_LAST_DATE_KEY, target_date.isoformat())
            _set_meta(db, _QIANCHUAN_DAILY_STATE_KEY, "partial" if total_errors else "completed")
            _set_meta(db, "qianchuan.metrics.daily.completed_at", _utc_iso(datetime.utcnow()))
            _set_meta(db, "qianchuan.metrics.daily.task_count", str(total_tasks))
            _set_meta(db, "qianchuan.metrics.daily.group_count", str(total_groups))
            _set_meta(db, "qianchuan.metrics.daily.error_groups", str(total_errors))
            db.commit()
        return {
            "status": "partial" if total_errors else "completed",
            "target_date": target_date.isoformat(),
            "dates": [item.isoformat() for item in dates],
            "task_count": total_tasks,
            "group_count": total_groups,
            "error_groups": total_errors,
        }
    except Exception as error:
        with SessionLocal() as db:
            _set_meta(db, _QIANCHUAN_DAILY_STATE_KEY, "failed")
            _set_meta(db, "qianchuan.metrics.daily.last_error", str(error)[:1000])
            db.commit()
        raise
    finally:
        _qianchuan_metrics_slot.release()


def run_qianchuan_manual_daily_metrics(
    task_ids: list[str],
    owner_number: str,
    stat_date: date,
) -> None:
    with _qianchuan_metrics_slot:
        _mark_manual_metrics_job(owner_number, "running", len(task_ids))
        try:
            with SessionLocal() as db:
                tasks = _daily_metric_tasks(
                    db,
                    owner_number=owner_number,
                    requested_task_ids=task_ids,
                    limit=settings.qianchuan_metrics_manual_batch_size,
                    stat_date=stat_date,
                )
            _run_daily_metric_date(stat_date, tasks)
        finally:
            _mark_manual_metrics_job(owner_number, "completed", len(task_ids))


def _manual_metrics_meta_key(owner_number: str, suffix: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_-]", "_", owner_number)[:48]
    return f"qianchuan.metrics.manual.{normalized}.{suffix}"


def select_qianchuan_metrics_task_ids(
    db: Session,
    *,
    owner_number: str = "",
    requested_task_ids: list[str] | None = None,
    limit: int,
    now: datetime | None = None,
) -> list[str]:
    """Select only stable, stale records so metrics reads stay bounded and do not race pushes."""
    current = now or datetime.utcnow()
    stale_before = current - timedelta(minutes=settings.qianchuan_metrics_record_min_age_minutes)
    query = select(QianchuanDelivery.id).where(
        QianchuanDelivery.platform_asset_id != "",
        QianchuanDelivery.deleted_at.is_(None),
        QianchuanDelivery.status.in_(_QIANCHUAN_METRICS_ELIGIBLE_STATUSES),
        or_(
            QianchuanDelivery.metrics_synced_at.is_(None),
            QianchuanDelivery.metrics_synced_at <= stale_before,
        ),
    )
    if owner_number:
        query = query.where(QianchuanDelivery.created_by_number == owner_number)
    if requested_task_ids:
        query = query.where(QianchuanDelivery.id.in_(requested_task_ids))
    query = query.order_by(
        QianchuanDelivery.metrics_synced_at.is_not(None),
        QianchuanDelivery.metrics_synced_at.asc(),
        QianchuanDelivery.updated_at.asc(),
    ).limit(max(1, limit))
    return list(db.scalars(query).all())


def _manual_metrics_status(db: Session, owner_number: str, now: datetime | None = None) -> dict:
    current = now or datetime.utcnow()
    requested_at = _parse_utc_iso(_meta(db, _manual_metrics_meta_key(owner_number, "requested_at")))
    state = _meta(db, _manual_metrics_meta_key(owner_number, "state"), "idle")
    task_count = int(_meta(db, _manual_metrics_meta_key(owner_number, "task_count"), "0") or 0)
    running_until = requested_at + timedelta(minutes=30) if requested_at else None
    running = state in {"queued", "running"} and bool(running_until and current < running_until)
    cooldown_until = (
        requested_at + timedelta(minutes=settings.qianchuan_metrics_manual_cooldown_minutes)
        if requested_at else None
    )
    available = not running and (not cooldown_until or current >= cooldown_until)
    return {
        "state": state,
        "running": running,
        "available": available,
        "task_count": task_count,
        "requested_at": _utc_iso(requested_at) if requested_at else None,
        "next_available_at": _utc_iso(cooldown_until) if cooldown_until and current < cooldown_until else None,
        "completed_at": _meta(db, _manual_metrics_meta_key(owner_number, "completed_at")) or None,
    }


def _metrics_sync_status(db: Session, owner_number: str, now: datetime | None = None) -> dict:
    current = now or datetime.utcnow()
    current_local = _metrics_local_now(now)
    next_run = current_local.replace(
        hour=settings.qianchuan_metrics_daily_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if current_local >= next_run:
        next_run += timedelta(days=1)
    manual = _manual_metrics_status(db, owner_number, current)
    return {
        "mode": "daily",
        "timezone": "Asia/Shanghai",
        "daily_hour": settings.qianchuan_metrics_daily_hour,
        "lookback_days": settings.qianchuan_metrics_daily_lookback_days,
        "daily_last_completed_date": _meta(db, _QIANCHUAN_DAILY_LAST_DATE_KEY) or None,
        "daily_target_date": _meta(db, _QIANCHUAN_DAILY_TARGET_KEY) or None,
        "daily_state": _meta(db, _QIANCHUAN_DAILY_STATE_KEY, "waiting"),
        "daily_started_at": _meta(db, "qianchuan.metrics.daily.started_at") or None,
        "daily_completed_at": _meta(db, "qianchuan.metrics.daily.completed_at") or None,
        "daily_task_count": int(_meta(db, "qianchuan.metrics.daily.task_count", "0") or 0),
        "daily_group_count": int(_meta(db, "qianchuan.metrics.daily.group_count", "0") or 0),
        "daily_processed_groups": int(_meta(db, "qianchuan.metrics.daily.processed_groups", "0") or 0),
        "daily_error_groups": int(_meta(db, "qianchuan.metrics.daily.error_groups", "0") or 0),
        "daily_next_run_at": next_run.isoformat(),
        "auto_interval_minutes": 24 * 60,
        "auto_batch_size": 0,
        "auto_last_run_at": _meta(db, "qianchuan.metrics.daily.completed_at") or None,
        "auto_next_run_at": next_run.isoformat(),
        "auto_state": _meta(db, _QIANCHUAN_DAILY_STATE_KEY, "waiting"),
        "record_min_age_minutes": 24 * 60,
        "manual_batch_size": settings.qianchuan_metrics_manual_batch_size,
        "manual_cooldown_minutes": settings.qianchuan_metrics_manual_cooldown_minutes,
        "manual_running": manual["running"],
        "manual_available": manual["available"],
        "manual_state": manual["state"],
        "manual_task_count": manual["task_count"],
        "manual_requested_at": manual["requested_at"],
        "manual_next_available_at": manual["next_available_at"],
        "manual_completed_at": manual["completed_at"],
    }


def _mark_manual_metrics_job(owner_number: str, state: str, task_count: int | None = None) -> None:
    if not owner_number:
        return
    with SessionLocal() as db:
        _set_meta(db, _manual_metrics_meta_key(owner_number, "state"), state)
        if task_count is not None:
            _set_meta(db, _manual_metrics_meta_key(owner_number, "task_count"), str(task_count))
        if state == "completed":
            _set_meta(db, _manual_metrics_meta_key(owner_number, "completed_at"), _utc_iso(datetime.utcnow()))
        db.commit()


def run_qianchuan_metrics(
    task_ids: list[str],
    start_date: str | None,
    end_date: str | None,
    owner_number: str = "",
) -> None:
    """Use one dedicated metrics lane so data reads never consume the three delivery lanes."""
    with _qianchuan_metrics_slot:
        _mark_manual_metrics_job(owner_number, "running", len(task_ids))
        try:
            _run_qianchuan_metrics_serial(task_ids, start_date, end_date)
        finally:
            _mark_manual_metrics_job(owner_number, "completed", len(task_ids))


def _run_qianchuan_metrics_serial(
    task_ids: list[str], start_date: str | None, end_date: str | None
) -> None:
    for task_id in task_ids:
        with SessionLocal() as db:
            task = db.get(QianchuanDelivery, task_id)
            if not task or task.deleted_at is not None or not task.platform_asset_id:
                continue
            try:
                _sync_qianchuan_task_metrics(db, task, start_date, end_date)
                task.updated_at = datetime.utcnow()
                db.commit()
            except QianchuanError as error:
                task.metrics_message = f"投放数据同步失败：{str(error)}"[:1000]
                if not task.error_message or task.failure_stage == "metrics_sync":
                    _record_qianchuan_error(task, error, "metrics_sync")
                task.updated_at = datetime.utcnow()
                db.commit()
            except Exception as error:
                task.metrics_message = "投放数据同步发生内部异常，请稍后重试"
                if not task.error_message or task.failure_stage == "metrics_sync":
                    _record_qianchuan_error(task, error, "metrics_sync")
                task.updated_at = datetime.utcnow()
                db.commit()


def run_qianchuan_auto_metrics_if_due(now: datetime | None = None) -> dict:
    """Run a small stale-first batch in the dedicated readback lane."""
    current = now or datetime.utcnow()
    if not _qianchuan_metrics_slot.acquire(blocking=False):
        return {"status": "busy", "task_ids": []}
    try:
        with SessionLocal() as db:
            last_run = _parse_utc_iso(_meta(db, _QIANCHUAN_METRICS_AUTO_LAST_RUN_KEY))
            due_at = (
                last_run + timedelta(minutes=settings.qianchuan_metrics_interval_minutes)
                if last_run else None
            )
            if due_at and current < due_at:
                return {"status": "waiting", "task_ids": [], "next_run_at": _utc_iso(due_at)}
            task_ids = select_qianchuan_metrics_task_ids(
                db,
                limit=settings.qianchuan_metrics_auto_batch_size,
                now=current,
            )
            _set_meta(db, _QIANCHUAN_METRICS_AUTO_STATE_KEY, "running" if task_ids else "completed_empty")
            db.commit()
        if task_ids:
            _run_qianchuan_metrics_serial(task_ids, None, None)
        completed_at = now or datetime.utcnow()
        with SessionLocal() as db:
            _set_meta(db, _QIANCHUAN_METRICS_AUTO_LAST_RUN_KEY, _utc_iso(completed_at))
            _set_meta(db, _QIANCHUAN_METRICS_AUTO_STATE_KEY, "completed")
            _set_meta(db, "qianchuan.metrics.auto.task_count", str(len(task_ids)))
            db.commit()
        return {"status": "completed", "task_ids": task_ids}
    except Exception as error:
        with SessionLocal() as db:
            _set_meta(db, _QIANCHUAN_METRICS_AUTO_STATE_KEY, "failed")
            _set_meta(db, "qianchuan.metrics.auto.last_error", str(error)[:1000])
            db.commit()
        raise
    finally:
        _qianchuan_metrics_slot.release()


async def qianchuan_metrics_loop() -> None:
    await asyncio.sleep(settings.qianchuan_metrics_startup_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_qianchuan_daily_metrics_if_due)
        except Exception:
            pass
        await asyncio.sleep(60)


from .push_schemes import install_push_schemes
install_push_schemes(app, get_db, require_user, user_number)


@app.get("/api/push-preferences/{platform}")
def push_preferences(
    platform: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if platform not in {"qianchuan", "adq"}:
        raise HTTPException(404, "暂不支持该推送平台")
    rows = db.scalars(
        select(PushPreference)
        .where(
            PushPreference.owner_number == user_number(user),
            PushPreference.platform == platform,
        )
        .order_by(
            PushPreference.pinned.desc(),
            PushPreference.last_used_at.desc(),
            PushPreference.use_count.desc(),
            PushPreference.updated_at.desc(),
        )
    ).all()
    return {"items": [_push_preference_out(item) for item in rows], "total": len(rows)}


@app.post("/api/push-preferences/{platform}/toggle")
def toggle_push_preference(
    platform: str,
    payload: PushPreferenceToggle,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if platform not in {"qianchuan", "adq"}:
        raise HTTPException(404, "暂不支持该推送平台")
    item = _upsert_push_preference(
        db,
        user_number(user),
        platform,
        payload.account_id.strip(),
        payload.account_name.strip(),
        payload.target_id.strip(),
        payload.target_name.strip(),
        payload.target_type.strip(),
    )
    item.pinned = not bool(item.pinned)
    item.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(item)
    return _push_preference_out(item)


@app.get("/api/qianchuan/status")
def qianchuan_status(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    result = qianchuan_service.validated_status(db)
    result["can_authorize"] = is_asset_admin(user)
    result["metrics_sync"] = _metrics_sync_status(db, user_number(user))
    return result


@app.get("/api/qianchuan/oauth/start")
def qianchuan_oauth_start(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    if not is_asset_admin(user):
        raise HTTPException(403, "仅千川管理员可以更新系统授权")
    try:
        return {"url": qianchuan_service.authorize_url(db)}
    except QianchuanError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/qianchuan/oauth/callback")
def qianchuan_oauth_callback(
    state: str = "",
    auth_code: str = "",
    code: str = "",
    db: Session = Depends(get_db),
):
    try:
        qianchuan_service.exchange_code(db, auth_code or code, state)
        return RedirectResponse(oauth_result_url('qianchuan', True), status_code=303)
    except QianchuanError:
        return RedirectResponse(oauth_result_url('qianchuan', False), status_code=303)


@app.get("/api/qianchuan/accounts")
def qianchuan_accounts(db: Session = Depends(get_db), _user: dict = Depends(require_user)):
    try:
        items = qianchuan_service.accounts(db)
        return {"items": items, "total": len(items)}
    except QianchuanError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/qianchuan/product-plan-map")
def qianchuan_product_plan_map(_user: dict = Depends(require_user)):
    """Return the reviewed product bundle map used by the push preview UI."""

    return product_plan_map_snapshot()


def _qianchuan_full_plan_evidence(db: Session, advertiser_id: str) -> dict[str, dict[str, str]]:
    """Return plans whose latest decisive delivery result says the material limit is full.

    A later successful delivery clears older full-capacity evidence. Non-capacity
    failures do not hide an earlier full response because they do not prove that
    the plan has space again.
    """
    tasks = db.execute(
        select(QianchuanDelivery)
        .where(
            QianchuanDelivery.advertiser_id == advertiser_id,
            QianchuanDelivery.plan_id != "",
            or_(
                QianchuanDelivery.status == "success",
                QianchuanDelivery.error_message.contains("素材数量超过上限"),
                QianchuanDelivery.error_advice.contains("素材数量超过上限"),
                QianchuanDelivery.message.contains("素材数量超过上限"),
            ),
        )
        .order_by(QianchuanDelivery.updated_at.desc(), QianchuanDelivery.created_at.desc())
    ).scalars().all()
    decided: set[str] = set()
    evidence: dict[str, dict[str, str]] = {}
    for task in tasks:
        plan_id = str(task.plan_id or "")
        if not plan_id or plan_id in decided:
            continue
        if task.status == "success":
            decided.add(plan_id)
            continue
        diagnostic = " ".join(
            str(value or "")
            for value in (task.error_message, task.error_advice, task.message)
        )
        if "素材数量超过上限" not in diagnostic:
            continue
        decided.add(plan_id)
        evidence[plan_id] = {
            "checked_at": (task.updated_at or task.created_at).isoformat(timespec="seconds"),
            "task_id": task.id,
        }
    return evidence


@app.get("/api/qianchuan/plans")
def qianchuan_plans(
    advertiser_id: str = Query(min_length=1, max_length=80),
    q: str = Query(default="", max_length=100),
    refresh: bool = Query(default=False),
    cached_only: bool = Query(default=False),
    scope: str = Query(
        default="all",
        pattern="^(all|multiplication|full_domain|standard)$",
    ),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    if not advertiser_id.isdigit():
        raise HTTPException(400, "千川账户 ID 格式不正确")
    try:
        source_result = qianchuan_service.plans(
            db,
            advertiser_id,
            q.strip(),
            scope=scope,
            force_refresh=refresh,
            cache_only=cached_only,
        )
        result = {**source_result, "counts": dict(source_result.get("counts") or {})}
        full_evidence = _qianchuan_full_plan_evidence(db, advertiser_id)
        items = []
        for raw_item in result.get("items") or []:
            item = dict(raw_item)
            evidence = full_evidence.get(str(item.get("id") or ""))
            is_full = evidence is not None
            item.update(
                {
                    "is_full": is_full,
                    "capacity_status": "full" if is_full else "available",
                    "capacity_message": (
                        "计划已满：千川最近一次推送明确返回素材数量超过上限，请改选其他计划"
                        if is_full
                        else ""
                    ),
                    "capacity_checked_at": evidence["checked_at"] if evidence else "",
                    "can_attach_video": bool(item.get("can_attach_video")),
                }
            )
            items.append(item)
        result["items"] = items
        result.setdefault("counts", {})["full"] = sum(1 for item in items if item["is_full"])
        return result
    except QianchuanError as error:
        raise HTTPException(502, str(error)) from error


@app.get("/api/qianchuan/plan-materials")
def qianchuan_plan_materials(
    advertiser_id: str = Query(min_length=1, max_length=80),
    plan_id: str = Query(min_length=1, max_length=80),
    start_date: str = Query(default="", max_length=10),
    end_date: str = Query(default="", max_length=10),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    """Read the actual video inventory and ROI2 data currently returned for one plan."""
    if not advertiser_id.isdigit() or not plan_id.isdigit():
        raise HTTPException(400, "千川账户或计划 ID 格式不正确")
    default_end = date.today() - timedelta(days=1)
    default_start = default_end - timedelta(days=6)
    try:
        selected_start = date.fromisoformat(start_date) if start_date else default_start
        selected_end = date.fromisoformat(end_date) if end_date else default_end
    except ValueError as error:
        raise HTTPException(400, "日期格式必须为 YYYY-MM-DD") from error
    if selected_start > selected_end:
        raise HTTPException(400, "开始日期不能晚于结束日期")
    if (selected_end - selected_start).days > 30:
        raise HTTPException(400, "计划素材数据一次最多读取 31 天")

    try:
        result = qianchuan_service.plan_material_videos(
            db,
            advertiser_id=advertiser_id,
            plan_id=plan_id,
            start_date=selected_start.isoformat(),
            end_date=selected_end.isoformat(),
            fields=list(PLAN_MATERIAL_REPORT_FIELDS),
        )
    except QianchuanError as error:
        raise HTTPException(502, str(error)) from error

    source_rows = result.get("videos") or []
    video_ids = {str(row.get("video_id") or "") for row in source_rows if row.get("video_id")}
    linked_by_video: dict[str, dict] = {}
    if video_ids:
        linked_rows = db.execute(
            select(QianchuanDelivery, Asset)
            .join(Asset, Asset.id == QianchuanDelivery.asset_id)
            .where(
                QianchuanDelivery.deleted_at.is_(None),
                QianchuanDelivery.advertiser_id == advertiser_id,
                QianchuanDelivery.plan_id == plan_id,
                QianchuanDelivery.platform_asset_id.in_(video_ids),
            )
            .order_by(QianchuanDelivery.updated_at.desc())
        ).all()
        for task, asset in linked_rows:
            linked_by_video.setdefault(
                task.platform_asset_id,
                {"asset_id": asset.id, "asset_name": asset.filename, "task_id": task.id},
            )

    items: list[dict] = []
    for row in source_rows:
        video_id = str(row.get("video_id") or "")
        metric_result = qianchuan_service.plan_material_metrics_from_result(
            {"videos": [row], "request_id": result.get("request_id", "")},
            plan_id=plan_id,
            video_id=video_id,
            start_date=selected_start.isoformat(),
            end_date=selected_end.isoformat(),
        )
        inactive = bool(row.get("needs_reactivation") or row.get("is_delete"))
        items.append(
            {
                "video_id": video_id,
                "material_id": str(row.get("material_id") or ""),
                "title": str(row.get("title") or ""),
                "audit_status": str(row.get("audit_status") or ""),
                "material_status": str(row.get("material_status") or ""),
                "is_delete": bool(row.get("is_delete")),
                "active": not inactive,
                "delivery_ready": bool(row.get("delivery_ready")),
                "needs_reactivation": bool(row.get("needs_reactivation")),
                "delivery_not_reason": row.get("delivery_not_reason") or [],
                "metrics": metric_result.get("metrics") or {},
                "has_data": bool(metric_result.get("has_data")),
                "linked_asset": linked_by_video.get(video_id),
            }
        )
    active_count = sum(1 for item in items if item["active"])
    return {
        "items": items,
        "total": len(items),
        "active_count": active_count,
        "inactive_count": len(items) - active_count,
        "start_date": selected_start.isoformat(),
        "end_date": selected_end.isoformat(),
        "pages": int(result.get("pages") or 1),
        "request_id": str(result.get("request_id") or ""),
        "source": "qianchuan/uni_promotion/ad/material/get",
        "read_at": _utc_iso(datetime.utcnow()),
        "message": "已回读千川计划中的视频及对应统计；系统不会自动删除或替换计划素材。",
    }


@app.post("/api/qianchuan/push")
def qianchuan_push(
    payload: QianchuanPushCreate,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    requested_asset_ids = list(
        dict.fromkeys([*(payload.asset_ids or []), *([payload.asset_id] if payload.asset_id else [])])
    )
    if not requested_asset_ids:
        raise HTTPException(400, "请至少选择 1 条视频素材")
    if len(requested_asset_ids) > 10:
        raise HTTPException(400, "一次最多推送 10 条视频素材")
    assets_by_id = {
        asset.id: asset
        for asset in db.scalars(select(Asset).where(Asset.id.in_(requested_asset_ids))).all()
    }
    if len(assets_by_id) != len(requested_asset_ids):
        raise HTTPException(404, "所选素材中存在已删除或不存在的记录，请刷新后重试")
    selected_assets = [assets_by_id[asset_id] for asset_id in requested_asset_ids]
    if any(asset.deleted_at is not None or asset.purged_at is not None for asset in selected_assets):
        raise HTTPException(404, "所选素材中存在已删除的记录，请刷新后重试")
    if any(asset.media_type != "video" for asset in selected_assets):
        raise HTTPException(400, "千川批量推送仅支持视频素材")
    _require_assets_review_approved(db, selected_assets)
    status = qianchuan_service.status(db)
    if not status["authorized"]:
        raise HTTPException(503, status["message"])

    creator_number = user_number(user)
    creator_name = str(user.get("realName") or user.get("name") or creator_number)[:120]
    resolved_targets: list[tuple[object, dict, str, str]] = []
    unique_targets: set[tuple[str, str]] = set()
    for target in payload.targets:
        advertiser_id = target.advertiser_id.strip()
        plan_id = target.plan_id.strip()
        if not advertiser_id.isdigit() or not plan_id.isdigit():
            raise HTTPException(400, "千川账户或计划 ID 格式不正确")
        identity = (advertiser_id, plan_id)
        if identity in unique_targets:
            continue
        try:
            resolved_plan = qianchuan_service.resolve_plan_target(
                db,
                advertiser_id=advertiser_id,
                plan_id=plan_id,
                plan_type=target.plan_type.strip(),
            )
        except QianchuanError as error:
            raise HTTPException(409, str(error)) from error
        if not resolved_plan.get("can_attach_video"):
            raise HTTPException(409, "所选计划只能查询，暂不支持接口直接投放，请选择乘方或全域推广计划")
        unique_targets.add(identity)
        resolved_targets.append((target, resolved_plan, advertiser_id, plan_id))
    if not resolved_targets:
        raise HTTPException(400, "没有可推送的目标")

    _record_push_preferences(
        db,
        creator_number,
        "qianchuan",
        [
            {
                "account_id": advertiser_id,
                "account_name": target.advertiser_name.strip(),
                "target_id": plan_id,
                "target_name": str((resolved_plan or {}).get("name") or target.plan_name).strip(),
                "target_type": str((resolved_plan or {}).get("plan_type") or target.plan_type).strip(),
            }
            for target, resolved_plan, advertiser_id, plan_id in resolved_targets
        ],
    )

    batch_id = str(uuid4())
    task_ids: list[str] = []
    new_task_ids: list[str] = []
    reused_task_ids: list[str] = []
    with _qianchuan_submission_lock:
        for asset in selected_assets:
            for target, resolved_plan, advertiser_id, plan_id in resolved_targets:
                existing = db.scalar(
                    select(QianchuanDelivery)
                    .where(
                        QianchuanDelivery.created_by_number == creator_number,
                        QianchuanDelivery.asset_id == asset.id,
                        QianchuanDelivery.advertiser_id == advertiser_id,
                        QianchuanDelivery.plan_id == plan_id,
                    )
                    .order_by(QianchuanDelivery.created_at.desc())
                    .limit(1)
                )
                if existing:
                    existing.deleted_at = None
                    existing.deleted_by_number = ""
                    existing.deleted_by_name = ""
                    task_ids.append(existing.id)
                    reused_task_ids.append(existing.id)
                    if existing.status in {"failed", "partial"}:
                        existing.batch_id = batch_id
                        existing.status = "pending"
                        existing.message = "等待重新投放；将先回读目标计划，避免重复添加"
                        _clear_qianchuan_error(existing)
                        existing.updated_at = datetime.utcnow()
                        new_task_ids.append(existing.id)
                    continue
                task_id = str(uuid4())
                task_ids.append(task_id)
                new_task_ids.append(task_id)
                db.add(
                    QianchuanDelivery(
                        id=task_id,
                        batch_id=batch_id,
                        asset_id=asset.id,
                        created_by_number=creator_number,
                        created_by_name=creator_name,
                        advertiser_id=advertiser_id,
                        advertiser_name=target.advertiser_name.strip(),
                        plan_id=plan_id,
                        plan_name=(resolved_plan or {}).get("name", target.plan_name.strip()),
                        plan_type=(resolved_plan or {}).get("plan_type", target.plan_type.strip()),
                        idempotency_key=f"{creator_number}|{asset.id}|{advertiser_id}|{plan_id}",
                        status="pending",
                        message="等待批量投放到所选计划",
                    )
                )
        if not task_ids:
            raise HTTPException(400, "没有可推送的目标")
        db.commit()
    return {
        "batch_id": batch_id,
        "task_ids": task_ids,
        "reused_task_ids": reused_task_ids,
        "asset_count": len(selected_assets),
        "target_count": len(resolved_targets),
        "new_task_count": len(new_task_ids),
        "status": "queued" if new_task_ids else "already_queued",
    }


@app.get("/api/workflow/receipt-candidates")
def workflow_receipt_candidates(
    platform: str = Query(pattern="^(qianchuan|wechat_channels)$"),
    asset_id: int = Query(ge=1),
    account_id: str = Query(min_length=1, max_length=120),
    plan_id: str = Query(default="", max_length=120),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Bounded exact-target lookup, not a retry, upload or advertising action."""
    require_module_access(user, "cloud-manager", db)
    if platform == "qianchuan" and not plan_id.strip():
        raise HTTPException(422, "千川核验需要明确计划编号")
    if platform == "wechat_channels" and plan_id:
        raise HTTPException(422, "视频号核验不接受千川计划编号")
    model = {"qianchuan": QianchuanDelivery, "wechat_channels": ChannelsDelivery}[platform]
    filters = [model.asset_id == asset_id, model.deleted_at.is_(None),
               Asset.deleted_at.is_(None), Asset.purged_at.is_(None)]
    if platform == "qianchuan":
        filters.extend([model.advertiser_id == account_id, model.plan_id == plan_id])
    else:
        filters.append(model.account_id == account_id)
    if not is_operation_admin(user):
        filters.append(model.created_by_number == user_number(user))
    rows = db.scalars(select(model).join(Asset, Asset.id == model.asset_id)
                      .where(*filters).order_by(model.created_at.desc(), model.id).limit(2)).all()
    # Never pick the latest or prefer a successful row when more than one matches.
    return {"items": [{"id": row.id} for row in rows], "ambiguous": len(rows) > 1,
            "read_only": True, "source": "cloud_manager_exact_target_lookup"}


@app.get("/api/workflow/receipts/{platform}/{task_id}")
def workflow_delivery_receipt(
    platform: str,
    task_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    """Exact, authorized readback for the hub. Never retry an external delivery here."""
    from .workflow_receipts import receipt_payload
    require_module_access(user, "cloud-manager", db)
    model = {"qianchuan": QianchuanDelivery, "wechat_channels": ChannelsDelivery}.get(platform)
    if model is None:
        raise HTTPException(404, "推送平台不存在")
    row = db.get(model, task_id)
    if (row is None or row.deleted_at is not None
            or (row.created_by_number != user_number(user) and not is_operation_admin(user))):
        raise HTTPException(404, "推送记录不存在或当前账号不可见")
    asset = db.get(Asset, row.asset_id)
    if asset is None or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "对应资产已不存在")
    return receipt_payload(row, platform)


@app.get("/api/qianchuan/tasks")
def qianchuan_tasks(
    asset_id: int | None = Query(default=None, ge=1),
    limit: int | None = Query(default=None, ge=1, le=300),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
    q: str = Query(default="", max_length=120),
    status: str = Query(
        default="all",
        pattern="^(all|pending|uploading|uploaded|binding|cancel_requested|cancelled|success|partial|failed|maintenance_hold)$",
    ),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    push_start_date: date | None = Query(default=None),
    push_end_date: date | None = Query(default=None),
):
    admin_view = is_operation_admin(user)
    viewer_number = user_number(user)
    filters = [QianchuanDelivery.deleted_at.is_(None)]
    _append_date_filters(filters, QianchuanDelivery.created_at, push_start_date, push_end_date)
    if not admin_view:
        filters.append(QianchuanDelivery.created_by_number == viewer_number)
    if asset_id:
        filters.append(QianchuanDelivery.asset_id == asset_id)
    selected_status = status if isinstance(status, str) else "all"
    if selected_status != "all":
        filters.append(QianchuanDelivery.status == selected_status)
    term = q.strip() if isinstance(q, str) else ""
    if term:
        filters.append(
            or_(
                Asset.filename.contains(term),
                QianchuanDelivery.advertiser_name.contains(term),
                QianchuanDelivery.advertiser_id.contains(term),
                QianchuanDelivery.plan_name.contains(term),
                QianchuanDelivery.plan_id.contains(term),
                QianchuanDelivery.platform_asset_id.contains(term),
                QianchuanDelivery.message.contains(term),
                QianchuanDelivery.error_message.contains(term),
                QianchuanDelivery.created_by_name.contains(term),
                QianchuanDelivery.created_by_number.contains(term),
            )
        )
    query = (
        select(QianchuanDelivery, Asset)
        .join(Asset, Asset.id == QianchuanDelivery.asset_id)
        .where(*filters)
    )
    total = db.scalar(
        select(func.count())
        .select_from(QianchuanDelivery)
        .join(Asset, Asset.id == QianchuanDelivery.asset_id)
        .where(*filters)
    ) or 0
    selected_page = page if isinstance(page, int) else 1
    if isinstance(page_size, int):
        if page_size not in {5, 10, 50, 100}:
            raise HTTPException(400, "每页条数只支持 5、10、50 或 100 条")
        selected_page_size = page_size
    else:
        selected_page_size = limit if isinstance(limit, int) else 10
    rows = db.execute(
        query.order_by(QianchuanDelivery.created_at.desc())
        .offset((selected_page - 1) * selected_page_size)
        .limit(selected_page_size)
    ).all()
    total_pages = max(1, (total + selected_page_size - 1) // selected_page_size)
    return {
        "items": [
            delivery_out(
                task,
                asset,
                _task_daily_summary(db, task),
                can_manage=task.created_by_number == viewer_number,
                can_cancel=bool(
                    (task.created_by_number == viewer_number or admin_view)
                    and (
                        task.status in {"pending", "uploading", "binding", "cancel_requested"}
                        or (task.status == "uploaded" and bool(task.plan_id) and not task.binding_verified_at)
                    )
                ),
            )
            for task, asset in rows
        ],
        "total": total,
        "page": selected_page,
        "page_size": selected_page_size,
        "total_pages": total_pages,
        "viewer_scope": "all" if admin_view else "personal",
    }


@app.post("/api/qianchuan/tasks/{task_id}/cancel")
def qianchuan_task_cancel(
    task_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    with _qianchuan_submission_lock:
        task = db.get(QianchuanDelivery, task_id)
        can_cancel = bool(
            task
            and task.deleted_at is None
            and (
                task.created_by_number == user_number(user)
                or is_operation_admin(user)
            )
        )
        if not can_cancel:
            raise HTTPException(404, "千川推送任务不存在")
        if task.status == "cancel_requested":
            return {"status": task.status, "task_id": task.id, "message": task.message}
        actor = user_name(user)
        if task.status == "pending":
            task.status = "cancelled"
            task.message = f"已由{actor}取消；任务尚未开始，未向千川发送视频"
        elif task.status in {"uploading", "binding"}:
            task.failure_stage = "plan_binding" if task.status == "binding" else "upload"
            task.status = "cancel_requested"
            task.message = (
                f"{actor}已请求取消；当前平台操作结束后停止后续步骤。"
                "若原视频已经进入千川素材库将保留，不会误删平台素材"
            )
        elif task.status == "uploaded" and task.plan_id and not task.binding_verified_at:
            task.status = "cancelled"
            task.message = (
                f"已由{actor}取消后续计划绑定；原视频已保留在千川账户素材库，"
                "未删除平台素材"
            )
        else:
            raise HTTPException(409, "平台动作已经完成或结果待核验，不能伪装成已取消")
        task.updated_at = datetime.utcnow()
        db.commit()
        return {"status": task.status, "task_id": task.id, "message": task.message}


@app.delete("/api/qianchuan/tasks/{task_id}")
def qianchuan_task_delete(
    task_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task = db.get(QianchuanDelivery, task_id)
    if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
        raise HTTPException(404, "推送记录不存在")
    if task.status in {"pending", "uploading", "binding", "cancel_requested", "maintenance_hold"}:
        raise HTTPException(409, "任务仍在处理中，完成后才能删除记录")
    task.deleted_at = datetime.utcnow()
    task.deleted_by_number = user_number(user)
    task.deleted_by_name = user_name(user)
    task.updated_at = datetime.utcnow()
    db.commit()
    return {
        "ok": True,
        "task_id": task.id,
        "message": "仅从你的列表中移除记录；千川计划、素材和审计数据均已保留",
    }


@app.post("/api/qianchuan/tasks/{task_id}/retry")
def qianchuan_task_retry(
    task_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    with _qianchuan_submission_lock:
        task = db.get(QianchuanDelivery, task_id)
        if (
            not task
            or task.created_by_number != user_number(user)
            or task.deleted_at is not None
        ):
            raise HTTPException(404, "推送任务不存在")
        if task.status not in {"failed", "partial"}:
            raise HTTPException(409, "只有失败或部分完成的任务可以重试")
        if task.status == "partial" and task.plan_type == "standard":
            raise HTTPException(409, "普通计划需在千川后台手动选择已上传素材，不能通过接口重复绑定")
        asset = db.get(Asset, task.asset_id)
        if not asset or asset.deleted_at is not None or asset.purged_at is not None:
            raise HTTPException(404, "原素材已不存在，无法重试")
        _require_assets_review_approved(db, [asset])
        _prepare_qianchuan_retry(task)
        db.commit()
    return {"status": "queued", "task_id": task.id}


_QIANCHUAN_TERMINAL_UPLOAD_TASK_ERRORS = {
    "async_upload_failed",
    "invalid_upload_task",
}


def _prepare_qianchuan_retry(task: QianchuanDelivery) -> None:
    """Resume the failed stage without discarding a completed original-video upload."""
    previous_stage = task.failure_stage
    previous_category = task.last_error_category
    if (
        previous_stage == "upload"
        and not task.platform_asset_id
        and previous_category in _QIANCHUAN_TERMINAL_UPLOAD_TASK_ERRORS
    ):
        # A terminal remote task cannot be polled successfully again. Starting a fresh
        # official upload task still sends the untouched original file.
        task.upload_task_id = ""
    task.status = "pending"
    if task.platform_asset_id:
        task.message = "等待继续加入失败的目标计划；复用已上传原视频，不会重复上传"
    elif task.upload_task_id:
        task.message = "等待续查已有原视频上传任务；不会压缩、转码或重复创建记录"
    else:
        task.message = "等待重新上传原视频；不压缩、不转码、不改变原文件"
    _clear_qianchuan_error(task)
    task.updated_at = datetime.utcnow()


@app.post("/api/qianchuan/batches/{batch_id}/retry-failed")
def qianchuan_batch_retry_failed(
    batch_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner_number = user_number(user)
    with _qianchuan_submission_lock:
        tasks = db.scalars(
            select(QianchuanDelivery).where(
                QianchuanDelivery.batch_id == batch_id,
                QianchuanDelivery.created_by_number == owner_number,
                QianchuanDelivery.deleted_at.is_(None),
            )
        ).all()
        if not tasks:
            raise HTTPException(404, "该批推送记录不存在")

        queued_task_ids: list[str] = []
        skipped: list[dict[str, str]] = []
        preserved_success_count = 0
        for task in tasks:
            if task.status == "success":
                preserved_success_count += 1
                continue
            if task.status not in {"failed", "partial"}:
                skipped.append({"task_id": task.id, "reason": "任务正在处理或无需重试"})
                continue
            if task.status == "partial" and task.plan_type == "standard":
                skipped.append({"task_id": task.id, "reason": "普通计划需在千川后台手动选择素材"})
                continue
            asset = db.get(Asset, task.asset_id)
            if not asset or asset.deleted_at is not None or asset.purged_at is not None:
                skipped.append({"task_id": task.id, "reason": "原素材已不存在"})
                continue
            try:
                _require_assets_review_approved(db, [asset])
            except HTTPException as error:
                skipped.append({"task_id": task.id, "reason": str(error.detail)})
                continue
            _prepare_qianchuan_retry(task)
            queued_task_ids.append(task.id)

        if not queued_task_ids:
            raise HTTPException(409, "该批次没有可自动重试的失败目标")
        db.commit()

    return {
        "status": "queued",
        "batch_id": batch_id,
        "queued_count": len(queued_task_ids),
        "queued_task_ids": queued_task_ids,
        "preserved_success_count": preserved_success_count,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "message": (
            f"已重新排队 {len(queued_task_ids)} 个失败目标；"
            f"保留 {preserved_success_count} 个已成功目标，不会重复投放"
        ),
    }


@app.post("/api/qianchuan/metrics/sync")
def qianchuan_metrics_sync(
    payload: QianchuanMetricsSync,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner_number = user_number(user)
    current = datetime.utcnow()
    with _qianchuan_metrics_schedule_lock:
        manual = _manual_metrics_status(db, owner_number, current)
        if manual["running"]:
            return {
                "status": "already_running",
                "task_ids": [],
                "message": f"已有 {manual['task_count']} 条数据正在同步，请等待完成",
                "next_available_at": manual["next_available_at"],
            }
        if not manual["available"]:
            return {
                "status": "cooldown",
                "task_ids": [],
                "message": f"为保护系统与千川接口，手动同步每 {settings.qianchuan_metrics_manual_cooldown_minutes} 分钟最多执行一次",
                "next_available_at": manual["next_available_at"],
            }
        stat_date = _metrics_local_now().date()
        selected_tasks = _daily_metric_tasks(
            db,
            owner_number=owner_number,
            requested_task_ids=payload.task_ids or None,
            limit=settings.qianchuan_metrics_manual_batch_size,
            stat_date=stat_date,
        )
        task_ids = [task.id for task in selected_tasks]
        if not task_ids:
            return {
                "status": "empty",
                "task_ids": [],
                "message": "当前没有已完成计划关联、可按日回流的数据记录",
                "next_available_at": None,
            }
        requested_at = _utc_iso(current)
        next_available_at = _utc_iso(
            current + timedelta(minutes=settings.qianchuan_metrics_manual_cooldown_minutes)
        )
        _set_meta(db, _manual_metrics_meta_key(owner_number, "requested_at"), requested_at)
        _set_meta(db, _manual_metrics_meta_key(owner_number, "state"), "queued")
        _set_meta(db, _manual_metrics_meta_key(owner_number, "task_count"), str(len(task_ids)))
        db.commit()
        background_tasks.add_task(
            run_qianchuan_manual_daily_metrics,
            task_ids,
            owner_number,
            stat_date,
        )
        return {
            "status": "queued",
            "task_ids": task_ids,
            "message": f"已进入今日数据刷新队列，本次覆盖 {len(task_ids)} 条个人记录",
            "next_available_at": next_available_at,
        }


_ADQ_WORKER_LIMIT = 2
_ADQ_SHARED_TARGET_ID = "__shared_library__"
_adq_job_slots = BoundedSemaphore(_ADQ_WORKER_LIMIT)
_adq_submission_lock = Lock()
_adq_metrics_slot = BoundedSemaphore(1)
_adq_metrics_schedule_lock = Lock()
_ADQ_METRICS_AUTO_LAST_RUN_KEY = "adq.metrics.auto.last_run_at"


def _require_adq_user_authorization(db: Session) -> dict:
    """Reject marketing-unit writes before creating or re-queuing delivery rows."""
    status = adq_service.user_authorization_status(db)
    if status["authorized"]:
        return status
    raise HTTPException(
        409,
        f"{status['message']}；完成认证后再推送营销单元，已上传到目标账户的原视频可继续复用",
    )


def _clear_adq_error(task: AdqDelivery) -> None:
    task.last_error_category = ""
    task.failure_stage = ""
    task.error_code = ""
    task.error_message = ""
    task.error_advice = ""


def _adq_error_advice(error: Exception, stage: str) -> str:
    category = getattr(error, "category", "")
    if category == "video_too_large":
        max_video_mb = adq_service.shared_library_config()["max_video_mb"]
        return f"腾讯 ADQ 单视频接口上限为 {max_video_mb}MB；不会压缩或转码，请更换符合平台限制的原文件。"
    if category == "unsupported_video":
        return "请使用腾讯 ADQ 支持的 MP4、MOV 或 AVI 原视频。"
    if category in {"network", "connect_timeout", "read_timeout", "connection_interrupted", "rate_limit"}:
        return "原视频和已完成步骤均会保留；请稍后点击重试，系统会先查重并从失败阶段继续。"
    if category == "user_token_required":
        return "腾讯 ADQ 要求操作人完成实名认证；管理员扫码认证后可复用已上传原视频继续重试。"
    if category == "template_unavailable":
        return "该营销单元没有可复用的视频创意模板；请先在 ADQ 后台建立一条可用视频创意。"
    if category == "binding_verification_pending":
        return "平台已受理但回读尚未确认；稍后重试会先回读，不会重复创建视频。"
    if category == "library_readback_pending" or stage == "library_readback":
        return "平台已返回视频 ID，但素材库尚未回读确认；稍后重试只会按视频 ID 复核，不会重复上传。"
    if category == "shared_authorization" or stage == "authorization":
        return "原视频已保留在统一素材源账户；稍后重试只会继续公司主体共享授权，不会重复上传。"
    if stage == "binding":
        return "原视频已保留在 ADQ 素材库；修正计划模板或权限后可仅重试关联步骤。"
    return "请展开详情查看平台原始错误；修正后可安全重试。"


def _record_adq_error(task: AdqDelivery, error: Exception, stage: str) -> None:
    entity_id = str(getattr(error, "entity_id", "") or "")
    category = str(getattr(error, "category", "platform"))[:50]
    if entity_id and category == "library_readback_pending":
        task.platform_asset_id = entity_id
    task.status = "partial" if task.platform_asset_id else "failed"
    task.failure_stage = stage
    task.last_error_category = category
    task.error_code = str(getattr(error, "code", ""))[:80]
    task.error_message = str(error)[:4000]
    task.error_advice = _adq_error_advice(error, stage)[:2000]
    task.message = task.error_message
    task.request_id = str(getattr(error, "request_id", "") or task.request_id)[:120]
    if entity_id and category != "library_readback_pending":
        task.dynamic_creative_id = entity_id
    task.updated_at = datetime.utcnow()


def claim_adq_tasks(limit: int) -> list[str]:
    if limit <= 0:
        return []
    with _adq_submission_lock, SessionLocal() as db:
        active_keys = set(
            db.execute(
                select(AdqDelivery.account_id, AdqDelivery.asset_id).where(
                    AdqDelivery.status.in_({"uploading", "binding"})
                )
            ).all()
        )
        candidates = db.scalars(
            select(AdqDelivery)
            .where(AdqDelivery.status == "pending", AdqDelivery.deleted_at.is_(None),
                   _recovery_asset_allowed(db, AdqDelivery.asset_id, "push"))
            .order_by(AdqDelivery.created_at.asc())
            .limit(max(limit * 10, 20))
        ).all()
        claimed: list[str] = []
        for task in candidates:
            key = (task.account_id, task.asset_id)
            if key in active_keys:
                continue
            active_keys.add(key)
            task.status = "uploading" if not task.platform_asset_id else "binding"
            if task.adgroup_id == _ADQ_SHARED_TARGET_ID:
                task.message = "正在上传原视频到 ADQ 统一素材源账户" if not task.platform_asset_id else "正在继续公司主体共享授权"
            else:
                task.message = "正在上传原视频到腾讯 ADQ" if not task.platform_asset_id else "正在继续关联目标营销单元"
            task.updated_at = datetime.utcnow()
            claimed.append(task.id)
            if len(claimed) >= limit:
                break
        if claimed:
            db.commit()
        return claimed


def run_adq_push(task_id: str) -> None:
    with _adq_job_slots, SessionLocal() as db:
        task = db.get(AdqDelivery, task_id)
        if not task or task.deleted_at is not None or task.status not in {"uploading", "binding", "pending"}:
            return
        if task.asset_id in _recovery_asset_hold_ids(db, "push"):
            return
        asset = db.get(Asset, task.asset_id)
        if not asset or asset.deleted_at is not None or asset.purged_at is not None:
            _record_adq_error(task, AdqError("原素材已不存在，无法继续推送", category="source_missing"), "upload")
            db.commit()
            return
        task.attempt_count += 1
        task.updated_at = datetime.utcnow()
        db.commit()
        stage = "upload"
        try:
            if not task.platform_asset_id:
                task.status = "uploading"
                task.message = "正在按原文件上传；不压缩、不转码"
                db.commit()
                uploaded = adq_service.upload_original_video(
                    db,
                    account_id=task.account_id,
                    object_key=asset.object_key,
                    filename=asset.filename,
                    size=asset.size,
                    etag=asset.etag,
                )
                task.platform_asset_id = str(uploaded["video_id"])
                task.cover_id = str(uploaded.get("cover_id") or "")
                task.request_id = str(uploaded.get("request_id") or "")
                task.binding_evidence = {
                    **(task.binding_evidence or {}),
                    "library_readback": uploaded.get("library_readback") or {},
                }
                task.message = (
                    "原视频已在 ADQ 统一素材源账户，正在授权公司主体"
                    if task.adgroup_id == _ADQ_SHARED_TARGET_ID
                    else "原视频已在 ADQ 素材库，正在关联营销单元"
                )
                task.status = "binding"
                _clear_adq_error(task)
                db.commit()

            stage = "library_readback"
            task.status = "binding"
            task.message = "正在按视频 ID 回读确认 ADQ 统一素材库"
            db.commit()
            readback = adq_service.verify_video_in_library(
                db,
                account_id=task.account_id,
                video_id=task.platform_asset_id,
            )
            task.cover_id = str(readback.get("cover_id") or task.cover_id)
            task.binding_evidence = {
                **(task.binding_evidence or {}),
                "library_readback": readback,
            }
            task.message = "已按视频 ID 回读确认素材存在，正在继续授权"
            _clear_adq_error(task)
            db.commit()

            if task.adgroup_id == _ADQ_SHARED_TARGET_ID:
                stage = "authorization"
                task.status = "binding"
                task.message = "正在确保公司主体下全部当前及未来广告账户可共享使用"
                db.commit()
                granted = adq_service.grant_all_videos_to_mdm(
                    db,
                    source_account_id=task.account_id,
                    mdm_id=settings.adq_shared_mdm_id,
                )
                task.binding_evidence = {
                    **(task.binding_evidence or {}),
                    **granted,
                    "video_id": task.platform_asset_id,
                    "verified": True,
                }
                task.binding_verified_at = datetime.utcnow()
                task.request_id = str(granted.get("request_id") or task.request_id)
                task.status = "success"
                task.message = "已回读确认视频存在于统一素材源账户，并完成公司主体全账户共享授权"
                task.metrics_status = "pending"
                task.metrics_message = "等待按视频 ID 回流素材库累计数据"
                task.updated_at = datetime.utcnow()
                _clear_adq_error(task)
                db.commit()
                return

            stage = "binding"
            task.status = "binding"
            task.message = "正在创建并回读目标营销单元的视频创意"
            db.commit()
            bound = adq_service.add_to_adgroup(
                db,
                account_id=task.account_id,
                adgroup_id=task.adgroup_id,
                source_dynamic_creative_id=task.source_dynamic_creative_id,
                video_id=task.platform_asset_id,
                cover_id=task.cover_id,
                filename=asset.filename,
            )
            task.dynamic_creative_id = str(bound.get("dynamic_creative_id") or "")
            task.binding_evidence = {
                **(task.binding_evidence or {}),
                **(bound.get("binding_evidence") or {}),
            }
            task.binding_verified_at = datetime.utcnow()
            task.request_id = str(bound.get("request_id") or task.request_id)
            task.status = "success"
            task.message = "已回读确认视频存在于目标 ADQ 营销单元"
            task.metrics_status = "pending"
            task.updated_at = datetime.utcnow()
            _clear_adq_error(task)
            db.commit()
        except AdqError as error:
            db.rollback()
            task = db.get(AdqDelivery, task_id)
            if task:
                _record_adq_error(task, error, stage)
                db.commit()
        except Exception as error:
            db.rollback()
            logger.exception("Unexpected ADQ push failure task_id=%s", task_id)
            task = db.get(AdqDelivery, task_id)
            if task:
                _record_adq_error(task, error, stage)
                db.commit()


def _prepare_adq_retry(task: AdqDelivery) -> None:
    task.status = "pending"
    if task.adgroup_id == _ADQ_SHARED_TARGET_ID:
        task.message = (
            "等待继续公司主体共享授权；复用已上传原视频"
            if task.platform_asset_id
            else "等待上传原视频到统一素材源账户；不压缩、不转码"
        )
    else:
        task.message = (
            "等待继续关联失败的营销单元；复用已上传原视频"
            if task.platform_asset_id
            else "等待重新上传原视频；不压缩、不转码，先查重后续传"
        )
    _clear_adq_error(task)
    task.updated_at = datetime.utcnow()


def _upsert_adq_daily(db: Session, task: AdqDelivery, item: dict) -> None:
    stat_date = str(item.get("date") or "")
    if not stat_date:
        return
    row = db.scalar(
        select(AdqMetricDaily).where(
            AdqMetricDaily.task_id == task.id,
            AdqMetricDaily.stat_date == stat_date,
        )
    )
    metrics = {
        key: item[key]
        for key in (
            "cost_yuan",
            "view_count",
            "valid_click_count",
            "view_click_rate",
            "order_amount_yuan",
            "order_roi",
            "order_24h_by_click_amount_yuan",
            "order_24h_by_click_roi",
            "order_net_amount_yuan",
            "order_net_roi",
            "impression",
            "click",
            "ctr",
        )
        if item.get(key) is not None
    }
    if not row:
        row = AdqMetricDaily(
            task_id=task.id,
            account_id=task.account_id,
            adgroup_id=task.adgroup_id,
            video_id=task.platform_asset_id,
            stat_date=stat_date,
        )
        db.add(row)
    row.metrics = metrics
    row.status = "success"
    row.has_data = True
    row.message = (
        "FanDo 根数据已按视频 ID 回流素材日级字段"
        if item.get("source") == "fandow_root"
        else "腾讯 ADQ 视频素材日报已返回新口径字段"
    )
    row.error_code = ""
    row.request_id = ""
    row.synced_at = datetime.utcnow()
    row.updated_at = datetime.utcnow()


def _apply_adq_metrics_result(db: Session, task: AdqDelivery, result: dict) -> None:
    task.metrics = result["metrics"]
    root_material_id = str(result.get("material_id") or "").strip()
    if root_material_id and result.get("material_id_status") == "verified":
        task.root_material_id = root_material_id
    task.metrics_status = "success" if result["has_data"] else "no_data"
    task.metrics_start_date = result["start_date"]
    task.metrics_end_date = result["end_date"]
    task.metrics_synced_at = datetime.utcnow()
    task.metrics_message = str(result.get("message") or (
        "已回流 ADQ 素材数据"
        if result["has_data"]
        else "平台在所选日期范围未返回该视频记录；这是暂无数据，不按 0 处理"
    ))[:2000]
    for item in result.get("daily") or []:
        _upsert_adq_daily(db, task, item)


def _sync_adq_task_metrics(
    db: Session,
    task: AdqDelivery,
    start_date: str | None = None,
    end_date: str | None = None,
    root_result: dict | None = None,
    root_error: str = "",
) -> None:
    if root_result and (root_result.get("has_data") or task.adgroup_id == _ADQ_SHARED_TARGET_ID):
        _apply_adq_metrics_result(db, task, root_result)
        task.updated_at = datetime.utcnow()
        return
    if task.adgroup_id == _ADQ_SHARED_TARGET_ID:
        task.metrics_status = "error" if root_error else "no_data"
        task.metrics_message = (
            f"ADQ 素材根数据回流暂未完成：{root_error[:1500]}"
            if root_error
            else "根数据在近 30 日未返回该视频记录；这是暂无数据，不按 0 处理"
        )
        task.metrics_synced_at = datetime.utcnow()
        task.updated_at = datetime.utcnow()
        return
    try:
        result = adq_service.material_metrics(
            db,
            account_id=task.account_id,
            adgroup_id=task.adgroup_id,
            video_id=task.platform_asset_id,
            start_date=start_date,
            end_date=end_date,
        )
        result["message"] = (
            "已由腾讯 ADQ 接口回流消耗、播放、有效点击、成交额与 ROI 数据"
            if result["has_data"]
            else "平台在所选日期范围未返回该视频记录；这是暂无数据，不按 0 处理"
        )
        _apply_adq_metrics_result(db, task, result)
    except AdqError as error:
        task.metrics_status = "error"
        task.metrics_message = str(error)[:2000]
        task.metrics_synced_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()


def run_adq_metrics(task_ids: list[str], start_date: str | None = None, end_date: str | None = None) -> None:
    with _adq_metrics_slot, SessionLocal() as db:
        tasks = [db.get(AdqDelivery, task_id) for task_id in task_ids]
        tasks = [
            task for task in tasks
            if task and task.status == "success" and task.platform_asset_id and task.deleted_at is None
        ]
        root_results: dict[str, dict] = {}
        root_error = ""
        if tasks:
            try:
                root_results = root_feedback_service.adq_material_metrics_batch(
                    [task.platform_asset_id for task in tasks],
                    start_date=start_date,
                    end_date=end_date,
                )
            except RootFeedbackError as error:
                root_error = str(error)
        for task in tasks:
            _sync_adq_task_metrics(
                db,
                task,
                start_date,
                end_date,
                root_result=root_results.get(task.platform_asset_id),
                root_error=root_error,
            )
            db.commit()


def run_adq_auto_metrics_if_due(now: datetime | None = None) -> dict:
    current = now or datetime.utcnow()
    with _adq_metrics_schedule_lock, SessionLocal() as db:
        last_run = _parse_utc_iso(_meta(db, _ADQ_METRICS_AUTO_LAST_RUN_KEY))
        interval = timedelta(minutes=settings.adq_metrics_interval_minutes)
        if last_run and current < last_run + interval:
            return {"status": "not_due", "task_ids": []}
        stale_before = current - timedelta(minutes=settings.adq_metrics_record_min_age_minutes)
        tasks = db.scalars(
            select(AdqDelivery)
            .where(
                AdqDelivery.status == "success",
                AdqDelivery.deleted_at.is_(None),
                AdqDelivery.platform_asset_id != "",
                or_(AdqDelivery.metrics_synced_at.is_(None), AdqDelivery.metrics_synced_at <= stale_before),
            )
            .order_by(AdqDelivery.metrics_synced_at.asc(), AdqDelivery.created_at.asc())
            .limit(settings.adq_metrics_auto_batch_size)
        ).all()
        task_ids = [task.id for task in tasks]
        _set_meta(db, _ADQ_METRICS_AUTO_LAST_RUN_KEY, _utc_iso(current))
        db.commit()
    if task_ids:
        run_adq_metrics(task_ids)
    return {"status": "completed", "task_ids": task_ids}


async def adq_metrics_loop() -> None:
    await asyncio.sleep(settings.adq_metrics_startup_delay_seconds)
    while True:
        try:
            await asyncio.to_thread(run_adq_auto_metrics_if_due)
        except Exception as error:
            with SessionLocal() as db:
                _set_meta(db, "adq.metrics.auto.last_error", str(error)[:1000])
                db.commit()
        await asyncio.sleep(60)


@app.get("/api/adq/status")
def adq_status(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    status = adq_service.validated_status(db)
    if status["authorized"]:
        try:
            status["account"] = adq_service.account(db)
        except AdqError as error:
            status["authorized"] = False
            status["message"] = str(error)
    status["can_authorize_user"] = is_asset_admin(user)
    metric_accounts = []
    if settings.adq_shared_source_account_id:
        metric_accounts.append({
            "account_id": settings.adq_shared_source_account_id,
            "account_name": f"ADQ 统一素材源账户 {settings.adq_shared_source_account_id}",
        })
    if status.get("account_id") and status["account_id"] != settings.adq_shared_source_account_id:
        metric_accounts.append({
            "account_id": status["account_id"],
            "account_name": str((status.get("account") or {}).get("account_name") or f"ADQ 账户 {status['account_id']}"),
        })
    status["account_metric_accounts"] = metric_accounts
    status["metrics_policy"] = {
        "interval_minutes": settings.adq_metrics_interval_minutes,
        "auto_batch_size": settings.adq_metrics_auto_batch_size,
        "manual_batch_size": settings.adq_metrics_manual_batch_size,
        "manual_cooldown_minutes": settings.adq_metrics_manual_cooldown_minutes,
    }
    return status


@app.get("/api/adq/user-authorization/start")
def adq_user_authorization_start(
    request: Request,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if not is_asset_admin(user):
        raise HTTPException(403, "仅素材管理员可以更新 ADQ 操作人实名认证")
    callback_url = external_app_url(request).rstrip("/") + "/api/adq/user-authorization/callback"
    try:
        return {
            "url": adq_service.user_authorize_url(
                db,
                redirect_uri=callback_url,
                owner_number=user_number(user),
            )
        }
    except AdqError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/adq/user-authorization/callback")
def adq_user_authorization_callback(
    state: str = "",
    user_status: int = 0,
    user_token: str = "",
    expire_time: str = "",
    db: Session = Depends(get_db),
):
    try:
        adq_service.complete_user_authorization(
            db,
            state=state,
            user_status=user_status,
            user_token=user_token,
            expire_time=expire_time,
        )
        return RedirectResponse(oauth_result_url('adq_user', True), status_code=303)
    except AdqError:
        return RedirectResponse(oauth_result_url('adq_user', False), status_code=303)


@app.get("/api/adq/shared-library/config")
def adq_shared_library_config(db: Session = Depends(get_db), _user: dict = Depends(require_user)):
    result = adq_service.shared_library_config()
    result["authorized"] = adq_service.status(db)["authorized"]
    return result


@app.post("/api/adq/shared-library/upload")
def adq_shared_library_upload(
    payload: AdqSharedUploadCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    config = adq_service.shared_library_config()
    if not config["configured"]:
        raise HTTPException(503, "腾讯 ADQ 统一素材源账户或主体 MDM ID 尚未配置")
    status = adq_service.status(db)
    if not status["authorized"]:
        raise HTTPException(503, status["message"])
    requested_asset_ids = list(dict.fromkeys(payload.asset_ids))
    assets = db.scalars(select(Asset).where(Asset.id.in_(requested_asset_ids))).all()
    assets_by_id = {asset.id: asset for asset in assets}
    if len(assets_by_id) != len(requested_asset_ids):
        raise HTTPException(404, "所选素材中存在已删除或不存在的记录")
    selected_assets = [assets_by_id[asset_id] for asset_id in requested_asset_ids]
    if any(asset.deleted_at is not None or asset.purged_at is not None for asset in selected_assets):
        raise HTTPException(404, "所选素材中存在已删除的记录")
    if any(asset.media_type != "video" for asset in selected_assets):
        raise HTTPException(400, "腾讯 ADQ 全账户共享仅支持视频素材")
    _require_assets_review_approved(db, selected_assets)
    max_bytes = int(config["max_video_mb"]) * 1024 * 1024
    oversized = [asset.filename for asset in selected_assets if asset.size <= 0 or asset.size > max_bytes]
    if oversized:
        raise HTTPException(
            400,
            f"腾讯 ADQ 单视频上限为 {config['max_video_mb']}MB；不会压缩或转码：{', '.join(oversized[:3])}",
        )

    owner_number = user_number(user)
    owner_name = user_name(user)
    source_account_id = str(config["source_account_id"])
    batch_id = str(uuid4())
    task_ids: list[str] = []
    new_task_ids: list[str] = []
    reused_task_ids: list[str] = []
    with _adq_submission_lock:
        for asset in selected_assets:
            existing = db.scalar(
                select(AdqDelivery)
                .where(
                    AdqDelivery.created_by_number == owner_number,
                    AdqDelivery.asset_id == asset.id,
                    AdqDelivery.account_id == source_account_id,
                    AdqDelivery.adgroup_id == _ADQ_SHARED_TARGET_ID,
                )
                .order_by(AdqDelivery.created_at.desc())
                .limit(1)
            )
            if existing:
                existing.deleted_at = None
                existing.deleted_by_number = ""
                existing.deleted_by_name = ""
                task_ids.append(existing.id)
                reused_task_ids.append(existing.id)
                if existing.status in {"failed", "partial"}:
                    existing.batch_id = batch_id
                    _prepare_adq_retry(existing)
                    new_task_ids.append(existing.id)
                continue
            task_id = str(uuid4())
            task_ids.append(task_id)
            new_task_ids.append(task_id)
            db.add(AdqDelivery(
                id=task_id,
                batch_id=batch_id,
                asset_id=asset.id,
                created_by_number=owner_number,
                created_by_name=owner_name,
                account_id=source_account_id,
                account_name=f"ADQ 统一素材源账户 {source_account_id}",
                adgroup_id=_ADQ_SHARED_TARGET_ID,
                adgroup_name="公司主体全账户共享素材库",
                idempotency_key=f"{owner_number}|{asset.id}|{source_account_id}|{_ADQ_SHARED_TARGET_ID}",
                status="pending",
                message="等待上传原视频并完成公司主体全账户共享授权",
                metrics_status="pending",
                metrics_message="上传成功后按视频 ID 回流素材库累计数据",
            ))
        db.commit()
    return {
        "batch_id": batch_id,
        "task_ids": task_ids,
        "reused_task_ids": reused_task_ids,
        "asset_count": len(selected_assets),
        "new_task_count": len(new_task_ids),
        "status": "queued" if new_task_ids else "already_queued",
        "message": (
            f"已创建 {len(new_task_ids)} 条全账户共享上传任务"
            if new_task_ids
            else "这些素材已经存在共享上传记录，没有重复创建"
        ),
    }


@app.get("/api/adq/accounts")
def adq_accounts(db: Session = Depends(get_db), _user: dict = Depends(require_user)):
    try:
        return adq_service.accounts(db)
    except AdqError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/adq/hierarchy")
def adq_hierarchy(
    q: str = Query(default="", max_length=120),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    try:
        return adq_service.business_hierarchy(db, q=q, fresh=refresh)
    except AdqError as error:
        raise HTTPException(503 if error.retryable else 409, str(error)) from error


@app.get("/api/adq/adgroups")
def adq_adgroups(
    account_id: str = Query(min_length=1, max_length=80),
    q: str = Query(default="", max_length=120),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    try:
        return adq_service.adgroups(db, account_id=account_id, q=q)
    except AdqError as error:
        raise HTTPException(503 if error.retryable else 409, str(error)) from error


@app.get("/api/adq/account-material-metrics")
def adq_account_material_metrics(
    account_id: str = Query(min_length=1, max_length=80),
    start_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    end_date: str | None = Query(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    q: str = Query(default="", max_length=120),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    db: Session = Depends(get_db),
    _user: dict = Depends(require_user),
):
    try:
        result = adq_service.account_material_metrics(
            db,
            account_id=account_id,
            start_date=start_date,
            end_date=end_date,
            q=q,
            page=page,
            page_size=page_size,
        )
        video_ids = [str(item.get("video_id") or "") for item in result.get("videos", [])]
        if video_ids:
            rows = db.execute(
                select(AdqDelivery, Asset)
                .join(Asset, Asset.id == AdqDelivery.asset_id)
                .where(AdqDelivery.platform_asset_id.in_(video_ids))
                .order_by(AdqDelivery.updated_at.desc())
            ).all()
            names: dict[str, tuple[int, str]] = {}
            for delivery, asset in rows:
                names.setdefault(delivery.platform_asset_id, (asset.id, asset.filename))
            for item in result.get("videos", []):
                asset_id, name = names.get(str(item.get("video_id") or ""), (None, ""))
                item["asset_id"] = asset_id
                item["video_name"] = name or f"视频 ID {item.get('video_id')}"
        return result
    except (AdqError, ValueError) as error:
        raise HTTPException(409, str(error)) from error


@app.post("/api/adq/push")
def adq_push(
    payload: AdqPushCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    requested_asset_ids = list(dict.fromkeys(([payload.asset_id] if payload.asset_id else []) + payload.asset_ids))
    if not requested_asset_ids:
        raise HTTPException(400, "请至少选择一条视频素材")
    if len(requested_asset_ids) > 10:
        raise HTTPException(400, "一次最多推送 10 条视频素材")
    assets = db.scalars(select(Asset).where(Asset.id.in_(requested_asset_ids))).all()
    assets_by_id = {asset.id: asset for asset in assets}
    if len(assets_by_id) != len(requested_asset_ids):
        raise HTTPException(404, "所选素材中存在已删除或不存在的记录")
    selected_assets = [assets_by_id[asset_id] for asset_id in requested_asset_ids]
    if any(asset.deleted_at is not None or asset.purged_at is not None for asset in selected_assets):
        raise HTTPException(404, "所选素材中存在已删除的记录")
    if any(asset.media_type != "video" for asset in selected_assets):
        raise HTTPException(400, "腾讯 ADQ 批量推送仅支持视频素材")
    _require_assets_review_approved(db, selected_assets)
    authorization = adq_service.status(db)
    if not authorization["authorized"]:
        raise HTTPException(503, authorization["message"])
    if not payload.targets:
        raise HTTPException(400, "请至少选择一个 ADQ 营销单元")
    _require_adq_user_authorization(db)
    resolved_targets: list[tuple[object, dict]] = []
    verified_accounts: dict[str, dict] = {}
    unique_targets: set[tuple[str, str]] = set()
    for target in payload.targets:
        account_id = target.account_id.strip()
        adgroup_id = target.adgroup_id.strip()
        if not account_id.isdigit() or not adgroup_id.isdigit():
            raise HTTPException(400, "ADQ 账户或营销单元 ID 格式不正确")
        identity = (account_id, adgroup_id)
        if identity in unique_targets:
            continue
        try:
            if account_id not in verified_accounts:
                verified_accounts[account_id] = adq_service.account(db, account_id=account_id)
            resolved = adq_service.resolve_target(
                db,
                account_id=account_id,
                adgroup_id=adgroup_id,
                source_dynamic_creative_id=target.source_dynamic_creative_id.strip(),
            )
        except AdqError as error:
            raise HTTPException(409, str(error)) from error
        unique_targets.add(identity)
        resolved_targets.append((target, resolved))
    if not resolved_targets:
        raise HTTPException(400, "没有可推送的 ADQ 营销单元")

    owner_number = user_number(user)
    owner_name = user_name(user)
    _record_push_preferences(
        db,
        owner_number,
        "adq",
        [
            {
                "account_id": target.account_id.strip(),
                "account_name": str(
                    verified_accounts[target.account_id.strip()].get("account_name") or target.account_name
                ).strip(),
                "target_id": target.adgroup_id.strip(),
                "target_name": str(resolved.get("adgroup_name") or target.adgroup_name).strip(),
                "target_type": "adgroup",
            }
            for target, resolved in resolved_targets
        ],
    )
    batch_id = str(uuid4())
    task_ids: list[str] = []
    new_task_ids: list[str] = []
    reused_task_ids: list[str] = []
    with _adq_submission_lock:
        for asset in selected_assets:
            for target, resolved in resolved_targets:
                existing = db.scalar(
                    select(AdqDelivery)
                    .where(
                        AdqDelivery.created_by_number == owner_number,
                        AdqDelivery.asset_id == asset.id,
                        AdqDelivery.account_id == target.account_id.strip(),
                        AdqDelivery.adgroup_id == target.adgroup_id.strip(),
                    )
                    .order_by(AdqDelivery.created_at.desc())
                    .limit(1)
                )
                if existing:
                    existing.deleted_at = None
                    existing.deleted_by_number = ""
                    existing.deleted_by_name = ""
                    task_ids.append(existing.id)
                    reused_task_ids.append(existing.id)
                    if existing.status in {"failed", "partial"}:
                        existing.batch_id = batch_id
                        _prepare_adq_retry(existing)
                        new_task_ids.append(existing.id)
                    continue
                task_id = str(uuid4())
                task_ids.append(task_id)
                new_task_ids.append(task_id)
                db.add(AdqDelivery(
                    id=task_id,
                    batch_id=batch_id,
                    asset_id=asset.id,
                    created_by_number=owner_number,
                    created_by_name=owner_name,
                    account_id=target.account_id.strip(),
                    account_name=str(
                        verified_accounts[target.account_id.strip()].get("account_name")
                        or target.account_name.strip()
                    ),
                    adgroup_id=target.adgroup_id.strip(),
                    adgroup_name=str(resolved.get("adgroup_name") or target.adgroup_name.strip()),
                    source_dynamic_creative_id=str(resolved.get("source_dynamic_creative_id") or ""),
                    idempotency_key=f"{owner_number}|{asset.id}|{target.account_id.strip()}|{target.adgroup_id.strip()}",
                    status="pending",
                    message="等待批量推送到所选腾讯 ADQ 营销单元",
                ))
        db.commit()
    return {
        "batch_id": batch_id,
        "task_ids": task_ids,
        "reused_task_ids": reused_task_ids,
        "asset_count": len(selected_assets),
        "target_count": len(resolved_targets),
        "new_task_count": len(new_task_ids),
        "status": "queued" if new_task_ids else "already_queued",
    }


@app.get("/api/adq/tasks")
def adq_tasks(
    asset_id: int | None = Query(default=None, ge=1),
    q: str = Query(default="", max_length=120),
    status: str = Query(default="all", pattern="^(all|pending|uploading|binding|success|partial|failed)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    library_only: bool = Query(default=False),
    push_start_date: date | None = Query(default=None),
    push_end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    selected_asset_id = asset_id if isinstance(asset_id, int) else None
    selected_q = q if isinstance(q, str) else ""
    selected_status = status if isinstance(status, str) else "all"
    selected_page = page if isinstance(page, int) else 1
    selected_page_size = page_size if isinstance(page_size, int) else 10
    selected_library_only = library_only if isinstance(library_only, bool) else False
    if selected_page_size not in {5, 10}:
        raise HTTPException(400, "每页条数只支持 5 条或 10 条")
    admin_view = is_operation_admin(user)
    viewer_number = user_number(user)
    filters = [AdqDelivery.deleted_at.is_(None)]
    _append_date_filters(filters, AdqDelivery.created_at, push_start_date, push_end_date)
    if selected_library_only:
        filters.append(AdqDelivery.adgroup_id == _ADQ_SHARED_TARGET_ID)
    if not admin_view:
        filters.append(AdqDelivery.created_by_number == viewer_number)
    if selected_asset_id:
        filters.append(AdqDelivery.asset_id == selected_asset_id)
    if selected_status != "all":
        filters.append(AdqDelivery.status == selected_status)
    term = selected_q.strip()
    if term:
        filters.append(or_(
            Asset.filename.contains(term),
            AdqDelivery.account_name.contains(term),
            AdqDelivery.account_id.contains(term),
            AdqDelivery.adgroup_name.contains(term),
            AdqDelivery.adgroup_id.contains(term),
            AdqDelivery.platform_asset_id.contains(term),
            AdqDelivery.dynamic_creative_id.contains(term),
            AdqDelivery.message.contains(term),
            AdqDelivery.error_message.contains(term),
            AdqDelivery.created_by_name.contains(term),
            AdqDelivery.created_by_number.contains(term),
        ))
    total = db.scalar(
        select(func.count())
        .select_from(AdqDelivery)
        .join(Asset, Asset.id == AdqDelivery.asset_id)
        .where(*filters)
    ) or 0
    rows = db.execute(
        select(AdqDelivery, Asset)
        .join(Asset, Asset.id == AdqDelivery.asset_id)
        .where(*filters)
        .order_by(AdqDelivery.created_at.desc())
        .offset((selected_page - 1) * selected_page_size)
        .limit(selected_page_size)
    ).all()
    items = [
        adq_delivery_out(db, task, asset, can_manage=task.created_by_number == viewer_number)
        for task, asset in rows
    ]
    if selected_library_only:
        for item in items:
            video_id = str(item.get("platform_asset_id") or "").strip()
            if item.get("status") != "success" or not video_id:
                continue
            try:
                report = adq_service.account_material_metrics(
                    db,
                    account_id=str(item.get("account_id") or settings.adq_shared_source_account_id),
                    q=video_id,
                    page=1,
                    page_size=100,
                )
                matched = next(
                    (video for video in report.get("videos") or [] if str(video.get("video_id") or "") == video_id),
                    None,
                )
                item["metrics_start_date"] = report.get("start_date") or ""
                item["metrics_end_date"] = report.get("end_date") or ""
                if matched:
                    item["metrics"] = matched.get("metrics") or {}
                    item["metrics_status"] = "success"
                    item["metrics_message"] = (
                        f"已按视频 ID 回流 {report.get('start_date')}—{report.get('end_date')} 累计数据"
                    )
                else:
                    item["metrics"] = {}
                    item["metrics_status"] = "no_data"
                    item["metrics_message"] = "平台在近 30 日素材报表中未返回该视频；保持待核验，不按 0 处理"
            except AdqError as error:
                item["metrics"] = {}
                item["metrics_status"] = "error"
                item["metrics_message"] = f"素材数据暂未回流：{str(error)}"
    return {
        "items": items,
        "total": total,
        "page": selected_page,
        "page_size": selected_page_size,
        "total_pages": max(1, (total + selected_page_size - 1) // selected_page_size),
        "viewer_scope": "all" if admin_view else "personal",
    }


@app.delete("/api/adq/tasks/{task_id}")
def adq_task_delete(task_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    task = db.get(AdqDelivery, task_id)
    if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
        raise HTTPException(404, "ADQ 推送记录不存在")
    if task.status in {"pending", "uploading", "binding"}:
        raise HTTPException(409, "任务仍在处理中，完成后才能删除记录")
    task.deleted_at = datetime.utcnow()
    task.deleted_by_number = user_number(user)
    task.deleted_by_name = user_name(user)
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task.id, "message": "仅从个人列表移除记录；ADQ 素材和创意不受影响"}


@app.post("/api/adq/tasks/{task_id}/retry")
def adq_task_retry(task_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    with _adq_submission_lock:
        task = db.get(AdqDelivery, task_id)
        if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
            raise HTTPException(404, "ADQ 推送任务不存在")
        if task.status not in {"failed", "partial"}:
            raise HTTPException(409, "只有失败或部分完成的任务可以重试")
        asset = db.get(Asset, task.asset_id)
        if not asset or asset.deleted_at is not None or asset.purged_at is not None:
            raise HTTPException(404, "原素材已不存在，无法重试")
        _require_assets_review_approved(db, [asset])
        if task.adgroup_id != _ADQ_SHARED_TARGET_ID:
            _require_adq_user_authorization(db)
        _prepare_adq_retry(task)
        db.commit()
    return {"status": "queued", "task_id": task.id}


@app.post("/api/adq/batches/{batch_id}/retry-failed")
def adq_batch_retry_failed(batch_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    with _adq_submission_lock:
        tasks = db.scalars(select(AdqDelivery).where(
            AdqDelivery.batch_id == batch_id,
            AdqDelivery.created_by_number == user_number(user),
            AdqDelivery.deleted_at.is_(None),
        )).all()
        if not tasks:
            raise HTTPException(404, "该批 ADQ 推送记录不存在")
        has_retryable_direct_task = any(
            task.status in {"failed", "partial"}
            and task.adgroup_id != _ADQ_SHARED_TARGET_ID
            and (asset := db.get(Asset, task.asset_id)) is not None
            and asset.deleted_at is None
            and asset.purged_at is None
            for task in tasks
        )
        if has_retryable_direct_task:
            _require_adq_user_authorization(db)
        queued: list[str] = []
        skipped: list[dict] = []
        preserved_success_count = 0
        for task in tasks:
            if task.status == "success":
                preserved_success_count += 1
                continue
            if task.status not in {"failed", "partial"}:
                skipped.append({"task_id": task.id, "reason": "任务正在处理或无需重试"})
                continue
            asset = db.get(Asset, task.asset_id)
            if not asset or asset.deleted_at is not None or asset.purged_at is not None:
                skipped.append({"task_id": task.id, "reason": "原素材已不存在"})
                continue
            try:
                _require_assets_review_approved(db, [asset])
            except HTTPException as error:
                skipped.append({"task_id": task.id, "reason": str(error.detail)})
                continue
            _prepare_adq_retry(task)
            queued.append(task.id)
        if not queued:
            raise HTTPException(409, "该批次没有可自动重试的失败目标")
        db.commit()
    return {
        "status": "queued",
        "batch_id": batch_id,
        "queued_count": len(queued),
        "queued_task_ids": queued,
        "preserved_success_count": preserved_success_count,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "message": f"已重排 {len(queued)} 个失败目标；保留 {preserved_success_count} 个成功目标",
    }


@app.post("/api/adq/metrics/sync")
def adq_metrics_sync(
    payload: AdqMetricsSync,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner = user_number(user)
    current = datetime.utcnow()
    key = f"adq.metrics.manual.{owner}.requested_at"
    last = _parse_utc_iso(_meta(db, key))
    if last and current < last + timedelta(minutes=settings.adq_metrics_manual_cooldown_minutes):
        return {
            "status": "cooldown",
            "task_ids": [],
            "message": f"为保护系统与 ADQ 接口，手动同步每 {settings.adq_metrics_manual_cooldown_minutes} 分钟最多一次",
            "next_available_at": _utc_iso(last + timedelta(minutes=settings.adq_metrics_manual_cooldown_minutes)),
        }
    filters = [
        AdqDelivery.created_by_number == owner,
        AdqDelivery.deleted_at.is_(None),
        AdqDelivery.status == "success",
        AdqDelivery.platform_asset_id != "",
        AdqDelivery.adgroup_id != _ADQ_SHARED_TARGET_ID,
    ]
    if payload.task_ids:
        filters.append(AdqDelivery.id.in_(payload.task_ids))
    tasks = db.scalars(
        select(AdqDelivery)
        .where(*filters)
        .order_by(AdqDelivery.metrics_synced_at.asc(), AdqDelivery.created_at.asc())
        .limit(settings.adq_metrics_manual_batch_size)
    ).all()
    task_ids = [task.id for task in tasks]
    if not task_ids:
        return {"status": "empty", "task_ids": [], "message": "当前没有可回流的个人 ADQ 推送记录"}
    _set_meta(db, key, _utc_iso(current))
    db.commit()
    background_tasks.add_task(run_adq_metrics, task_ids, payload.start_date, payload.end_date)
    return {
        "status": "queued",
        "task_ids": task_ids,
        "message": f"已进入 ADQ 数据刷新队列，本次覆盖 {len(task_ids)} 条个人记录",
        "next_available_at": _utc_iso(current + timedelta(minutes=settings.adq_metrics_manual_cooldown_minutes)),
    }


_channels_submission_lock = Lock()

_CHANNELS_ANNOTATION_LABELS = {
    "none": "无需标注",
    "ai_generated": "含AI生成内容",
    "fictional": "内容为虚构剧情，仅供娱乐",
    "personal_opinion": "个人观点，仅供参考",
    "marketing_ad": "内容包含营销广告",
    "self_shot": "内容为自行拍摄",
    "repost": "内容为转载",
}


def _default_channels_annotation(asset: Asset) -> str:
    """Conservatively preselect the platform AI marker for known AI assets."""
    haystack = " ".join([
        asset.filename or "",
        asset.asset_subtype or "",
        asset.content_type or "",
        asset.material_description or "",
        " ".join(asset.tags or []),
    ])
    return "ai_generated" if re.search(
        r"(?i)(?:aigc|数字人|人工智能生成|ai生成|ai原创|ai混剪|(?:^|[^a-z])ai(?:[^a-z]|$))",
        haystack,
    ) else "none"


def channels_promotion_order_out(order: ChannelsPromotionOrder | None) -> dict | None:
    if order is None:
        return None
    return {
        "id": order.id,
        "delivery_id": order.delivery_id,
        "account_id": order.account_id,
        "promotion_target": order.promotion_target,
        "budget_wecoin": order.budget_wecoin,
        "quoted_wecoin": order.quoted_wecoin,
        "duration_hours": order.duration_hours,
        "order_name": order.order_name,
        "status": order.status,
        "promotion_id": order.promotion_id,
        "cost_wecoin": order.cost_wecoin,
        "error_code": order.error_code,
        "error_message": order.error_message,
        "created_at": order.created_at.isoformat() + "Z",
        "updated_at": order.updated_at.isoformat() + "Z",
    }


def channels_promotion_account_out(row: ChannelsPromotionAccount | None) -> dict:
    now = datetime.utcnow()
    user_type = int(row.user_type or 0) if row else 0
    is_enterprise = user_type == 3
    authorized = bool(
        row
        and row.status == "active"
        and row.expires_at
        and row.expires_at > now
        and row.auth_file
        and is_enterprise
    )
    message = row.message if row else "尚未进行独立加热授权"
    status = row.status if row else "not_authorized"
    if row and not is_enterprise:
        actual = row.nickname or "未知账号"
        account_type = channels_promotion_service.user_type_label(user_type or None)
        message = f"当前加热授权为{account_type}“{actual}”，请重新扫码并在手机端选择企业账户"
        status = "enterprise_required"
    return {
        "authorized": authorized,
        "status": status,
        "message": message,
        "nickname": row.nickname if row else "",
        "user_type": user_type or None,
        "account_type": channels_promotion_service.user_type_label(user_type or None) if row else "",
        "is_enterprise": is_enterprise,
        "balance": row.balance_wecoin if row else None,
        "authorized_at": row.authorized_at.isoformat() + "Z" if row and row.authorized_at else None,
        "expires_at": row.expires_at.isoformat() + "Z" if row and row.expires_at else None,
    }


def channels_delivery_out(
    task: ChannelsDelivery,
    asset: Asset | None = None,
    can_manage: bool = True,
    can_cancel: bool = False,
    latest_promotion: ChannelsPromotionOrder | None = None,
) -> dict:
    from .channels_credentials import public_channels_message
    return {
        "id": task.id,
        "batch_id": task.batch_id,
        "asset_id": task.asset_id,
        "asset_name": asset.filename if asset else "",
        "created_by_number": task.created_by_number,
        "created_by_name": task.created_by_name,
        "can_manage": can_manage,
        "can_cancel": can_cancel,
        "can_edit": bool(can_manage and _channels_task_editable(task)),
        "account_id": task.account_id,
        "account_name": task.account_name,
        "title": task.title,
        "description": task.description,
        "tags": task.tags or [],
        "product_id": task.product_id,
        "product_name": task.product_name,
        "cover_filename": task.cover_filename,
        "cover_status": (task.cover_status or "unverified") if task.cover_object_key else "not_requested",
        "cover_message": task.cover_message or ("自定义封面尚未完成图片一致性核验" if task.cover_object_key else "使用视频默认封面"),
        "cover_url": oss_service.url_for(task.cover_object_key) if task.cover_object_key else "",
        "video_annotation": task.video_annotation or "none",
        "video_annotation_label": _CHANNELS_ANNOTATION_LABELS.get(task.video_annotation or "none", task.video_annotation or "none"),
        "annotation_shooting_time": task.annotation_shooting_time,
        "annotation_shooting_location": task.annotation_shooting_location,
        "annotation_repost_source": task.annotation_repost_source,
        "status": task.status,
        "message": public_channels_message(task.message),
        "failure_stage": task.failure_stage,
        "attempt_count": task.attempt_count,
        "error_message": public_channels_message(task.error_message),
        "platform_content_id": task.platform_content_id,
        "platform_export_id": task.platform_export_id,
        "platform_export_source": task.platform_export_source,
        "platform_export_verified_at": task.platform_export_verified_at.isoformat() + "Z" if task.platform_export_verified_at else None,
        "platform_content_url": task.platform_content_url,
        "publish_clicked_at": task.publish_clicked_at.isoformat() + "Z" if task.publish_clicked_at else None,
        "publish_client_id": task.publish_client_id,
        "publish_transport": task.publish_transport,
        "platform_check_attempts": task.platform_check_attempts,
        "can_promote": bool(
            can_manage
            and task.status == "success"
            and task.platform_export_id
            and task.platform_export_verified_at
        ),
        "latest_promotion": channels_promotion_order_out(latest_promotion),
        "view_count": task.view_count,
        "like_count": task.like_count,
        "comment_count": task.comment_count,
        "share_count": task.share_count,
        "order_count": task.order_count,
        "gmv_yuan": round(task.gmv_fen / 100, 2) if task.gmv_fen is not None else None,
        "metrics_date": task.metrics_date or None,
        "metrics_message": public_channels_message(task.metrics_message),
        "metrics_updated_at": task.metrics_updated_at.isoformat() + "Z" if task.metrics_updated_at else None,
        "submitted_at": task.submitted_at.isoformat() + "Z" if task.submitted_at else None,
        "published_at": task.published_at.isoformat() + "Z" if task.published_at else None,
        "created_at": task.created_at.isoformat() + "Z",
        "updated_at": task.updated_at.isoformat() + "Z",
    }


def claim_channels_tasks(limit: int) -> list[str]:
    if limit <= 0:
        return []
    with _channels_submission_lock, SessionLocal() as db:
        # Settle requests whose original browser worker disappeared during a
        # service restart.  The cancel endpoint rejects submit/confirm stages,
        # so every row here is still before the irreversible publish click.
        cancelled_rows = db.scalars(
            select(ChannelsDelivery).where(
                ChannelsDelivery.deleted_at.is_(None),
                ChannelsDelivery.status == "cancel_requested",
                ChannelsDelivery.submitted_at.is_(None),
                ChannelsDelivery.platform_content_id == "",
                ChannelsDelivery.platform_export_id == "",
            )
        ).all()
        for cancelled in cancelled_rows:
            cancelled.status = "cancelled"
            cancelled.message = "已停止视频号发布；尚未点击平台发布按钮"
            cancelled.failure_stage = ""
            cancelled.error_message = ""
            cancelled.updated_at = datetime.utcnow()
        if cancelled_rows:
            db.commit()
        active_accounts = set(db.scalars(
            select(ChannelsDelivery.account_id).where(ChannelsDelivery.status == "publishing")
        ).all())
        rows = db.scalars(
            select(ChannelsDelivery)
            .where(ChannelsDelivery.status == "pending", ChannelsDelivery.deleted_at.is_(None),
                   _recovery_asset_allowed(db, ChannelsDelivery.asset_id, "push"))
            .order_by(ChannelsDelivery.created_at.asc())
            .limit(30)
        ).all()
        claimed = []
        for row in rows:
            if row.account_id in active_accounts:
                continue
            row.status = "publishing"
            row.message = "正在按原文件发布到视频号主页；不压缩、不转码"
            row.updated_at = datetime.utcnow()
            active_accounts.add(row.account_id)
            claimed.append(row.id)
            if len(claimed) >= limit:
                break
        if claimed:
            db.commit()
        return claimed


def run_channels_push(task_id: str) -> None:
    with SessionLocal() as db:
        task = db.get(ChannelsDelivery, task_id)
        if not task or task.deleted_at is not None:
            return
        if task.status == "cancel_requested":
            task.status = "cancelled"
            task.message = "已停止视频号发布；尚未点击平台发布按钮"
            task.failure_stage = ""
            task.error_message = ""
            task.updated_at = datetime.utcnow()
            db.commit()
            return
        if task.status not in {"pending", "publishing"}:
            return
        if task.asset_id in _recovery_asset_hold_ids(db, "push"):
            return
        if task.publish_clicked_at:
            task.status = "submitted"
            task.failure_stage = "confirm_publish"
            task.submitted_at = task.submitted_at or task.publish_clicked_at
            task.error_message = ""
            task.message = "已进入平台发布边界，已锁定重复上传并转作品回查"
            task.updated_at = datetime.utcnow()
            db.commit()
            return
        asset = db.get(Asset, task.asset_id)
        account = db.get(ChannelsAccount, task.account_id)
        if not asset or asset.deleted_at is not None or asset.purged_at is not None:
            task.status = "failed"
            task.message = task.error_message = "原素材已不存在，无法继续发布"
            task.updated_at = datetime.utcnow()
            db.commit()
            return
        if not account or account.owner_number != task.created_by_number or account.status != "active":
            task.status = "failed"
            task.message = task.error_message = "视频号授权不可用，请重新扫码"
            task.updated_at = datetime.utcnow()
            db.commit()
            return
        try:
            _require_assets_review_approved(db, [asset])
        except HTTPException as error:
            task.status = "failed"
            task.failure_stage = "review_required"
            task.message = task.error_message = str(error.detail)[:1000]
            task.updated_at = datetime.utcnow()
            db.commit()
            return
        task.attempt_count += 1
        task.status = "publishing"
        task.message = "正在上传原视频并等待平台允许发布"
        task.failure_stage = "download_original"
        task.error_message = ""
        task.updated_at = datetime.utcnow()
        payload = {
            "account": account,
            "object_key": asset.object_key,
            "filename": asset.filename,
            "title": task.title,
            "description": task.description,
            "tags": task.tags or [],
            "product_id": task.product_id,
            "product_name": task.product_name,
            "cover_object_key": task.cover_object_key,
            "cover_filename": task.cover_filename,
            "video_annotation": task.video_annotation or "none",
            "annotation_shooting_time": task.annotation_shooting_time,
            "annotation_shooting_location": task.annotation_shooting_location,
            "annotation_repost_source": task.annotation_repost_source,
        }
        # The browser workflow can run for several minutes.  Detach the loaded
        # account snapshot and release SQLite before opening Chrome.
        db.expunge(account)
        db.commit()

    def update_stage(stage: str, message: str) -> None:
        with _channels_submission_lock, SessionLocal() as progress_db:
            progress = progress_db.get(ChannelsDelivery, task_id)
            if not progress or progress.deleted_at is not None:
                return
            if progress.status == "cancelled":
                return
            if progress.status != "cancel_requested":
                progress.status = "publishing"
            progress.failure_stage = stage
            if progress.status != "cancel_requested":
                progress.message = message[:1000]
            progress.updated_at = datetime.utcnow()
            progress_db.commit()

    def should_cancel() -> bool:
        with SessionLocal() as check_db:
            current = check_db.get(ChannelsDelivery, task_id)
            return bool(
                not current
                or current.deleted_at is not None
                or current.status in {"cancel_requested", "cancelled"}
            )

    def mark_submitted(client_id: str) -> None:
        with _channels_submission_lock, SessionLocal() as submit_db:
            current = submit_db.get(ChannelsDelivery, task_id)
            if not current or current.deleted_at is not None:
                raise ChannelsError("视频号发布任务已不存在", "submit_publish")
            if current.status in {"cancel_requested", "cancelled"}:
                raise ChannelsCancelled("已按要求停止发布；尚未点击视频号平台的发布按钮", "submit_publish")
            if current.publish_clicked_at:
                raise ChannelsError(
                    "该任务已进入平台发布边界，禁止再次提交；将只回查作品列表",
                    "confirm_publish",
                )
            current_asset = submit_db.get(Asset, current.asset_id)
            if not current_asset or current_asset.deleted_at or current_asset.purged_at:
                raise ChannelsError("原素材已不存在，已停止发布", "review_required", code="REVIEW_REQUIRED")
            try:
                _require_assets_review_approved(submit_db, [current_asset])
            except HTTPException as error:
                raise ChannelsError(str(error.detail)[:1000], "review_required", code="REVIEW_REQUIRED") from None
            now = datetime.utcnow()
            current.publish_clicked_at = now
            current.publish_client_id = str(client_id or "")[:80]
            current.submitted_at = current.submitted_at or now
            current.status = "publishing"
            current.failure_stage = "submit_publish"
            current.message = "已写入防重发检查点，正在向视频号提交唯一一次发布请求"
            current.updated_at = now
            submit_db.commit()

    def remember_identity(external_account_id: str) -> None:
        value = str(external_account_id or "").strip()[:160]
        if not value:
            return
        with SessionLocal() as identity_db:
            current = identity_db.get(ChannelsAccount, payload["account"].id)
            if not current:
                return
            if current.external_account_id and current.external_account_id != value:
                raise ChannelsError(
                    "当前登录的视频号与任务账号不一致，已停止发布",
                    "internal_preflight",
                )
            current.external_account_id = value
            current.updated_at = datetime.utcnow()
            identity_db.commit()

    payload["on_stage"] = update_stage
    payload["should_cancel"] = should_cancel
    payload["mark_submitted"] = mark_submitted
    payload["on_identity"] = remember_identity
    try:
        result = channels_service.publish(**payload)
    except ChannelsCancelled as error:
        with SessionLocal() as db:
            current = db.get(ChannelsDelivery, task_id)
            if current:
                current.status = "cancelled"
                current.message = str(error)[:1000]
                current.failure_stage = ""
                current.error_message = ""
                current.updated_at = datetime.utcnow()
                db.commit()
        return
    except Exception as error:
        with SessionLocal() as db:
            current = db.get(ChannelsDelivery, task_id)
            if current:
                error_text = str(error)[:4000]
                failure_stage = getattr(error, "stage", "") or current.failure_stage
                cancelled_before_submit = bool(
                    current.status == "cancel_requested"
                    and failure_stage not in {"submit_publish", "confirm_publish"}
                    and not current.publish_clicked_at
                    and not current.submitted_at
                    and not current.platform_content_id
                    and not current.platform_export_id
                )
                ambiguous_after_submit = (
                    bool(current.publish_clicked_at or failure_stage == "confirm_publish")
                    and getattr(error, "code", "") not in {"PUBLISH_REJECTED", "AUTH_EXPIRED"}
                    and not re.search(r"拒绝发布|发布失败|发表失败|审核失败|违规|未通过", error_text)
                )
                if getattr(error, "transport", ""):
                    current.publish_transport = error.transport
                current.status = "cancelled" if cancelled_before_submit else ("submitted" if ambiguous_after_submit else "failed")
                current.error_message = "" if (cancelled_before_submit or ambiguous_after_submit) else error_text
                current.message = (
                    "已停止视频号发布；尚未点击平台发布按钮"
                    if cancelled_before_submit
                    else (
                        "已发起发布但回执连接中断，先核验视频号内容列表；不会直接重复上传"
                        if ambiguous_after_submit
                        else error_text
                    )
                )
                current.failure_stage = "" if cancelled_before_submit else ("confirm_publish" if ambiguous_after_submit else failure_stage)
                current.updated_at = datetime.utcnow()
                if ambiguous_after_submit:
                    current.submitted_at = current.submitted_at or datetime.utcnow()
                if getattr(error, "code", "") == "AUTH_EXPIRED" or "授权已过期" in error_text or "授权已失效" in error_text:
                    expired_account = db.get(ChannelsAccount, current.account_id)
                    if expired_account:
                        expired_account.status = "expired"
                        expired_account.message = "授权已过期，请重新扫码"
                        expired_account.updated_at = datetime.utcnow()
                db.commit()
        return

    with SessionLocal() as db:
        current = db.get(ChannelsDelivery, task_id)
        if current:
            cancel_arrived_late = current.status == "cancel_requested"
            publicly_verified = bool(result.get("publicly_verified"))
            cover_warning = str(result.get("cover_warning") or "").strip()
            current.status = "success" if publicly_verified else "submitted"
            current.message = (
                str(result.get("message") or ("已经公开核验" if publicly_verified else "平台已受理，等待后台确认"))
                + ("；取消请求到达时平台已经受理，未伪装为已取消" if cancel_arrived_late else "")
            )[:1000]
            current.error_message = ""
            current.failure_stage = "set_cover" if publicly_verified and cover_warning else ("" if publicly_verified else "confirm_publish")
            current.platform_content_id = str(result.get("platform_content_id") or current.platform_content_id or "")[:160]
            current.publish_clicked_at = current.publish_clicked_at or datetime.utcnow()
            current.publish_client_id = str(result.get("publish_client_id") or current.publish_client_id or "")[:80]
            current.publish_transport = str(result.get("publish_transport") or current.publish_transport or "")[:30]
            if result.get("platform_export_id"):
                current.platform_export_id = str(result["platform_export_id"])[:160]
                current.platform_export_source = str(result.get("platform_export_source") or "publish_response")[:40]
                current.platform_export_verified_at = datetime.utcnow()
            current.submitted_at = datetime.utcnow()
            if publicly_verified:
                current.published_at = datetime.utcnow()
            current.updated_at = datetime.utcnow()
            db.commit()


_CHANNELS_DAILY_LAST_DATE_KEY = "channels.metrics.daily.last_completed_date"
_CHANNELS_DAILY_STATE_KEY = "channels.metrics.daily.state"
_CHANNELS_DAILY_LAST_ATTEMPT_KEY = "channels.metrics.daily.last_attempt_at"
_CHANNELS_DAILY_RETRY_MINUTES = 30


def _apply_channels_readback(db: Session, task: ChannelsDelivery, result: dict, data_date: str) -> None:
    now = datetime.utcnow()
    identity_values = {
        str(result.get(field) or "").strip()
        for field in ("platform_content_id", "platform_export_id")
        if str(result.get(field) or "").strip()
    }
    identity_conflict = None
    if identity_values:
        identity_conflict = db.scalar(
            select(ChannelsDelivery).where(
                ChannelsDelivery.id != task.id,
                ChannelsDelivery.account_id == task.account_id,
                ChannelsDelivery.deleted_at.is_(None),
                or_(
                    ChannelsDelivery.platform_content_id.in_(identity_values),
                    ChannelsDelivery.platform_export_id.in_(identity_values),
                ),
            ).limit(1)
        )
    if identity_conflict:
        # Never let one creator-list/API object stand in for several delivery
        # rows. A previous loose matcher did exactly that when titles shared a
        # prefix, which also copied the same metrics to different videos.
        if task.platform_content_id in identity_values:
            task.platform_content_id = ""
            task.platform_content_url = ""
        if task.platform_export_id in identity_values:
            task.platform_export_id = ""
            task.platform_export_source = ""
            task.platform_export_verified_at = None
        task.status = "submitted"
        task.failure_stage = "confirm_publish"
        task.error_message = ""
        task.message = "平台返回的内容标识已关联另一条发布任务，已停止误匹配并继续按短标题核验"
        for field in ("view_count", "like_count", "comment_count", "share_count", "order_count", "gmv_fen"):
            setattr(task, field, None)
        task.metrics_date = ""
        task.metrics_message = task.message
        task.metrics_updated_at = now
        task.updated_at = now
        return
    if result.get("platform_content_id"):
        task.platform_content_id = str(result["platform_content_id"])[:160]
    if result.get("platform_content_url"):
        task.platform_content_url = str(result["platform_content_url"])[:2048]
    if result.get("platform_export_id"):
        task.platform_export_id = str(result["platform_export_id"])[:160]
        task.platform_export_source = str(result.get("platform_export_source") or "creator_readback")[:40]
        task.platform_export_verified_at = now
    if result.get("published_at") and result.get("source", "").startswith("fandow_root"):
        try:
            root_published = datetime.fromisoformat(str(result["published_at"]).replace("Z", "+00:00")).replace(tzinfo=None)
            task.published_at = root_published - timedelta(hours=8)
        except (TypeError, ValueError):
            pass
    publication_state = str(result.get("publication_state") or "")
    if result.get("found") and publication_state == "failed":
        task.status = "failed"
        task.failure_stage = "platform_review"
        task.error_message = str(result.get("message") or "视频号平台显示发布或审核失败")[:1000]
        task.message = task.error_message
    elif result.get("found") and result.get("published"):
        if task.cover_object_key:
            if result.get("cover_status") in {"verified", "mismatch", "unverified"}:
                task.cover_status = result["cover_status"]
                task.cover_message = str(result.get("cover_message") or "")[:500]
            task.cover_status = task.cover_status or "unverified"
        custom_cover_unverified = bool(task.cover_object_key) and task.cover_status != "verified"
        if custom_cover_unverified:
            # Publication and custom-cover verification are independent facts.
            # Once platform/root readback proves the video is public, keep the
            # delivery successful while continuing to verify the chosen cover.
            task.status = "success"
            task.failure_stage = "set_cover"
            task.error_message = ""
            task.message = (
                "视频已公开，但自定义封面未生效；不会重复发布"
                if task.cover_status == "mismatch" else
                "根数据已确认视频公开，正在继续核验自定义封面；不会重复发布"
                if str(result.get("source") or "").startswith("fandow_root")
                else "视频已公开，但内容列表尚未回读到自定义封面；不会重复发布，稍后继续核验封面"
            )
        else:
            task.status = "success"
            task.failure_stage = ""
            task.error_message = ""
            task.message = (
                "视频号内容列表已确认公开发布，且主页封面与所选图片一致"
                if task.cover_object_key and task.cover_status == "verified"
                else "视频号内容列表已确认公开发布"
            )
        task.published_at = task.published_at or now
    elif result.get("found") and task.status in {"submitted", "failed"}:
        task.status = "submitted"
        task.failure_stage = "confirm_publish"
        task.error_message = ""
        task.message = str(result.get("message") or "视频号已受理，公开展示状态待核验")[:1000]
    fields = ("view_count", "like_count", "comment_count", "share_count", "order_count")
    for field in fields:
        if result.get(field) is not None:
            setattr(task, field, max(0, int(result[field])))
    if result.get("gmv_fen") is not None:
        task.gmv_fen = max(0, int(result["gmv_fen"]))
    found = bool(result.get("found"))
    if found:
        task.metrics_date = data_date
    elif (
        all(getattr(task, field) is None for field in fields)
        and task.gmv_fen is None
    ):
        # Older builds stamped the requested date even when the platform row
        # was not found. Clear that misleading date while keeping all genuine
        # historical metrics untouched.
        task.metrics_date = ""
    task.metrics_message = str(result.get("message") or "已完成视频号日级回读")[:1000]
    task.metrics_updated_at = now
    task.updated_at = now
    if not found:
        return
    row = db.scalar(select(ChannelsMetricDaily).where(
        ChannelsMetricDaily.delivery_id == task.id,
        ChannelsMetricDaily.data_date == data_date,
    ))
    if row is None:
        row = ChannelsMetricDaily(delivery_id=task.id, account_id=task.account_id, data_date=data_date)
        db.add(row)
    for field in (*fields, "gmv_fen"):
        setattr(row, field, getattr(task, field))
    row.message = task.metrics_message
    row.source = str(result.get("source") or "channels_creator")[:40]
    row.collected_at = now


def _active_channels_account_for_task(db: Session, task: ChannelsDelivery) -> ChannelsAccount | None:
    """Resolve an active account and heal tasks left on an expired copy."""
    account = db.get(ChannelsAccount, task.account_id)
    if account and account.status == "active":
        return account
    replacement = db.scalar(
        select(ChannelsAccount)
        .where(
            ChannelsAccount.owner_number == task.created_by_number,
            (ChannelsAccount.external_account_id == account.external_account_id)
            if account and account.external_account_id else (
                (ChannelsAccount.nickname == task.account_name) & (ChannelsAccount.external_account_id == "")
            ),
            ChannelsAccount.status == "active",
        )
        .order_by(ChannelsAccount.authorized_at.desc(), ChannelsAccount.updated_at.desc())
        .limit(1)
    )
    if replacement:
        task.account_id = replacement.id
        task.account_name = replacement.nickname
        if task.status == "submitted":
            task.failure_stage = "confirm_publish"
            task.message = "已切换到可用授权，等待继续核验公开展示状态"
        return replacement
    return None


def _readback_channels_tasks(task_ids: list[str], data_date: str) -> dict:
    processed = 0
    errors = 0
    root_items: list[dict] = []
    with SessionLocal() as root_db:
        for task_id in task_ids:
            task = root_db.get(ChannelsDelivery, task_id)
            asset = root_db.get(Asset, task.asset_id) if task else None
            if not task or task.deleted_at is not None or not asset:
                continue
            root_items.append({
                "id": task.id,
                "account_name": task.account_name,
                "title": task.title,
                "description": task.description,
                "filename": asset.filename,
                "platform_content_id": task.platform_content_id,
                "platform_export_id": task.platform_export_id,
                "published_at": task.published_at,
                "submitted_at": task.submitted_at,
                "created_at": task.created_at,
            })
    root_results: dict[str, dict] = {}
    root_error = ""
    if root_items:
        try:
            root_results = root_feedback_service.channels_delivery_batch(root_items)
        except RootFeedbackError as error:
            root_error = str(error)
    for task_id in task_ids:
        with SessionLocal() as db:
            task = db.get(ChannelsDelivery, task_id)
            if not task or task.deleted_at is not None:
                continue
            task.platform_check_attempts = int(task.platform_check_attempts or 0) + 1
            account = _active_channels_account_for_task(db, task)
            asset = db.get(Asset, task.asset_id)
            if not asset:
                continue
            root_result = root_results.get(task_id)
            if root_result:
                _apply_channels_readback(
                    db,
                    task,
                    root_result,
                    str(root_result.get("metrics_date") or data_date),
                )
                db.commit()
                processed += 1
                # Root data can prove that the post exists, but it does not
                # expose whether the selected custom cover is actually shown.
                # Keep going through the creator page for that final check.
                if not (task.cover_object_key and root_result.get("published") and account):
                    continue
            if not account:
                task.metrics_message = (
                    f"视频号根数据暂未匹配，且授权已失效；请重新扫码同一视频号后继续核验。根数据错误：{root_error[:400]}"
                    if root_error
                    else "视频号根数据暂未匹配；重新扫码同一视频号后将继续平台核验，后续根数据到达也会自动回流"
                )
                task.metrics_updated_at = datetime.utcnow()
                task.updated_at = datetime.utcnow()
                if task.status == "submitted":
                    task.failure_stage = "authorization"
                    task.message = "平台已受理，但公开核验因授权失效暂停；重新扫码后自动继续"
                if (
                    all(getattr(task, field) is None for field in ("view_count", "like_count", "comment_count", "share_count", "order_count"))
                    and task.gmv_fen is None
                ):
                    task.metrics_date = ""
                db.commit()
                errors += 1
                continue
            account_id = account.id
            title = task.title
            filename = asset.filename
            platform_content_id = task.platform_content_id
            platform_export_id = task.platform_export_id
            cover_object_key = task.cover_object_key or ""
            identity_rows = db.execute(
                select(
                    ChannelsDelivery.platform_content_id,
                    ChannelsDelivery.platform_export_id,
                ).where(
                    ChannelsDelivery.id != task.id,
                    ChannelsDelivery.account_id == task.account_id,
                    ChannelsDelivery.deleted_at.is_(None),
                )
            ).all()
            excluded_platform_ids = {
                str(value).strip()
                for row in identity_rows
                for value in row
                if str(value or "").strip()
            }
            # Browser navigation may take a minute. Detach the account and
            # release SQLite before opening Chromium so upload workers do not
            # exhaust the shared connection pool.
            db.expunge(account)
            db.commit()
        try:
            result = channels_service.read_delivery(
                account,
                title,
                filename,
                platform_content_id=platform_content_id,
                platform_export_id=platform_export_id,
                excluded_platform_ids=excluded_platform_ids,
                cover_object_key=cover_object_key,
            )
            with SessionLocal() as db:
                task = db.get(ChannelsDelivery, task_id)
                if not task or task.deleted_at is not None:
                    continue
                _apply_channels_readback(db, task, result, data_date)
                db.commit()
                if result.get("found"):
                    processed += 1
                else:
                    errors += 1
        except Exception as error:
            with SessionLocal() as db:
                task = db.get(ChannelsDelivery, task_id)
                if not task or task.deleted_at is not None:
                    continue
                account = db.get(ChannelsAccount, account_id)
                error_text = str(error)
                authorization_invalid = "授权已失效" in error_text or "授权已过期" in error_text
                if account and authorization_invalid:
                    account.status = "expired"
                    account.message = "授权已失效，请重新扫码"
                    account.updated_at = datetime.utcnow()
                task.metrics_message = f"视频号日级回流暂未完成：{str(error)[:500]}"
                task.metrics_updated_at = datetime.utcnow()
                task.updated_at = datetime.utcnow()
                if task.status == "submitted" and authorization_invalid:
                    task.failure_stage = "authorization"
                    task.message = "平台已受理，但公开核验因授权失效暂停；重新扫码后自动继续"
                if authorization_invalid and (
                    all(getattr(task, field) is None for field in ("view_count", "like_count", "comment_count", "share_count", "order_count"))
                    and task.gmv_fen is None
                ):
                    task.metrics_date = ""
                db.commit()
                errors += 1
    return {"processed": processed, "errors": errors}


def run_channels_confirmation_if_due(now: datetime | None = None) -> dict:
    current = now or datetime.utcnow()
    cutoff = current - timedelta(minutes=settings.channels_confirmation_interval_minutes)
    with SessionLocal() as db:
        ids = list(db.scalars(select(ChannelsDelivery.id).where(
            ChannelsDelivery.deleted_at.is_(None),
            or_(
                ChannelsDelivery.status == "submitted",
                and_(
                    ChannelsDelivery.status == "success",
                    ChannelsDelivery.failure_stage == "set_cover",
                ),
            ),
            ChannelsDelivery.updated_at <= cutoff,
        ).order_by(ChannelsDelivery.updated_at.asc()).limit(20)).all())
    if not ids:
        return {"status": "waiting", "processed": 0}
    return {"status": "completed", **_readback_channels_tasks(ids, _metrics_local_now(current).date().isoformat())}


def run_channels_daily_metrics_if_due(now: datetime | None = None) -> dict:
    current_local = _metrics_local_now(now)
    current_utc = current_local.astimezone(timezone.utc).replace(tzinfo=None)
    target_date = current_local.date() - timedelta(days=1)
    if current_local.hour < settings.channels_metrics_daily_hour:
        return {"status": "waiting", "target_date": target_date.isoformat()}
    with SessionLocal() as db:
        state = _meta(db, _CHANNELS_DAILY_STATE_KEY, "waiting")
        if (
            _meta(db, _CHANNELS_DAILY_LAST_DATE_KEY) >= target_date.isoformat()
            and state != "partial"
        ):
            return {"status": "waiting", "target_date": target_date.isoformat()}
        last_attempt = _parse_utc_iso(_meta(db, _CHANNELS_DAILY_LAST_ATTEMPT_KEY))
        if (
            state in {"running", "partial"}
            and last_attempt
            and current_utc < last_attempt + timedelta(minutes=_CHANNELS_DAILY_RETRY_MINUTES)
        ):
            return {
                "status": "waiting",
                "target_date": target_date.isoformat(),
                "retry_at": _utc_iso(last_attempt + timedelta(minutes=_CHANNELS_DAILY_RETRY_MINUTES)),
            }
        ids = list(db.scalars(select(ChannelsDelivery.id).where(
            ChannelsDelivery.deleted_at.is_(None),
            ChannelsDelivery.status.in_({"success", "submitted"}),
        ).order_by(ChannelsDelivery.updated_at.asc())).all())
        _set_meta(db, _CHANNELS_DAILY_STATE_KEY, "running")
        _set_meta(db, _CHANNELS_DAILY_LAST_ATTEMPT_KEY, _utc_iso(current_utc))
        db.commit()
    result = _readback_channels_tasks(ids, target_date.isoformat())
    with SessionLocal() as db:
        if not result["errors"]:
            _set_meta(db, _CHANNELS_DAILY_LAST_DATE_KEY, target_date.isoformat())
        _set_meta(db, _CHANNELS_DAILY_STATE_KEY, "partial" if result["errors"] else "completed")
        _set_meta(db, "channels.metrics.daily.completed_at", _utc_iso(datetime.utcnow()))
        _set_meta(db, "channels.metrics.daily.processed", str(result["processed"]))
        _set_meta(db, "channels.metrics.daily.errors", str(result["errors"]))
        db.commit()
    return {"status": "completed", "target_date": target_date.isoformat(), **result}


@app.get("/api/channels/status")
def channels_status(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    result = channels_service.status()
    result["account_count"] = db.scalar(select(func.count()).select_from(ChannelsAccount).where(
        ChannelsAccount.owner_number == user_number(user), ChannelsAccount.status == "active"
    )) or 0
    result["metrics"] = {
        "mode": "root_data+creator_readback",
        "timezone": "Asia/Shanghai",
        "daily_hour": settings.channels_metrics_daily_hour,
        "last_completed_date": _meta(db, _CHANNELS_DAILY_LAST_DATE_KEY) or None,
        "state": _meta(db, _CHANNELS_DAILY_STATE_KEY, "waiting"),
        "processed": int(_meta(db, "channels.metrics.daily.processed", "0") or 0),
        "errors": int(_meta(db, "channels.metrics.daily.errors", "0") or 0),
    }
    return result


@app.post("/api/channels/auth/start")
def channels_auth_start(user: dict = Depends(require_user)):
    try:
        return channels_service.start_authorization(user_number(user), user_name(user))
    except ChannelsError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/channels/auth/sessions/{session_id}")
def channels_auth_session(session_id: str, user: dict = Depends(require_user)):
    try:
        return channels_service.session_status(session_id, user_number(user))
    except ChannelsError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/channels/auth/sessions/{session_id}/qr")
def channels_auth_qr(
    session_id: str,
    revision: int = Query(default=0, ge=0),
    user: dict = Depends(require_user),
):
    try:
        cache_control = "private, max-age=31536000, immutable" if revision else "no-store"
        return Response(
            content=channels_service.session_qr(session_id, user_number(user)),
            media_type="image/png",
            headers={"Cache-Control": cache_control},
        )
    except ChannelsError as error:
        raise HTTPException(404, str(error)) from error


@app.post("/api/channels/auth/sessions/{session_id}/interact")
def channels_auth_interact(
    session_id: str,
    payload: ChannelsInteraction,
    user: dict = Depends(require_user),
):
    try:
        return channels_service.queue_interaction(
            session_id,
            user_number(user),
            payload.x,
            payload.y,
            payload.action,
            payload.delta_y,
            payload.choice_id,
        )
    except ChannelsError as error:
        raise HTTPException(409, str(error)) from error


@app.get("/api/channels/accounts")
def channels_accounts(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    rows = db.scalars(select(ChannelsAccount).where(
        ChannelsAccount.owner_number == user_number(user), ChannelsAccount.status == "active"
    ).order_by(ChannelsAccount.authorized_at.desc())).all()
    promotion_rows = {
        row.account_id: row
        for row in db.scalars(select(ChannelsPromotionAccount).where(
            ChannelsPromotionAccount.account_id.in_([item.id for item in rows])
        )).all()
    } if rows else {}
    return {
        "items": [{
            "id": row.id,
            "nickname": row.nickname,
            "avatar_url": row.avatar_url,
            "status": row.status,
            "message": row.message,
            "authorized_at": row.authorized_at.isoformat() + "Z" if row.authorized_at else None,
            "promotion": channels_promotion_account_out(promotion_rows.get(row.id)),
        } for row in rows],
        "total": len(rows),
    }


def _owned_channels_account(db: Session, account_id: str, user: dict) -> ChannelsAccount:
    account = db.get(ChannelsAccount, account_id)
    if (
        not account
        or account.owner_number != user_number(user)
        or account.status != "active"
    ):
        raise HTTPException(404, "视频号授权不存在")
    return account


@app.get("/api/channels/accounts/{account_id}/promotion-auth")
def channels_promotion_auth_status(
    account_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    account = _owned_channels_account(db, account_id, user)
    return channels_promotion_account_out(db.get(ChannelsPromotionAccount, account_id))


@app.post("/api/channels/accounts/{account_id}/promotion-auth/start")
def channels_promotion_auth_start(
    account_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    account = _owned_channels_account(db, account_id, user)
    db.expunge(account)
    try:
        return channels_promotion_service.start_authorization(account)
    except ChannelsPromotionError as error:
        raise HTTPException(503, str(error)) from error


@app.get("/api/channels/promotion-auth/sessions/{session_id}")
def channels_promotion_auth_session(session_id: str, user: dict = Depends(require_user)):
    try:
        return channels_promotion_service.session_status(session_id, user_number(user))
    except ChannelsPromotionError as error:
        raise HTTPException(404, str(error)) from error


@app.get("/api/channels/promotion-auth/sessions/{session_id}/capture")
def channels_promotion_auth_capture(
    session_id: str,
    revision: int = Query(default=0, ge=0),
    user: dict = Depends(require_user),
):
    try:
        return Response(
            content=channels_promotion_service.session_capture(session_id, user_number(user)),
            media_type="image/png",
            headers={
                "Cache-Control": "private, max-age=31536000, immutable" if revision else "no-store"
            },
        )
    except ChannelsPromotionError as error:
        raise HTTPException(404, str(error)) from error


@app.delete("/api/channels/accounts/{account_id}")
def channels_account_delete(account_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    row = db.get(ChannelsAccount, account_id)
    if not row or row.owner_number != user_number(user) or row.status not in {"active", "expired"}:
        raise HTTPException(404, "视频号授权不存在")
    active = db.scalar(select(func.count()).select_from(ChannelsDelivery).where(
        ChannelsDelivery.account_id == row.id,
        ChannelsDelivery.created_by_number == user_number(user),
        ChannelsDelivery.status.in_({"pending", "publishing", "submitted"}),
    )) or 0
    if active:
        raise HTTPException(409, "仍有该视频号的发布任务正在处理")
    row.status = "revoked"
    row.channel_cookies_ciphertext = ""
    row.session_cookie_ciphertext = ""
    row.cookies_updated_at = None
    row.last_verified_at = None
    row.message = "已解除授权"
    row.updated_at = datetime.utcnow()
    channels_promotion_service.revoke_authorization(row.id, user_number(user))
    try:
        channels_service.remove_authorization_files(row.auth_file)
    except OSError:
        pass
    db.commit()
    return {"ok": True, "account_id": row.id}


@app.get("/api/channels/accounts/{account_id}/products")
def channels_account_products(
    account_id: str,
    q: str = Query(default="", max_length=80),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    account = db.get(ChannelsAccount, account_id)
    if not account or account.owner_number != user_number(user) or account.status != "active":
        raise HTTPException(404, "视频号授权不存在")
    try:
        result = channels_service.products_status(account, q, force_refresh=refresh)
    except ChannelsError as error:
        raise HTTPException(409, str(error)) from error
    return {
        **result,
        "account_id": account.id,
        "account_name": account.nickname,
        "query": q.strip(),
        "source": "视频号橱窗后台读取与账号级缓存",
    }


@app.post("/api/channels/push")
def channels_push(payload: ChannelsPushCreate, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    asset_ids = list(dict.fromkeys(payload.asset_ids))
    account_ids = list(dict.fromkeys(payload.account_ids))
    assets = db.scalars(select(Asset).where(Asset.id.in_(asset_ids))).all()
    accounts = db.scalars(select(ChannelsAccount).where(
        ChannelsAccount.id.in_(account_ids),
        ChannelsAccount.owner_number == user_number(user),
        ChannelsAccount.status == "active",
    )).all()
    if len(assets) != len(asset_ids):
        raise HTTPException(404, "所选素材中存在已删除或不存在的记录")
    if len(accounts) != len(account_ids):
        raise HTTPException(403, "只能选择自己扫码授权的视频号")
    if any(asset.media_type != "video" or asset.deleted_at is not None or asset.purged_at is not None for asset in assets):
        raise HTTPException(400, "视频号主页推送仅支持可用的视频原文件")
    _require_assets_review_approved(db, assets)
    asset_ids_set = set(asset_ids)
    cover_inputs = {cover.asset_id: cover for cover in payload.covers}
    if not set(cover_inputs).issubset(asset_ids_set):
        raise HTTPException(400, "封面与所选视频不匹配")
    title_inputs = {item.asset_id: item.title.strip() for item in payload.titles}
    if not set(title_inputs).issubset(asset_ids_set):
        raise HTTPException(400, "标题与所选视频不匹配")
    for title in [*title_inputs.values(), *([payload.title.strip()] if payload.title else [])]:
        if not 6 <= len(title) <= 16:
            raise HTTPException(400, "每条视频的短标题需填写 6–16 个字")
    annotation_inputs = {item.asset_id: item for item in payload.annotations}
    if not set(annotation_inputs).issubset(asset_ids_set):
        raise HTTPException(400, "视频标注与所选视频不匹配")
    for item in annotation_inputs.values():
        if item.annotation == "self_shot" and (not item.shooting_time.strip() or not item.shooting_location.strip()):
            raise HTTPException(400, "选择“内容为自行拍摄”时必须填写拍摄时间和地点")
    validated_covers: dict[int, dict[str, str]] = {}
    for asset_id, cover in cover_inputs.items():
        validated = _validated_reference_images([{
            "object_key": cover.object_key,
            "filename": cover.filename,
        }])
        if validated:
            validated_covers[asset_id] = validated[0]
    batch_id = str(uuid4())
    task_ids = []
    duplicate_task_ids = []
    with _channels_submission_lock:
        for asset in assets:
            cover = validated_covers.get(asset.id, {})
            annotation_input = annotation_inputs.get(asset.id)
            annotation_code = annotation_input.annotation if annotation_input else _default_channels_annotation(asset)
            for account in accounts:
                existing = db.scalar(
                    select(ChannelsDelivery)
                    .where(
                        ChannelsDelivery.asset_id == asset.id,
                        ChannelsDelivery.account_id == account.id,
                        ChannelsDelivery.deleted_at.is_(None),
                        or_(
                            ChannelsDelivery.status.in_({"pending", "publishing", "submitted", "success"}),
                            ChannelsDelivery.submitted_at.is_not(None),
                            ChannelsDelivery.publish_clicked_at.is_not(None),
                            ChannelsDelivery.publish_client_id != "",
                            ChannelsDelivery.failure_stage.in_({"submit_publish", "confirm_publish"}),
                            ChannelsDelivery.platform_content_id != "",
                            ChannelsDelivery.platform_export_id != "",
                        ),
                    )
                    .order_by(ChannelsDelivery.created_at.desc())
                    .limit(1)
                )
                if existing:
                    duplicate_task_ids.append(existing.id)
                    continue
                task_id = str(uuid4())
                task_ids.append(task_id)
                db.add(ChannelsDelivery(
                    id=task_id,
                    batch_id=batch_id,
                    asset_id=asset.id,
                    created_by_number=user_number(user),
                    created_by_name=user_name(user),
                    account_id=account.id,
                    account_name=account.nickname,
                    title=(title_inputs.get(asset.id) or payload.title.strip() or Path(asset.filename).stem)[:16],
                    description=payload.description.strip(),
                    tags=[tag.strip().lstrip("#")[:40] for tag in payload.tags if tag.strip()][:10],
                    product_id=payload.product_id.strip(),
                    product_name=payload.product_name.strip(),
                    cover_object_key=str(cover.get("object_key") or ""),
                    cover_filename=str(cover.get("filename") or ""),
                    video_annotation=annotation_code,
                    annotation_shooting_time=annotation_input.shooting_time.strip() if annotation_input else "",
                    annotation_shooting_location=annotation_input.shooting_location.strip() if annotation_input else "",
                    annotation_repost_source=annotation_input.repost_source.strip() if annotation_input else "",
                    status="pending",
                    message=f"等待发布到视频号主页；视频标注：{_CHANNELS_ANNOTATION_LABELS[annotation_code]}",
                ))
        db.commit()
    return {
        "batch_id": batch_id,
        "task_ids": task_ids,
        "duplicate_task_ids": duplicate_task_ids,
        "asset_count": len(assets),
        "account_count": len(accounts),
        "status": "queued" if task_ids else "duplicate_blocked",
        "message": (
            f"已入队 {len(task_ids)} 条；已阻止 {len(duplicate_task_ids)} 条可能重复的视频号发布"
            if duplicate_task_ids
            else f"已入队 {len(task_ids)} 条视频号发布任务"
        ),
    }


@app.get("/api/channels/tasks")
def channels_tasks(
    q: str = Query(default="", max_length=120),
    status: str = Query(default="all", pattern="^(all|pending|publishing|cancel_requested|cancelled|submitted|success|failed)$"),
    scope: str = Query(default="all", pattern="^(mine|all)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10),
    push_start_date: date | None = Query(default=None),
    push_end_date: date | None = Query(default=None),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if page_size not in {5, 10}:
        raise HTTPException(400, "每页条数只支持 5 条或 10 条")
    scope_value = scope if isinstance(scope, str) else "all"
    can_view_all = is_operation_admin(user)
    admin_view = can_view_all and scope_value == "all"
    viewer_number = user_number(user)
    filters = [ChannelsDelivery.deleted_at.is_(None)]
    _append_date_filters(filters, ChannelsDelivery.created_at, push_start_date, push_end_date)
    if not admin_view:
        filters.append(ChannelsDelivery.created_by_number == viewer_number)
    if status != "all":
        filters.append(ChannelsDelivery.status == status)
    if q.strip():
        pattern = q.strip()
        filters.append(or_(
            Asset.filename.contains(pattern),
            ChannelsDelivery.account_name.contains(pattern),
            ChannelsDelivery.title.contains(pattern),
            ChannelsDelivery.message.contains(pattern),
            ChannelsDelivery.created_by_name.contains(pattern),
            ChannelsDelivery.created_by_number.contains(pattern),
        ))
    total = db.scalar(select(func.count()).select_from(ChannelsDelivery).join(Asset).where(*filters)) or 0
    rows = db.execute(
        select(ChannelsDelivery, Asset).join(Asset).where(*filters)
        .order_by(ChannelsDelivery.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
    ).all()
    task_ids = [task.id for task, _asset in rows]
    latest_promotions: dict[str, ChannelsPromotionOrder] = {}
    if task_ids:
        for order in db.scalars(
            select(ChannelsPromotionOrder)
            .where(ChannelsPromotionOrder.delivery_id.in_(task_ids))
            .order_by(ChannelsPromotionOrder.updated_at.desc())
        ).all():
            latest_promotions.setdefault(order.delivery_id, order)
    return {
        "items": [
            channels_delivery_out(
                task,
                asset,
                can_manage=task.created_by_number == viewer_number,
                can_cancel=bool(
                    (task.created_by_number == viewer_number or admin_view)
                    and task.status in {"pending", "publishing", "cancel_requested"}
                    and not task.publish_clicked_at
                    and not task.submitted_at
                    and not task.platform_content_id
                    and not task.platform_export_id
                ),
                latest_promotion=latest_promotions.get(task.id),
            )
            for task, asset in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "viewer_scope": "all" if admin_view else "personal",
        "can_view_all": can_view_all,
    }


@app.post("/api/channels/tasks/{task_id}/cancel")
def channels_task_cancel(
    task_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    with _channels_submission_lock:
        task = db.get(ChannelsDelivery, task_id)
        can_cancel = bool(
            task
            and task.deleted_at is None
            and (
                task.created_by_number == user_number(user)
                or is_operation_admin(user)
            )
        )
        if not can_cancel:
            raise HTTPException(404, "视频号发布任务不存在")
        if task.status == "cancel_requested":
            return {"status": task.status, "task_id": task.id, "message": task.message}
        irreversible = bool(
            task.publish_clicked_at
            or task.publish_client_id
            or task.submitted_at
            or task.platform_content_id
            or task.platform_export_id
            or task.status in {"submitted", "success"}
            or task.failure_stage in {"submit_publish", "confirm_publish"}
        )
        if irreversible:
            raise HTTPException(409, "视频号平台可能已经受理，不能伪装成已取消；请继续刷新并回传")
        actor = user_name(user)
        if task.status == "pending":
            task.status = "cancelled"
            task.message = f"已由{actor}取消；尚未开始上传，也未点击视频号平台发布按钮"
        elif task.status == "publishing":
            task.status = "cancel_requested"
            task.message = (
                f"{actor}已请求取消；当前文件或页面操作结束后，"
                "会在点击视频号平台发布按钮前停止"
            )
        else:
            raise HTTPException(409, "当前状态不能取消；平台已受理的任务将继续只读核验")
        task.updated_at = datetime.utcnow()
        db.commit()
        return {"status": task.status, "task_id": task.id, "message": task.message}


def _owned_promotable_channels_task(
    db: Session,
    task_id: str,
    user: dict,
) -> tuple[ChannelsDelivery, ChannelsAccount, ChannelsPromotionAccount]:
    task = db.get(ChannelsDelivery, task_id)
    owner = user_number(user)
    if not task or task.created_by_number != owner or task.deleted_at is not None:
        raise HTTPException(404, "视频号发布任务不存在")
    if task.status != "success" or not task.published_at:
        raise HTTPException(409, "只有已确认公开的视频才能加热")
    if not task.platform_export_id or not task.platform_export_verified_at:
        raise HTTPException(409, "该视频尚未取得已核验的加热标识，请先刷新数据回传")
    publishing_account = db.get(ChannelsAccount, task.account_id)
    if not publishing_account or publishing_account.owner_number != owner:
        raise HTTPException(409, "原发布视频号归属无法核验")

    promotion_rows = db.scalars(
        select(ChannelsPromotionAccount)
        .where(ChannelsPromotionAccount.owner_number == owner)
        .order_by(ChannelsPromotionAccount.updated_at.desc())
    ).all()
    authorized_rows = [
        row for row in promotion_rows
        if channels_promotion_account_out(row)["authorized"]
    ]
    promotion_account = next(
        (row for row in authorized_rows if row.account_id == task.account_id),
        authorized_rows[0] if authorized_rows else None,
    )
    if not promotion_account:
        task_row = next((row for row in promotion_rows if row.account_id == task.account_id), None)
        summary = channels_promotion_account_out(task_row)
        raise HTTPException(
            409,
            summary["message"] if task_row else "请先完成任一企业加热账户授权",
        )
    return task, publishing_account, promotion_account


def _promotion_http_error(error: ChannelsPromotionError) -> HTTPException:
    status_code = 409 if error.code in {
        "auth_expired", "insufficient_balance", "invalid_target", "invalid_budget",
        "invalid_duration", "invalid_bid", "invalid_schedule", "payment_session_failed",
        "platform_unknown", "account_mismatch", "enterprise_required", "unsupported_funding",
        "unsupported_billing", "audience_required", "portrait_proof_required", "voucher_required",
    } else 502
    return HTTPException(status_code, str(error))


def _promotion_options(payload: ChannelsPromotionQuote) -> dict:
    return {
        "funding_type": payload.funding_type,
        "bid_mode": payload.bid_mode,
        "bid_value": payload.bid_value,
        "start_mode": payload.start_mode,
        "scheduled_at": payload.scheduled_at,
        "billing_method": payload.billing_method,
        "promotion_mode": payload.promotion_mode,
        "portrait_mode": payload.portrait_mode,
        "voucher_mode": payload.voucher_mode,
    }


def _sync_channels_promotion_order_row(
    db: Session,
    order: ChannelsPromotionOrder,
    promotion_account: ChannelsPromotionAccount,
) -> dict:
    result = channels_promotion_service.get_order_detail(promotion_account, order.promotion_id)
    order.status = str(result.get("status") or "manual_review")[:80]
    order.cost_wecoin = result.get("cost_wecoin") if order.status == "success" else None
    order.error_code = "" if order.status == "success" else order.status
    order.error_message = "" if order.status == "success" else str(result.get("message") or "")[:1000]
    order.response_summary = str(result.get("response_summary") or "")[:1200]
    order.updated_at = datetime.utcnow()
    if result.get("balance") is not None:
        promotion_account.balance_wecoin = int(result["balance"])
        promotion_account.updated_at = datetime.utcnow()
    db.commit()
    return {**channels_promotion_order_out(order), "platform_message": result.get("message") or ""}


_CHANNELS_PAYMENT_ACTIVE_STATUSES = {"preparing", "awaiting_scan", "confirming"}
_CHANNELS_PAYMENT_TERMINAL_STATUSES = {"succeeded", "expired", "cancelled", "failed", "uncertain"}


def _channels_payment_no_store(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(
        payload,
        status_code=status_code,
        headers={
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "Referrer-Policy": "no-referrer",
        },
    )


def _channels_payment_session_out(row: ChannelsPromotionPaymentSession) -> dict:
    launch_url = ""
    if row.status in _CHANNELS_PAYMENT_ACTIVE_STATUSES and row.pc_sdk_info_ciphertext:
        pc_sdk_info = channels_promotion_service.decrypt_secret(row.pc_sdk_info_ciphertext)
        launch_url = channels_promotion_service.payment_launch_url(pc_sdk_info)
    return {
        "id": row.id,
        "order_id": row.order_id,
        "status": row.status,
        "expected_amount": row.expected_amount,
        "sdk_status": row.sdk_status,
        "launch_url": launch_url,
        "expires_at": row.expires_at.isoformat() + "Z" if row.expires_at else None,
        "completed_at": row.completed_at.isoformat() + "Z" if row.completed_at else None,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _owned_channels_promotion_order(
    db: Session,
    task_id: str,
    order_id: str,
    user: dict,
) -> tuple[ChannelsDelivery, ChannelsPromotionOrder, ChannelsPromotionAccount]:
    task = db.get(ChannelsDelivery, task_id)
    order = db.get(ChannelsPromotionOrder, order_id)
    owner = user_number(user)
    if (
        not task or task.created_by_number != owner or task.deleted_at is not None
        or not order or order.delivery_id != task_id or order.created_by_number != owner
    ):
        raise HTTPException(404, "加热订单不存在")
    promotion_account = db.get(ChannelsPromotionAccount, order.account_id)
    if not promotion_account:
        raise HTTPException(409, "加热账户授权不存在，请重新授权")
    return task, order, promotion_account


def _update_channels_payment_session_from_order(
    db: Session,
    session: ChannelsPromotionPaymentSession,
    order: ChannelsPromotionOrder,
) -> None:
    now = datetime.utcnow()
    if order.status == "success":
        session.status = "succeeded"
    elif order.status in {"failed", "cancelled"}:
        session.status = order.status
    elif order.status == "manual_review":
        session.status = "uncertain"
    elif order.status == "payment_processing" or session.sdk_status == "consumeSuccess":
        session.status = "confirming"
    elif session.expires_at and session.expires_at <= now:
        session.status = "expired"
    else:
        session.status = "awaiting_scan"
    if session.status in _CHANNELS_PAYMENT_TERMINAL_STATUSES:
        session.active_key = None
        session.pc_sdk_info_ciphertext = ""
        session.completed_at = session.completed_at or now
    session.updated_at = now


@app.post("/api/channels/tasks/{task_id}/promotion/quote")
def channels_promotion_quote(
    task_id: str,
    payload: ChannelsPromotionQuote,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task, _publishing_account, promotion_account = _owned_promotable_channels_task(db, task_id, user)
    promotion_account_id = promotion_account.account_id
    db.expunge(promotion_account)
    db.commit()
    try:
        result = channels_promotion_service.quote(
            promotion_account,
            task.platform_export_id,
            payload.promotion_target,
            payload.budget_wecoin,
            payload.duration_hours,
            **_promotion_options(payload),
        )
    except ChannelsPromotionError as error:
        if error.code == "auth_expired":
            channels_promotion_service.mark_expired(promotion_account_id)
        raise _promotion_http_error(error) from error
    current = db.get(ChannelsPromotionAccount, promotion_account_id)
    if current:
        current.balance_wecoin = result.get("balance")
        current.nickname = str(result.get("nickname") or current.nickname)[:255]
        current.user_type = int(result.get("user_type") or current.user_type or 0)
        current.message = "企业加热账户可用，已完成询价"
        current.updated_at = datetime.utcnow()
        db.commit()
    return {
        **result,
        "task_id": task.id,
        "platform_export_id": task.platform_export_id,
        "promotion_target": payload.promotion_target,
        "budget_wecoin": payload.budget_wecoin,
        "duration_hours": payload.duration_hours,
    }


@app.post("/api/channels/tasks/{task_id}/promotion/orders")
def channels_promotion_order_create(
    task_id: str,
    payload: ChannelsPromotionOrderCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    if not payload.confirmed:
        raise HTTPException(409, "请先确认本操作会真实扣除微信豆，失败不会自动重试")
    task, _publishing_account, promotion_account = _owned_promotable_channels_task(db, task_id, user)
    owner = user_number(user)
    existing = db.scalar(select(ChannelsPromotionOrder).where(
        ChannelsPromotionOrder.delivery_id == task.id,
        ChannelsPromotionOrder.created_by_number == owner,
        ChannelsPromotionOrder.idempotency_key == payload.idempotency_key,
    ))
    if existing:
        return {**channels_promotion_order_out(existing), "duplicate_request": True}

    task_export_id = task.platform_export_id
    account_id = promotion_account.account_id
    creator_name = user_name(user)
    db.expunge(promotion_account)
    db.commit()
    with channels_promotion_service.account_lock(account_id):
        existing = db.scalar(select(ChannelsPromotionOrder).where(
            ChannelsPromotionOrder.delivery_id == task_id,
            ChannelsPromotionOrder.created_by_number == owner,
            ChannelsPromotionOrder.idempotency_key == payload.idempotency_key,
        ))
        if existing:
            return {**channels_promotion_order_out(existing), "duplicate_request": True}
        try:
            quote_result = channels_promotion_service.quote(
                promotion_account,
                task_export_id,
                payload.promotion_target,
                payload.budget_wecoin,
                payload.duration_hours,
                **_promotion_options(payload),
            )
        except ChannelsPromotionError as error:
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(account_id)
            raise _promotion_http_error(error) from error

        order = ChannelsPromotionOrder(
            id=str(uuid4()),
            delivery_id=task_id,
            account_id=account_id,
            created_by_number=owner,
            created_by_name=creator_name,
            platform_export_id=task_export_id,
            promotion_target=payload.promotion_target,
            budget_wecoin=payload.budget_wecoin,
            quoted_wecoin=quote_result.get("need_pay"),
            duration_hours=payload.duration_hours,
            order_name=payload.order_name.strip(),
            idempotency_key=payload.idempotency_key,
            quote_snapshot={
                "need_pay": quote_result.get("need_pay"),
                "balance": quote_result.get("balance"),
                "quote_fallback": bool(quote_result.get("quote_fallback")),
            },
            request_snapshot={
                "input": payload.model_dump(mode="json"),
                "options": _promotion_options(payload),
                "platform_request": channels_promotion_service.build_create_payload(
                    task_export_id,
                    payload.promotion_target,
                    payload.budget_wecoin,
                    payload.duration_hours,
                    payload.order_name.strip(),
                    bid_mode=payload.bid_mode,
                    bid_value=payload.bid_value,
                    start_mode=payload.start_mode,
                    scheduled_at=payload.scheduled_at,
                    promotion_mode=payload.promotion_mode,
                ),
            },
            confirmed_at=datetime.utcnow(),
            status="submitting",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(order)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicate = db.scalar(select(ChannelsPromotionOrder).where(
                ChannelsPromotionOrder.delivery_id == task_id,
                ChannelsPromotionOrder.created_by_number == owner,
                ChannelsPromotionOrder.idempotency_key == payload.idempotency_key,
            ))
            if duplicate:
                return {**channels_promotion_order_out(duplicate), "duplicate_request": True}
            raise HTTPException(409, "加热请求正在处理，请勿重复提交")

        try:
            current = db.get(ChannelsPromotionOrder, order.id)
            current.create_attempts = int(current.create_attempts or 0) + 1
            db.commit()
            result = channels_promotion_service.create_promotion(
                promotion_account,
                task_export_id,
                payload.promotion_target,
                payload.budget_wecoin,
                payload.duration_hours,
                payload.order_name.strip(),
                **_promotion_options(payload),
                request_payload=dict((current.request_snapshot or {}).get("platform_request") or {}),
            )
        except ChannelsPromotionError as error:
            current = db.get(ChannelsPromotionOrder, order.id)
            if current:
                current.status = "manual_review" if error.code == "platform_unknown" else "failed"
                current.promotion_id = str(error.promotion_id or "")[:160]
                current.error_code = error.code[:80]
                current.error_message = str(error)[:1000]
                current.response_summary = str(error.summary or "")[:1200]
                current.updated_at = datetime.utcnow()
                db.commit()
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(account_id)
            raise _promotion_http_error(error) from error

        current = db.get(ChannelsPromotionOrder, order.id)
        current.status = "pending_payment"
        current.promotion_id = str(result.get("promotion_id") or "")[:160]
        current.cost_wecoin = None
        current.error_code = "pending_payment"
        current.error_message = "腾讯计划已创建，等待用户主动打开腾讯支付页并扫码支付"
        current.response_summary = str(result.get("response_summary") or "")[:1200]
        current.updated_at = datetime.utcnow()
        db.commit()
        return {
            **channels_promotion_order_out(current),
            "duplicate_request": False,
        }


@app.post("/api/channels/tasks/{task_id}/promotion/orders/{order_id}/payment-sessions")
def channels_promotion_payment_session_create(
    task_id: str,
    order_id: str,
    payload: ChannelsPromotionPaymentSessionCreate,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner = user_number(user)
    _task, order, promotion_account = _owned_channels_promotion_order(db, task_id, order_id, user)
    if not order.promotion_id:
        raise HTTPException(409, "该订单尚未取得腾讯计划 ID，不能打开支付")
    if order.status == "success":
        raise HTTPException(409, "该订单已由腾讯确认支付成功，无需重复支付")
    if order.status in {"failed", "cancelled", "manual_review"}:
        raise HTTPException(409, "该订单当前不能发起支付，请先核验腾讯订单状态")

    idempotency_key = hashlib.sha256(
        f"{order.id}:{owner}:{payload.request_token}".encode("utf-8")
    ).hexdigest()
    duplicate = db.scalar(select(ChannelsPromotionPaymentSession).where(
        ChannelsPromotionPaymentSession.idempotency_key == idempotency_key,
    ))
    if duplicate:
        return _channels_payment_no_store(_channels_payment_session_out(duplicate))

    account_id = promotion_account.account_id
    with channels_promotion_service.account_lock(account_id):
        # Every retry starts with a Tencent read. A local SDK event is never
        # sufficient to mark the order paid or to issue another credential.
        try:
            _sync_channels_promotion_order_row(db, order, promotion_account)
        except ChannelsPromotionError as error:
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(account_id)
            raise _promotion_http_error(error) from error
        order = db.get(ChannelsPromotionOrder, order_id)
        if order.status == "success":
            raise HTTPException(409, "腾讯已确认该订单支付成功，无需重复支付")
        if order.status in {"failed", "cancelled", "manual_review"}:
            raise HTTPException(409, "腾讯订单状态不允许继续支付，请人工核对")

        unresolved = db.scalar(
            select(ChannelsPromotionPaymentSession)
            .where(
                ChannelsPromotionPaymentSession.order_id == order.id,
                ChannelsPromotionPaymentSession.status == "uncertain",
            )
            .order_by(ChannelsPromotionPaymentSession.created_at.desc())
        )
        if unresolved:
            # The prior request is only released after Tencent explicitly
            # confirms that the order is still unpaid.
            unresolved.status = "failed"
            unresolved.response_summary = "腾讯订单已回读为待支付，可由用户重新发起支付"
            unresolved.completed_at = unresolved.completed_at or datetime.utcnow()
            unresolved.updated_at = datetime.utcnow()
            db.commit()

        active = db.scalar(select(ChannelsPromotionPaymentSession).where(
            ChannelsPromotionPaymentSession.order_id == order.id,
            ChannelsPromotionPaymentSession.active_key == order.id,
        ))
        if active:
            _update_channels_payment_session_from_order(db, active, order)
            db.commit()
            if active.status in _CHANNELS_PAYMENT_ACTIVE_STATUSES:
                return _channels_payment_no_store(_channels_payment_session_out(active))

        quote_payload = ChannelsPromotionQuote.model_validate(
            dict((order.request_snapshot or {}).get("input") or {})
        )
        try:
            latest_quote = channels_promotion_service.quote(
                promotion_account,
                order.platform_export_id,
                quote_payload.promotion_target,
                quote_payload.budget_wecoin,
                quote_payload.duration_hours,
                **_promotion_options(quote_payload),
            )
        except ChannelsPromotionError as error:
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(account_id)
            raise _promotion_http_error(error) from error
        expected_amount = int(latest_quote.get("need_pay") or 0)
        if expected_amount <= 0 or expected_amount > order.budget_wecoin:
            raise HTTPException(409, "腾讯最新应付金额超过已确认预算，请重新询价并确认后再创建订单")

        session = ChannelsPromotionPaymentSession(
            id=str(uuid4()),
            order_id=order.id,
            active_key=order.id,
            idempotency_key=idempotency_key,
            status="preparing",
            expected_amount=expected_amount,
            prepare_attempts=1,
            requested_by=owner,
            expires_at=datetime.utcnow() + timedelta(minutes=5),
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        db.add(session)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            duplicate = db.scalar(select(ChannelsPromotionPaymentSession).where(
                ChannelsPromotionPaymentSession.order_id == order.id,
                ChannelsPromotionPaymentSession.active_key == order.id,
            ))
            if duplicate:
                return _channels_payment_no_store(_channels_payment_session_out(duplicate))
            raise HTTPException(409, "支付会话正在创建，请勿重复点击")

        try:
            result = channels_promotion_service.payment_session(promotion_account, order.promotion_id)
        except ChannelsPromotionError as error:
            current = db.get(ChannelsPromotionPaymentSession, session.id)
            current.status = "uncertain" if error.uncertain or error.code == "platform_unknown" else "failed"
            current.active_key = None
            current.response_summary = str(error.summary or error)[:1200]
            current.completed_at = datetime.utcnow()
            current.updated_at = datetime.utcnow()
            db.commit()
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(account_id)
            raise _promotion_http_error(error) from error

        current = db.get(ChannelsPromotionPaymentSession, session.id)
        current.status = "awaiting_scan"
        current.pc_sdk_info_ciphertext = channels_promotion_service.encrypt_secret(result["pc_sdk_info"])
        current.response_summary = str(result.get("response_summary") or "")[:1200]
        current.updated_at = datetime.utcnow()
        order = db.get(ChannelsPromotionOrder, order.id)
        order.status = "payment_processing"
        order.error_code = "payment_processing"
        order.error_message = "支付页已生成，等待用户扫码；最终结果以腾讯订单回读为准"
        order.updated_at = datetime.utcnow()
        db.commit()
        return _channels_payment_no_store(_channels_payment_session_out(current))


def _owned_channels_payment_session(
    db: Session,
    task_id: str,
    order_id: str,
    session_id: str,
    user: dict,
) -> tuple[ChannelsPromotionOrder, ChannelsPromotionAccount, ChannelsPromotionPaymentSession]:
    _task, order, promotion_account = _owned_channels_promotion_order(db, task_id, order_id, user)
    session = db.get(ChannelsPromotionPaymentSession, session_id)
    if not session or session.order_id != order.id or session.requested_by != user_number(user):
        raise HTTPException(404, "支付会话不存在")
    return order, promotion_account, session


@app.get("/api/channels/tasks/{task_id}/promotion/orders/{order_id}/payment-sessions/{session_id}")
def channels_promotion_payment_session_get(
    task_id: str,
    order_id: str,
    session_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    order, promotion_account, session = _owned_channels_payment_session(
        db, task_id, order_id, session_id, user
    )
    if session.status in _CHANNELS_PAYMENT_ACTIVE_STATUSES:
        try:
            _sync_channels_promotion_order_row(db, order, promotion_account)
        except ChannelsPromotionError as error:
            if error.code == "auth_expired":
                channels_promotion_service.mark_expired(order.account_id)
            raise _promotion_http_error(error) from error
        order = db.get(ChannelsPromotionOrder, order_id)
        session = db.get(ChannelsPromotionPaymentSession, session_id)
        session.query_attempts = int(session.query_attempts or 0) + 1
        _update_channels_payment_session_from_order(db, session, order)
        db.commit()
    return _channels_payment_no_store(_channels_payment_session_out(session))


@app.post("/api/channels/tasks/{task_id}/promotion/orders/{order_id}/payment-sessions/{session_id}/events")
def channels_promotion_payment_session_event(
    task_id: str,
    order_id: str,
    session_id: str,
    payload: ChannelsPromotionPaymentEvent,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    _order, _promotion_account, session = _owned_channels_payment_session(
        db, task_id, order_id, session_id, user
    )
    session.sdk_status = payload.action
    if payload.action == "consumeSuccess" and session.status in _CHANNELS_PAYMENT_ACTIVE_STATUSES:
        session.status = "confirming"
    elif payload.action == "consumeClose" and session.status in _CHANNELS_PAYMENT_ACTIVE_STATUSES:
        session.status = "cancelled"
        session.active_key = None
        session.pc_sdk_info_ciphertext = ""
        session.completed_at = datetime.utcnow()
    if payload.message:
        session.response_summary = payload.message[:1200]
    session.updated_at = datetime.utcnow()
    db.commit()
    return _channels_payment_no_store(_channels_payment_session_out(session))


@app.post("/api/channels/tasks/{task_id}/promotion/orders/{order_id}/sync")
def channels_promotion_order_sync(
    task_id: str,
    order_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task = db.get(ChannelsDelivery, task_id)
    order = db.get(ChannelsPromotionOrder, order_id)
    owner = user_number(user)
    if (
        not task or task.created_by_number != owner or task.deleted_at is not None
        or not order or order.delivery_id != task_id or order.created_by_number != owner
    ):
        raise HTTPException(404, "加热订单不存在")
    if not order.promotion_id:
        raise HTTPException(409, "该订单尚未取得腾讯计划 ID，无法核验支付")
    promotion_account = db.get(ChannelsPromotionAccount, order.account_id)
    if not promotion_account:
        raise HTTPException(409, "加热账户授权不存在，请重新授权")
    try:
        return _sync_channels_promotion_order_row(db, order, promotion_account)
    except ChannelsPromotionError as error:
        if error.code == "auth_expired":
            channels_promotion_service.mark_expired(order.account_id)
        raise _promotion_http_error(error) from error


@app.get("/api/channels/tasks/{task_id}/promotion/orders")
def channels_promotion_orders(
    task_id: str,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    task = db.get(ChannelsDelivery, task_id)
    if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
        raise HTTPException(404, "视频号发布任务不存在")
    rows = db.scalars(
        select(ChannelsPromotionOrder)
        .where(
            ChannelsPromotionOrder.delivery_id == task_id,
            ChannelsPromotionOrder.created_by_number == user_number(user),
        )
        .order_by(ChannelsPromotionOrder.created_at.desc())
        .limit(20)
    ).all()
    return {"items": [channels_promotion_order_out(row) for row in rows], "total": len(rows)}


@app.post("/api/channels/metrics/sync")
def channels_metrics_sync(
    payload: ChannelsMetricsSync,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    filters = [
        ChannelsDelivery.created_by_number == user_number(user),
        ChannelsDelivery.deleted_at.is_(None),
        ChannelsDelivery.status.in_({"success", "submitted"}),
    ]
    if payload.task_ids:
        filters.append(ChannelsDelivery.id.in_(list(dict.fromkeys(payload.task_ids))))
    task_ids = list(db.scalars(
        select(ChannelsDelivery.id)
        .where(*filters)
        .order_by(ChannelsDelivery.metrics_updated_at.asc(), ChannelsDelivery.created_at.asc())
        .limit(20)
    ).all())
    if not task_ids:
        return {"status": "empty", "task_ids": [], "message": "当前没有可回传的个人视频号记录"}
    background_tasks.add_task(
        _readback_channels_tasks,
        task_ids,
        _metrics_local_now().date().isoformat(),
    )
    return {
        "status": "queued",
        "task_ids": task_ids,
        "message": f"已进入视频号回传队列，本次核验 {len(task_ids)} 条记录",
    }


def _channels_task_editable(task: ChannelsDelivery) -> bool:
    return bool(task.deleted_at is None and task.status in {"failed", "cancelled"} and not (
        task.publish_clicked_at or task.publish_client_id or task.submitted_at
        or task.platform_content_id or task.platform_export_id
        or task.failure_stage in {"submit_publish", "confirm_publish"}))


def _require_no_other_channels_publication(db: Session, task: ChannelsDelivery) -> None:
    existing = db.scalar(select(ChannelsDelivery.id).where(
        ChannelsDelivery.id != task.id,
        ChannelsDelivery.asset_id == task.asset_id,
        ChannelsDelivery.account_id == task.account_id,
        ChannelsDelivery.deleted_at.is_(None),
        or_(ChannelsDelivery.status.in_({"pending", "publishing", "cancel_requested", "submitted", "success"}),
            ChannelsDelivery.submitted_at.is_not(None), ChannelsDelivery.publish_clicked_at.is_not(None),
            ChannelsDelivery.publish_client_id != "", ChannelsDelivery.platform_content_id != "",
            ChannelsDelivery.platform_export_id != "",
            ChannelsDelivery.failure_stage.in_({"submit_publish", "confirm_publish"})),
    ).limit(1))
    if existing:
        raise HTTPException(409, "该素材在此视频号已有发布或待核验任务，请查看原记录，避免重复发布")


@app.patch("/api/channels/tasks/{task_id}")
def channels_task_edit(task_id: str, payload: ChannelsTaskEdit,
                       db: Session = Depends(get_db), user: dict = Depends(require_user)):
    # Same lock as task claiming: a queued worker cannot race with this edit.
    with _channels_submission_lock:
        task = db.get(ChannelsDelivery, task_id)
        if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
            raise HTTPException(404, "视频号推送任务不存在")
        if not _channels_task_editable(task):
            raise HTTPException(409, "仅支持修改尚未提交平台的失败或取消任务；已提交内容需先核验")
        title = payload.title.strip()
        if not 6 <= len(title) <= 16:
            raise HTTPException(400, "短标题需填写 6–16 个字")
        account = db.get(ChannelsAccount, task.account_id)
        if not account or account.owner_number != user_number(user) or account.status != "active":
            raise HTTPException(409, "请先重新授权原视频号")
        if payload.product_id.strip() and not payload.product_name.strip():
            raise HTTPException(400, "请从该视频号橱窗选择完整商品")
        asset = db.get(Asset, task.asset_id)
        if not asset or asset.deleted_at or asset.purged_at:
            raise HTTPException(404, "原素材已不存在")
        if payload.retry:
            _require_assets_review_approved(db, [asset])
            _require_no_other_channels_publication(db, task)
        if payload.cover_object_key is not None:
            cover = _validated_reference_images([{'object_key': payload.cover_object_key,
                'filename': payload.cover_filename}])[0] if payload.cover_object_key else {}
            task.cover_object_key = cover.get('object_key', '')
            task.cover_status = "unverified" if task.cover_object_key else ""
            task.cover_message = ""
            task.cover_filename = cover.get('filename', '')
        task.title = title
        task.description = payload.description.strip()
        task.product_id = payload.product_id.strip()
        task.product_name = payload.product_name.strip()
        if payload.retry:
            task.status = "pending"
            task.failure_stage = ""
            task.error_message = ""
        task.message = "发布信息已修改；等待重新发布" if payload.retry else "发布信息已保存，可确认后重试"
        task.updated_at = datetime.utcnow()
        db.commit()
        return {"status": task.status, "task_id": task.id, "message": task.message}


@app.post("/api/channels/tasks/{task_id}/retry")
def channels_task_retry(task_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    with _channels_submission_lock:
        return _channels_task_retry_locked(task_id, db, user)


def _channels_task_retry_locked(task_id: str, db: Session, user: dict):
    task = db.get(ChannelsDelivery, task_id)
    if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
        raise HTTPException(404, "视频号推送任务不存在")
    if task.status != "failed":
        raise HTTPException(409, "只有失败任务可以重试")
    asset = db.get(Asset, task.asset_id)
    if not asset or asset.deleted_at is not None or asset.purged_at is not None:
        raise HTTPException(404, "原素材已不存在，无法重试")
    account = db.get(ChannelsAccount, task.account_id)
    task_error = task.error_message or task.message or ""
    authorization_invalid = "授权已过期" in task_error or (
        task.failure_stage == "open_publish_page" and "刷新授权" in task_error
    )
    if account and authorization_invalid:
        account.status = "expired"
        account.message = "授权已过期，请重新扫码"
        account.updated_at = datetime.utcnow()
        db.flush()
    if not account or account.status != "active":
        account = _active_channels_account_for_task(db, task)
        if not account:
            db.commit()
            raise HTTPException(409, "该视频号授权已过期，请先重新扫码授权后再重试")
    may_have_reached_platform = bool(
        task.publish_clicked_at
        or task.publish_client_id
        or task.submitted_at
        or task.platform_content_id
        or task.platform_export_id
        or task.failure_stage in {"submit_publish", "confirm_publish"}
    )
    if may_have_reached_platform:
        if account and asset:
            try:
                result = channels_service.read_delivery(
                    account,
                    task.title,
                    asset.filename,
                    platform_content_id=task.platform_content_id,
                    platform_export_id=task.platform_export_id,
                )
                if result.get("found"):
                    _apply_channels_readback(db, task, result, _metrics_local_now().date().isoformat())
                    db.commit()
                    if task.status == "success":
                        return {"status": "success", "task_id": task.id, "message": "已确认视频号内容公开展示，无需重复上传"}
                    return {"status": task.status, "task_id": task.id, "message": task.message}
            except Exception:
                pass
        task.status = "submitted"
        task.failure_stage = "confirm_publish"
        task.error_message = ""
        task.message = "该任务可能已到达视频号，已锁定重复上传；请恢复授权或继续公开核验"
        task.submitted_at = task.submitted_at or datetime.utcnow()
        task.updated_at = datetime.utcnow()
        db.commit()
        return {"status": "submitted", "task_id": task.id, "message": task.message}
    _require_assets_review_approved(db, [asset])
    _require_no_other_channels_publication(db, task)
    task.status = "pending"
    task.message = "等待重新发布原视频"
    task.failure_stage = ""
    task.error_message = ""
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"status": "queued", "task_id": task.id}


@app.delete("/api/channels/tasks/{task_id}")
def channels_task_delete(task_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    task = db.get(ChannelsDelivery, task_id)
    if not task or task.created_by_number != user_number(user) or task.deleted_at is not None:
        raise HTTPException(404, "视频号推送记录不存在")
    if task.status in {"pending", "publishing", "cancel_requested", "submitted"}:
        raise HTTPException(409, "任务仍在处理或等待平台确认，确认完成后才能删除记录")
    task.deleted_at = datetime.utcnow()
    task.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "task_id": task.id, "message": "仅移除个人记录，不会删除已发布内容"}


VIDEO_REQUEST_STATUS_LABELS = {
    "submitted": "待分配",
    "assigned": "已分配",
    "in_production": "制作中",
    "delivered": "待验收",
    "revision_requested": "修改中",
    "accepted": "已完成",
    "returned": "已退回",
}

VIDEO_REQUEST_PROGRESS = {
    "submitted": (10, "需求已提交，等待主管分配"),
    "assigned": (30, "已分配制作人，等待开始制作"),
    "in_production": (55, "制作人正在制作"),
    "revision_requested": (65, "已反馈修改意见，正在返修"),
    "delivered": (85, "成片已上传，等待提需人验收"),
    "accepted": (100, "已验收完成，等待投放数据持续回流"),
    "returned": (100, "需求已退回，不进入制作"),
}


def _video_request_is_requester(item: VideoRequest, user: dict) -> bool:
    return item.requester_number == user_number(user)


def _video_request_is_assignee(item: VideoRequest, user: dict) -> bool:
    if item.assignee_number:
        return item.assignee_number == user_number(user)
    return bool(item.assignee_name and item.assignee_name.strip().casefold() == user_name(user).strip().casefold())


def _video_request_visible(item: VideoRequest, user: dict) -> bool:
    return bool(
        is_operation_admin(user)
        or is_video_request_assigner(user)
        or _video_request_is_requester(item, user)
        or _video_request_is_assignee(item, user)
    )


def _video_request_supervisor_viewer(user: dict) -> bool:
    """Grant the protected system owners a read-only supervisor task view."""
    return is_super_admin(user) or is_video_request_assigner(user)


def _video_request_event(
    db: Session,
    item: VideoRequest,
    action: str,
    user: dict,
    detail: dict | None = None,
) -> None:
    db.add(VideoRequestEvent(
        request_id=item.id,
        action=action,
        actor_number=user_number(user),
        actor_name=user_name(user),
        detail=detail or {},
    ))


def _notify_video_request(
    db: Session,
    *,
    recipient_number: str = "",
    recipient_name: str = "",
    title: str,
    message: str,
    request_id: str,
) -> None:
    if not recipient_number.strip() and not recipient_name.strip():
        return
    db.add(UserNotification(
        recipient_number=recipient_number.strip()[:80],
        recipient_name=recipient_name.strip()[:120],
        kind="video_request",
        title=title[:180],
        message=message,
        resource_type="video_request",
        resource_id=request_id,
    ))


def _video_request_metrics(db: Session, asset_id: int | None) -> dict:
    if not asset_id:
        return {
            "state": "pending",
            "message": "成片尚未上传，暂无可关联的投放数据",
            "qianchuan": None,
            "adq": None,
            "channels": None,
        }
    qianchuan_tasks = db.scalar(
        select(func.count()).select_from(QianchuanDelivery).where(
            QianchuanDelivery.asset_id == asset_id,
            QianchuanDelivery.deleted_at.is_(None),
        )
    ) or 0
    qianchuan_summary = _asset_gmv_summary(db, [asset_id]).get(asset_id)
    adq_rows = db.execute(
        select(AdqDelivery.status, AdqDelivery.metrics, AdqDelivery.metrics_synced_at).where(
            AdqDelivery.asset_id == asset_id,
            AdqDelivery.deleted_at.is_(None),
        )
    ).all()
    adq_latest = max((row[2] for row in adq_rows if row[2]), default=None)
    adq_with_data = sum(1 for _, metrics, _ in adq_rows if metrics)
    channel_rows = db.scalars(
        select(ChannelsDelivery).where(
            ChannelsDelivery.asset_id == asset_id,
            ChannelsDelivery.deleted_at.is_(None),
        )
    ).all()
    channel_with_data = [row for row in channel_rows if row.metrics_updated_at]
    latest_updates = [
        value for value in (
            qianchuan_summary.get("updated_at") if qianchuan_summary else None,
            adq_latest,
            max((row.metrics_updated_at for row in channel_with_data), default=None),
        ) if value
    ]
    return {
        "state": "available" if qianchuan_summary or adq_with_data or channel_with_data else "pending",
        "message": "仅展示平台已回流并完成关联的数据" if qianchuan_summary or adq_with_data or channel_with_data else "已关联成片，等待投放平台回流",
        "updated_at": (max(latest_updates).isoformat() + "Z") if latest_updates else None,
        "qianchuan": {
            "task_count": int(qianchuan_tasks),
            "gmv_yuan": qianchuan_summary["gmv_yuan"] if qianchuan_summary else None,
            "target_count": qianchuan_summary["target_count"] if qianchuan_summary else 0,
            "updated_at": qianchuan_summary["updated_at"].isoformat() + "Z" if qianchuan_summary else None,
        } if qianchuan_tasks else None,
        "adq": {
            "task_count": len(adq_rows),
            "data_task_count": adq_with_data,
            "updated_at": adq_latest.isoformat() + "Z" if adq_latest else None,
        } if adq_rows else None,
        "channels": {
            "task_count": len(channel_rows),
            "data_task_count": len(channel_with_data),
            "view_count": sum(row.view_count or 0 for row in channel_with_data) if channel_with_data else None,
            "like_count": sum(row.like_count or 0 for row in channel_with_data) if channel_with_data else None,
            "comment_count": sum(row.comment_count or 0 for row in channel_with_data) if channel_with_data else None,
            "share_count": sum(row.share_count or 0 for row in channel_with_data) if channel_with_data else None,
            "order_count": sum(row.order_count or 0 for row in channel_with_data) if channel_with_data else None,
            "gmv_yuan": round(sum(row.gmv_fen or 0 for row in channel_with_data) / 100, 2) if channel_with_data else None,
            "updated_at": max((row.metrics_updated_at for row in channel_with_data), default=None).isoformat() + "Z" if channel_with_data else None,
        } if channel_rows else None,
    }


_reference_preview_lock = Lock()
_reference_preview_slots = BoundedSemaphore(1)
_reference_preview_active: set[str] = set()


def _video_request_reference_entries(item: VideoRequest) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for raw in item.reference_videos or []:
        if not isinstance(raw, dict):
            continue
        object_key = str(raw.get("object_key") or "").strip()
        if not object_key:
            continue
        entry = {
            "object_key": object_key,
            "filename": str(raw.get("filename") or Path(object_key).name),
        }
        for field in ("preview_object_key", "preview_status", "preview_error"):
            value = str(raw.get(field) or "").strip()
            if value:
                entry[field] = value
        entries.append(entry)
    if item.reference_video_key and not any(
        entry["object_key"] == item.reference_video_key for entry in entries
    ):
        entries.insert(0, {
            "object_key": item.reference_video_key,
            "filename": item.reference_video_name or Path(item.reference_video_key).name,
        })
    return entries


def _reference_preview_key(object_key: str) -> str:
    digest = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:24]
    return f"{settings.prefix.rstrip('/')}/reference-previews/{digest}.mp4"


def _reference_video_probe(object_key: str) -> dict[str, str]:
    source_url = oss_service.url_for(object_key, expires_in=7200)
    if not source_url:
        raise RuntimeError("参考视频读取地址不可用")
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error", "-show_entries",
                "stream=codec_name,codec_type,pix_fmt", "-of", "json", source_url,
            ],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("参考视频编码检查超时") from error
    if result.returncode != 0:
        raise RuntimeError("参考视频编码检查失败")
    try:
        streams = json.loads(result.stdout or "{}").get("streams") or []
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("参考视频编码信息不可读取") from error
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    if not video:
        raise RuntimeError("参考文件中没有可用视频轨道")
    return {
        "codec_name": str(video.get("codec_name") or "").lower(),
        "pix_fmt": str(video.get("pix_fmt") or "").lower(),
        "source_url": source_url,
    }


def _build_reference_preview(object_key: str, output_path: Path) -> str:
    probe = _reference_video_probe(object_key)
    if probe["codec_name"] == "h264" and probe["pix_fmt"] in {"yuv420p", "yuvj420p"}:
        return object_key
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-v", "error", "-i", probe["source_url"],
                "-map", "0:v:0", "-map", "0:a?", "-vf",
                "scale='min(720,iw)':-2:flags=lanczos,format=yuv420p",
                "-c:v", "libx264", "-profile:v", "high", "-level", "4.1",
                "-preset", "veryfast", "-crf", "24", "-threads", "2",
                "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart",
                str(output_path),
            ],
            capture_output=True,
            timeout=900,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("兼容预览生成超时") from error
    if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size <= 0:
        raise RuntimeError("兼容预览生成失败")
    preview_key = _reference_preview_key(object_key)
    oss_service.upload_reference_preview(str(output_path), preview_key)
    return preview_key


def _run_reference_preview_job(request_id: str) -> None:
    try:
        with _reference_preview_slots, SessionLocal() as db:
            item = db.get(VideoRequest, request_id)
            if not item:
                return
            entries = _video_request_reference_entries(item)
            for index, entry in enumerate(entries):
                if entry.get("preview_status") == "ready" and entry.get("preview_object_key"):
                    continue
                entries[index] = {**entry, "preview_status": "processing", "preview_error": ""}
                item.reference_videos = [dict(value) for value in entries]
                db.commit()
                try:
                    preview_key = _reference_preview_key(entry["object_key"])
                    existing = oss_service.head_asset(preview_key)
                    if existing and existing.get("size"):
                        resolved_key = preview_key
                    else:
                        with tempfile.TemporaryDirectory(prefix="wis-reference-preview-") as temp_dir:
                            resolved_key = _build_reference_preview(
                                entry["object_key"], Path(temp_dir) / "preview.mp4"
                            )
                    entries[index] = {
                        **entry,
                        "preview_object_key": resolved_key,
                        "preview_status": "ready",
                        "preview_error": "",
                    }
                except Exception as error:
                    logger.warning("Reference preview failed request_id=%s index=%s error=%s", request_id, index, type(error).__name__)
                    entries[index] = {
                        **entry,
                        "preview_status": "failed",
                        "preview_error": str(error)[:160] or "兼容预览生成失败",
                    }
                item.reference_videos = [dict(value) for value in entries]
                db.commit()
    finally:
        with _reference_preview_lock:
            _reference_preview_active.discard(request_id)


def _video_request_out(db: Session, item: VideoRequest, user: dict) -> dict:
    deliveries = db.execute(
        select(VideoRequestDelivery, Asset)
        .join(Asset, Asset.id == VideoRequestDelivery.asset_id)
        .where(VideoRequestDelivery.request_id == item.id)
        .order_by(VideoRequestDelivery.version.desc())
    ).all()
    events = db.scalars(
        select(VideoRequestEvent)
        .where(VideoRequestEvent.request_id == item.id)
        .order_by(VideoRequestEvent.created_at.desc())
        .limit(30)
    ).all()
    latest_asset = db.get(Asset, item.latest_asset_id) if item.latest_asset_id else None
    latest_submission_id = deliveries[0][0].submission_id if deliveries else ""
    latest_delivery_rows = [
        (delivery, asset) for delivery, asset in deliveries
        if (latest_submission_id and delivery.submission_id == latest_submission_id)
        or (not latest_submission_id and delivery.version == item.delivery_version)
    ]
    progress_percent, progress_label = VIDEO_REQUEST_PROGRESS.get(
        item.status, (0, VIDEO_REQUEST_STATUS_LABELS.get(item.status, item.status))
    )
    reference_videos = _video_request_reference_entries(item)
    return {
        "id": item.id,
        "product": item.product,
        "description": item.description,
        "reference_url": item.reference_url,
        "reference_video_key": item.reference_video_key,
        "reference_video_name": item.reference_video_name,
        "reference_video_url": oss_service.url_for(item.reference_video_key) if item.reference_video_key else "",
        "reference_videos": [
            {
                **reference,
                "original_url": oss_service.url_for(reference["object_key"]),
                "preview_url": oss_service.url_for(reference.get("preview_object_key") or "")
                if reference.get("preview_status") == "ready" and reference.get("preview_object_key") else "",
                "url": oss_service.url_for(reference.get("preview_object_key") or reference["object_key"]),
                "preview_status": reference.get("preview_status") or "pending",
                "preview_error": reference.get("preview_error") or "",
            }
            for reference in reference_videos
        ],
        "reference_images": [
            {
                "object_key": str(reference.get("object_key") or ""),
                "filename": str(reference.get("filename") or "参考图片"),
                "url": oss_service.url_for(str(reference.get("object_key") or "")),
            }
            for reference in (item.reference_images or [])
            if str(reference.get("object_key") or "")
        ],
        "requester_number": item.requester_number,
        "requester_name": item.requester_name,
        "requester_department": item.requester_department,
        "assignee_number": item.assignee_number,
        "assignee_name": item.assignee_name,
        "status": item.status,
        "status_label": VIDEO_REQUEST_STATUS_LABELS.get(item.status, item.status),
        "progress_percent": progress_percent,
        "progress_label": progress_label,
        "latest_asset_id": item.latest_asset_id,
        "latest_asset": {
            "id": latest_asset.id,
            "filename": latest_asset.filename,
            "preview_url": oss_service.url_for(latest_asset.object_key),
            "download_url": oss_service.url_for(latest_asset.object_key, download=True),
            "cover_url": latest_asset.cover_url or "",
        } if latest_asset and not latest_asset.purged_at else None,
        "delivery_version": item.delivery_version,
        "latest_feedback": item.latest_feedback,
        "return_reason": item.return_reason,
        "returned_by_number": item.returned_by_number,
        "returned_by_name": item.returned_by_name,
        "returned_at": item.returned_at.isoformat() + "Z" if item.returned_at else None,
        "assigned_at": item.assigned_at.isoformat() + "Z" if item.assigned_at else None,
        "started_at": item.started_at.isoformat() + "Z" if item.started_at else None,
        "delivered_at": item.delivered_at.isoformat() + "Z" if item.delivered_at else None,
        "accepted_at": item.accepted_at.isoformat() + "Z" if item.accepted_at else None,
        "created_at": item.created_at.isoformat() + "Z",
        "updated_at": item.updated_at.isoformat() + "Z",
        "permissions": {
            "can_assign": is_video_request_assigner(user),
            "can_work": _video_request_is_assignee(item, user) or is_video_request_assigner(user),
            "can_review": _video_request_is_requester(item, user),
            "can_view_supervisor": _video_request_supervisor_viewer(user),
        },
        "deliveries": [
            {
                "id": delivery.id,
                "version": delivery.version,
                "submission_id": delivery.submission_id,
                "asset_id": asset.id,
                "filename": asset.filename,
                "preview_url": oss_service.url_for(asset.object_key) if not asset.purged_at else "",
                "cover_url": asset.cover_url or "",
                "category": asset.category,
                "content_type": asset.content_type,
                "asset_subtype": asset.asset_subtype,
                "folder_name": asset.folder_name,
                "tags": asset.tags or [],
                "note": delivery.note,
                "uploaded_by_name": delivery.uploaded_by_name,
                "created_at": delivery.created_at.isoformat() + "Z",
            }
            for delivery, asset in deliveries
        ],
        "latest_assets": [
            {
                "id": asset.id,
                "filename": asset.filename,
                "preview_url": oss_service.url_for(asset.object_key) if not asset.purged_at else "",
                "download_url": oss_service.url_for(asset.object_key, download=True) if not asset.purged_at else "",
                "cover_url": asset.cover_url or "",
                "category": asset.category,
                "content_type": asset.content_type,
                "asset_subtype": asset.asset_subtype,
            }
            for _, asset in sorted(latest_delivery_rows, key=lambda row: row[0].version)
        ],
        "events": [
            {
                "id": event.id,
                "action": event.action,
                "actor_name": event.actor_name,
                "detail": event.detail or {},
                "created_at": event.created_at.isoformat() + "Z",
            }
            for event in events
        ],
        "metrics": _video_request_metrics(db, item.latest_asset_id),
    }


def _get_visible_video_request(db: Session, request_id: str, user: dict) -> VideoRequest:
    item = db.get(VideoRequest, request_id)
    if not item or not _video_request_visible(item, user):
        raise HTTPException(404, "提需记录不存在")
    return item


@app.post("/api/video-requests")
def video_request_create(payload: VideoRequestCreate, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    reference_url = _normalized_reference_url(payload.reference_url)
    requested_videos = list(payload.reference_videos or [])
    if payload.reference_video_key and not any(
        str(reference.get("object_key") or "").strip() == payload.reference_video_key.strip()
        for reference in requested_videos
    ):
        requested_videos.insert(0, {
            "object_key": payload.reference_video_key,
            "filename": payload.reference_video_name,
        })
    reference_videos = _validated_video_request_references(requested_videos)
    reference_key = reference_videos[0]["object_key"] if reference_videos else ""
    reference_name = reference_videos[0]["filename"] if reference_videos else ""
    reference_images = _validated_reference_images(payload.reference_images)
    description = payload.description.strip()
    if not reference_url and not reference_videos and not reference_images and not description:
        raise HTTPException(400, "请至少填写竞对链接、上传参考视频或图片，或提供语言描述中的一项")
    request_id = str(uuid5(NAMESPACE_URL, f"wis-video-request:{user_number(user)}:{payload.client_request_id}")) if payload.client_request_id else str(uuid4())
    def existing_receipt():
        existing = db.get(VideoRequest, request_id)
        if existing is None:
            return None
        same = (existing.requester_number == user_number(user)
                and existing.product == canonical_product_category(payload.product, "通用")
                and existing.description == description and existing.reference_url == reference_url
                and (existing.reference_videos or []) == reference_videos
                and (existing.reference_images or []) == reference_images)
        if not same:
            raise HTTPException(409, "同一提交标识的内容已改变，请重新核对需求")
        return _video_request_out(db, existing, user)
    if payload.client_request_id:
        receipt = existing_receipt()
        if receipt is not None:
            return receipt
    item = VideoRequest(
        id=request_id,
        product=canonical_product_category(payload.product, "通用"),
        description=description,
        reference_url=reference_url,
        reference_video_key=reference_key,
        reference_video_name=reference_name,
        reference_videos=reference_videos,
        reference_images=reference_images,
        requester_number=user_number(user),
        requester_name=user_name(user),
        requester_department=str(user.get("groupName") or user.get("department") or "")[:255],
        status="submitted",
    )
    db.add(item)
    _video_request_event(db, item, "submitted", user, {"product": item.product})
    _notify_video_request(
        db,
        recipient_name=os.getenv("VIDEO_REQUEST_ASSIGNER_NAME", "何雨庭"),
        recipient_number=os.getenv("VIDEO_REQUEST_ASSIGNER_NUMBER", ""),
        title=f"新视频提需 · {item.product}",
        message=f"{item.requester_name} 已提交视频制作需求，请安排制作人。\n需求说明：{item.description[:400]}",
        request_id=item.id,
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if payload.client_request_id:
            receipt = existing_receipt()
            if receipt is not None:
                return receipt
        raise
    return _video_request_out(db, item, user)


@app.get("/api/video-requests")
def video_request_list(
    scope: str = Query(default="mine", pattern="^(mine|assigned|all)$"),
    status: str = Query(default="all"),
    q: str = Query(default="", max_length=200),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=5, le=30),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    number = user_number(user)
    name = user_name(user)
    if scope == "all" and not (is_operation_admin(user) or _video_request_supervisor_viewer(user)):
        scope = "mine"
    if scope == "assigned":
        filters = [or_(
            VideoRequest.assignee_number == number,
            and_(VideoRequest.assignee_number == "", VideoRequest.assignee_name == name),
        )]
    elif scope == "all":
        filters = []
    else:
        filters = [VideoRequest.requester_number == number]
    if status == "production_pending":
        filters.append(VideoRequest.status.in_({
            "submitted", "assigned", "in_production", "revision_requested"
        }))
    elif status == "completed":
        filters.append(VideoRequest.status == "accepted")
    elif status != "all":
        filters.append(VideoRequest.status == status)
    if q.strip():
        pattern = q.strip()
        filters.append(or_(
            VideoRequest.product.contains(pattern),
            VideoRequest.description.contains(pattern),
            VideoRequest.requester_name.contains(pattern),
            VideoRequest.assignee_name.contains(pattern),
        ))
    total = db.scalar(select(func.count()).select_from(VideoRequest).where(*filters)) or 0
    items = db.scalars(
        select(VideoRequest).where(*filters)
        .order_by(VideoRequest.updated_at.desc())
        .offset((page - 1) * page_size).limit(page_size)
    ).all()
    return {
        "items": [_video_request_out(db, item, user) for item in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "scope": scope,
    }


@app.get("/api/video-requests/assignees")
def video_request_assignees(
    q: str = Query(default="", max_length=120),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_video_request_assigner(user)
    candidates = _video_request_assignee_candidates(db)
    keyword = q.strip().casefold()
    items = [item for item in candidates.values() if not keyword or keyword in f"{item['name']} {item['number']} {item['department']}".casefold()]
    items.sort(key=lambda item: (item["name"], item["number"]))
    return {"items": items, "total": len(items)}


def _video_request_assignee_candidates(db: Session) -> dict[str, dict]:
    """Use verified center membership, never infer it from previous activity."""
    try:
        roster = json.loads(VIDEO_REQUEST_ASSIGNEE_ROSTER_PATH.read_text(encoding="utf-8"))
        members = roster["members"]
        if not isinstance(members, list) or not all(isinstance(member, dict) for member in members):
            raise ValueError("invalid members")
    except (OSError, ValueError, KeyError, TypeError):
        raise HTTPException(503, "视频中心人员名单暂不可用，请稍后重试")

    grants = db.scalars(select(OaAccessGrant)).all()
    inactive_numbers = {row.user_number.strip().upper() for row in grants if not row.active and row.user_number.strip()}
    inactive_names = {_normalized_person_name(row.real_name) for row in grants if not row.active and not row.user_number.strip()}
    candidates: dict[str, dict] = {}

    def add(number: str, name: str) -> None:
        number = str(number or "").strip().upper()
        name = str(name or "").strip()
        normalized_name = _normalized_person_name(name)
        if not number or not name:
            return
        if number in VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NUMBERS or normalized_name in VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NAMES:
            return
        if number in inactive_numbers or normalized_name in inactive_names:
            return
        candidates[number] = {"number": number, "name": name, "department": "品牌营销部-视频中心"}

    # The verified snapshot covers members who have never uploaded or logged in.
    for member in members:
        add(member.get("number", ""), member.get("name", ""))
    # New active OA members are visible without needing an upload first.
    for grant in grants:
        if grant.active and "视频中心" in f"{grant.department} {grant.center}":
            add(grant.user_number, grant.real_name)
    return candidates


def _resolve_video_request_assignee(db: Session, payload: VideoRequestAssign) -> dict:
    number = payload.assignee_number.strip().upper()
    name = _normalized_person_name(payload.assignee_name)
    if number in VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NUMBERS or name in VIDEO_REQUEST_EXCLUDED_ASSIGNEE_NAMES:
        raise HTTPException(409, "赵佳乐不在可分配对象范围内")
    candidates = _video_request_assignee_candidates(db)
    if number:
        candidate = candidates.get(number)
        if candidate and name != _normalized_person_name(candidate["name"]):
            raise HTTPException(422, "制作人工号与姓名不匹配，请重新选择")
    else:
        matches = [item for item in candidates.values() if _normalized_person_name(item["name"]) == name]
        candidate = matches[0] if len(matches) == 1 else None
    if not candidate:
        raise HTTPException(409, "只能分配给当前视频中心可分配名单中的同事，请刷新后重新选择")
    return candidate


@app.get("/api/video-requests/{request_id}")
def video_request_detail(request_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    return _video_request_out(db, _get_visible_video_request(db, request_id, user), user)


@app.post("/api/video-requests/{request_id}/reference-previews")
def video_request_reference_previews(
    request_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    item = _get_visible_video_request(db, request_id, user)
    entries = _video_request_reference_entries(item)
    if not entries:
        return {"status": "empty", "request_id": request_id}
    if all(
        entry.get("preview_status") == "ready" and entry.get("preview_object_key")
        for entry in entries
    ):
        return {"status": "ready", "request_id": request_id}
    with _reference_preview_lock:
        if request_id in _reference_preview_active:
            return {"status": "processing", "request_id": request_id}
        _reference_preview_active.add(request_id)
    try:
        background_tasks.add_task(_run_reference_preview_job, request_id)
    except Exception:
        with _reference_preview_lock:
            _reference_preview_active.discard(request_id)
        raise
    return {"status": "queued", "request_id": request_id}


@app.post("/api/video-requests/{request_id}/assign")
def video_request_assign(request_id: str, payload: VideoRequestAssign, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    require_video_request_assigner(user)
    item = db.get(VideoRequest, request_id)
    if not item:
        raise HTTPException(404, "提需记录不存在")
    if item.status in {"accepted", "returned"}:
        raise HTTPException(409, "已结束的提需不可重新分配")
    assignee = _resolve_video_request_assignee(db, payload)
    item.assignee_number = assignee["number"]
    item.assignee_name = assignee["name"]
    item.status = "assigned"
    item.assigned_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    _video_request_event(db, item, "assigned", user, {"assignee_name": item.assignee_name})
    _notify_video_request(
        db,
        recipient_number=item.assignee_number,
        recipient_name=item.assignee_name,
        title=f"视频制作任务 · {item.product}",
        message=(
            f"提需人：{item.requester_name}\n"
            f"需求：{item.description[:240]}\n"
            f"{user_name(user)} 已将任务分配给你，请进入视频中心查看参考素材并开始制作。"
        ),
        request_id=item.id,
    )
    db.commit()
    return _video_request_out(db, item, user)


@app.post("/api/video-requests/{request_id}/return")
def video_request_return(
    request_id: str,
    payload: VideoRequestReturn,
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    require_video_request_assigner(user)
    item = db.get(VideoRequest, request_id)
    if not item:
        raise HTTPException(404, "提需记录不存在")
    if item.status not in {"submitted", "assigned", "in_production", "revision_requested"}:
        raise HTTPException(409, "当前状态不能退回")
    reason = payload.reason.strip()
    item.status = "returned"
    item.return_reason = reason
    item.returned_by_number = user_number(user)
    item.returned_by_name = user_name(user)
    item.returned_at = datetime.utcnow()
    item.updated_at = item.returned_at
    _video_request_event(db, item, "returned", user, {"reason": reason})
    _notify_video_request(
        db,
        recipient_number=item.requester_number,
        recipient_name=item.requester_name,
        title=f"视频需求已退回 · {item.product}",
        message=f"退回原因：{reason}",
        request_id=item.id,
    )
    if item.assignee_name:
        _notify_video_request(
            db,
            recipient_number=item.assignee_number,
            recipient_name=item.assignee_name,
            title=f"视频任务已退回 · {item.product}",
            message=f"退回原因：{reason}",
            request_id=item.id,
        )
    db.commit()
    return _video_request_out(db, item, user)


@app.post("/api/video-requests/{request_id}/start")
def video_request_start(request_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    item = _get_visible_video_request(db, request_id, user)
    if not (_video_request_is_assignee(item, user) or is_video_request_assigner(user)):
        raise HTTPException(403, "仅该任务制作人可以开始制作")
    if item.status not in {"assigned", "revision_requested"}:
        raise HTTPException(409, "当前状态不能开始制作")
    item.status = "in_production"
    item.started_at = item.started_at or datetime.utcnow()
    item.updated_at = datetime.utcnow()
    _video_request_event(db, item, "started", user)
    db.commit()
    return _video_request_out(db, item, user)


@app.post("/api/video-requests/{request_id}/deliver")
def video_request_deliver(request_id: str, payload: VideoRequestDeliver, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    item = _get_visible_video_request(db, request_id, user)
    if not (_video_request_is_assignee(item, user) or is_video_request_assigner(user)):
        raise HTTPException(403, "仅该任务制作人可以上传成片")
    if item.status not in {"assigned", "in_production", "revision_requested"}:
        raise HTTPException(409, "当前状态不能上传成片")
    asset_ids = list(dict.fromkeys([*payload.asset_ids, *([payload.asset_id] if payload.asset_id else [])]))
    if not asset_ids:
        raise HTTPException(400, "请至少选择一条有效视频成片")
    if len(asset_ids) > 10:
        raise HTTPException(400, "每次最多提交 10 条成片")
    assets = [db.get(Asset, asset_id) for asset_id in asset_ids]
    if any(not asset or asset.deleted_at or asset.purged_at or asset.media_type != "video" for asset in assets):
        raise HTTPException(400, "请选择素材库中有效的视频成片")
    if not is_video_request_assigner(user) and any(
        asset.uploaded_by_number != user_number(user) for asset in assets if asset
    ):
        raise HTTPException(403, "只能提交自己上传的成片")
    submission_id = str(uuid4())
    first_version = item.delivery_version + 1
    for offset, asset in enumerate(assets):
        # Preserve the per-file category/type chosen during upload while
        # enforcing that every delivered result lands in the source library.
        asset.asset_scope = "marketing_video"
        asset.library_type = "source"
        asset.asset_subtype = asset.asset_subtype or "AI原创素材"
        selected_category = asset.category if asset.category not in {"", "待分类"} else item.product
        asset.category = canonical_product_category(selected_category)
        db.add(VideoRequestDelivery(
            id=str(uuid4()),
            request_id=item.id,
            asset_id=asset.id,
            version=first_version + offset,
            submission_id=submission_id,
            note=payload.note.strip(),
            uploaded_by_number=user_number(user),
            uploaded_by_name=user_name(user),
        ))
    final_version = first_version + len(assets) - 1
    item.latest_asset_id = assets[-1].id
    item.delivery_version = final_version
    item.status = "delivered"
    item.latest_feedback = ""
    item.delivered_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    _video_request_event(db, item, "delivered", user, {
        "asset_ids": asset_ids,
        "asset_count": len(asset_ids),
        "submission_id": submission_id,
        "version_start": first_version,
        "version_end": final_version,
    })
    _notify_video_request(
        db,
        recipient_number=item.requester_number,
        recipient_name=item.requester_name,
        title=f"成片待验收 · {item.product}",
        message=(
            f"制作人：{item.assignee_name or user_name(user)}\n"
            f"本批成片：{len(asset_ids)} 条\n"
            f"需求：{item.description[:220]}\n"
            "请进入视频中心查看成片，并确认通过或提出修改意见。"
        ),
        request_id=item.id,
    )
    db.commit()
    return _video_request_out(db, item, user)


@app.post("/api/video-requests/{request_id}/accept")
def video_request_accept(request_id: str, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    item = _get_visible_video_request(db, request_id, user)
    if not _video_request_is_requester(item, user):
        raise HTTPException(403, "仅提需人可以验收成片")
    if item.status != "delivered":
        raise HTTPException(409, "当前没有待验收成片")
    item.status = "accepted"
    item.accepted_at = datetime.utcnow()
    item.updated_at = datetime.utcnow()
    _video_request_event(db, item, "accepted", user)
    _notify_video_request(
        db, recipient_number=item.assignee_number, recipient_name=item.assignee_name,
        title=f"成片已验收 · {item.product}", message=f"{item.requester_name} 已确认第 {item.delivery_version} 版成片。",
        request_id=item.id,
    )
    db.commit()
    return _video_request_out(db, item, user)


@app.post("/api/video-requests/{request_id}/revision")
def video_request_revision(request_id: str, payload: VideoRequestFeedback, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    item = _get_visible_video_request(db, request_id, user)
    if not _video_request_is_requester(item, user):
        raise HTTPException(403, "仅提需人可以提出修改意见")
    if item.status != "delivered":
        raise HTTPException(409, "当前没有待反馈成片")
    item.status = "revision_requested"
    item.latest_feedback = payload.feedback.strip()
    item.updated_at = datetime.utcnow()
    _video_request_event(db, item, "revision_requested", user, {"feedback": item.latest_feedback})
    message = f"{item.requester_name} 对第 {item.delivery_version} 版成片提出修改意见：{item.latest_feedback}"
    _notify_video_request(
        db, recipient_number=item.assignee_number, recipient_name=item.assignee_name,
        title=f"成片需修改 · {item.product}", message=message, request_id=item.id,
    )
    _notify_video_request(
        db, recipient_name=os.getenv("VIDEO_REQUEST_ASSIGNER_NAME", "何雨庭"), recipient_number=os.getenv("VIDEO_REQUEST_ASSIGNER_NUMBER", ""),
        title=f"成片返修 · {item.product}", message=message, request_id=item.id,
    )
    db.commit()
    return _video_request_out(db, item, user)


def _notification_filter(user: dict):
    return or_(
        UserNotification.recipient_number == user_number(user),
        UserNotification.recipient_name == user_name(user),
    )


@app.get("/api/notifications")
def notifications_list(
    limit: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
    user: dict = Depends(require_user),
):
    owner_filter = _notification_filter(user)
    items = db.scalars(
        select(UserNotification).where(owner_filter).order_by(UserNotification.created_at.desc()).limit(limit)
    ).all()
    unread = db.scalar(
        select(func.count()).select_from(UserNotification).where(owner_filter, UserNotification.read_at.is_(None))
    ) or 0
    return {
        "items": [{
            "id": item.id, "kind": item.kind, "title": item.title, "message": item.message,
            "resource_type": item.resource_type, "resource_id": item.resource_id,
            "external_status": item.external_status,
            "external_message_id": item.external_message_id,
            "external_error": item.external_error if item.external_status == "failed" else "",
            "external_sent_at": item.external_sent_at.isoformat() + "Z" if item.external_sent_at else None,
            "read_at": item.read_at.isoformat() + "Z" if item.read_at else None,
            "created_at": item.created_at.isoformat() + "Z",
        } for item in items],
        "unread": unread,
    }


@app.post("/api/notifications/{notification_id}/read")
def notification_read(notification_id: int, db: Session = Depends(get_db), user: dict = Depends(require_user)):
    item = db.scalar(select(UserNotification).where(UserNotification.id == notification_id, _notification_filter(user)))
    if not item:
        raise HTTPException(404, "通知不存在")
    item.read_at = item.read_at or datetime.utcnow()
    db.commit()
    return {"ok": True, "id": item.id}


@app.post("/api/notifications/read-all")
def notifications_read_all(db: Session = Depends(get_db), user: dict = Depends(require_user)):
    items = db.scalars(select(UserNotification).where(_notification_filter(user), UserNotification.read_at.is_(None))).all()
    now = datetime.utcnow()
    for item in items:
        item.read_at = now
    db.commit()
    return {"ok": True, "count": len(items)}


app.include_router(muse_router)

from .private_assets import build_private_router


def _publish_private_asset(db: Session, private, file: Path, user: dict) -> dict:
    # A deterministic object key makes retries reuse one public copy. This is
    # reached only through the owner-authenticated, explicit confirmation route.
    key = f"{settings.prefix.rstrip('/')}/uploads/private-shared/{private.id}/{private.filename}"
    asset = db.get(Asset, private.shared_asset_id) if private.shared_asset_id else None
    if asset and (asset.deleted_at or asset.purged_at):
        raise HTTPException(409, '共享副本已被移入回收站，请在素材大厅处理，不自动恢复')
    if not asset:
        asset = db.scalar(select(Asset).where(Asset.object_key == key))
    if not asset:
        try:
            oss_service.upload_private_share(str(file), key, private.content_type)
        except (ValueError, RuntimeError) as error:
            raise HTTPException(502, str(error)) from error
        asset = Asset(object_key=key, filename=private.filename,
                      media_type='video' if private.content_type.startswith('video/') else 'image',
                      size=private.size, category=canonical_product_category(private.category),
                      folder_name=private.folder_name, ingest_source='oa_upload',
                      uploaded_by_number=private.owner_number, uploaded_by_name=private.owner_name,
                      tags=['主动共享'], asset_scope='marketing_video', library_type='source')
        db.add(asset)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            asset = db.scalar(select(Asset).where(Asset.object_key == key))
            if not asset:
                raise
    if asset.deleted_at or asset.purged_at:
        raise HTTPException(409, '共享副本已被移入回收站，请在素材大厅处理')
    private.shared_asset_id = asset.id
    private.updated_at = datetime.utcnow()
    _audit_asset(db, asset, 'private_share', actor_number=user_number(user), actor_name=user_name(user), detail='本人主动共享；私人原文件保留')
    db.commit()
    _invalidate_catalog_cache()
    return {'shared_asset_id': asset.id, 'private_original_preserved': True}


app.include_router(build_private_router(require_user, require_workstation, _publish_private_asset))

from .workspace_roles import install_workspace_roles
install_workspace_roles(app, require_user, require_permission_manager, MODULE_CATALOG)


frontend_dist = Path(__file__).resolve().parents[2] / "frontend" / (
    "integrated-dist-verified" if os.getenv("HUB_INTEGRATED_MODE") == "1" else "dist"
)
if os.getenv("HUB_INTEGRATED_MODE") == "1":
    expected_index_sha = os.getenv("CLOUD_INTEGRATED_INDEX_SHA256", "").strip().lower()
    index_file = frontend_dist / "index.html"
    if not re.fullmatch(r"[0-9a-f]{64}", expected_index_sha) or not index_file.is_file():
        raise RuntimeError("云管家集成版前端未安装或缺少固定 SHA")
    installed_index_sha = hashlib.sha256(index_file.read_bytes()).hexdigest()
    # r46 updates the audit-video frontend in the existing container. The
    # container's pinned env remains the prior digest until the next image
    # release; accept only this recorded before/after pair.
    reviewed_overlay = (
        expected_index_sha == "441ad4190f9b47c43b5982b31535ee93047b5376a6668084a0331990603c5dd0"
        and installed_index_sha == "ffb9d6eea797072905dde797c3219d6a246f6aec70c17117bab8817534887104"
    )
    if installed_index_sha != expected_index_sha and not reviewed_overlay:
        raise RuntimeError("云管家集成版前端 SHA 与发布记录不一致")
if frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=frontend_dist, html=True), name="frontend")
