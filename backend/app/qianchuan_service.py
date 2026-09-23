from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import requests
from requests_toolbelt.multipart.encoder import MultipartEncoder
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .public_urls import qianchuan_callback_uri
from .models import AppMeta, QianchuanDelivery
from .oss_service import display_filename_from_object_key, oss_service


API_ROOT = "https://api.oceanengine.com"
AUTHORIZE_URL = "https://ad.oceanengine.com/openapi/audit/oauth.html"
TOKEN_URL = f"{API_ROOT}/open_api/oauth2/access_token/"
REFRESH_URL = f"{API_ROOT}/open_api/oauth2/refresh_token/"
ACCOUNTS_URL = f"{API_ROOT}/open_api/oauth2/advertiser/get/"
SHOP_ADVERTISERS_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/shop/advertiser/list/"
EBP_ADVERTISERS_URL = f"{API_ROOT}/open_api/2/ebp/advertiser/list/"
CUSTOMER_ACCOUNTS_URL = f"{API_ROOT}/open_api/v3.0/customer_center/account/list/"
ADVERTISER_INFO_URL = f"{API_ROOT}/open_api/2/advertiser/public_info/"
PLAN_LIST_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/ad/get/"
FULL_DOMAIN_PLAN_LIST_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/uni_promotion/list/"
FULL_DOMAIN_PLAN_DETAIL_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/uni_promotion/ad/detail/"
FULL_DOMAIN_MATERIAL_LIST_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/uni_promotion/ad/material/get/"
VIDEO_UPLOAD_URL = f"{API_ROOT}/open_api/2/file/video/ad/"
ASYNC_VIDEO_UPLOAD_URL = f"{API_ROOT}/open_api/2/file/upload_task/create/"
ASYNC_VIDEO_UPLOAD_RESULT_URL = f"{API_ROOT}/open_api/2/file/video/upload_task/list/"
QIANCHUAN_VIDEO_LIST_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/video/get/"
IMAGE_UPLOAD_URL = f"{API_ROOT}/open_api/2/file/image/ad/"
FULL_DOMAIN_ADD_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/uni_promotion/ad/material/add/"
MATERIAL_REPORT_URL = f"{API_ROOT}/open_api/v1.0/qianchuan/report/material/get/"

REPORT_FIELDS = [
    "stat_cost",
    "show_cnt",
    "click_cnt",
    "ctr",
    "pay_order_amount",
    "pay_order_count",
    "create_order_roi",
    "prepay_and_pay_order_roi",
    "convert_cnt",
    "cpa_platform",
    "total_play",
    "play_over_rate",
]

PLAN_MATERIAL_REPORT_FIELDS = [
    "stat_cost_for_roi2",
    "total_pay_order_gmv_include_coupon_for_roi2",
    "total_pay_order_gmv_for_roi2",
    "total_pay_order_count_for_roi2",
    "total_prepay_and_pay_order_roi2",
    "product_show_count_for_roi2",
    "product_click_count_for_roi2",
]

UNIFIED_PLAN_SCENES = (
    ("OVERALL_PROJECT", "multiplication", "乘方计划"),
    ("UNI_PROJECT", "full_domain", "全域推广"),
)

logger = logging.getLogger(__name__)

# Production runs one uvicorn process with multiple threaded workers. Share the
# locks across service instances; OAuth saves must also fence stale refreshes.
_token_refresh_lock = threading.Lock()
_token_write_lock = threading.RLock()


class QianchuanError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str = "",
        request_id: str = "",
        category: str = "platform",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.request_id = request_id
        self.category = category
        self.retryable = retryable


def _meta(db: Session, key: str, fallback: str = "") -> str:
    row = db.get(AppMeta, key)
    return row.value if row else fallback


def _set_meta(db: Session, key: str, value: Any) -> None:
    text = "" if value is None else str(value)
    row = db.get(AppMeta, key)
    if row:
        row.value = text
    else:
        db.add(AppMeta(key=key, value=text))


