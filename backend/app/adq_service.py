from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from sqlalchemy.orm import Session

from .config import settings
from .models import AppMeta
from .oss_service import oss_service


API_ROOT = "https://api.e.qq.com/v3.0"
LEGACY_API_ROOT = "https://api.e.qq.com/v1.3"
ASSET_API_ROOT = "https://api.e.qq.com/v1.1"
TOKEN_URL = "https://api.e.qq.com/oauth/token"
ADVERTISER_URL = f"{API_ROOT}/advertiser/get"
BUSINESS_MANAGER_RELATIONS_URL = f"{LEGACY_API_ROOT}/business_manager_relations/get"
BUSINESS_MDM_RELATIONS_URL = f"{LEGACY_API_ROOT}/business_mdm_account_relations/get"
CAMPAIGNS_URL = f"{LEGACY_API_ROOT}/campaigns/get"
ADGROUPS_URL = f"{API_ROOT}/adgroups/get"
VIDEOS_GET_URL = f"{API_ROOT}/videos/get"
VIDEOS_ADD_URL = f"{LEGACY_API_ROOT}/videos/add"
ASSET_PERMISSIONS_ADD_URL = f"{ASSET_API_ROOT}/asset_permissions/add"
DYNAMIC_CREATIVES_GET_URL = f"{API_ROOT}/dynamic_creatives/get"
DYNAMIC_CREATIVES_ADD_URL = f"{API_ROOT}/dynamic_creatives/add"
DAILY_REPORTS_URL = f"{API_ROOT}/daily_reports/get"
USER_AUTHORIZE_URL = "https://ad.qq.com/account-center/single/user-authorize"

ADGROUP_FIELDS = [
    "adgroup_id",
    "adgroup_name",
    "campaign_id",
    "configured_status",
    "system_status",
    "created_time",
    "last_modified_time",
]
CAMPAIGN_FIELDS = [
    "campaign_id",
    "campaign_name",
    "configured_status",
    "campaign_type",
    "promoted_object_type",
    "created_time",
    "last_modified_time",
    "is_deleted",
]
DYNAMIC_CREATIVE_FIELDS = [
    "dynamic_creative_id",
    "dynamic_creative_name",
    "adgroup_id",
    "configured_status",
    "creative_template_id",
    "delivery_mode",
    "dynamic_creative_type",
    "creative_components",
    "created_time",
    "last_modified_time",
]
VIDEO_FIELDS = [
    "video_id",
    "signature",
    "cover_id",
    "width",
    "height",
    "video_name",
]
REPORT_FIELDS = [
    "date",
    "adgroup_id",
    "video_id",
    "cost",
    "view_count",
    "valid_click_count",
    "order_amount",
    "order_roi",
    "order_24h_by_click_amount",
    "order_24h_by_click_roi",
    "order_net_amount",
    "order_net_roi",
]
ALLOWED_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi"}
MAX_VIDEO_BYTES = 500 * 1024 * 1024
DEFAULT_BUSINESS_UNITS = {
    "67858196": "WIS慕可",
    "85526701": "WIS新品",
}
DEFAULT_CATALOG_ACCOUNT_IDS = (
    "70856305", "71520518", "80431518", "80431526", "80431500", "70856302",
    "80431396", "71495119", "71520526", "71520521", "85522193", "80431403",
    "78391278", "70581102", "65909064", "70856299", "78391277", "82041713",
    "82041714", "85522197", "85522200",
)

logger = logging.getLogger(__name__)


class AdqError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str = "",
        request_id: str = "",
        category: str = "platform",
        retryable: bool = False,
        entity_id: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.category = category
        self.retryable = retryable
        self.entity_id = entity_id


def _meta(db: Session, key: str, fallback: str = "") -> str:
    row = db.get(AppMeta, key)
    return row.value if row else fallback


def _set_meta(db: Session, key: str, value: Any) -> None:
    text_value = "" if value is None else str(value)
    row = db.get(AppMeta, key)
    if row:
        row.value = text_value
    else:
        db.add(AppMeta(key=key, value=text_value))


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _items(payload: dict) -> list[dict]:
    data = payload.get("data") or {}
    values = data.get("list") or data.get("items") or []
    return [item for item in values if isinstance(item, dict)]


def _page_info(payload: dict) -> dict:
    return (payload.get("data") or {}).get("page_info") or {}