def _parse_time(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _normalized_list(data: Any) -> list[dict]:
    if not isinstance(data, dict):
        return []
    for key in ("list", "account_list", "advertiser_list", "accounts", "advertisers"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


class QianchuanService:
    timeout = (10, 90)

    def __init__(self) -> None:
        self._account_cache_lock = threading.RLock()
        self._account_cache: dict[str, Any] | None = None
        self._plan_request_lock = threading.Lock()
        self._video_upload_lock = threading.Lock()
        self._plan_cache_lock = threading.RLock()
        self._last_plan_request_at = 0.0
        self._last_video_upload_at = 0.0
        self._plan_source_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._plan_source_locks: dict[tuple[str, str, str], threading.Lock] = {}
        self._standard_permission_denied_until = 0.0

    @property
    def plan_request_interval(self) -> float:
        try:
            return max(0.0, float(os.getenv("QIANCHUAN_PLAN_REQUEST_INTERVAL", "0.8")))
        except ValueError:
            return 0.8

    @property
    def video_upload_request_interval(self) -> float:
        try:
            return max(
                0.0,
                float(os.getenv("QIANCHUAN_VIDEO_UPLOAD_REQUEST_INTERVAL", "3")),
            )
        except ValueError:
            return 3.0

    @property
    def plan_cache_seconds(self) -> int:
        try:
            return max(0, int(os.getenv("QIANCHUAN_PLAN_CACHE_SECONDS", "1800")))
        except ValueError:
            return 1800

    @property
    def account_cache_seconds(self) -> int:
        try:
            return max(60, int(os.getenv("QIANCHUAN_ACCOUNT_CACHE_SECONDS", "900")))
        except ValueError:
            return 900

    @property
    def account_stale_seconds(self) -> int:
        try:
            return max(
                self.account_cache_seconds,
                int(os.getenv("QIANCHUAN_ACCOUNT_STALE_SECONDS", "21600")),
            )
        except ValueError:
            return 21600

    @property
    def plan_stale_seconds(self) -> int:
        try:
            return max(self.plan_cache_seconds, int(os.getenv("QIANCHUAN_PLAN_STALE_SECONDS", "21600")))
        except ValueError:
            return 21600

    @property
    def plan_load_budget_seconds(self) -> float:
        try:
            return max(15.0, float(os.getenv("QIANCHUAN_PLAN_LOAD_BUDGET_SECONDS", "50")))
        except ValueError:
            return 50.0

    @property
    def standard_permission_cache_seconds(self) -> int:
        try:
            return max(60, int(os.getenv("QIANCHUAN_STANDARD_PERMISSION_CACHE_SECONDS", "3600")))
        except ValueError:
            return 3600

    @property
    def async_upload_poll_seconds(self) -> float:
        try:
            return max(1.0, float(os.getenv("QIANCHUAN_ASYNC_UPLOAD_POLL_SECONDS", "3")))
        except ValueError:
            return 3.0

    @property
    def async_upload_timeout_seconds(self) -> int:
        try:
            return max(30, int(os.getenv("QIANCHUAN_ASYNC_UPLOAD_TIMEOUT_SECONDS", "900")))
        except ValueError:
            return 900

    @property
    def async_upload_url_expiry_seconds(self) -> int:
        try:
            return max(3600, int(os.getenv("QIANCHUAN_ASYNC_UPLOAD_URL_EXPIRY_SECONDS", "21600")))
        except ValueError:
            return 21600

    @property
    def app_id(self) -> str:
        return os.getenv("QIANCHUAN_APP_ID", "").strip()

    @property
    def app_secret(self) -> str:
        return os.getenv("QIANCHUAN_APP_SECRET", "").strip()

    @property
    def redirect_uri(self) -> str:
        return qianchuan_callback_uri()

    def _access_token(self, db: Session) -> str:
        if not self._authorization_scope_matches(db):
            return ""
        if _meta(db, "qianchuan.authorization_invalid") == "1":
            return ""
        return _meta(db, "qianchuan.access_token") or os.getenv("QIANCHUAN_ACCESS_TOKEN", "").strip()

    def _refresh_token(self, db: Session) -> str:
        if not self._authorization_scope_matches(db):
            return ""
        if _meta(db, "qianchuan.authorization_invalid") == "1":
            return ""
        return _meta(db, "qianchuan.refresh_token") or os.getenv("QIANCHUAN_REFRESH_TOKEN", "").strip()

    def _authorization_scope_matches(self, db: Session) -> bool:
        expected = os.getenv("QIANCHUAN_AUTHORIZATION_SCOPE", "").strip()
        return not expected or _meta(db, "qianchuan.authorization_scope") == expected

    def _saved_at(self, db: Session) -> datetime | None:
        return _parse_time(
            _meta(db, "qianchuan.saved_at") or os.getenv("QIANCHUAN_TOKEN_SAVED_AT", "").strip()
        )

    def _expires_in(self, db: Session, *, refresh: bool = False) -> int:
        key = "qianchuan.refresh_expires_in" if refresh else "qianchuan.expires_in"
        env_key = "QIANCHUAN_REFRESH_EXPIRES_IN" if refresh else "QIANCHUAN_EXPIRES_IN"
        try:
            return int(_meta(db, key) or os.getenv(env_key, "0") or 0)
        except ValueError:
            return 0

    def _access_needs_refresh(self, db: Session) -> bool:
        token = self._access_token(db)
        saved_at = self._saved_at(db)
        expires_in = self._expires_in(db)
        if not token:
            return True
        if not saved_at or not expires_in:
            return False
        return datetime.now(timezone.utc) >= saved_at + timedelta(seconds=max(0, expires_in - 300))

    def _refresh_valid_until(self, db: Session) -> datetime | None:
        fixed = _parse_time(_meta(db, "qianchuan.refresh_valid_until"))
        saved, lifetime = self._saved_at(db), self._expires_in(db, refresh=True)
        return fixed or (saved + timedelta(seconds=lifetime) if saved and lifetime else None)

    def status(self, db: Session) -> dict:
        saved_at = self._saved_at(db)
        refresh_expires = self._expires_in(db, refresh=True)
        refresh_until = self._refresh_valid_until(db)
        has_access = bool(self._access_token(db))
        has_refresh = bool(self._refresh_token(db))
        access_valid = bool(has_access and not self._access_needs_refresh(db))
        refresh_valid = bool(has_refresh and (not refresh_until or refresh_until > datetime.now(timezone.utc)))
        configured = bool(self.app_id and self.app_secret)
        authorized = configured and (access_valid or refresh_valid)
        if not configured:
            message = "尚未配置千川开放平台应用"
        elif not authorized:
            message = "应用已配置，等待千川授权"
        elif self._access_needs_refresh(db) and refresh_valid:
            message = "已有授权，访问令牌将在调用时自动刷新"
        else:
            message = "千川授权可用"
        retry_at = _parse_time(_meta(db, "qianchuan.refresh_retry_at"))
        temporarily_unavailable = bool(
            authorized and self._access_needs_refresh(db)
            and _meta(db, "qianchuan.refresh_error")
        )
        if temporarily_unavailable:
            message = "千川续期暂时失败；授权凭据及原任务已保留，系统将退避重试"
        elif configured and not authorized:
            message = "千川需要重新授权；原任务和已上传素材保留，不会自动补推历史失败记录"
        return {
            "configured": configured,
            "authorized": authorized,
            "message": message,
            "authorization_state": (
                "not_configured" if not configured else
                "reauthorization_required" if not authorized else
                "refresh_temporarily_unavailable" if temporarily_unavailable else "ready"
            ),
            "dispatch_paused": not authorized or temporarily_unavailable,
            "refresh_retry_at": retry_at.isoformat() if retry_at else None,
            "app_id": self.app_id,
            "authorized_at": saved_at.isoformat() if saved_at else None,
            "refresh_valid_until": refresh_until.isoformat() if refresh_until else None,
            "redirect_uri": self.redirect_uri,
            "capabilities": {
                "account_material_upload": True,
                "full_domain_plan_add": True,
                "standard_plan_direct_add": False,
                "material_report": True,
            },
        }

    def validated_status(self, db: Session) -> dict:
        snapshot = self.status(db)
        if snapshot["authorized"] and self._access_needs_refresh(db):
            try:
                self.refresh_access_token(db)
            except QianchuanError:
                pass
        return self.status(db)

    def authorize_url(self, db: Session) -> str:
        if not self.app_id:
            raise QianchuanError("尚未配置千川开放平台 App ID")
        state = secrets.token_urlsafe(32)
        _set_meta(db, "qianchuan.oauth_state", state)
        _set_meta(db, "qianchuan.oauth_state_scope", os.getenv("QIANCHUAN_AUTHORIZATION_SCOPE", "").strip())
        _set_meta(db, "qianchuan.oauth_state_expires", (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat())
        db.commit()
        configured_url = os.getenv("QIANCHUAN_AUTH_URL", "").strip()
        base = configured_url or AUTHORIZE_URL
        parsed = urlsplit(base)
        query = dict(parse_qsl(parsed.query, keep_blank_values=True))
        query.update({"app_id": self.app_id, "state": state, "redirect_uri": self.redirect_uri})
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))

    def exchange_code(self, db: Session, auth_code: str, state: str) -> None:
        expected = _meta(db, "qianchuan.oauth_state")
        expected_scope = os.getenv("QIANCHUAN_AUTHORIZATION_SCOPE", "").strip()
        state_scope_matches = not expected_scope or _meta(db, "qianchuan.oauth_state_scope") == expected_scope
        expires = _parse_time(_meta(db, "qianchuan.oauth_state_expires"))
        _set_meta(db, "qianchuan.oauth_state", "")
        _set_meta(db, "qianchuan.oauth_state_expires", "")
        if not state_scope_matches or not expected or not state or not secrets.compare_digest(expected, state):
            db.commit()
            raise QianchuanError("千川授权状态校验失败，请重新发起授权")
        if not expires or expires <= datetime.now(timezone.utc):
            db.commit()
            raise QianchuanError("千川授权链接已过期，请重新发起授权")
        payload = self._request_json(
            "POST",
            TOKEN_URL,
            json_body={"app_id": int(self.app_id), "secret": self.app_secret, "auth_code": auth_code},
            token="",
        )
        self._store_token(db, payload.get("data") or {})

    def _store_token(self, db: Session, data: dict) -> None:
        # Do not carry an old identity-map snapshot across another token writer.
        db.commit()
        with _token_write_lock:
            db.expire_all()
            self._store_token_locked(db, data)

    def _store_token_locked(self, db: Session, data: dict) -> None:
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise QianchuanError("千川返回成功，但没有访问令牌")
        previous_refresh_until = self._refresh_valid_until(db)
        refresh_lifetime = int(data.get("refresh_token_expires_in") or 0)
        refresh_until = (
            datetime.now(timezone.utc) + timedelta(seconds=refresh_lifetime)
            if data.get("refresh_token") and refresh_lifetime > 0 else previous_refresh_until
        )
        _set_meta(db, "qianchuan.access_token", access_token)
        _set_meta(db, "qianchuan.authorization_scope", os.getenv("QIANCHUAN_AUTHORIZATION_SCOPE", "").strip())
        if data.get("refresh_token"):
            _set_meta(db, "qianchuan.refresh_token", data["refresh_token"])
        _set_meta(db, "qianchuan.expires_in", data.get("expires_in") or 0)
        _set_meta(db, "qianchuan.refresh_expires_in", data.get("refresh_token_expires_in") or 0)
        _set_meta(db, "qianchuan.refresh_valid_until", refresh_until.isoformat() if refresh_until else "")
        _set_meta(db, "qianchuan.saved_at", datetime.now(timezone.utc).isoformat())
        _set_meta(db, "qianchuan.authorization_invalid", "0")
        _set_meta(db, "qianchuan.authorization_error", "")
        for key in ("refresh_error", "refresh_retry_at", "refresh_failure_count"):
            _set_meta(db, "qianchuan." + key, "")
        _set_meta(db, "qianchuan.accounts_cache", "")
        # accounts() already holds its cache lock while requesting a token.
        # An atomic reset avoids cache-lock/refresh-lock inversion.
        self._account_cache = None
        db.commit()

    def dispatch_block_reason(self, db: Session) -> str:
        """Read-only gate: never refresh or mutate deliveries from the scheduler."""
        if not self.app_id or not self.app_secret:
            return "千川应用未配置；原任务保留，请联系管理员"
        if not self._authorization_scope_matches(db):
            return "当前部署需要独立完成千川授权；已保留旧授权记录，不会刷新或使用旧站凭据"
        if _meta(db, "qianchuan.authorization_invalid") == "1":
            return "千川需要重新授权；原任务及已上传素材保留，尚未继续推送"
        if not self._access_needs_refresh(db):
            return ""
        refresh_until = self._refresh_valid_until(db)
        if not self._refresh_token(db) or (
            refresh_until and datetime.now(timezone.utc) >= refresh_until
        ):
            return "千川需要重新授权；原任务及已上传素材保留，尚未继续推送"
        retry_at = _parse_time(_meta(db, "qianchuan.refresh_retry_at"))
        if retry_at and retry_at > datetime.now(timezone.utc):
            return "千川续期暂时不可用，正在退避等待；授权凭据及原任务已保留"
        return ""

    @staticmethod
    def _refresh_definitively_invalid(error: QianchuanError) -> bool:
        # Never infer revocation from HTTP 401, generic token/app errors, or
        # undocumented numeric codes. Only explicit refresh-credential rejection.
        if error.retryable or error.category != "platform":
            return False
        message = re.sub(r"[\s_\-]", "", str(error).casefold())
        return bool(re.fullmatch(
            r"(?:refreshtoken|刷新令牌|刷新凭据)(?:已)?(?:过期|失效|无效|被撤销|不存在)[。.!]?"
            r"|(?:invalid|expired|revoked)refreshtoken[.!]?"
            r"|refreshtoken(?:is|has)?(?:invalid|expired|revoked)[.!]?",
            message,
        ))

    def refresh_access_token(self, db: Session) -> str:
        # Release the DB connection before joining the single-flight lane.
        db.commit()
        with _token_refresh_lock:
            db.expire_all()
            blocked = self.dispatch_block_reason(db)
            if blocked:
                temporary = bool(_parse_time(_meta(db, "qianchuan.refresh_retry_at")))
                raise QianchuanError(blocked, category="authorization_temporary" if temporary else "authorization_required", retryable=temporary)
            # A preceding waiter may already have rotated both tokens.
            if not self._access_needs_refresh(db):
                return self._access_token(db)
            with _token_write_lock:
                db.expire_all()
                if not self._access_needs_refresh(db):
                    return self._access_token(db)
                snapshot = (self._access_token(db), self._refresh_token(db), _meta(db, "qianchuan.saved_at"))
                db.commit()
            failure = None
            try:
                payload = self._request_json(
                    "POST", REFRESH_URL,
                    json_body={"app_id": int(self.app_id), "secret": self.app_secret, "refresh_token": snapshot[1]},
                    token="", request_timeout=(10, 30),
                )
                data = payload.get("data") or {}
                if not isinstance(data, dict) or not data.get("access_token"):
                    raise QianchuanError("千川续期响应缺少访问令牌", category="invalid_response")
                try:
                    valid_expiry = int(data.get("expires_in") or 0) > 0
                    int(data.get("refresh_token_expires_in") or 0)
                except (ValueError, TypeError):
                    valid_expiry = False
                if not valid_expiry:
                    raise QianchuanError("千川续期响应缺少有效期限", category="invalid_response")
            except QianchuanError as error:
                failure = error
            with _token_write_lock:
                db.expire_all()
                current = (self._access_token(db), self._refresh_token(db), _meta(db, "qianchuan.saved_at"))
                if current != snapshot:
                    # A fresh OAuth save wins over both stale success and failure.
                    if not self._access_needs_refresh(db):
                        return self._access_token(db)
                    raise QianchuanError("千川授权已更新，请使用当前授权稍后接续原任务", category="authorization_temporary", retryable=True)
                if failure is None:
                    self._store_token_locked(db, data)
                    return self._access_token(db)
                # Keep secrets out of diagnostics, even if an upstream echoes them.
                detail = str(failure)
                for secret in (*snapshot[:2], self.app_secret):
                    if secret:
                        detail = detail.replace(secret, "[redacted]")
                if self._refresh_definitively_invalid(failure):
                    _set_meta(db, "qianchuan.authorization_invalid", "1")
                    _set_meta(db, "qianchuan.authorization_error", detail[:500])
                    db.commit()
                    raise QianchuanError("千川明确返回刷新凭据无效，请重新授权；原任务已保留", category="authorization_required", code=failure.code, request_id=failure.request_id) from failure
                try:
                    failures = min(5, int(_meta(db, "qianchuan.refresh_failure_count") or 0) + 1)
                except ValueError:
                    failures = 1
                delay = min(300, 30 * 2 ** (failures - 1))
                _set_meta(db, "qianchuan.refresh_error", detail[:500])
                _set_meta(db, "qianchuan.refresh_failure_count", failures)
                _set_meta(db, "qianchuan.refresh_retry_at", (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat())
                db.commit()
                logger.warning("Qianchuan refresh deferred category=%s code=%s request_id=%s", failure.category, failure.code, failure.request_id)
                raise QianchuanError("千川续期暂时失败；授权凭据及原任务已保留，将退避重试", category="authorization_temporary", retryable=True, code=failure.code, request_id=failure.request_id) from failure

    def token(self, db: Session) -> str:
        if self._access_needs_refresh(db):
            return self.refresh_access_token(db)
        token = self._access_token(db)
        if not token:
            raise QianchuanError("千川尚未授权")
        return token

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        token: str,
        params: dict | None = None,
        json_body: dict | None = None,
        files: dict | None = None,
        data: dict | None = None,
        extra_headers: dict[str, str] | None = None,
        request_timeout: tuple[int, int] | None = None,
    ) -> dict:
        headers = {"Accept": "application/json"}
        if token:
            headers["Access-Token"] = token
        if extra_headers:
            headers.update(extra_headers)
        endpoint = urlsplit(url).path
        try:
            response = requests.request(
                method,
                url,
                headers=headers,
                params=params,
                json=json_body,
                files=files,
                data=data,
                timeout=request_timeout or self.timeout,
            )
        except requests.ConnectTimeout as error:
            logger.warning("Qianchuan connect timeout endpoint=%s type=%s", endpoint, type(error).__name__)
            raise QianchuanError(
                "连接千川开放平台超时，请稍后重试",
                category="connect_timeout",
                retryable=True,
            ) from error
        except requests.ReadTimeout as error:
            logger.warning("Qianchuan read timeout endpoint=%s type=%s", endpoint, type(error).__name__)
            raise QianchuanError(
                "千川处理请求超时；异步上传任务会保留，重试时继续查询原视频处理结果",
                category="read_timeout",
                retryable=True,
            ) from error
        except requests.ConnectionError as error:
            logger.warning("Qianchuan connection interrupted endpoint=%s type=%s", endpoint, type(error).__name__)
            is_video_transfer = bool(files) or endpoint.endswith("/file/video/ad/")
            message = "视频传输过程中千川中断了连接；系统不会压缩视频，将按原文件安全重试" if is_video_transfer else "连接千川开放平台中断，请稍后重试"
            raise QianchuanError(message, category="connection_interrupted", retryable=True) from error
        except requests.RequestException as error:
            logger.warning("Qianchuan request failed endpoint=%s type=%s", endpoint, type(error).__name__)
            raise QianchuanError(
                "连接千川开放平台失败，请稍后重试",
                category="network",
                retryable=True,
            ) from error
        try:
            payload = response.json()
        except ValueError as error:
            if response.status_code == 413:
                raise QianchuanError(
                    "千川拒绝了同步大文件请求；系统不会压缩视频，请改用原文件异步上传",
                    code=413,
                    category="video_too_large",
                ) from error
            if response.status_code < 200 or response.status_code >= 300:
                raise QianchuanError(
                    f"千川开放平台返回 HTTP {response.status_code}",
                    code=response.status_code,
                    category="platform_http",
                    retryable=response.status_code >= 500,
                ) from error
            raise QianchuanError("千川开放平台返回了无法识别的数据", category="invalid_response") from error
        code = payload.get("code")
        if response.status_code < 200 or response.status_code >= 300 or code not in (0, "0", None):
            message = str(payload.get("message") or payload.get("error") or f"HTTP {response.status_code}")
            normalized_code = str(code or response.status_code)
            normalized_message = message.casefold()
            rate_limited = (
                response.status_code == 429
                or normalized_code in {"429", "40100", "4028"}
                or "频率" in message
                or "限频" in message
                or "too much throughput" in normalized_message
                or "slow down" in normalized_message
            )
            raise QianchuanError(
                message[:1000],
                code=code or response.status_code,
                request_id=str(payload.get("request_id") or ""),
                category="rate_limit" if rate_limited else "platform",
                retryable=rate_limited or response.status_code >= 500,
            )
        return payload

    def _authorized_request(self, db: Session, method: str, url: str, **kwargs) -> dict:
        token = self.token(db)
        # Token/meta reads start a SQLAlchemy transaction.  Releasing it here
        # prevents a slow Qianchuan request from occupying a database
        # connection needed by the UI, upload progress or the readback lane.
        db.commit()
        return self._request_json(method, url, token=token, **kwargs)

    def _fetch_accounts(self, db: Session) -> list[dict]:
        payload = self._authorized_request(
            db,
            "GET",
            ACCOUNTS_URL,
            params={"app_id": self.app_id, "secret": self.app_secret},
        )
        management = []
        for item in _normalized_list(payload.get("data")):
            advertiser = item.get("advertiser") if isinstance(item.get("advertiser"), dict) else {}
            account_id = item.get("advertiser_id") or item.get("account_id") or item.get("id") or advertiser.get("id")
            if account_id:
                management.append(
                    {
                        "id": str(account_id),
                        "name": str(item.get("advertiser_name") or item.get("account_name") or item.get("name") or advertiser.get("name") or "未命名账户"),
                        "role": str(item.get("role") or item.get("account_role") or item.get("account_type") or item.get("advertiser_role") or ""),
                        "company": str(item.get("company") or ""),
                    }
                )
        advertisers: dict[str, dict] = {}
        advertiser_ids: set[str] = set()
        for parent in management:
            role = parent["role"]
            if role == "PLATFORM_ROLE_ENTERPRISE_BP_OPERATOR":
                page = 1
                while True:
                    child_payload = self._authorized_request(
                        db,
                        "GET",
                        EBP_ADVERTISERS_URL,
                        params={
                            "enterprise_organization_id": parent["id"],
                            "account_source": "QIANCHUAN",
                            "page": page,
                            "page_size": 100,
                        },
                    )
                    data = child_payload.get("data") or {}
                    for item in data.get("account_list") or []:
                        account_id = item.get("advertiser_id") or item.get("account_id") or item.get("id")
                        if account_id:
                            advertisers[str(account_id)] = {
                                "id": str(account_id),
                                "name": str(item.get("advertiser_name") or item.get("account_name") or item.get("name") or "未命名账户"),
                                "role": "QIANCHUAN_ADVERTISER",
                                "company": str(item.get("company") or parent.get("name") or ""),
                            }
                    page_info = data.get("page_info") or {}
                    total_page = int(page_info.get("total_page") or 1)
                    if page >= total_page:
                        break
                    page += 1
            elif role in {"CUSTOMER_ADMIN", "CUSTOMER_OPERATOR"}:
                page = 1
                while True:
                    child_payload = self._authorized_request(
                        db,
                        "GET",
                        CUSTOMER_ACCOUNTS_URL,
                        params={
                            "account_id": parent["id"],
                            "filter": json.dumps({"account_type": "QIANCHUAN"}),
                            "page": page,
                            "page_size": 100,
                        },
                    )
                    data = child_payload.get("data") or {}
                    for item in data.get("accounts") or []:
                        account_id = item.get("account_id") or item.get("advertiser_id") or item.get("id")
                        if account_id:
                            advertisers[str(account_id)] = {
                                "id": str(account_id),
                                "name": str(item.get("account_name") or item.get("advertiser_name") or item.get("name") or "未命名账户"),
                                "role": "QIANCHUAN_ADVERTISER",
                                "company": str(item.get("company") or parent.get("name") or ""),
                            }
                    page_info = data.get("page_info") or {}
                    total_page = int(page_info.get("total_page") or 1)
                    if page >= total_page:
                        break
                    page += 1

        shops = [item for item in management if item["role"] == "PLATFORM_ROLE_SHOP_ACCOUNT"]
        for shop in shops:
            page = 1
            while True:
                shop_payload = self._authorized_request(
                    db,
                    "GET",
                    SHOP_ADVERTISERS_URL,
                    params={"shop_id": shop["id"], "page": page, "page_size": 100},
                )
                data = shop_payload.get("data") or {}
                for item in data.get("adv_id_list") or data.get("list") or []:
                    account_id = item.get("adv_id") if isinstance(item, dict) else item
                    if account_id:
                        advertiser_ids.add(str(account_id))
                total_page = int((data.get("page_info") or {}).get("total_page") or 1)
                if page >= total_page:
                    break
                page += 1

        ids = sorted(advertiser_ids)
        for index in range(0, len(ids), 50):
            public_info = self._authorized_request(
                db,
                "GET",
                ADVERTISER_INFO_URL,
                params={"advertiser_ids": json.dumps(ids[index:index + 50])},
            )
            for item in public_info.get("data") or []:
                account_id = str(item.get("id") or "")
                if account_id:
                    advertisers[account_id] = {
                        "id": account_id,
                        "name": str(item.get("name") or "未命名账户"),
                        "role": "QIANCHUAN_ADVERTISER",
                        "company": str(item.get("company") or ""),
                    }
        if advertisers:
            return sorted(advertisers.values(), key=lambda item: (item["name"], item["id"]))
        return sorted(management, key=lambda item: (item["name"], item["id"]))

    @staticmethod
    def _copy_accounts(items: list[dict]) -> list[dict]:
        return [item.copy() for item in items]

    def _persistent_account_cache(self, db: Session) -> dict[str, Any] | None:
        raw = _meta(db, "qianchuan.accounts_cache")
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            saved_at = _parse_time(str(payload.get("saved_at") or ""))
            items = payload.get("items")
            if not saved_at or not isinstance(items, list):
                return None
            age = max(0.0, (datetime.now(timezone.utc) - saved_at).total_seconds())
            return {
                "stored_at": time.monotonic() - age,
                "saved_at": saved_at.isoformat(),
                "items": [item.copy() for item in items if isinstance(item, dict)],
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def accounts(self, db: Session) -> list[dict]:
        """Return the account directory with single-flight and last-success caching."""
        with self._account_cache_lock:
            now = time.monotonic()
            cached = self._account_cache or self._persistent_account_cache(db)
            if cached and now - cached["stored_at"] <= self.account_cache_seconds:
                self._account_cache = cached
                return self._copy_accounts(cached["items"])
            try:
                items = self._fetch_accounts(db)
            except QianchuanError:
                if cached and now - cached["stored_at"] <= self.account_stale_seconds:
                    logger.warning("Qianchuan account directory unavailable; serving last-success cache")
                    self._account_cache = cached
                    return self._copy_accounts(cached["items"])
                raise
            saved_at = datetime.now(timezone.utc).isoformat()
            _set_meta(
                db,
                "qianchuan.accounts_cache",
                json.dumps({"saved_at": saved_at, "items": items}, ensure_ascii=False),
            )
            db.commit()
            self._account_cache = {
                "stored_at": now,
                "saved_at": saved_at,
                "items": self._copy_accounts(items),
            }
            return self._copy_accounts(items)

    @staticmethod
    def _plan_status_label(value: str) -> str:
        return {
            "ENABLE": "投放中",
            "DISABLE": "已暂停",
            "DELETE": "已删除",
            "AD_STATUS_DELIVERY_OK": "投放中",
            "AD_STATUS_DISABLE": "已暂停",
            "AD_STATUS_DONE": "已结束",
            "AD_STATUS_AUDIT": "审核中",
            "AD_STATUS_REJECT": "审核拒绝",
            "AD_STATUS_BALANCE_EXCEED": "余额不足",
        }.get(value, value or "状态未知")

    def _standard_plans(self, db: Session, advertiser_id: str, marketing_goal: str) -> list[dict]:
        plans: list[dict] = []
        page = 1
        while True:
            payload = self._plan_api_request(
                db,
                "GET",
                PLAN_LIST_URL,
                params={
                    "advertiser_id": advertiser_id,
                    "filtering": json.dumps({"marketing_goal": marketing_goal}),
                    "request_aweme_info": "false",
                    "page": page,
                    "page_size": 100,
                },
            )
            data = payload.get("data") or {}
            for item in data.get("list") or []:
                status = str(item.get("status") or item.get("opt_status") or "")
                plans.append(
                    {
                        "id": str(item.get("ad_id") or item.get("id") or ""),
                        "name": str(item.get("name") or "未命名普通计划"),
                        "status": status,
                        "status_label": self._plan_status_label(status),
                        "marketing_goal": str(item.get("marketing_goal") or marketing_goal),
                        "marketing_scene": str(item.get("marketing_scene") or ""),
                        "campaign_scene": str(item.get("campaign_scene") or ""),
                        "campaign_id": str(item.get("campaign_id") or ""),
                        "plan_type": "standard",
                        "plan_type_label": "普通计划",
                        "can_attach_video": False,
                        "direct_add_api": "",
                    }
                )
            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        return [item for item in plans if item["id"]]

    def _plan_api_request(self, db: Session, method: str, url: str, **kwargs) -> dict:
        """Rate-limit request starts without blocking every colleague behind one slow call."""
        kwargs.setdefault("request_timeout", (6, 15))
        retry_delays = (0.0, 1.5)
        for attempt, retry_delay in enumerate(retry_delays):
            if retry_delay:
                time.sleep(retry_delay)
            with self._plan_request_lock:
                remaining = self.plan_request_interval - (time.monotonic() - self._last_plan_request_at)
                if remaining > 0:
                    time.sleep(remaining)
                self._last_plan_request_at = time.monotonic()
            try:
                return self._authorized_request(db, method, url, **kwargs)
            except QianchuanError as error:
                should_retry = str(error.code) in {"40100", "429"} or error.retryable
                if not should_retry or attempt == len(retry_delays) - 1:
                    raise
                logger.warning(
                    "Qianchuan plan request retry attempt=%s code=%s category=%s",
                    attempt + 1,
                    error.code,
                    error.category,
                )
        raise QianchuanError("千川计划读取失败")

    def _unified_plans(
        self,
        db: Session,
        advertiser_id: str,
        marketing_goal: str,
        requested_scene: str,
    ) -> list[dict]:
        plans: list[dict] = []
        page = 1
        end = datetime.now().strftime("%Y-%m-%d 23:59:59")
        start = (datetime.now() - timedelta(days=179)).strftime("%Y-%m-%d 00:00:00")
        while True:
            payload = self._plan_api_request(
                db,
                "GET",
                FULL_DOMAIN_PLAN_LIST_URL,
                params={
                    "advertiser_id": advertiser_id,
                    "start_time": start,
                    "end_time": end,
                    "marketing_goal": marketing_goal,
                    "adlab_scene": requested_scene,
                    "fields": json.dumps(["stat_cost"]),
                    "page": page,
                    "page_size": 100,
                },
            )
            data = payload.get("data") or {}
            for item in data.get("ad_list") or []:
                ad = item.get("ad_info") if isinstance(item.get("ad_info"), dict) else item
                status = str(ad.get("status") or ad.get("opt_status") or "")
                scene = str(ad.get("adlab_scene") or requested_scene)
                is_multiplication = scene == "OVERALL_PROJECT"
                plans.append(
                    {
                        "id": str(ad.get("id") or ad.get("ad_id") or ""),
                        "name": str(ad.get("name") or "未命名全域计划"),
                        "status": status,
                        "status_label": self._plan_status_label(status),
                        "marketing_goal": str(ad.get("marketing_goal") or marketing_goal),
                        "marketing_scene": str(ad.get("marketing_services_mode") or ""),
                        "campaign_scene": scene,
                        "campaign_id": "",
                        "plan_type": "multiplication" if is_multiplication else "full_domain",
                        "plan_type_label": "乘方计划" if is_multiplication else "全域推广",
                        "can_attach_video": True,
                        "direct_add_api": "qianchuan/uni_promotion/ad/material/add",
                    }
                )
            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        return [item for item in plans if item["id"]]

    def _standard_plan_source(self, db: Session, advertiser_id: str, marketing_goal: str) -> list[dict]:
        if time.monotonic() < self._standard_permission_denied_until:
            raise QianchuanError(
                "普通计划读取权限尚未开通（/qianchuan/ad/get/）",
                code=40002,
            )
        try:
            return self._standard_plans(db, advertiser_id, marketing_goal)
        except QianchuanError as error:
            if error.code == 40002 or "/qianchuan/ad/get/" in str(error):
                self._standard_permission_denied_until = (
                    time.monotonic() + self.standard_permission_cache_seconds
                )
            raise

    @staticmethod
    def _plan_cache_meta_key(cache_key: tuple[str, str, str]) -> str:
        digest = hashlib.sha256("|".join(cache_key).encode("utf-8")).hexdigest()
        return f"qianchuan.plan_cache.{digest}"

    def _persistent_plan_cache(
        self,
        db: Session,
        cache_key: tuple[str, str, str],
        now_monotonic: float,
    ) -> dict[str, Any] | None:
        raw = _meta(db, self._plan_cache_meta_key(cache_key))
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            items = payload.get("items")
            source_read_at = str(payload.get("source_read_at") or "")
            parsed_read_at = _parse_time(source_read_at)
            if not isinstance(items, list) or not parsed_read_at:
                return None
            age_seconds = max(
                0.0,
                (datetime.now(timezone.utc) - parsed_read_at.astimezone(timezone.utc)).total_seconds(),
            )
            return {
                "stored_at": now_monotonic - age_seconds,
                "source_read_at": source_read_at,
                "items": [item.copy() for item in items if isinstance(item, dict)],
            }
        except (TypeError, ValueError, json.JSONDecodeError):
            return None

    def _save_persistent_plan_cache(
        self,
        db: Session,
        cache_key: tuple[str, str, str],
        *,
        source_read_at: str,
        items: list[dict],
    ) -> None:
        _set_meta(
            db,
            self._plan_cache_meta_key(cache_key),
            json.dumps(
                {"source_read_at": source_read_at, "items": items},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        )
        db.commit()

    def _load_plan_source(
        self,
        db: Session,
        cache_key: tuple[str, str, str],
        loader,
        *,
        force_refresh: bool = False,
        cache_only: bool = False,
    ) -> tuple[list[dict], bool, str, bool, str]:
        """Load one source with single-flight and persistent last-success fallback."""
        with self._plan_cache_lock:
            source_lock = self._plan_source_locks.setdefault(cache_key, threading.Lock())
        with source_lock:
            now_monotonic = time.monotonic()
            with self._plan_cache_lock:
                cached_entry = self._plan_source_cache.get(cache_key)
                if not cached_entry:
                    cached_entry = self._persistent_plan_cache(db, cache_key, now_monotonic)
                    if cached_entry:
                        self._plan_source_cache[cache_key] = cached_entry
            cache_age = (
                now_monotonic - cached_entry["stored_at"]
                if cached_entry
                else self.plan_stale_seconds + 1
            )
            if (
                not force_refresh
                and cached_entry
                and cache_age <= self.plan_cache_seconds
            ):
                return (
                    [item.copy() for item in cached_entry["items"]],
                    True,
                    cached_entry["source_read_at"],
                    False,
                    "",
                )
            if cache_only:
                if cached_entry:
                    return (
                        [item.copy() for item in cached_entry["items"]],
                        True,
                        cached_entry["source_read_at"],
                        True,
                        "实时读取暂时不可用，已显示最近一次成功读取的计划",
                    )
                raise QianchuanError("该账户暂无可用的计划缓存，请点击重新获取")
            try:
                items = loader()
            except QianchuanError as error:
                if cached_entry and cache_age <= self.plan_stale_seconds:
                    logger.warning(
                        "Qianchuan plan source fallback advertiser_id=%s goal=%s scene=%s code=%s category=%s",
                        cache_key[0],
                        cache_key[1],
                        cache_key[2],
                        error.code,
                        error.category,
                    )
                    return (
                        [item.copy() for item in cached_entry["items"]],
                        True,
                        cached_entry["source_read_at"],
                        True,
                        str(error),
                    )
                raise
            if not items and cached_entry and cached_entry["items"] and cache_age <= self.plan_stale_seconds:
                logger.warning(
                    "Qianchuan plan source returned empty; preserving last success advertiser_id=%s goal=%s scene=%s",
                    cache_key[0],
                    cache_key[1],
                    cache_key[2],
                )
                return (
                    [item.copy() for item in cached_entry["items"]],
                    True,
                    cached_entry["source_read_at"],
                    True,
                    "千川本次返回空计划列表，已保留上一次成功结果",
                )
            source_read_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self._save_persistent_plan_cache(
                db,
                cache_key,
                source_read_at=source_read_at,
                items=items,
            )
            with self._plan_cache_lock:
                self._plan_source_cache[cache_key] = {
                    "stored_at": now_monotonic,
                    "source_read_at": source_read_at,
                    "items": [item.copy() for item in items],
                }
            return [item.copy() for item in items], False, source_read_at, False, ""

    @staticmethod
    def _plan_result(
        items: list[dict],
        warnings: list[str],
        keyword: str,
        *,
        complete: bool,
        cached: bool,
        source_read_at: str,
        scope: str,
    ) -> dict:
        filtered = [
            item.copy()
            for item in items
            if not keyword or keyword.lower() in item["name"].lower() or keyword in item["id"]
        ]
        type_order = {"multiplication": 0, "full_domain": 1, "standard": 2}
        filtered.sort(
            key=lambda item: (
                type_order.get(item["plan_type"], 9),
                not item["can_attach_video"],
                item["name"],
                item["id"],
            )
        )
        return {
            "items": filtered,
            "warnings": list(dict.fromkeys(warnings)),
            "complete": complete,
            "cached": cached,
            "source_read_at": source_read_at,
            "scope": scope,
            "counts": {
                "total": len(filtered),
                "multiplication": sum(1 for item in filtered if item["plan_type"] == "multiplication"),
                "full_domain": sum(1 for item in filtered if item["plan_type"] == "full_domain"),
                "standard": sum(1 for item in filtered if item["plan_type"] == "standard"),
            },
        }

    def plans(
        self,
        db: Session,
        advertiser_id: str,
        keyword: str = "",
        scope: str = "all",
        *,
        force_refresh: bool = False,
        cache_only: bool = False,
    ) -> dict:
        if scope not in {"all", "multiplication", "full_domain", "standard"}:
            raise QianchuanError("计划读取范围不正确")
        plans: list[dict] = []
        warnings: list[str] = []
        successful_sources = 0
        cached_any = False
        source_read_times: list[str] = []
        load_deadline = time.monotonic() + self.plan_load_budget_seconds

        requested_scenes = {
            scene: (plan_type, scene_label)
            for scene, plan_type, scene_label in UNIFIED_PLAN_SCENES
            if scope == "all" or scope == plan_type
        }
        for marketing_goal in ("VIDEO_PROM_GOODS", "LIVE_PROM_GOODS"):
            goal_label = "商品" if marketing_goal == "VIDEO_PROM_GOODS" else "直播"
            for scene, (_plan_type, scene_label) in requested_scenes.items():
                try:
                    source_cache_only = cache_only or time.monotonic() >= load_deadline - 20
                    items, was_cached, read_at, stale, stale_error = self._load_plan_source(
                        db,
                        (advertiser_id, marketing_goal, scene),
                        lambda goal=marketing_goal, selected_scene=scene: self._unified_plans(
                            db, advertiser_id, goal, selected_scene
                        ),
                        force_refresh=force_refresh,
                        cache_only=source_cache_only,
                    )
                    plans.extend(items)
                    successful_sources += 1
                    cached_any = cached_any or was_cached
                    source_read_times.append(read_at)
                    if stale:
                        warnings.append(
                            f"{goal_label}{scene_label}读取波动，已保留 {read_at} 成功读取的结果：{stale_error}"
                        )
                except QianchuanError as error:
                    warnings.append(f"{goal_label}{scene_label}读取未完成：{str(error)}")
            if scope in {"all", "standard"}:
                try:
                    source_cache_only = cache_only or time.monotonic() >= load_deadline - 20
                    items, was_cached, read_at, stale, stale_error = self._load_plan_source(
                        db,
                        (advertiser_id, marketing_goal, "STANDARD"),
                        lambda goal=marketing_goal: self._standard_plan_source(
                            db, advertiser_id, goal
                        ),
                        force_refresh=force_refresh,
                        cache_only=source_cache_only,
                    )
                    plans.extend(items)
                    successful_sources += 1
                    cached_any = cached_any or was_cached
                    source_read_times.append(read_at)
                    if stale:
                        warnings.append(
                            f"{goal_label}普通计划读取异常，已保留 {read_at} 成功读取的结果：{stale_error}"
                        )
                except QianchuanError as error:
                    message = str(error)
                    warning = (
                        "普通计划读取权限尚未开通（/qianchuan/ad/get/）；"
                        "全域与乘方计划不受此权限影响"
                        if error.code == 40002 or "/qianchuan/ad/get/" in message
                        else f"{goal_label}普通计划读取未完成：{message}"
                    )
                    if warning not in warnings:
                        warnings.append(warning)

        unique_items = {
            (item["plan_type"], item["id"]): item
            for item in plans
        }
        source_read_at = min(source_read_times) if source_read_times else ""
        complete = not warnings
        if not successful_sources:
            raise QianchuanError("；".join(warnings) or "千川计划读取失败")
        items = list(unique_items.values())
        return self._plan_result(
            items,
            warnings,
            keyword,
            complete=complete,
            cached=cached_any,
            source_read_at=source_read_at,
            scope=scope,
        )

    def resolve_plan_target(
        self,
        db: Session,
        *,
        advertiser_id: str,
        plan_id: str,
        plan_type: str,
    ) -> dict:
        if plan_type not in {"multiplication", "full_domain", "standard"}:
            raise QianchuanError("请选择明确的千川计划类型")
        try:
            cached_result = self.plans(db, advertiser_id, scope=plan_type, cache_only=True)
        except QianchuanError:
            cached_result = {"items": []}
        for item in cached_result["items"]:
            if item["id"] == plan_id and item["plan_type"] == plan_type:
                return item.copy()
        result = self.plans(db, advertiser_id, scope=plan_type, force_refresh=True)
        for item in result["items"]:
            if item["id"] == plan_id and item["plan_type"] == plan_type:
                return item.copy()
        raise QianchuanError("所选计划不属于当前千川账户，或已失效，请重新选择")

    @staticmethod
    def _object_md5_cache_key(object_key: str) -> str:
        identity = hashlib.sha256(object_key.encode("utf-8")).hexdigest()
        return f"qianchuan.object_md5.{identity}"

    def _cached_object_md5(self, db: Session, object_key: str) -> str:
        raw = _meta(db, self._object_md5_cache_key(object_key))
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            return ""
        signature = str(payload.get("signature") or "").lower()
        if payload.get("object_key") != object_key or not re.fullmatch(r"[0-9a-f]{32}", signature):
            return ""
        return signature

    def _save_object_md5(self, db: Session, object_key: str, signature: str) -> None:
        _set_meta(
            db,
            self._object_md5_cache_key(object_key),
            json.dumps(
                {
                    "object_key": object_key,
                    "signature": signature.lower(),
                    "saved_at": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
            ),
        )
        db.commit()

    def _object_md5(self, object_key: str) -> str:
        """Return the original OSS object's MD5 without changing or transcoding it."""
        if not oss_service.configured:
            raise QianchuanError("OSS 尚未配置，无法读取素材文件")
        try:
            head = oss_service.client.head_object(Bucket=settings.bucket, Key=object_key)
            etag = str(head.get("ETag") or "").strip('"').lower()
            if re.fullmatch(r"[0-9a-f]{32}", etag):
                return etag
            remote = oss_service.client.get_object(Bucket=settings.bucket, Key=object_key)
            body = remote["Body"]
            digest = hashlib.md5()
            try:
                while True:
                    chunk = body.read(4 * 1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
            finally:
                body.close()
            return digest.hexdigest()
        except QianchuanError:
            raise
        except Exception as error:
            raise QianchuanError("读取 OSS 原视频校验值失败", category="oss_read") from error

    @staticmethod
    def _delivery_filename(filename: str) -> str:
        """Keep internal OSS upload prefixes out of the Qianchuan material name."""
        safe_name = display_filename_from_object_key(filename)
        return safe_name[:255] or "original-video.mp4"

    def _video_by_signature(
        self,
        db: Session,
        *,
        advertiser_id: str,
        signature: str,
        filename: str = "",
    ) -> dict | None:
        """Reuse identical bytes only when the existing material also has the requested name."""
        desired_name = self._delivery_filename(filename) if filename else ""
        try:
            payload = self._authorized_request(
                db,
                "GET",
                QIANCHUAN_VIDEO_LIST_URL,
                params={
                    "advertiser_id": advertiser_id,
                    "filtering": json.dumps({"signatures": [signature]}, ensure_ascii=False),
                    "page": 1,
                    "page_size": 20,
                },
            )
        except QianchuanError as error:
            logger.warning(
                "Qianchuan video signature lookup skipped code=%s category=%s",
                error.code,
                error.category,
            )
            return None
        data = payload.get("data") or {}
        for item in _normalized_list(data):
            item_signature = str(item.get("signature") or item.get("video_signature") or "").lower()
            if item_signature != signature.lower():
                continue
            remote_name = str(
                item.get("filename")
                or item.get("file_name")
                or item.get("video_name")
                or item.get("name")
                or ""
            ).strip()
            if desired_name and Path(remote_name).name != desired_name:
                logger.info(
                    "Qianchuan signature match not reused because filename differs advertiser_id=%s remote=%s desired=%s",
                    advertiser_id,
                    remote_name,
                    desired_name,
                )
                continue
            video_id = item.get("id") or item.get("video_id")
            if video_id:
                return {
                    "platform_asset_id": str(video_id),
                    "request_id": str(payload.get("request_id") or ""),
                }
        return None

    def _upload_original_file_direct(
        self,
        db: Session,
        *,
        advertiser_id: str,
        object_key: str,
        filename: str,
        signature: str = "",
    ) -> dict:
        """Stream the unchanged OSS object to Qianchuan with safe whole-file retries."""
        if not oss_service.configured:
            raise QianchuanError("OSS 尚未配置，无法读取素材文件")
        safe_filename = self._delivery_filename(filename)
        content_type = mimetypes.guess_type(safe_filename)[0] or "video/mp4"
        try:
            remote = oss_service.client.get_object(Bucket=settings.bucket, Key=object_key)
            body = remote["Body"]
            with tempfile.TemporaryFile(mode="w+b") as staged:
                digest = hashlib.md5()
                try:
                    while True:
                        chunk = body.read(4 * 1024 * 1024)
                        if not chunk:
                            break
                        digest.update(chunk)
                        staged.write(chunk)
                finally:
                    body.close()
                computed_signature = digest.hexdigest().lower()
                if signature and computed_signature != signature.lower():
                    raise QianchuanError(
                        "OSS 原视频读取校验失败，已停止推送以避免文件损坏",
                        category="source_checksum",
                    )
                if not signature:
                    signature = computed_signature
                    self._save_object_md5(db, object_key, signature)

                last_error: QianchuanError | None = None
                for attempt, retry_delay in enumerate((0.0, 3.0, 8.0), start=1):
                    if retry_delay:
                        time.sleep(retry_delay)
                    if attempt > 1:
                        existing = self._video_by_signature(
                            db,
                            advertiser_id=advertiser_id,
                            signature=signature,
                            filename=safe_filename,
                        )
                        if existing:
                            return {
                                **existing,
                                "upload_task_id": "",
                                "signature": signature,
                                "reused": True,
                            }
                    staged.seek(0)
                    encoder = MultipartEncoder(
                        fields={
                            "advertiser_id": advertiser_id,
                            "filename": safe_filename,
                            "is_aigc": "false",
                            "upload_type": "UPLOAD_BY_FILE",
                            "video_signature": signature,
                            "video_file": (safe_filename, staged, content_type),
                        }
                    )
                    try:
                        with self._video_upload_lock:
                            remaining = self.video_upload_request_interval - (
                                time.monotonic() - self._last_video_upload_at
                            )
                            if remaining > 0:
                                time.sleep(remaining)
                            try:
                                payload = self._authorized_request(
                                    db,
                                    "POST",
                                    VIDEO_UPLOAD_URL,
                                    data=encoder,
                                    extra_headers={
                                        "Content-Type": encoder.content_type,
                                        "Content-Length": str(encoder.len),
                                    },
                                    request_timeout=(30, 900),
                                )
                            finally:
                                self._last_video_upload_at = time.monotonic()
                    except QianchuanError as error:
                        last_error = error
                        if not error.retryable or attempt == 3:
                            raise
                        continue
                    data = payload.get("data") or {}
                    video_id = data.get("video_id") or data.get("id")
                    if not video_id:
                        raise QianchuanError(
                            "千川已接收原视频，但没有返回视频 ID",
                            request_id=str(payload.get("request_id") or ""),
                            category="invalid_response",
                        )
                    return {
                        "platform_asset_id": str(video_id),
                        "upload_task_id": "",
                        "signature": str(data.get("video_signature") or signature),
                        "request_id": str(payload.get("request_id") or ""),
                        "reused": False,
                    }
                if last_error:
                    raise last_error
        except QianchuanError:
            raise
        except Exception as error:
            raise QianchuanError(
                "读取 OSS 原视频用于零压缩直传时失败",
                category="oss_read",
                retryable=True,
            ) from error
        raise QianchuanError("原视频直传未完成", category="video_upload", retryable=True)

    def begin_video_upload(
        self,
        db: Session,
        *,
        advertiser_id: str,
        object_key: str,
        filename: str,
    ) -> dict:
        """Ask Qianchuan to fetch the original OSS object asynchronously."""
        # The normal URL-upload path does not require a local checksum.  Avoid
        # downloading every multipart OSS object back through the app server
        # before Qianchuan can start fetching it.  A previously computed checksum
        # is still used for safe account-library reuse; a new checksum is only
        # calculated when the platform rejects URL upload and direct transfer is
        # genuinely required.
        signature = self._cached_object_md5(db, object_key)
        safe_filename = self._delivery_filename(filename)
        if signature:
            existing = self._video_by_signature(
                db,
                advertiser_id=advertiser_id,
                signature=signature,
                filename=safe_filename,
            )
            if existing:
                return {**existing, "upload_task_id": "", "signature": signature, "reused": True}

        try:
            account_id = int(advertiser_id)
        except (TypeError, ValueError) as error:
            raise QianchuanError("千川账户 ID 无效", category="invalid_account") from error
        video_url = oss_service.url_for(
            object_key,
            expires_in=self.async_upload_url_expiry_seconds,
        )
        if not video_url:
            raise QianchuanError("无法生成原视频读取地址", category="oss_url")
        try:
            payload = self._authorized_request(
                db,
                "POST",
                ASYNC_VIDEO_UPLOAD_URL,
                json_body={
                    "account_id": account_id,
                    "account_type": "ADVERTISER",
                    "filename": safe_filename,
                    "video_url": video_url,
                },
            )
        except QianchuanError as error:
            if "仅支持连山云" not in str(error) and "TOS" not in str(error).upper():
                raise
            return self._upload_original_file_direct(
                db,
                advertiser_id=advertiser_id,
                object_key=object_key,
                filename=safe_filename,
                signature=signature,
            )
        data = payload.get("data") or {}
        upload_task_id = data.get("task_id") or data.get("upload_task_id") or data.get("id")
        if not upload_task_id:
            raise QianchuanError(
                "千川已接收异步上传请求，但没有返回任务 ID",
                request_id=str(payload.get("request_id") or ""),
                category="invalid_response",
            )
        return {
            "platform_asset_id": "",
            "upload_task_id": str(upload_task_id),
            "signature": signature,
            "request_id": str(payload.get("request_id") or ""),
            "reused": False,
        }

    def wait_video_upload(
        self,
        db: Session,
        *,
        advertiser_id: str,
        upload_task_id: str,
    ) -> dict:
        """Poll the official async-upload result without retransmitting the video."""
        try:
            account_id = int(advertiser_id)
            numeric_task_id = int(upload_task_id)
        except (TypeError, ValueError) as error:
            raise QianchuanError("千川异步上传任务 ID 无效", category="invalid_upload_task") from error
        deadline = time.monotonic() + self.async_upload_timeout_seconds
        last_request_id = ""
        while time.monotonic() < deadline:
            payload = self._authorized_request(
                db,
                "GET",
                ASYNC_VIDEO_UPLOAD_RESULT_URL,
                params={
                    "account_id": account_id,
                    "account_type": "ADVERTISER",
                    "task_ids": json.dumps([numeric_task_id]),
                },
            )
            last_request_id = str(payload.get("request_id") or last_request_id)
            data = payload.get("data") or {}
            items = _normalized_list(data)
            current = next(
                (item for item in items if str(item.get("task_id") or "") == str(numeric_task_id)),
                items[0] if len(items) == 1 else None,
            )
            if current:
                status = str(current.get("status") or "").upper()
                video_info = current.get("video_info") or {}
                video_id = video_info.get("video_id") or current.get("video_id")
                if status == "SUCCESS" and video_id:
                    return {
                        "platform_asset_id": str(video_id),
                        "request_id": last_request_id,
                        "upload_task_id": str(numeric_task_id),
                        "reused": False,
                    }
                if status == "FAILED":
                    detail = str(current.get("error_msg") or "千川未返回失败原因")
                    raise QianchuanError(
                        f"千川异步读取原视频失败：{detail}",
                        request_id=last_request_id,
                        category="async_upload_failed",
                    )
            time.sleep(self.async_upload_poll_seconds)
        raise QianchuanError(
            "千川仍在异步处理原视频；任务已保留，稍后重试会继续查询，不会压缩或重复上传",
            request_id=last_request_id,
            category="async_upload_processing",
            retryable=True,
        )

    def plan_detail(self, db: Session, *, advertiser_id: str, plan_id: str) -> dict:
        payload = self._plan_api_request(
            db,
            "GET",
            FULL_DOMAIN_PLAN_DETAIL_URL,
            params={"advertiser_id": advertiser_id, "ad_id": plan_id},
        )
        data = payload.get("data") or {}
        if str(data.get("ad_id") or "") != str(plan_id):
            raise QianchuanError(
                "千川计划详情返回异常，请重新选择计划",
                request_id=str(payload.get("request_id") or ""),
            )
        return data

    @staticmethod
    def _detail_video_matches(detail: dict, video_id: str) -> list[dict]:
        """Extract exact plan/video evidence from the full-domain plan detail response."""
        matches: list[dict] = []
        programmatic = (detail.get("programmatic_creative_media_list") or {}).get(
            "video_material"
        ) or []
        for item in programmatic:
            if str(item.get("video_id") or "") == str(video_id):
                matches.append(
                    {
                        "video_id": str(video_id),
                        "material_id": str(item.get("material_id") or ""),
                        "placement": "programmatic_creative_media_list",
                    }
                )
        for creative in detail.get("multi_product_creative_list") or []:
            for item in creative.get("video_material") or []:
                if str(item.get("video_id") or "") != str(video_id):
                    continue
                matches.append(
                    {
                        "video_id": str(video_id),
                        "material_id": str(item.get("material_id") or ""),
                        "placement": "multi_product_creative_list",
                        "product_id": str(creative.get("product_id") or ""),
                        "aweme_uid": str(creative.get("aweme_uid") or ""),
                    }
                )
        return matches

    def plan_material_videos(
        self,
        db: Session,
        *,
        advertiser_id: str,
        plan_id: str,
        start_date: str = "",
        end_date: str = "",
        fields: list[str] | None = None,
    ) -> dict:
        """Read the official creative list under one full-domain/multiplication plan."""
        videos: list[dict] = []
        request_id = ""
        page = 1
        while True:
            filtering = {"material_type": "VIDEO", "material_status": "ALL"}
            if start_date:
                filtering["start_date"] = start_date
            if end_date:
                filtering["end_date"] = end_date
            params: dict[str, Any] = {
                "advertiser_id": advertiser_id,
                "ad_id": plan_id,
                "filtering": json.dumps(filtering, ensure_ascii=False),
                "page": page,
                "page_size": 100,
            }
            if fields:
                params["fields"] = json.dumps(fields, ensure_ascii=False)
            payload = self._plan_api_request(
                db,
                "GET",
                FULL_DOMAIN_MATERIAL_LIST_URL,
                params=params,
            )
            request_id = str(payload.get("request_id") or request_id)
            data = payload.get("data") or {}
            for row in data.get("ad_material_infos") or []:
                if not isinstance(row, dict):
                    continue
                material_info = row.get("material_info") or {}
                video = material_info.get("video_material") or {}
                video_id = str(video.get("video_id") or "")
                if not video_id:
                    continue
                material_status = str(row.get("material_status") or "")
                videos.append(
                    {
                        "video_id": video_id,
                        "material_id": str(video.get("material_id") or ""),
                        "title": str(video.get("title") or ""),
                        "audit_status": str(row.get("audit_status") or ""),
                        "material_status": material_status,
                        "is_delete": bool(row.get("is_delete", False)),
                        "delivery_ready": material_status == "DELIVERY_OK",
                        "needs_reactivation": bool(row.get("is_delete", False)) or material_status in {
                            "DELETED", "DELIVERY_NOT", "EXCLUDE",
                        },
                        "delivery_not_reason": row.get("delivery_not_reason") or [],
                        "product_id_list": [str(item) for item in row.get("product_id_list") or []],
                        "aweme_id_list": [str(item) for item in row.get("aweme_id_list") or []],
                        "stats_info": row.get("stats_info") or {},
                        "placement": "plan_material_list",
                    }
                )
            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        return {"videos": videos, "request_id": request_id, "pages": page}

    def plan_material_metrics(
        self,
        db: Session,
        *,
        advertiser_id: str,
        plan_id: str,
        video_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        """Read ROI2 metrics already scoped to an exact plan and exact video."""
        end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=6)
        if start > end:
            raise QianchuanError("数据开始日期不能晚于结束日期")
        result = self.plan_material_videos(
            db,
            advertiser_id=advertiser_id,
            plan_id=plan_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            fields=PLAN_MATERIAL_REPORT_FIELDS,
        )
        return self.plan_material_metrics_from_result(
            result,
            plan_id=plan_id,
            video_id=video_id,
            start_date=start.isoformat(),
            end_date=end.isoformat(),
        )

    @staticmethod
    def plan_material_metrics_from_result(
        result: dict,
        *,
        plan_id: str,
        video_id: str,
        start_date: str,
        end_date: str,
    ) -> dict:
        """Aggregate one video's daily ROI2 fields from a plan-level material read."""
        all_matches = [
            item for item in result.get("videos") or []
            if str(item.get("video_id") or "") == str(video_id)
        ]
        matches = [item for item in all_matches if not item.get("needs_reactivation")]
        additive_fields = {
            "stat_cost_for_roi2",
            "total_pay_order_gmv_include_coupon_for_roi2",
            "total_pay_order_gmv_for_roi2",
            "total_pay_order_count_for_roi2",
            "product_show_count_for_roi2",
            "product_click_count_for_roi2",
        }
        native: dict[str, Any] = {}
        has_data = False
        for item in matches:
            values = item.get("stats_info") or {}
            has_data = has_data or bool(values)
            for field in PLAN_MATERIAL_REPORT_FIELDS:
                incoming = values.get(field)
                if incoming is None:
                    continue
                if field in additive_fields:
                    try:
                        native[field] = float(native.get(field, 0) or 0) + float(incoming or 0)
                    except (TypeError, ValueError):
                        if field not in native:
                            native[field] = incoming
                elif field not in native:
                    native[field] = incoming

        for field in additive_fields:
            value = native.get(field)
            if isinstance(value, float) and value.is_integer():
                native[field] = int(value)

        cost = native.get("stat_cost_for_roi2")
        gmv_for_roi = native.get("total_pay_order_gmv_include_coupon_for_roi2")
        if gmv_for_roi is None:
            gmv_for_roi = native.get("total_pay_order_gmv_for_roi2")
        if has_data and isinstance(cost, (int, float)) and cost > 0 and isinstance(gmv_for_roi, (int, float)):
            native["total_prepay_and_pay_order_roi2"] = round(float(gmv_for_roi) / float(cost), 4)

        metrics = dict(native)
        aliases = {
            "stat_cost": "stat_cost_for_roi2",
            "pay_order_count": "total_pay_order_count_for_roi2",
            "prepay_and_pay_order_roi": "total_prepay_and_pay_order_roi2",
            "show_cnt": "product_show_count_for_roi2",
            "click_cnt": "product_click_count_for_roi2",
        }
        for alias, source in aliases.items():
            if native.get(source) is not None:
                metrics[alias] = native[source]
        gmv = native.get("total_pay_order_gmv_include_coupon_for_roi2")
        if gmv is None:
            gmv = native.get("total_pay_order_gmv_for_roi2")
        if gmv is not None:
            metrics["pay_order_amount"] = gmv
        metrics["metric_scope"] = "uni_promotion_plan_material_roi2"
        from .platform_audit import material_audit_receipt
        return {
            "platform_audit": material_audit_receipt(all_matches, video_id, str(result.get('request_id') or '')),
            "metrics": metrics,
            "related_ad_ids": [str(plan_id)] if matches else [],
            "related_creative_ids": [],
            "start_date": start_date,
            "end_date": end_date,
            "has_data": has_data,
            "row_count": len(matches),
            "found_count": len(all_matches),
            "matches": matches,
            "request_id": result.get("request_id", ""),
            "link_verified": bool(matches),
            "source": "qianchuan/uni_promotion/ad/material/get",
        }

    def plan_video_evidence(
        self,
        db: Session,
        *,
        advertiser_id: str,
        plan_id: str,
        video_id: str,
    ) -> dict:
        """Return auditable evidence that one exact video exists in one exact plan."""
        readback_errors: list[str] = []
        try:
            result = self.plan_material_videos(
                db,
                advertiser_id=advertiser_id,
                plan_id=plan_id,
            )
            matches = [
                item for item in result["videos"]
                if str(item.get("video_id") or "") == str(video_id)
            ]
            if matches:
                eligible_matches = [item for item in matches if not item["needs_reactivation"]]
                return {
                    "advertiser_id": str(advertiser_id),
                    "plan_id": str(plan_id),
                    "video_id": str(video_id),
                    "found_count": len(matches),
                    "matched_count": len(eligible_matches),
                    "matches": matches,
                    "delivery_ready": any(item["delivery_ready"] for item in eligible_matches),
                    "needs_reactivation": not eligible_matches,
                    "readback_source": "qianchuan/uni_promotion/ad/material/get",
                    "request_id": result.get("request_id", ""),
                    "verified_at": datetime.now(timezone.utc).isoformat(),
                }
        except QianchuanError as error:
            readback_errors.append(str(error))

        detail = self.plan_detail(db, advertiser_id=advertiser_id, plan_id=plan_id)
        matches = self._detail_video_matches(detail, video_id)
        return {
            "advertiser_id": str(advertiser_id),
            "plan_id": str(plan_id),
            "video_id": str(video_id),
            "matched_count": len(matches),
            "found_count": len(matches),
            "matches": matches,
            "delivery_ready": None,
            "needs_reactivation": False,
            "readback_source": "qianchuan/uni_promotion/ad/detail",
            "request_id": "",
            "verified_at": datetime.now(timezone.utc).isoformat() if matches else "",
            "readback_errors": readback_errors,
        }

    def _video_creative_material(
        self,
        db: Session,
        *,
        advertiser_id: str,
        video_id: str,
    ) -> dict:
        """Resolve the video's native ratio and register its platform poster as the plan cover."""
        cache_key = f"qianchuan.video_material.{advertiser_id}.{video_id}"
        cached_raw = _meta(db, cache_key)
        if cached_raw:
            try:
                cached = json.loads(cached_raw)
                if cached.get("video_cover_id") and cached.get("image_mode"):
                    return {
                        "video_id": video_id,
                        "image_mode": str(cached["image_mode"]),
                        "video_cover_id": str(cached["video_cover_id"]),
                    }
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        video_payload = self._authorized_request(
            db,
            "GET",
            QIANCHUAN_VIDEO_LIST_URL,
            params={
                "advertiser_id": advertiser_id,
                "filtering": json.dumps({"video_ids": [video_id]}, ensure_ascii=False),
                "page": 1,
                "page_size": 20,
            },
        )
        items = _normalized_list(video_payload.get("data"))
        video = next(
            (item for item in items if str(item.get("id") or item.get("video_id") or "") == video_id),
            None,
        )
        if not video:
            # The account video-list endpoint can temporarily omit an already-used video.
            # Recover the exact cover and orientation from another verified plan under the
            # same advertiser instead of uploading, compressing, or changing the video.
            known_plan_ids = list(
                db.scalars(
                    select(QianchuanDelivery.plan_id)
                    .where(
                        QianchuanDelivery.advertiser_id == advertiser_id,
                        QianchuanDelivery.platform_asset_id == video_id,
                        QianchuanDelivery.plan_id != "",
                    )
                    .order_by(QianchuanDelivery.binding_verified_at.desc())
                    .limit(20)
                ).all()
            )
            for known_plan_id in dict.fromkeys(known_plan_ids):
                try:
                    detail = self.plan_detail(
                        db,
                        advertiser_id=advertiser_id,
                        plan_id=str(known_plan_id),
                    )
                except QianchuanError:
                    continue
                plan_videos = list(
                    ((detail.get("programmatic_creative_media_list") or {}).get("video_material") or [])
                )
                for creative in detail.get("multi_product_creative_list") or []:
                    plan_videos.extend(creative.get("video_material") or [])
                matched = next(
                    (
                        item for item in plan_videos
                        if str(item.get("video_id") or "") == str(video_id)
                        and item.get("video_cover_id")
                    ),
                    None,
                )
                if not matched:
                    continue
                recovered = {
                    "video_id": video_id,
                    "image_mode": str(matched.get("image_mode") or "VIDEO_VERTICAL"),
                    "video_cover_id": str(matched["video_cover_id"]),
                }
                _set_meta(db, cache_key, json.dumps(recovered, ensure_ascii=False))
                return recovered
            raise QianchuanError(
                "原视频已上传，但千川视频列表暂未返回详情，且现有计划中未找到可复用封面；请稍后重试",
                category="video_detail_pending",
                retryable=True,
            )

        width = int(video.get("width") or 0)
        height = int(video.get("height") or 0)
        image_mode = str(video.get("image_mode") or "")
        if image_mode not in {"VIDEO_VERTICAL", "VIDEO_LARGE"}:
            image_mode = "VIDEO_VERTICAL" if height > width else "VIDEO_LARGE"
        poster_url = str(video.get("poster_url") or "")
        if not poster_url:
            raise QianchuanError("千川没有返回原视频封面，暂时无法加入计划")

        # The add-material API requires the cover to be an image-library ID.  Qianchuan's
        # video list returns a URL, so register that existing platform-generated poster by
        # URL.  This does not download, transcode, compress, or otherwise alter the video.
        cover_payload = self._authorized_request(
            db,
            "POST",
            IMAGE_UPLOAD_URL,
            files={
                "advertiser_id": (None, advertiser_id),
                "filename": (None, f"video-cover-{video_id}.jpg"),
                "image_url": (None, poster_url),
                "upload_type": (None, "UPLOAD_BY_URL"),
                "is_aigc": (None, "false"),
            },
        )
        cover_id = str((cover_payload.get("data") or {}).get("id") or "")
        if not cover_id:
            raise QianchuanError(
                "千川已接收视频封面，但没有返回封面 ID",
                request_id=str(cover_payload.get("request_id") or ""),
                category="invalid_response",
            )
        material = {
            "video_id": video_id,
            "image_mode": image_mode,
            "video_cover_id": cover_id,
        }
        _set_meta(db, cache_key, json.dumps(material, ensure_ascii=False))
        return material

    def _wait_for_plan_video(
        self,
        db: Session,
        *,
        advertiser_id: str,
        plan_id: str,
        video_id: str,
    ) -> dict:
        """Confirm the exact video ID is visible in the target plan before reporting success."""
        last_evidence: dict | None = None
        for retry_delay in (0.0, 2.0, 5.0, 10.0):
            if retry_delay:
                time.sleep(retry_delay)
            detail = self.plan_detail(db, advertiser_id=advertiser_id, plan_id=plan_id)
            matches = self._detail_video_matches(detail, video_id)
            last_evidence = {
                "advertiser_id": str(advertiser_id),
                "plan_id": str(plan_id),
                "video_id": str(video_id),
                "matched_count": len(matches),
                "found_count": len(matches),
                "matches": matches,
                "delivery_ready": None,
                "needs_reactivation": False,
                "readback_source": "qianchuan/uni_promotion/ad/detail",
                "request_id": "",
                "verified_at": datetime.now(timezone.utc).isoformat() if matches else "",
            }
            if matches:
                return last_evidence
        # One final material-list read catches the rare case where the binding is
        # visible in the official material list before the plan detail refreshes.
        last_evidence = self.plan_video_evidence(
            db,
            advertiser_id=advertiser_id,
            plan_id=plan_id,
            video_id=video_id,
        )
        if last_evidence["matched_count"]:
            return last_evidence
        raise QianchuanError(
            "千川已受理加入计划，但回读目标计划尚未发现该视频；任务保留为部分完成，请稍后重试",
            category="plan_verification_pending",
            retryable=True,
        )

    def add_to_plan(self, db: Session, *, advertiser_id: str, plan_id: str, video_id: str) -> dict:
        # The material-list read can be ahead of the plan-detail cache. Always
        # reconcile the exact target first, including after an uncertain POST.
        existing = self.plan_video_evidence(
            db, advertiser_id=advertiser_id, plan_id=plan_id, video_id=video_id,
        )
        if existing["matched_count"]:
            return {
                "request_id": existing.get("request_id", ""),
                "marketing_goal": "",
                "verified_matches": existing["matched_count"],
                "already_present": True,
                "delivery_entity_type": "uni_promotion_ad",
                "delivery_entity_id": str(plan_id),
                "binding_evidence": existing,
            }
        if existing.get("readback_errors"):
            raise QianchuanError(
                "目标计划素材回读暂不可用，尚不能确定是否已经加入；已保留原视频，暂不重复添加",
                category="plan_verification_pending", retryable=True,
            )
        detail = self.plan_detail(db, advertiser_id=advertiser_id, plan_id=plan_id)
        marketing_goal = str(detail.get("marketing_goal") or "")
        video_material = self._video_creative_material(
            db,
            advertiser_id=advertiser_id,
            video_id=video_id,
        )
        request_body: dict[str, Any] = {
            "advertiser_id": int(advertiser_id),
            "ad_id": int(plan_id),
        }
        if marketing_goal == "VIDEO_PROM_GOODS":
            primary_aweme_id = str(detail.get("aweme_id") or "")
            creative_candidates: list[tuple[int, int, dict]] = []
            seen_targets: set[tuple[str, str]] = set()
            for creative in detail.get("multi_product_creative_list") or []:
                product_id = str(creative.get("product_id") or "")
                aweme_uid = str(creative.get("aweme_uid") or "")
                identity = (product_id, aweme_uid)
                has_active_video = bool(creative.get("video_material") or [])
                is_primary_identity = bool(primary_aweme_id and aweme_uid == primary_aweme_id)
                # Account-library videos can safely bootstrap the plan's own Douyin
                # identity even when it has no existing video. Collaborator identities
                # only accept homepage videos, so never select them when the detail gives
                # us an explicit primary aweme_id.
                if primary_aweme_id:
                    identity_is_safe = is_primary_identity
                else:
                    identity_is_safe = has_active_video
                if not product_id or not identity_is_safe or identity in seen_targets:
                    continue
                seen_targets.add(identity)
                target: dict[str, Any] = {
                    "product_id": int(product_id),
                    "video_material": [dict(video_material)],
                }
                if aweme_uid:
                    target["aweme_uid"] = int(aweme_uid)
                creative_candidates.append(
                    (
                        0 if is_primary_identity else 1,
                        len(creative.get("video_material") or []),
                        target,
                    )
                )
            if not creative_candidates:
                raise QianchuanError(
                    "该商品计划暂未返回可安全使用的目标账号创意身份；请稍后重试，系统不会误用合作达人身份",
                    category="plan_identity_unavailable",
                    retryable=True,
                )
            # One plan-level material only needs one identity. Prefer the explicit primary
            # account identity, then the smallest safe active library for legacy responses.
            creative_candidates.sort(
                key=lambda item: (
                    item[0],
                    item[1],
                    str(item[2].get("product_id") or ""),
                    str(item[2].get("aweme_uid") or ""),
                )
            )
            request_body["multi_product_creative_list"] = [creative_candidates[0][2]]
        elif marketing_goal == "LIVE_PROM_GOODS":
            request_body["programmatic_creative_media_list"] = {
                "video_material": [dict(video_material)]
            }
        else:
            raise QianchuanError("该计划的营销目标暂不支持直接加入视频素材")
        payload = self._authorized_request(
            db,
            "POST",
            FULL_DOMAIN_ADD_URL,
            json_body=request_body,
        )
        evidence = self._wait_for_plan_video(
            db,
            advertiser_id=advertiser_id,
            plan_id=plan_id,
            video_id=video_id,
        )
        return {
            "request_id": str(evidence.get("request_id") or payload.get("request_id") or ""),
            "marketing_goal": marketing_goal,
            "verified_matches": evidence["matched_count"],
            "already_present": False,
            "delivery_entity_type": "uni_promotion_ad",
            "delivery_entity_id": str(plan_id),
            "binding_evidence": evidence,
        }

    def material_metrics(
        self,
        db: Session,
        *,
        advertiser_id: str,
        material_id: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict:
        end = date.fromisoformat(end_date) if end_date else date.today() - timedelta(days=1)
        start = date.fromisoformat(start_date) if start_date else end - timedelta(days=6)
        if start > end:
            raise QianchuanError("数据开始日期不能晚于结束日期")
        try:
            numeric_material_id = int(material_id)
        except (TypeError, ValueError) as error:
            raise QianchuanError("千川视频素材 ID 格式不正确", category="invalid_material") from error
        rows: list[dict] = []
        request_id = ""
        page = 1
        while True:
            payload = self._authorized_request(
                db,
                "GET",
                MATERIAL_REPORT_URL,
                params={
                    "advertiser_id": advertiser_id,
                    "start_date": start.isoformat(),
                    "end_date": end.isoformat(),
                    "fields": json.dumps(REPORT_FIELDS, ensure_ascii=False),
                    "filtering": json.dumps(
                        {"material_type": "VIDEO", "material_id": [numeric_material_id]},
                        ensure_ascii=False,
                    ),
                    "page": page,
                    "page_size": 100,
                },
            )
            request_id = str(payload.get("request_id") or request_id)
            data = payload.get("data") or {}
            rows.extend(
                row for row in data.get("list") or []
                if isinstance(row, dict) and str(row.get("material_id") or "") == str(material_id)
            )
            page_info = data.get("page_info") or {}
            total_page = int(page_info.get("total_page") or 1)
            if page >= total_page:
                break
            page += 1
        metrics: dict[str, Any] = {}
        related_ad_ids: list[str] = []
        related_creative_ids: list[str] = []
        additive_fields = {
            "stat_cost", "show_cnt", "click_cnt", "pay_order_amount", "pay_order_count",
            "convert_cnt", "total_play",
        }
        for row in rows:
            values = (
                row.get("fields") if isinstance(row.get("fields"), dict)
                else row.get("metrics") if isinstance(row.get("metrics"), dict)
                else row
            )
            for field in REPORT_FIELDS:
                if values.get(field) is not None:
                    current = metrics.get(field)
                    incoming = values[field]
                    if (
                        field in additive_fields
                        and isinstance(current, (int, float))
                        and isinstance(incoming, (int, float))
                    ):
                        metrics[field] = current + incoming
                    elif current is None:
                        metrics[field] = incoming
            related_ad_ids.extend(str(item) for item in row.get("related_ad_ids") or [] if item)
            related_creative_ids.extend(
                str(item) for item in row.get("related_creative_ids") or [] if item
            )
        return {
            "metrics": metrics,
            "related_ad_ids": list(dict.fromkeys(related_ad_ids)),
            "related_creative_ids": list(dict.fromkeys(related_creative_ids)),
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "has_data": bool(rows),
            "row_count": len(rows),
            "request_id": request_id,
        }


qianchuan_service = QianchuanService()