class AdqService:
    timeout = (10, 90)

    def __init__(self) -> None:
        self._catalog_lock = threading.Lock()
        self._catalog_cache: dict[str, tuple[float, Any]] = {}
        self._adgroup_write_locks_guard = threading.Lock()
        self._adgroup_write_locks: dict[str, threading.Lock] = {}
        self._shared_library_lock = threading.Lock()
        # ADQ refresh tokens rotate on use. Every request in this process must
        # share one refresh gate, otherwise concurrent status/catalog/metrics
        # calls can consume the same refresh token and invalidate each other.
        self._token_refresh_lock = threading.Lock()

    @property
    def app_id(self) -> str:
        return (os.getenv("ADQ_APP_ID", "") or os.getenv("ADQ_CLIENT_ID", "")).strip()

    @property
    def app_secret(self) -> str:
        return (os.getenv("ADQ_APP_SECRET", "") or os.getenv("ADQ_CLIENT_SECRET", "")).strip()

    @property
    def configured_account_id(self) -> str:
        values = self.configured_account_ids
        return values[0] if values else ""

    @property
    def configured_account_ids(self) -> list[str]:
        """Return the explicit ADQ delivery-account allowlist.

        Tencent's public API does not expose the customer-workbench business
        unit hierarchy as an account catalogue.  Keep the source-account and
        delivery-account concerns separate, and only expose account IDs that
        the operator has explicitly authorized for this application.
        """
        primary = os.getenv("ADQ_ACCOUNT_ID", "").strip()
        configured = re.split(r"[,;\s]+", os.getenv("ADQ_ACCOUNT_IDS", "").strip())
        result: list[str] = []
        for value in [primary, *configured]:
            normalized = value.strip()
            if normalized and normalized not in result:
                result.append(normalized)
        return result

    @staticmethod
    def _normalized_ids(value: Any) -> list[str]:
        if isinstance(value, (list, tuple, set)):
            candidates = [str(item) for item in value]
        else:
            candidates = re.split(r"[,;\s]+", str(value or "").strip())
        result: list[str] = []
        for candidate in candidates:
            normalized = str(candidate or "").strip()
            if normalized.isdigit() and normalized not in result:
                result.append(normalized)
        return result

    @property
    def subject_id(self) -> str:
        return os.getenv("ADQ_SUBJECT_ID", settings.adq_shared_mdm_id or "33471608").strip()

    @property
    def business_units_config(self) -> list[dict]:
        """Return the operator-owned ADQ business-unit catalogue.

        Tencent's public Marketing API accepts advertiser IDs, not a customer-
        workbench business-unit ID.  We therefore preserve the two business
        units as explicit grouping metadata and require their advertiser IDs
        to be configured instead of silently treating a business-unit ID as an
        advertiser ID.
        """
        units: list[dict] = []
        raw_json = os.getenv("ADQ_BUSINESS_UNITS_JSON", "").strip()
        if raw_json:
            try:
                parsed = json.loads(raw_json)
            except (TypeError, ValueError):
                parsed = []
            if isinstance(parsed, list):
                for raw in parsed:
                    if not isinstance(raw, dict):
                        continue
                    unit_id = str(raw.get("business_unit_id") or raw.get("id") or "").strip()
                    if not unit_id.isdigit():
                        continue
                    units.append({
                        "business_unit_id": unit_id,
                        "business_unit_name": str(
                            raw.get("business_unit_name") or raw.get("name") or f"WIS 业务单元 {unit_id}"
                        ).strip(),
                        "account_ids": self._normalized_ids(raw.get("account_ids")),
                        "seed_account_id": str(raw.get("seed_account_id") or "").strip(),
                    })
        configured_unit_ids = self._normalized_ids(
            os.getenv("ADQ_BUSINESS_UNIT_IDS", "67858196,85526701")
        )
        for unit_id in configured_unit_ids:
            existing = next((item for item in units if item["business_unit_id"] == unit_id), None)
            env_account_ids = self._normalized_ids(
                os.getenv(f"ADQ_BUSINESS_UNIT_{unit_id}_ACCOUNT_IDS", "")
            )
            if existing:
                existing["account_ids"] = self._normalized_ids([*existing["account_ids"], *env_account_ids])
                if not existing["seed_account_id"]:
                    existing["seed_account_id"] = os.getenv(
                        f"ADQ_BUSINESS_UNIT_{unit_id}_SEED_ACCOUNT_ID", ""
                    ).strip()
                continue
            units.append({
                "business_unit_id": unit_id,
                "business_unit_name": os.getenv(
                    f"ADQ_BUSINESS_UNIT_{unit_id}_NAME",
                    DEFAULT_BUSINESS_UNITS.get(unit_id, f"WIS 业务单元 {unit_id}"),
                ).strip(),
                "account_ids": env_account_ids,
                "seed_account_id": os.getenv(
                    f"ADQ_BUSINESS_UNIT_{unit_id}_SEED_ACCOUNT_ID", ""
                ).strip(),
            })
        return units

    @property
    def configured_catalog_account_ids(self) -> list[str]:
        result = self._normalized_ids(
            os.getenv("ADQ_CATALOG_ACCOUNT_IDS", ",".join(DEFAULT_CATALOG_ACCOUNT_IDS))
        )
        for unit in self.business_units_config:
            result = self._normalized_ids([*result, *unit["account_ids"]])
        return result

    def _catalog_account_ids(self) -> list[str]:
        discovered = self._cache_get("catalog-account-ids", max_age=3600) or []
        return self._normalized_ids([*self.configured_catalog_account_ids, *discovered])

    def _user_token(self, db: Session) -> str:
        stored = _meta(db, "adq.user_token").strip()
        stored_app_id = _meta(db, "adq.user_token_app_id").strip()
        environment = os.getenv("ADQ_USER_TOKEN", "").strip()
        environment_app_id = os.getenv("ADQ_USER_TOKEN_APP_ID", self.app_id).strip()
        if stored and stored_app_id == self.app_id:
            return stored
        if environment and environment_app_id == self.app_id:
            return environment
        return ""

    def _user_token_expires_at(self, db: Session) -> datetime | None:
        raw = (
            _meta(db, "adq.user_token_expires_at").strip()
            or os.getenv("ADQ_USER_TOKEN_EXPIRES_AT", "").strip()
        )
        if not raw:
            return None
        if raw.isdigit():
            try:
                return datetime.fromtimestamp(int(raw), tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None
        return _parse_time(raw)

    def user_authorization_status(self, db: Session) -> dict:
        token = self._user_token(db)
        expires_at = self._user_token_expires_at(db)
        expired = bool(expires_at and expires_at <= datetime.now(timezone.utc) + timedelta(seconds=60))
        authorized = bool(token and not expired)
        if authorized:
            message = "ADQ 操作人实名认证令牌可用"
        elif token and expired:
            message = "ADQ 操作人实名认证令牌已过期，请重新扫码认证"
        else:
            message = "创建投放创意前需完成一次 ADQ 操作人实名认证"
        return {
            "authorized": authorized,
            "message": message,
            "expires_at": expires_at.isoformat() if expires_at else None,
        }

    def user_authorize_url(self, db: Session, *, redirect_uri: str, owner_number: str) -> str:
        if not redirect_uri.startswith("https://"):
            raise AdqError("ADQ 实名认证回调地址必须使用 HTTPS", category="configuration")
        state = uuid4().hex
        _set_meta(db, "adq.user_auth_state", state)
        _set_meta(
            db,
            "adq.user_auth_state_expires",
            (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat(),
        )
        _set_meta(db, "adq.user_auth_owner", owner_number[:80])
        db.commit()
        return f"{USER_AUTHORIZE_URL}?{urlencode({'redirect_uri': redirect_uri, 'state': state})}"

    def complete_user_authorization(
        self,
        db: Session,
        *,
        state: str,
        user_status: int,
        user_token: str,
        expire_time: str,
    ) -> dict:
        expected_state = _meta(db, "adq.user_auth_state").strip()
        expires = _parse_time(_meta(db, "adq.user_auth_state_expires").strip())
        _set_meta(db, "adq.user_auth_state", "")
        _set_meta(db, "adq.user_auth_state_expires", "")
        if not state or state != expected_state or not expires or expires <= datetime.now(timezone.utc):
            db.commit()
            raise AdqError("ADQ 实名认证回调已过期或校验失败，请重新发起", category="authorization")
        if int(user_status) != 2:
            db.commit()
            message = {
                0: "未找到对应的腾讯广告员工账号，请先加入服务商系统或客户工作台",
                1: "腾讯广告员工账号尚未完成实名认证，请完成后重试",
            }.get(int(user_status), "腾讯广告实名认证未通过，请重新发起")
            raise AdqError(message, category="authorization")
        normalized_token = str(user_token or "").strip()
        if not normalized_token or len(normalized_token) > 256:
            db.commit()
            raise AdqError("腾讯广告未返回有效的实名认证令牌", category="authorization")
        token_expires_at = self._parse_user_expire_time(expire_time)
        if token_expires_at and token_expires_at <= datetime.now(timezone.utc):
            db.commit()
            raise AdqError("腾讯广告返回的实名认证令牌已经过期", category="authorization")
        _set_meta(db, "adq.user_token", normalized_token)
        _set_meta(db, "adq.user_token_app_id", self.app_id)
        _set_meta(db, "adq.user_token_expires_at", token_expires_at.isoformat() if token_expires_at else "")
        _set_meta(db, "adq.user_token_saved_at", datetime.now(timezone.utc).isoformat())
        db.commit()
        return self.user_authorization_status(db)

    @staticmethod
    def _parse_user_expire_time(value: str) -> datetime | None:
        normalized = str(value or "").strip()
        if not normalized:
            return None
        if normalized.isdigit():
            try:
                return datetime.fromtimestamp(int(normalized), tz=timezone.utc)
            except (OSError, OverflowError, ValueError):
                return None
        return _parse_time(normalized)

    def _stored_token_app_id(self, db: Session) -> str:
        return _meta(db, "adq.app_id").strip()

    @property
    def _environment_token_app_id(self) -> str:
        return os.getenv("ADQ_TOKEN_APP_ID", "").strip()

    def _database_token(self, db: Session, key: str) -> str:
        """Return a persisted token only when it belongs to this ADQ app."""
        token = _meta(db, key).strip()
        if not token or self._stored_token_app_id(db) != self.app_id:
            return ""
        return token

    def _environment_token(self, key: str) -> str:
        """Environment tokens also require an explicit app binding."""
        token = os.getenv(key, "").strip()
        if not token or self._environment_token_app_id != self.app_id:
            return ""
        return token

    def _access_token(self, db: Session) -> str:
        return self._database_token(db, "adq.access_token") or self._environment_token("ADQ_ACCESS_TOKEN")

    def _refresh_token(self, db: Session) -> str:
        return self._database_token(db, "adq.refresh_token") or self._environment_token("ADQ_REFRESH_TOKEN")

    @property
    def _environment_catalog_token_app_id(self) -> str:
        return os.getenv("ADQ_CATALOG_TOKEN_APP_ID", "").strip()

    def _database_catalog_token(self, db: Session, key: str) -> str:
        token = _meta(db, key).strip()
        stored_app_id = _meta(db, "adq.catalog.app_id").strip()
        if not token or stored_app_id != self.app_id:
            return ""
        return token

    def _environment_catalog_token(self, key: str) -> str:
        token = os.getenv(key, "").strip()
        if not token or self._environment_catalog_token_app_id != self.app_id:
            return ""
        return token

    def _catalog_access_token(self, db: Session) -> str:
        return self._database_catalog_token(db, "adq.catalog.access_token") or self._environment_catalog_token(
            "ADQ_CATALOG_ACCESS_TOKEN"
        )

    def _catalog_refresh_token(self, db: Session) -> str:
        return self._database_catalog_token(db, "adq.catalog.refresh_token") or self._environment_catalog_token(
            "ADQ_CATALOG_REFRESH_TOKEN"
        )

    def _catalog_saved_at(self, db: Session) -> datetime | None:
        return _parse_time(
            _meta(db, "adq.catalog.saved_at") or os.getenv("ADQ_CATALOG_TOKEN_SAVED_AT", "").strip()
        )

    def _catalog_expires_in(self, db: Session, *, refresh: bool = False) -> int:
        key = "adq.catalog.refresh_expires_in" if refresh else "adq.catalog.expires_in"
        env_key = "ADQ_CATALOG_REFRESH_EXPIRES_IN" if refresh else "ADQ_CATALOG_ACCESS_EXPIRES_IN"
        try:
            return int(_meta(db, key) or os.getenv(env_key, "0") or 0)
        except ValueError:
            return 0

    def _catalog_access_needs_refresh(self, db: Session) -> bool:
        token = self._catalog_access_token(db)
        if not token:
            return True
        saved_at = self._catalog_saved_at(db)
        expires_in = self._catalog_expires_in(db)
        if not saved_at or not expires_in:
            return False
        return datetime.now(timezone.utc) >= saved_at + timedelta(seconds=max(0, expires_in - 300))

    def catalog_status(self, db: Session) -> dict:
        has_access = bool(self._catalog_access_token(db))
        has_refresh = bool(self._catalog_refresh_token(db))
        authorized = bool(self.app_id and (has_access or has_refresh))
        units = self.business_units_config
        configured_accounts = self.configured_catalog_account_ids
        if not authorized:
            message = "两个 WIS 业务单元已登记，等待主体目录令牌"
        elif not configured_accounts:
            message = "主体目录令牌已就绪，仍需登记两个业务单元下的广告账户 ID"
        else:
            message = f"主体目录令牌已就绪，已登记 {len(configured_accounts)} 个业务单元广告账户"
        return {
            "configured": bool(self.app_id and units),
            "authorized": authorized,
            "message": message,
            "subject_id": self.subject_id,
            "business_unit_total": len(units),
            "configured_account_total": len(configured_accounts),
            "authorized_at": self._catalog_saved_at(db).isoformat() if self._catalog_saved_at(db) else None,
        }

    def _has_unbound_or_mismatched_tokens(self, db: Session) -> bool:
        database_tokens_present = bool(_meta(db, "adq.access_token") or _meta(db, "adq.refresh_token"))
        environment_tokens_present = bool(
            os.getenv("ADQ_ACCESS_TOKEN", "").strip() or os.getenv("ADQ_REFRESH_TOKEN", "").strip()
        )
        database_mismatch = database_tokens_present and self._stored_token_app_id(db) != self.app_id
        environment_mismatch = environment_tokens_present and self._environment_token_app_id != self.app_id
        return database_mismatch or environment_mismatch

    def _saved_at(self, db: Session) -> datetime | None:
        return _parse_time(_meta(db, "adq.saved_at") or os.getenv("ADQ_TOKEN_SAVED_AT", "").strip())

    def _expires_in(self, db: Session, *, refresh: bool = False) -> int:
        key = "adq.refresh_expires_in" if refresh else "adq.expires_in"
        env_key = "ADQ_REFRESH_EXPIRES_IN" if refresh else "ADQ_ACCESS_EXPIRES_IN"
        try:
            return int(_meta(db, key) or os.getenv(env_key, "0") or 0)
        except ValueError:
            return 0

    def _access_needs_refresh(self, db: Session) -> bool:
        token = self._access_token(db)
        if not token:
            return True
        saved_at = self._saved_at(db)
        expires_in = self._expires_in(db)
        if not saved_at or not expires_in:
            return False
        return datetime.now(timezone.utc) >= saved_at + timedelta(seconds=max(0, expires_in - 300))

    @staticmethod
    def _friendly_platform_message(message: str) -> tuple[str, bool]:
        """Translate common Tencent token failures into actionable UI copy."""
        normalized = str(message or "").strip()
        lowered = normalized.lower()
        if "refreshtoken is being used" in lowered or "refresh token is being used" in lowered:
            return "腾讯 ADQ 正在刷新授权，请稍后重试；系统不会重复创建投放任务", True
        if "refreshtoken不存在" in lowered or "refresh token不存在" in lowered:
            return "腾讯 ADQ 授权已失效，请管理员重新获取当前应用的 access_token 和 refresh_token", False
        if "refresh token" in lowered and any(term in lowered for term in ("invalid", "expired", "无效", "过期")):
            return "腾讯 ADQ 授权已过期，请管理员重新授权", False
        if "请求缺失实名认证令牌" in normalized or "user_token" in lowered and "缺" in normalized:
            return "腾讯 ADQ 需要操作人实名认证令牌，请管理员扫码认证后重试", False
        if "实名认证令牌无效" in normalized or "实名认证令牌" in normalized and "过期" in normalized:
            return "腾讯 ADQ 操作人实名认证令牌已失效，请管理员重新扫码认证", False
        return normalized or "腾讯 ADQ 请求失败", False

    def status(self, db: Session) -> dict:
        configured = bool(self.app_id and self.app_secret and self.configured_account_id)
        has_access = bool(self._access_token(db))
        has_refresh = bool(self._refresh_token(db))
        authorized = configured and (has_access or has_refresh)
        token_app_mismatch = self._has_unbound_or_mismatched_tokens(db)
        if not configured:
            message = "尚未完整配置腾讯 ADQ 应用与广告账户"
        elif token_app_mismatch and not authorized:
            message = "腾讯 ADQ 应用已切换，旧应用令牌已隔离，等待当前应用重新授权"
        elif not authorized:
            message = "腾讯 ADQ 应用已配置，等待账户授权"
        elif self._access_needs_refresh(db) and has_refresh:
            message = "腾讯 ADQ 已授权，访问令牌将在调用时自动刷新"
        else:
            message = "腾讯 ADQ 授权可用"
        return {
            "configured": configured,
            "authorized": authorized,
            "message": message,
            "app_id": self.app_id,
            "account_id": self.configured_account_id,
            "authorized_at": self._saved_at(db).isoformat() if self._saved_at(db) else None,
            "authorization_matches_app": authorized,
            "user_authorization": self.user_authorization_status(db),
            "capabilities": {
                "original_video_upload": True,
                "dynamic_creative_add": True,
                "shared_library_upload": bool(
                    settings.adq_shared_source_account_id and settings.adq_shared_mdm_id
                ),
                "material_report": True,
                "purchase_and_roi_report": True,
            },
        }

    def shared_library_config(self) -> dict:
        source_account_id = settings.adq_shared_source_account_id
        mdm_id = settings.adq_shared_mdm_id
        return {
            "configured": bool(source_account_id and mdm_id),
            "source_account_id": source_account_id,
            "mdm_id": mdm_id,
            "scope": "公司主体下全部当前及未来广告账户",
            "asset_type": "ASSET_TYPE_CANVAS_VIDEO",
            "original_only": True,
            "max_video_mb": MAX_VIDEO_BYTES // (1024 * 1024),
            "business_units": [
                {"id": unit["business_unit_id"], "name": unit["business_unit_name"]}
                for unit in self.business_units_config
            ],
            "known_account_total": len(self.configured_catalog_account_ids),
            "account_scope_source": "verified_allowlist",
        }

    def validated_status(self, db: Session) -> dict:
        snapshot = self.status(db)
        if snapshot["authorized"] and self._access_needs_refresh(db) and self._refresh_token(db):
            try:
                self.refresh_access_token(db)
            except AdqError:
                pass
        return self.status(db)

    def _store_tokens(self, db: Session, data: dict) -> None:
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise AdqError("腾讯 ADQ 刷新令牌成功响应中没有 access_token", category="invalid_response")
        _set_meta(db, "adq.access_token", access_token)
        _set_meta(db, "adq.app_id", self.app_id)
        if data.get("refresh_token"):
            _set_meta(db, "adq.refresh_token", data["refresh_token"])
        _set_meta(db, "adq.expires_in", data.get("expires_in") or data.get("access_token_expires_in") or 0)
        _set_meta(db, "adq.refresh_expires_in", data.get("refresh_token_expires_in") or 0)
        _set_meta(db, "adq.saved_at", datetime.now(timezone.utc).isoformat())
        db.commit()

    def _store_catalog_tokens(self, db: Session, data: dict) -> None:
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise AdqError("腾讯 ADQ 主体目录令牌响应中没有 access_token", category="invalid_response")
        _set_meta(db, "adq.catalog.access_token", access_token)
        _set_meta(db, "adq.catalog.app_id", self.app_id)
        if data.get("refresh_token"):
            _set_meta(db, "adq.catalog.refresh_token", data["refresh_token"])
        _set_meta(
            db,
            "adq.catalog.expires_in",
            data.get("expires_in") or data.get("access_token_expires_in") or 0,
        )
        _set_meta(db, "adq.catalog.refresh_expires_in", data.get("refresh_token_expires_in") or 0)
        _set_meta(db, "adq.catalog.saved_at", datetime.now(timezone.utc).isoformat())
        db.commit()

    def refresh_access_token(self, db: Session, *, stale_access_token: str = "") -> str:
        with self._token_refresh_lock:
            # Another request may have completed a rotating-token refresh while
            # this request was waiting for the gate. Reload rows before deciding
            # whether a second refresh is necessary.
            db.expire_all()
            current_access_token = self._access_token(db)
            if stale_access_token and current_access_token and current_access_token != stale_access_token:
                return current_access_token
            if not stale_access_token and current_access_token and not self._access_needs_refresh(db):
                return current_access_token

            refresh_token = self._refresh_token(db)
            if not (self.app_id and self.app_secret and refresh_token):
                raise AdqError("腾讯 ADQ 授权已失效，且没有可用刷新令牌", category="authorization")
            db.commit()
            try:
                response = requests.get(
                    TOKEN_URL,
                    params={
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    timeout=(10, 30),
                )
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                raise AdqError("刷新腾讯 ADQ 授权失败，请稍后重试", category="network", retryable=True) from error
            if response.status_code >= 400 or int(payload.get("code") or 0) != 0:
                friendly_message, retryable = self._friendly_platform_message(
                    str(payload.get("message_cn") or payload.get("message") or "腾讯 ADQ 授权刷新失败")
                )
                raise AdqError(
                    friendly_message,
                    code=payload.get("code") or response.status_code,
                    request_id=str(payload.get("trace_id") or ""),
                    category="authorization",
                    retryable=retryable,
                )
            self._store_tokens(db, payload.get("data") or payload)
            return self._access_token(db)

    def token(self, db: Session) -> str:
        if self._access_needs_refresh(db) and self._refresh_token(db):
            return self.refresh_access_token(db)
        token = self._access_token(db)
        if not token:
            raise AdqError("腾讯 ADQ 尚未授权", category="authorization")
        return token

    def refresh_catalog_access_token(self, db: Session, *, stale_access_token: str = "") -> str:
        with self._token_refresh_lock:
            db.expire_all()
            current_access_token = self._catalog_access_token(db)
            if stale_access_token and current_access_token and current_access_token != stale_access_token:
                return current_access_token
            if not stale_access_token and current_access_token and not self._catalog_access_needs_refresh(db):
                return current_access_token
            refresh_token = self._catalog_refresh_token(db)
            if not (self.app_id and self.app_secret and refresh_token):
                raise AdqError("腾讯 ADQ 主体目录授权已失效，且没有可用刷新令牌", category="authorization")
            db.commit()
            try:
                response = requests.get(
                    TOKEN_URL,
                    params={
                        "client_id": self.app_id,
                        "client_secret": self.app_secret,
                        "grant_type": "refresh_token",
                        "refresh_token": refresh_token,
                    },
                    timeout=(10, 30),
                )
                payload = response.json()
            except (requests.RequestException, ValueError) as error:
                raise AdqError("刷新腾讯 ADQ 主体目录授权失败，请稍后重试", category="network", retryable=True) from error
            if response.status_code >= 400 or int(payload.get("code") or 0) != 0:
                friendly_message, retryable = self._friendly_platform_message(
                    str(payload.get("message_cn") or payload.get("message") or "腾讯 ADQ 主体目录授权刷新失败")
                )
                raise AdqError(
                    friendly_message,
                    code=payload.get("code") or response.status_code,
                    request_id=str(payload.get("trace_id") or ""),
                    category="authorization",
                    retryable=retryable,
                )
            self._store_catalog_tokens(db, payload.get("data") or payload)
            return self._catalog_access_token(db)

    def catalog_token(self, db: Session) -> str:
        if self._catalog_access_needs_refresh(db) and self._catalog_refresh_token(db):
            return self.refresh_catalog_access_token(db)
        token = self._catalog_access_token(db)
        if not token:
            raise AdqError("腾讯 ADQ 主体目录尚未授权", category="authorization")
        return token

    @staticmethod
    def _is_auth_error(code: Any, message: str) -> bool:
        normalized = str(message).lower()
        return str(code) in {"11001", "11002", "11011", "12001"} or any(
            term in normalized for term in ("access_token", "token expired", "token无效", "令牌失效")
        )

    def _request(
        self,
        db: Session,
        method: str,
        url: str,
        *,
        params: dict | None = None,
        json_body: dict | None = None,
        data: Any = None,
        headers: dict[str, str] | None = None,
        timeout: tuple[int, int] | None = None,
        allow_refresh: bool = True,
        token_profile: str = "delivery",
        requires_user_token: bool = False,
    ) -> dict:
        query = dict(params or {})
        query.update({
            "access_token": self.catalog_token(db) if token_profile == "catalog" else self.token(db),
            "timestamp": int(time.time()),
            "nonce": uuid4().hex[:24],
        })
        if requires_user_token:
            user_token = self._user_token(db)
            status = self.user_authorization_status(db)
            if not user_token or not status["authorized"]:
                raise AdqError(status["message"], category="user_token_required")
            query["user_token"] = user_token
        # Do not retain a database connection for the duration of a remote ADQ
        # request.  All required tokens are now copied into the request query.
        db.commit()
        try:
            response = requests.request(
                method,
                url,
                params=query,
                json=json_body,
                data=data,
                headers={"Accept": "application/json", **(headers or {})},
                timeout=timeout or self.timeout,
            )
        except requests.ConnectTimeout as error:
            raise AdqError("连接腾讯 ADQ 超时，请稍后重试", category="connect_timeout", retryable=True) from error
        except requests.ReadTimeout as error:
            raise AdqError("腾讯 ADQ 处理请求超时，请稍后安全重试", category="read_timeout", retryable=True) from error
        except requests.ConnectionError as error:
            raise AdqError("视频传输过程中腾讯 ADQ 中断了连接；原文件未改动，可安全重试", category="connection_interrupted", retryable=True) from error
        except requests.RequestException as error:
            raise AdqError("连接腾讯 ADQ 失败，请稍后重试", category="network", retryable=True) from error
        try:
            payload = response.json()
        except ValueError as error:
            raise AdqError(
                "腾讯 ADQ 返回了无法识别的响应",
                code=response.status_code,
                category="invalid_response",
                retryable=response.status_code >= 500,
            ) from error
        code = payload.get("code")
        message = str(payload.get("message_cn") or payload.get("message") or "")
        if response.status_code >= 400 or int(code or 0) != 0:
            profile_refresh_token = (
                self._catalog_refresh_token(db) if token_profile == "catalog" else self._refresh_token(db)
            )
            if allow_refresh and self._is_auth_error(code, message) and profile_refresh_token:
                if token_profile == "catalog":
                    self.refresh_catalog_access_token(db, stale_access_token=str(query.get("access_token") or ""))
                else:
                    self.refresh_access_token(db, stale_access_token=str(query.get("access_token") or ""))
                if isinstance(data, MultipartEncoder):
                    raise AdqError(
                        "腾讯 ADQ 授权已刷新，原视频将在新连接中继续上传",
                        code=code or response.status_code,
                        request_id=str(payload.get("trace_id") or ""),
                        category="authorization_refreshed",
                        retryable=True,
                    )
                return self._request(
                    db,
                    method,
                    url,
                    params=params,
                    json_body=json_body,
                    data=data,
                    headers=headers,
                    timeout=timeout,
                    allow_refresh=False,
                    token_profile=token_profile,
                    requires_user_token=requires_user_token,
                )
            friendly_message, platform_retryable = self._friendly_platform_message(message)
            category = "rate_limit" if response.status_code == 429 or "频" in message else "platform"
            if str(code) in {"11101", "11102"} or "user_token" in message.lower() or "用户授权" in message or "实名认证令牌" in message:
                category = "user_token_required"
            raise AdqError(
                friendly_message or f"腾讯 ADQ 请求失败（HTTP {response.status_code}）",
                code=code or response.status_code,
                request_id=str(payload.get("trace_id") or payload.get("request_id") or ""),
                category=category,
                retryable=platform_retryable or response.status_code >= 500 or response.status_code == 429,
            )
        return payload

    def _cache_get(self, key: str, max_age: int = 300) -> Any | None:
        with self._catalog_lock:
            cached = self._catalog_cache.get(key)
            if cached and time.monotonic() - cached[0] <= max_age:
                return cached[1]
        return None

    def _cache_set(self, key: str, value: Any) -> Any:
        with self._catalog_lock:
            self._catalog_cache[key] = (time.monotonic(), value)
        return value

    def invalidate_catalog(self) -> None:
        with self._catalog_lock:
            self._catalog_cache.clear()

    def _business_manager_accounts(self, db: Session, *, fresh: bool = False) -> list[dict]:
        """Return every advertiser exposed by the current BM access token."""
        cache_key = "business-manager-accounts"
        if not fresh:
            cached = self._cache_get(cache_key)
            if cached is not None:
                return cached
        rows = self._fetch_all(
            db,
            BUSINESS_MANAGER_RELATIONS_URL,
            {"fields": json.dumps(["account_id", "corporation_name"], ensure_ascii=False)},
            page_size=100,
            pagination_mode=False,
        )
        items: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            account_id = str(row.get("account_id") or "").strip()
            if not account_id or account_id in seen:
                continue
            seen.add(account_id)
            corporation_name = str(row.get("corporation_name") or "").strip()
            items.append({
                "account_id": account_id,
                "account_name": corporation_name or f"ADQ 账户 {account_id}",
                "corporation_name": corporation_name,
                "system_status": str(row.get("system_status") or ""),
            })
        return self._cache_set(cache_key, items)

    def _account_detail(self, db: Session, account_id: str, *, token_profile: str = "delivery") -> dict:
        cached = self._cache_get(f"account:{token_profile}:{account_id}")
        if cached:
            return cached
        payload = self._request(
            db,
            "GET",
            ADVERTISER_URL,
            params={
                "account_id": account_id,
                "fields": json.dumps(["account_id", "corporation_name", "system_status"], ensure_ascii=False),
                "pagination_mode": "PAGINATION_MODE_NORMAL",
                "page": 1,
                "page_size": 100,
            },
            token_profile=token_profile,
        )
        item = next((value for value in _items(payload) if str(value.get("account_id")) == account_id), None)
        if not item:
            data = payload.get("data") or {}
            item = data if str(data.get("account_id") or "") == account_id else None
        if not item:
            raise AdqError("当前授权未返回配置的腾讯 ADQ 广告账户", category="account_unavailable")
        result = {
            "account_id": account_id,
            "account_name": str(item.get("corporation_name") or f"ADQ 账户 {account_id}"),
            "corporation_name": str(item.get("corporation_name") or ""),
            "system_status": str(item.get("system_status") or ""),
        }
        return self._cache_set(f"account:{token_profile}:{account_id}", result)

    def account(self, db: Session, *, account_id: str = "") -> dict:
        account_id = (account_id or self.configured_account_id).strip()
        if not account_id:
            raise AdqError("未配置腾讯 ADQ 广告账户 ID", category="configuration")
        if account_id in self._normalized_ids([*self.configured_account_ids, *self.configured_catalog_account_ids]):
            return self._account_detail_with_fallback(db, account_id)
        try:
            discovered = self._business_manager_accounts(db)
        except AdqError as error:
            raise AdqError(
                "当前授权未能读取该腾讯 ADQ 投放账户，请刷新账户列表后重试",
                category="account_unavailable",
            ) from error
        item = next((value for value in discovered if value["account_id"] == account_id), None)
        if not item:
            raise AdqError("当前授权未开放该腾讯 ADQ 投放账户", category="account_unavailable")
        return item

    def _account_token_profile(self, db: Session, account_id: str) -> str:
        cached = str(self._cache_get(f"account-token-profile:{account_id}", max_age=3600) or "").strip()
        if cached in {"delivery", "catalog"}:
            return cached
        if account_id in self.configured_account_ids:
            return "delivery"
        catalog_available = bool(self._catalog_access_token(db) or self._catalog_refresh_token(db))
        return "catalog" if catalog_available and account_id in self._catalog_account_ids() else "delivery"

    def _account_detail_with_fallback(self, db: Session, account_id: str) -> dict:
        preferred = "delivery" if account_id in self.configured_account_ids else "catalog"
        errors: list[AdqError] = []
        for token_profile in dict.fromkeys([preferred, "delivery", "catalog"]):
            try:
                detail = self._account_detail(db, account_id, token_profile=token_profile)
                self._cache_set(f"account-token-profile:{account_id}", token_profile)
                return detail
            except AdqError as error:
                errors.append(error)
        raise errors[0] if errors else AdqError("当前授权未开放该腾讯 ADQ 投放账户", category="account_unavailable")

    def accounts(self, db: Session) -> dict:
        discovered: list[dict] = []
        discovery_error = ""
        try:
            discovered = self._business_manager_accounts(db, fresh=True)
        except AdqError as error:
            discovery_error = str(error)
        items_by_id = {item["account_id"]: item for item in discovered}
        unavailable: list[dict] = []
        configured_ids = self._normalized_ids([
            *self.configured_account_ids,
            *self.configured_catalog_account_ids,
        ])
        unit_by_account = {
            account_id: unit
            for unit in self.business_units_config
            for account_id in unit["account_ids"]
        }
        for account_id in configured_ids:
            if account_id in items_by_id:
                continue
            try:
                detail = self._account_detail_with_fallback(db, account_id)
                unit = unit_by_account.get(account_id) or {}
                detail.update({
                    "business_unit_id": str(unit.get("business_unit_id") or ""),
                    "business_unit_name": str(unit.get("business_unit_name") or ""),
                })
                items_by_id[account_id] = detail
            except AdqError as error:
                unavailable.append({
                    "account_id": account_id,
                    "message": str(error),
                    "category": error.category,
                })
        items = list(items_by_id.values())
        if not items and unavailable:
            first = unavailable[0]
            raise AdqError(
                first["message"],
                category=first["category"],
            )
        complete = bool(discovered) and not discovery_error and not unavailable
        if discovery_error:
            message = f"商务管家账户目录读取失败；当前仅显示服务器已配置账户：{discovery_error}"
        elif not discovered:
            message = "商务管家接口未返回关联广告账户；当前仅显示服务器已配置账户"
        elif unavailable:
            message = f"已读取商务管家账户目录，但有 {len(unavailable)} 个服务器配置账户暂不可用"
        else:
            message = f"已完整读取商务管家下 {len(discovered)} 个广告账户"
        return {
            "items": items,
            "total": len(items),
            "configured_total": len(configured_ids),
            "discovered_total": len(discovered),
            "unavailable": unavailable,
            "discovery_error": discovery_error,
            "complete": complete,
            "message": message,
        }

    def _fetch_all(
        self,
        db: Session,
        url: str,
        base_params: dict,
        page_size: int = 100,
        *,
        pagination_mode: bool = True,
        token_profile: str = "delivery",
    ) -> list[dict]:
        result: list[dict] = []
        page = 1
        while True:
            page_params = {**base_params, "page": page, "page_size": page_size}
            if pagination_mode:
                page_params["pagination_mode"] = "PAGINATION_MODE_NORMAL"
            payload = self._request(
                db,
                "GET",
                url,
                params=page_params,
                token_profile=token_profile,
            )
            result.extend(_items(payload))
            info = _page_info(payload)
            total_page = int(info.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        return result

    def _related_business_account_ids(self, db: Session, *, seed_account_id: str, fresh: bool = False) -> list[str]:
        cache_key = f"catalog-business-accounts:{seed_account_id}"
        if not fresh:
            cached = self._cache_get(cache_key, max_age=900)
            if cached is not None:
                return cached
        rows = self._fetch_all(
            db,
            BUSINESS_MDM_RELATIONS_URL,
            {
                "account_id": seed_account_id,
                "relation_type": "RELATION_TYPE_BUSINESS",
                "fields": json.dumps(["account_id"], ensure_ascii=False),
            },
            page_size=100,
            pagination_mode=False,
            token_profile="catalog",
        )
        account_ids = self._normalized_ids([seed_account_id, *[row.get("account_id") for row in rows]])
        return self._cache_set(cache_key, account_ids)

    def campaigns(self, db: Session, *, account_id: str, fresh: bool = False) -> list[dict]:
        cache_key = f"campaigns:{account_id}"
        if not fresh:
            cached = self._cache_get(cache_key, max_age=300)
            if cached is not None:
                return cached
        rows = self._fetch_all(
            db,
            CAMPAIGNS_URL,
            {
                "account_id": account_id,
                "fields": json.dumps(CAMPAIGN_FIELDS, ensure_ascii=False),
                "is_deleted": "false",
            },
            page_size=100,
            pagination_mode=False,
            token_profile=self._account_token_profile(db, account_id),
        )
        result: list[dict] = []
        seen: set[str] = set()
        for row in rows:
            campaign_id = str(row.get("campaign_id") or "").strip()
            if not campaign_id or campaign_id in seen or bool(row.get("is_deleted")):
                continue
            seen.add(campaign_id)
            result.append({
                "campaign_id": campaign_id,
                "campaign_name": str(row.get("campaign_name") or f"推广计划 {campaign_id}"),
                "configured_status": str(row.get("configured_status") or ""),
                "campaign_type": str(row.get("campaign_type") or ""),
                "promoted_object_type": str(row.get("promoted_object_type") or ""),
                "created_time": row.get("created_time"),
                "last_modified_time": row.get("last_modified_time"),
            })
        return self._cache_set(cache_key, result)

    def business_hierarchy(self, db: Session, *, q: str = "", fresh: bool = False) -> dict:
        """Read the configured subject -> business unit -> account -> campaign hierarchy.

        The endpoint is deliberately read-only.  Missing authorization or an
        incomplete account mapping is surfaced as incomplete coverage; it is
        never converted into a zero count.
        """
        catalog = self.catalog_status(db)
        expected_account_total = int(os.getenv("ADQ_EXPECTED_ACTIVE_ACCOUNT_TOTAL", "21") or 21)
        expected_campaign_total = int(os.getenv("ADQ_EXPECTED_CAMPAIGN_TOTAL", "136") or 136)
        term = q.strip().casefold()
        unit_results: list[dict] = []
        all_discovered_account_ids: list[str] = []
        observed_active_accounts = 0
        observed_campaigns = 0
        errors: list[dict] = []

        for unit in self.business_units_config:
            unit_id = unit["business_unit_id"]
            account_ids = list(unit["account_ids"])
            seed_account_id = unit["seed_account_id"] or (account_ids[0] if account_ids else "")
            unit_errors: list[dict] = []
            relation_complete = bool(account_ids)
            if catalog["authorized"] and seed_account_id:
                try:
                    account_ids = self._normalized_ids([
                        *account_ids,
                        *self._related_business_account_ids(
                            db,
                            seed_account_id=seed_account_id,
                            fresh=fresh,
                        ),
                    ])
                    relation_complete = True
                except AdqError as error:
                    unit_errors.append({
                        "stage": "business_relation",
                        "account_id": seed_account_id,
                        "message": str(error),
                        "category": error.category,
                    })
            elif not account_ids:
                unit_errors.append({
                    "stage": "account_mapping",
                    "account_id": "",
                    "message": "该业务单元尚未登记广告账户 ID；业务单元 ID 不能直接替代广告账户 ID",
                    "category": "configuration",
                })

            all_discovered_account_ids.extend(account_ids)
            self._cache_set(
                "catalog-account-ids",
                self._normalized_ids([*all_discovered_account_ids, *self.configured_catalog_account_ids]),
            )
            account_results: list[dict] = []
            for account_id in account_ids:
                account_result: dict[str, Any] = {
                    "account_id": account_id,
                    "account_name": f"ADQ 账户 {account_id}",
                    "corporation_name": "",
                    "system_status": "",
                    "active": None,
                    "campaign_total": None,
                    "active_campaign_total": None,
                    "campaigns": [],
                    "complete": False,
                    "message": "待读取",
                    "business_unit_id": unit_id,
                    "business_unit_name": unit["business_unit_name"],
                }
                if not catalog["authorized"]:
                    account_result["message"] = "主体目录令牌尚未接入"
                    account_results.append(account_result)
                    continue
                try:
                    detail = self._account_detail(db, account_id, token_profile="catalog")
                    account_result.update(detail)
                    system_status = str(detail.get("system_status") or "")
                    account_result["active"] = system_status in {
                        "CUSTOMER_STATUS_NORMAL",
                        "ADVERTISER_STATUS_NORMAL",
                        "ACCOUNT_STATUS_NORMAL",
                    }
                    campaigns = self.campaigns(db, account_id=account_id, fresh=fresh)
                    account_result["campaigns"] = campaigns
                    account_result["campaign_total"] = len(campaigns)
                    account_result["active_campaign_total"] = sum(
                        1 for item in campaigns if item["configured_status"] == "AD_STATUS_NORMAL"
                    )
                    account_result["complete"] = True
                    account_result["message"] = "读取完成"
                    if account_result["active"]:
                        observed_active_accounts += 1
                    observed_campaigns += len(campaigns)
                except AdqError as error:
                    account_result["message"] = str(error)
                    unit_errors.append({
                        "stage": "account_campaigns",
                        "account_id": account_id,
                        "message": str(error),
                        "category": error.category,
                    })
                searchable = " ".join([
                    account_result["account_id"],
                    account_result["account_name"],
                    account_result["corporation_name"],
                    *[
                        f"{campaign['campaign_id']} {campaign['campaign_name']}"
                        for campaign in account_result["campaigns"]
                    ],
                ]).casefold()
                account_result["matches_search"] = not term or term in searchable
                account_results.append(account_result)

            errors.extend([{**item, "business_unit_id": unit_id} for item in unit_errors])
            unit_complete = bool(
                catalog["authorized"]
                and relation_complete
                and account_ids
                and not unit_errors
                and all(item["complete"] for item in account_results)
            )
            display_accounts = [
                {key: value for key, value in item.items() if key != "matches_search"}
                for item in account_results
                if item.get("matches_search", True)
            ]
            unit_results.append({
                "business_unit_id": unit_id,
                "business_unit_name": unit["business_unit_name"],
                "seed_account_id": seed_account_id,
                "configured_account_total": len(unit["account_ids"]),
                "account_total": len(account_ids) if relation_complete else None,
                "active_account_total": (
                    sum(1 for item in account_results if item["active"] is True) if unit_complete else None
                ),
                "campaign_total": (
                    sum(int(item["campaign_total"] or 0) for item in account_results) if unit_complete else None
                ),
                "accounts": display_accounts,
                "complete": unit_complete,
                "message": (
                    f"已完整读取 {len(account_ids)} 个账户"
                    if unit_complete
                    else unit_errors[0]["message"] if unit_errors else catalog["message"]
                ),
            })

        self._cache_set("catalog-account-ids", self._normalized_ids(all_discovered_account_ids))
        complete = bool(unit_results) and all(unit["complete"] for unit in unit_results)
        count_matches = bool(
            complete
            and observed_active_accounts == expected_account_total
            and observed_campaigns == expected_campaign_total
        )
        if complete and count_matches:
            message = f"已完整回读 {observed_active_accounts} 个使用中账户、{observed_campaigns} 个推广计划"
        elif complete:
            message = (
                f"已完整回读，但实时数量为 {observed_active_accounts} 个使用中账户、"
                f"{observed_campaigns} 个推广计划，与交接参考数量不同"
            )
        else:
            message = "业务单元目录尚未完整回读；未覆盖数量保持待核验，不按 0 处理"
        return {
            "subject": {
                "subject_id": self.subject_id,
                "subject_name": os.getenv("ADQ_SUBJECT_NAME", "广州慕可生物科技有限公司").strip(),
            },
            "catalog": catalog,
            "business_units": unit_results,
            "business_unit_total": len(unit_results),
            "active_account_total": observed_active_accounts if complete else None,
            "campaign_total": observed_campaigns if complete else None,
            "expected_active_account_total": expected_account_total,
            "expected_campaign_total": expected_campaign_total,
            "known_account_ids": self.configured_catalog_account_ids,
            "known_account_total": len(self.configured_catalog_account_ids),
            "business_unit_account_mapping": "pending_api_readback",
            "count_matches_reference": count_matches,
            "complete": complete,
            "errors": errors,
            "message": message,
        }

    def dynamic_creatives(
        self,
        db: Session,
        *,
        account_id: str,
        adgroup_id: str = "",
        fresh: bool = False,
    ) -> list[dict]:
        key = f"creatives:{account_id}:{adgroup_id or 'all'}"
        if not fresh:
            cached = self._cache_get(key)
            if cached is not None:
                return cached
        params: dict[str, Any] = {
            "account_id": account_id,
            "fields": json.dumps(DYNAMIC_CREATIVE_FIELDS, ensure_ascii=False),
        }
        if adgroup_id:
            params["filtering"] = json.dumps(
                [{"field": "adgroup_id", "operator": "EQUALS", "values": [str(adgroup_id)]}],
                ensure_ascii=False,
            )
        values = self._fetch_all(
            db,
            DYNAMIC_CREATIVES_GET_URL,
            params,
            token_profile=self._account_token_profile(db, account_id),
        )
        return self._cache_set(key, values)

    @staticmethod
    def _has_video_component(creative: dict) -> bool:
        components = creative.get("creative_components") or {}
        return bool(isinstance(components, dict) and components.get("video"))

    @staticmethod
    def _choose_source(creatives: list[dict]) -> dict | None:
        candidates = [item for item in creatives if AdqService._has_video_component(item)]
        if not candidates:
            return None
        candidates.sort(
            key=lambda item: (
                str(item.get("configured_status") or "") == "AD_STATUS_NORMAL",
                str(item.get("system_status") or "") == "AD_STATUS_NORMAL",
                str(item.get("last_modified_time") or item.get("created_time") or ""),
            ),
            reverse=True,
        )
        return candidates[0]

    def adgroups(self, db: Session, *, account_id: str, q: str = "", fresh: bool = False) -> dict:
        configured_ids = self._normalized_ids([*self.configured_account_ids, *self.configured_catalog_account_ids])
        if account_id in self.configured_account_ids:
            self._cache_set(f"account-token-profile:{account_id}", "delivery")
        elif account_id in configured_ids:
            self.account(db, account_id=account_id)
        elif account_id not in self.configured_account_ids:
            discovered = self._business_manager_accounts(db)
            if account_id not in {item["account_id"] for item in discovered}:
                raise AdqError("当前授权未开放该腾讯 ADQ 投放账户", category="account_unavailable")
        token_profile = self._account_token_profile(db, account_id)
        cache_key = f"adgroups:{account_id}"
        rows = None if fresh else self._cache_get(cache_key)
        if rows is None:
            groups = self._fetch_all(
                db,
                ADGROUPS_URL,
                {"account_id": account_id, "fields": json.dumps(ADGROUP_FIELDS, ensure_ascii=False)},
                token_profile=token_profile,
            )
            creatives = self.dynamic_creatives(db, account_id=account_id, fresh=fresh)
            by_group: dict[str, list[dict]] = {}
            for creative in creatives:
                by_group.setdefault(str(creative.get("adgroup_id") or ""), []).append(creative)
            rows = []
            for group in groups:
                adgroup_id = str(group.get("adgroup_id") or "")
                source = self._choose_source(by_group.get(adgroup_id, []))
                rows.append({
                    "adgroup_id": adgroup_id,
                    "adgroup_name": str(group.get("adgroup_name") or adgroup_id),
                    "campaign_id": str(group.get("campaign_id") or ""),
                    "configured_status": str(group.get("configured_status") or ""),
                    "system_status": str(group.get("system_status") or ""),
                    "can_attach_video": bool(source),
                    "source_dynamic_creative_id": str((source or {}).get("dynamic_creative_id") or ""),
                    "creative_template_id": str((source or {}).get("creative_template_id") or ""),
                    "delivery_mode": str((source or {}).get("delivery_mode") or ""),
                    "dynamic_creative_type": str((source or {}).get("dynamic_creative_type") or ""),
                })
            self._cache_set(cache_key, rows)
        term = q.strip().casefold()
        items = [
            item for item in rows
            if not term or term in item["adgroup_name"].casefold() or term in item["adgroup_id"].casefold()
        ]
        return {
            "items": items,
            "total": len(items),
            "source_total": len(rows),
            "attachable_total": sum(1 for item in rows if item["can_attach_video"]),
            "complete": True,
            "message": "已完整读取全部营销单元；只有具备现有视频创意模板的营销单元可直接推送",
        }

    def resolve_target(
        self,
        db: Session,
        *,
        account_id: str,
        adgroup_id: str,
        source_dynamic_creative_id: str = "",
    ) -> dict:
        result = self.adgroups(db, account_id=account_id, fresh=True)
        group = next((item for item in result["items"] if item["adgroup_id"] == str(adgroup_id)), None)
        if not group:
            raise AdqError("所选腾讯 ADQ 营销单元已不存在，请刷新后重选", category="adgroup_unavailable")
        if not group["can_attach_video"]:
            raise AdqError("该营销单元没有可安全复用的视频创意模板，暂不能直接推送", category="template_unavailable")
        if source_dynamic_creative_id:
            creatives = self.dynamic_creatives(db, account_id=account_id, adgroup_id=adgroup_id, fresh=True)
            source = next(
                (
                    item for item in creatives
                    if str(item.get("dynamic_creative_id") or "") == source_dynamic_creative_id
                    and self._has_video_component(item)
                ),
                None,
            )
            if source:
                group = {**group, "source_dynamic_creative_id": source_dynamic_creative_id}
        return group

    def _find_video_by_signature(self, db: Session, *, account_id: str, signature: str) -> dict | None:
        for field in ("media_signature", "signature"):
            try:
                payload = self._request(
                    db,
                    "GET",
                    VIDEOS_GET_URL,
                    params={
                        "account_id": account_id,
                        "filtering": json.dumps(
                            [{"field": field, "operator": "EQUALS", "values": [signature]}],
                            ensure_ascii=False,
                        ),
                        "fields": json.dumps(VIDEO_FIELDS, ensure_ascii=False),
                        "pagination_mode": "PAGINATION_MODE_NORMAL",
                        "page": 1,
                        "page_size": 100,
                    },
                )
            except AdqError as error:
                if error.category == "platform":
                    continue
                raise
            match = next(
                (
                    item for item in _items(payload)
                    if str(item.get("signature") or item.get("media_signature") or "").lower() == signature.lower()
                ),
                None,
            )
            if match:
                return match
        return None

    def _video_detail(self, db: Session, *, account_id: str, video_id: str) -> dict | None:
        for field in ("media_id", "video_id"):
            try:
                payload = self._request(
                    db,
                    "GET",
                    VIDEOS_GET_URL,
                    params={
                        "account_id": account_id,
                        "filtering": json.dumps(
                            [{"field": field, "operator": "EQUALS", "values": [str(video_id)]}],
                            ensure_ascii=False,
                        ),
                        "fields": json.dumps(VIDEO_FIELDS, ensure_ascii=False),
                        "pagination_mode": "PAGINATION_MODE_NORMAL",
                        "page": 1,
                        "page_size": 20,
                    },
                )
            except AdqError as error:
                if error.category == "platform":
                    continue
                raise
            detail = next(
                (
                    item for item in _items(payload)
                    if str(item.get("video_id") or item.get("media_id") or "") == str(video_id)
                ),
                None,
            )
            if detail:
                return detail
        return None

    def verify_video_in_library(self, db: Session, *, account_id: str, video_id: str) -> dict:
        """Require an exact source-library readback before reporting upload success."""
        detail = self._video_detail(db, account_id=account_id, video_id=video_id)
        if not detail:
            raise AdqError(
                "腾讯 ADQ 已返回视频 ID，但统一素材源账户暂未回读到该视频",
                category="library_readback_pending",
                retryable=True,
                entity_id=str(video_id),
            )
        return {
            "verified": True,
            "account_id": str(account_id),
            "video_id": str(video_id),
            "video_name": str(detail.get("video_name") or ""),
            "cover_id": str(detail.get("cover_id") or ""),
            "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        }

    def upload_original_video(
        self,
        db: Session,
        *,
        account_id: str,
        object_key: str,
        filename: str,
        size: int,
        etag: str = "",
    ) -> dict:
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_VIDEO_EXTENSIONS:
            raise AdqError("腾讯 ADQ 仅支持 MP4、MOV、AVI 原视频", category="unsupported_video")
        if size <= 0 or size > MAX_VIDEO_BYTES:
            max_video_mb = MAX_VIDEO_BYTES // (1024 * 1024)
            raise AdqError(
                f"腾讯 ADQ 单个视频上限为 {max_video_mb}MB；系统不会压缩或转码原视频",
                category="video_too_large",
            )
        if not oss_service.client:
            raise AdqError("OSS 未配置，无法读取原视频", category="configuration")

        clean_etag = etag.strip('"').lower()
        trusted_etag = bool(re.fullmatch(r"[0-9a-f]{32}", clean_etag))
        with tempfile.TemporaryFile(mode="w+b") as staged:
            digest = hashlib.md5()
            response = oss_service.client.get_object(Bucket=settings.bucket, Key=object_key)
            body = response["Body"]
            received = 0
            while True:
                chunk = body.read(1024 * 1024)
                if not chunk:
                    break
                staged.write(chunk)
                digest.update(chunk)
                received += len(chunk)
            signature = digest.hexdigest()
            if received != size:
                raise AdqError("从 OSS 读取的原视频大小与素材记录不一致，请刷新素材后重试", category="source_changed")
            if trusted_etag and signature != clean_etag:
                raise AdqError("原视频完整性校验失败，请刷新素材后重试", category="source_changed")

            existing = self._find_video_by_signature(db, account_id=account_id, signature=signature)
            if existing:
                video_id = str(existing.get("video_id") or existing.get("media_id") or "")
                readback = self.verify_video_in_library(db, account_id=account_id, video_id=video_id)
                return {
                    "video_id": video_id,
                    "cover_id": str(existing.get("cover_id") or ""),
                    "signature": signature,
                    "already_present": True,
                    "request_id": "",
                    "library_readback": readback,
                }

            last_error: AdqError | None = None
            for attempt in range(3):
                staged.seek(0)
                encoder = MultipartEncoder(fields={
                    "account_id": str(account_id),
                    "signature": signature,
                    "description": Path(filename).stem[:120],
                    "video_file": (Path(filename).name, staged, "application/octet-stream"),
                })
                try:
                    payload = self._request(
                        db,
                        "POST",
                        VIDEOS_ADD_URL,
                        data=encoder,
                        headers={"Content-Type": encoder.content_type},
                        timeout=(15, 300),
                    )
                    data = payload.get("data") or {}
                    video_id = str(data.get("video_id") or data.get("id") or "")
                    if not video_id:
                        raise AdqError(
                            "腾讯 ADQ 已接收原视频，但未返回视频 ID",
                            request_id=str(payload.get("trace_id") or ""),
                            category="invalid_response",
                            retryable=True,
                        )
                    detail = None
                    for delay in (0, 2, 5, 10):
                        if delay:
                            time.sleep(delay)
                        detail = self._video_detail(db, account_id=account_id, video_id=video_id)
                        if detail:
                            break
                    if not detail:
                        raise AdqError(
                            "腾讯 ADQ 已返回视频 ID，但统一素材源账户暂未回读到该视频",
                            request_id=str(payload.get("trace_id") or payload.get("request_id") or ""),
                            category="library_readback_pending",
                            retryable=False,
                            entity_id=video_id,
                        )
                    readback = {
                        "verified": True,
                        "account_id": str(account_id),
                        "video_id": video_id,
                        "video_name": str(detail.get("video_name") or ""),
                        "cover_id": str(detail.get("cover_id") or ""),
                        "checked_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                    }
                    return {
                        "video_id": video_id,
                        "cover_id": str(detail.get("cover_id") or data.get("cover_id") or ""),
                        "signature": signature,
                        "already_present": False,
                        "request_id": str(payload.get("trace_id") or payload.get("request_id") or ""),
                        "library_readback": readback,
                    }
                except AdqError as error:
                    last_error = error
                    if not error.retryable or attempt == 2:
                        raise
                    time.sleep(2 ** attempt)
            raise last_error or AdqError("腾讯 ADQ 原视频上传失败", category="upload")

    def grant_all_videos_to_mdm(
        self,
        db: Session,
        *,
        source_account_id: str,
        mdm_id: str,
    ) -> dict:
        if not source_account_id.isdigit() or not mdm_id.isdigit():
            raise AdqError("腾讯 ADQ 共享素材账户或主体 MDM ID 未正确配置", category="configuration")
        with self._shared_library_lock:
            payload = self._request(
                db,
                "POST",
                ASSET_PERMISSIONS_ADD_URL,
                data={
                    "account_id": source_account_id,
                    "asset_type": "ASSET_TYPE_CANVAS_VIDEO",
                    "asset_permission_spec": json.dumps(
                        [{
                            "asset_permission_grant_type": "ASSET_PERMISSION_GRANT_TYPE_ACCOUNT",
                        }],
                        ensure_ascii=False,
                    ),
                    "licensing_id_type": "ASSET_TYPE_GROUP_MDM",
                    "path_id": mdm_id,
                },
            )
        failures = (payload.get("data") or {}).get("fail_reason") or []
        meaningful = [
            item for item in failures
            if isinstance(item, dict) and str(item.get("message") or item.get("reason") or "").strip()
        ]
        if meaningful:
            raise AdqError(
                f"原视频已上传，但公司主体共享授权存在失败项：{meaningful[:3]}",
                request_id=str(payload.get("trace_id") or payload.get("request_id") or ""),
                category="shared_authorization",
                retryable=True,
            )
        return {
            "source_account_id": source_account_id,
            "mdm_id": mdm_id,
            "scope": "all_current_and_future_accounts",
            "failures": failures,
            "request_id": str(payload.get("trace_id") or payload.get("request_id") or ""),
        }

    @staticmethod
    def _component_refs_with_video(source: dict, video_id: str, cover_id: str) -> dict:
        source_components = source.get("creative_components") or {}
        if not isinstance(source_components, dict):
            raise AdqError("现有创意模板缺少组件信息，无法安全复用", category="template_unavailable")
        result: dict[str, list[dict]] = {}
        for component_type, entries in source_components.items():
            if component_type in {"video", "video_channels_content"} or not isinstance(entries, list):
                continue
            refs = [
                {"component_id": item["component_id"]}
                for item in entries
                if isinstance(item, dict) and item.get("component_id")
            ]
            if refs:
                result[component_type] = refs
        # Tencent dynamic_creatives/add requires material identifiers to remain strings,
        # even when the identifiers contain only digits.
        video_value: dict[str, Any] = {"video_id": str(video_id)}
        if cover_id:
            video_value["cover_id"] = str(cover_id)
        result["video"] = [{"value": video_value}]
        return result

    def video_evidence(self, db: Session, *, account_id: str, adgroup_id: str, video_id: str) -> dict:
        creatives = self.dynamic_creatives(db, account_id=account_id, adgroup_id=adgroup_id, fresh=True)
        matches: list[dict] = []
        for creative in creatives:
            components = creative.get("creative_components") or {}
            for item in components.get("video") or []:
                value = item.get("value") if isinstance(item, dict) else {}
                if str((value or {}).get("video_id") or "") == str(video_id):
                    matches.append({
                        "dynamic_creative_id": str(creative.get("dynamic_creative_id") or ""),
                        "dynamic_creative_name": str(creative.get("dynamic_creative_name") or ""),
                        "configured_status": str(creative.get("configured_status") or ""),
                        "system_status": str(creative.get("system_status") or ""),
                    })
        return {
            "account_id": str(account_id),
            "adgroup_id": str(adgroup_id),
            "video_id": str(video_id),
            "verified": bool(matches),
            "matched_count": len(matches),
            "matches": matches,
            "readback_source": "dynamic_creatives/get",
            "verified_at": datetime.now(timezone.utc).isoformat() if matches else "",
        }

    def _write_lock(self, account_id: str, adgroup_id: str) -> threading.Lock:
        key = f"{account_id}:{adgroup_id}"
        with self._adgroup_write_locks_guard:
            return self._adgroup_write_locks.setdefault(key, threading.Lock())

    def add_to_adgroup(
        self,
        db: Session,
        *,
        account_id: str,
        adgroup_id: str,
        source_dynamic_creative_id: str,
        video_id: str,
        cover_id: str,
        filename: str,
    ) -> dict:
        with self._write_lock(account_id, adgroup_id):
            existing = self.video_evidence(
                db, account_id=account_id, adgroup_id=adgroup_id, video_id=video_id
            )
            if existing["matched_count"]:
                return {
                    "dynamic_creative_id": existing["matches"][0]["dynamic_creative_id"],
                    "request_id": "",
                    "already_present": True,
                    "binding_evidence": existing,
                }
            creatives = self.dynamic_creatives(db, account_id=account_id, adgroup_id=adgroup_id, fresh=True)
            source = next(
                (
                    item for item in creatives
                    if str(item.get("dynamic_creative_id") or "") == str(source_dynamic_creative_id)
                    and self._has_video_component(item)
                ),
                None,
            ) or self._choose_source(creatives)
            if not source:
                raise AdqError("目标营销单元没有可安全复用的视频创意模板", category="template_unavailable")
            body: dict[str, Any] = {
                "account_id": int(account_id) if account_id.isdigit() else account_id,
                "adgroup_id": int(adgroup_id) if adgroup_id.isdigit() else adgroup_id,
                "dynamic_creative_name": self._safe_creative_name(filename),
                "creative_components": self._component_refs_with_video(source, video_id, cover_id),
            }
            for field in ("creative_template_id", "delivery_mode", "dynamic_creative_type"):
                if source.get(field) not in (None, ""):
                    value = source[field]
                    body[field] = int(value) if field == "creative_template_id" and str(value).isdigit() else value
            payload = self._request(
                db,
                "POST",
                DYNAMIC_CREATIVES_ADD_URL,
                json_body=body,
                requires_user_token=True,
            )
            data = payload.get("data") or {}
            creative_id = str(data.get("dynamic_creative_id") or data.get("id") or "")
            self.invalidate_catalog()
            for delay in (0, 2, 5, 10):
                if delay:
                    time.sleep(delay)
                evidence = self.video_evidence(
                    db, account_id=account_id, adgroup_id=adgroup_id, video_id=video_id
                )
                if evidence["matched_count"]:
                    match = next(
                        (item for item in evidence["matches"] if item["dynamic_creative_id"] == creative_id),
                        evidence["matches"][0],
                    )
                    return {
                        "dynamic_creative_id": match["dynamic_creative_id"],
                        "request_id": str(payload.get("trace_id") or payload.get("request_id") or ""),
                        "already_present": False,
                        "binding_evidence": evidence,
                    }
            raise AdqError(
                "腾讯 ADQ 已受理创意，但回读营销单元尚未发现该视频；可稍后安全重试",
                request_id=str(payload.get("trace_id") or payload.get("request_id") or ""),
                category="binding_verification_pending",
                retryable=True,
                entity_id=creative_id,
            )

    @staticmethod
    def _safe_creative_name(filename: str) -> str:
        stem = re.sub(r"[<>＆&‘’“”/\\\t\r\n]+", "-", Path(filename).stem).strip(" .-") or "WIS素材"
        return f"WIS-{stem[:48]}-{datetime.now().strftime('%m%d%H%M%S')}-{uuid4().hex[:5]}"[:80]

    @staticmethod
    def _optional_number(value: Any, *, integer: bool = False) -> int | float | None:
        if value in (None, ""):
            return None
        try:
            return int(value) if integer else float(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _metric_money_yuan(cls, value: Any) -> float | None:
        number = cls._optional_number(value)
        return round(float(number) / 100, 2) if number is not None else None

    def _material_report_rows(
        self,
        db: Session,
        *,
        account_id: str,
        start: date,
        end: date,
        adgroup_id: str = "",
        video_id: str = "",
    ) -> list[dict]:
        filtering: list[dict] = []
        group_by = ["date"]
        if adgroup_id:
            filtering.append({"field": "adgroup_id", "operator": "EQUALS", "values": [str(adgroup_id)]})
            group_by.append("adgroup_id")
        if video_id:
            filtering.append({"field": "video_id", "operator": "EQUALS", "values": [str(video_id)]})
        group_by.append("video_id")
        params = {
            "account_id": account_id,
            "level": "REPORT_LEVEL_MATERIAL_VIDEO",
            "date_range": json.dumps({"start_date": start.isoformat(), "end_date": end.isoformat()}),
            "group_by": json.dumps(group_by),
            "fields": json.dumps(REPORT_FIELDS),
            "time_line": "REPORTING_TIME",
            "adq_accounts_upgrade_enabled": "true",
        }
        if filtering:
            params["filtering"] = json.dumps(filtering)
        return self._fetch_all(
            db,
            DAILY_REPORTS_URL,
            params,
            page_size=100,
            pagination_mode=False,
            token_profile=self._account_token_profile(db, account_id),
        )

    @classmethod
    def _report_row_metrics(cls, row: dict) -> dict:
        cost_fen = cls._optional_number(row.get("cost"), integer=True)
        view_count = cls._optional_number(row.get("view_count"), integer=True)
        valid_click_count = cls._optional_number(row.get("valid_click_count"), integer=True)
        metrics = {
            "cost_yuan": cls._metric_money_yuan(cost_fen),
            "view_count": view_count,
            "valid_click_count": valid_click_count,
            "view_click_rate": (
                round(valid_click_count / view_count, 6)
                if valid_click_count is not None and view_count not in (None, 0)
                else None
            ),
            "order_amount_yuan": cls._metric_money_yuan(row.get("order_amount")),
            "order_roi": cls._optional_number(row.get("order_roi")),
            "order_24h_by_click_amount_yuan": cls._metric_money_yuan(row.get("order_24h_by_click_amount")),
            "order_24h_by_click_roi": cls._optional_number(row.get("order_24h_by_click_roi")),
            "order_net_amount_yuan": cls._metric_money_yuan(row.get("order_net_amount")),
            "order_net_roi": cls._optional_number(row.get("order_net_roi")),
        }
        # Temporary aliases keep existing stored task rows/UI compatible while
        # labels migrate from the old, unsupported impression/click names.
        metrics["impression"] = view_count
        metrics["click"] = valid_click_count
        metrics["ctr"] = metrics["view_click_rate"]
        return {key: value for key, value in metrics.items() if value is not None}

    @classmethod
    def _aggregate_report_metrics(cls, rows: list[dict]) -> dict:
        def sum_int(field: str) -> int | None:
            values = [cls._optional_number(row.get(field), integer=True) for row in rows]
            available = [int(value) for value in values if value is not None]
            return sum(available) if available else None

        cost_fen = sum_int("cost")
        views = sum_int("view_count")
        clicks = sum_int("valid_click_count")
        order_amount_fen = sum_int("order_amount")
        order_24h_fen = sum_int("order_24h_by_click_amount")
        order_net_fen = sum_int("order_net_amount")
        metrics = {
            "cost_yuan": cls._metric_money_yuan(cost_fen),
            "view_count": views,
            "valid_click_count": clicks,
            "view_click_rate": round(clicks / views, 6) if clicks is not None and views not in (None, 0) else None,
            "order_amount_yuan": cls._metric_money_yuan(order_amount_fen),
            "order_roi": round(order_amount_fen / cost_fen, 6) if order_amount_fen is not None and cost_fen not in (None, 0) else None,
            "order_24h_by_click_amount_yuan": cls._metric_money_yuan(order_24h_fen),
            "order_24h_by_click_roi": round(order_24h_fen / cost_fen, 6) if order_24h_fen is not None and cost_fen not in (None, 0) else None,
            "order_net_amount_yuan": cls._metric_money_yuan(order_net_fen),
            "order_net_roi": round(order_net_fen / cost_fen, 6) if order_net_fen is not None and cost_fen not in (None, 0) else None,
        }
        metrics["impression"] = views
        metrics["click"] = clicks
        metrics["ctr"] = metrics["view_click_rate"]
        return {key: value for key, value in metrics.items() if value is not None}

    def material_metrics(
        self,
        db: Session,
        *,
        account_id: str,
        adgroup_id: str,
        video_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        local_today = datetime.now(timezone(timedelta(hours=8))).date()
        end = date.fromisoformat(end_date) if end_date else local_today - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=6)
        if start > end:
            raise AdqError("数据开始日期不能晚于结束日期", category="invalid_date")
        rows = self._material_report_rows(
            db,
            account_id=account_id,
            start=start,
            end=end,
            adgroup_id=adgroup_id,
            video_id=video_id,
        )
        daily: list[dict] = []
        for row in rows:
            metrics = self._report_row_metrics(row)
            daily.append({
                "date": str(row.get("date") or ""),
                **metrics,
                "has_data": True,
            })
        metrics = self._aggregate_report_metrics(rows) if rows else {}
        return {
            "metrics": metrics,
            "daily": daily,
            "has_data": bool(rows),
            "row_count": len(rows),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "request_id": "",
            "source": "daily_reports/get:REPORT_LEVEL_MATERIAL_VIDEO",
        }

    def account_material_metrics(
        self,
        db: Session,
        *,
        account_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
        limit: int | None = None,
        q: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        allowed_ids = self._normalized_ids([
            *self.configured_account_ids,
            *self.configured_catalog_account_ids,
            settings.adq_shared_source_account_id,
        ])
        if account_id not in allowed_ids:
            raise AdqError("当前授权未开放该腾讯 ADQ 账户的素材数据", category="account_unavailable")
        local_today = datetime.now(timezone(timedelta(hours=8))).date()
        end = date.fromisoformat(end_date) if end_date else local_today - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=29)
        if start > end:
            raise AdqError("数据开始日期不能晚于结束日期", category="invalid_date")
        if (end - start).days > 30:
            raise AdqError("账户级素材回流单次最多读取 31 天", category="invalid_date")
        cache_key = f"account-material-metrics:{account_id}:{start}:{end}:all"
        cached = self._cache_get(cache_key, max_age=300)
        if cached is None:
            rows = self._material_report_rows(
                db,
                account_id=account_id,
                start=start,
                end=end,
            )
            by_video: dict[str, list[dict]] = {}
            for row in rows:
                video_id = str(row.get("video_id") or "").strip()
                if video_id:
                    by_video.setdefault(video_id, []).append(row)
            videos = [
                {
                    "video_id": video_id,
                    "row_count": len(video_rows),
                    "metrics": self._aggregate_report_metrics(video_rows),
                }
                for video_id, video_rows in by_video.items()
            ]
            videos.sort(
                key=lambda item: float(item["metrics"].get("cost_yuan") or 0),
                reverse=True,
            )
            cached = self._cache_set(cache_key, {
                "account_id": account_id,
                "metrics": self._aggregate_report_metrics(rows) if rows else {},
                "has_data": bool(rows),
                "row_count": len(rows),
                "video_count": len(by_video),
                "videos": videos,
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "source": "daily_reports/get:REPORT_LEVEL_MATERIAL_VIDEO:account",
                "message": (
                    f"已按素材汇总 {len(by_video)} 条视频的累计数据"
                    if rows
                    else "平台在所选日期范围未返回素材记录；这是暂无数据，不按 0 处理"
                ),
            })
        effective_size = max(1, min(100, int(limit or page_size)))
        effective_page = max(1, int(page))
        term = q.strip().casefold()
        filtered = [item for item in cached["videos"] if not term or term in item["video_id"].casefold()]
        total = len(filtered)
        offset = (effective_page - 1) * effective_size
        return {
            **{key: value for key, value in cached.items() if key != "videos"},
            "videos": filtered[offset:offset + effective_size],
            "total": total,
            "page": effective_page,
            "page_size": effective_size,
            "total_pages": max(1, (total + effective_size - 1) // effective_size),
        }


adq_service = AdqService()
