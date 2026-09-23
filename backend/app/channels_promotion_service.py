import json
import math
import os
import re
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote
from uuid import uuid4

import requests
from cryptography.fernet import Fernet, InvalidToken

from .database import SessionLocal
from .models import ChannelsAccount, ChannelsPromotionAccount


PROMOTION_LOGIN_URL = "https://channels.weixin.qq.com/promote/pages/platform/login"
PROMOTION_HOME_URL = "https://channels.weixin.qq.com/promote/pages/platform/home"
TRANSFER_BASE = "https://channels.weixin.qq.com/promote/api/web/transfer/"
PROMOTION_TARGETS = {
    # Tencent's current short-video promotion target enum (2026-08-29).
    # Keep the readable keys stable in our API; never infer these numbers from
    # display order because the official page enables/disables them per video.
    "play": 1,
    "follow": 5,
    "like": 6,
    "product_click": 7,
    "click": 7,  # Backwards-compatible alias used by older local drafts.
    "product_pay": 8,
    "deal_roi": 11,
    "smart": 14,
    "net_deal_roi": 40,
    "net_product_pay": 42,
    "heart": 43,
}
PROMOTION_DURATIONS = {
    6: 21600,
    8: 28800,
    12: 43200,
    24: 86400,
}
# Current Tencent enums read from the live promotion web application.
PROMOTION_CURRENCY_WECOIN = 1
PROMOTION_BILLING_PREPAID = 0
PROMOTION_TYPE_SMART = 1
PROMOTION_TYPE_TARGETED = 2
PROMOTION_PRICING_OCPX = 1
FEED_ORDER_UNPAID = 1
FEED_ORDER_FAILED = {6, 7}
FEED_ORDER_CANCELLED = {4}
WECOIN_PAY_PAID = 1
WECOIN_PAY_WAIT_CALLBACK = 2
AUTH_ERROR_CODES = {-330, -334}
WecoinUserType = SimpleNamespace(IOS=1, ANDROID=2, CORPORATE=3)
WECOIN_USER_TYPE_LABELS = {
    WecoinUserType.IOS: "Apple 个人账户",
    WecoinUserType.ANDROID: "Android 个人账户",
    WecoinUserType.CORPORATE: "企业账户",
}


class ChannelsPromotionError(RuntimeError):
    def __init__(
        self,
        message: str,
        code: str,
        *,
        promotion_id: str = "",
        uncertain: bool = False,
        summary: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.promotion_id = promotion_id
        self.uncertain = uncertain
        self.summary = summary


class ChannelsPromotionService:
    """Independent Video Channels promotion auth and payment client.

    The promotion cookie is stored only in a server-side 0600 file. It is
    never returned by public methods, persisted in business logs or embedded
    in an exception message.
    """

    def __init__(self) -> None:
        self.auth_root = Path(os.getenv("CHANNELS_PROMOTION_AUTH_DIR", "/data/channels-promotion-auth"))
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}
        self.request_timeout = max(10, int(os.getenv("CHANNELS_PROMOTION_TIMEOUT_SECONDS", "30")))

    def _fernet(self) -> Fernet:
        configured = os.getenv("CHANNELS_PROMOTION_FERNET_KEY", "").strip()
        if configured:
            key = configured.encode("ascii")
        else:
            self.auth_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.auth_root, 0o700)
            key_path = self.auth_root / ".payment-fernet.key"
            if key_path.is_file():
                key = key_path.read_bytes().strip()
            else:
                key = Fernet.generate_key()
                temporary = self.auth_root / f".payment-fernet.{uuid4().hex}.tmp"
                temporary.write_bytes(key)
                os.chmod(temporary, 0o600)
                temporary.replace(key_path)
                os.chmod(key_path, 0o600)
        try:
            return Fernet(key)
        except (TypeError, ValueError) as error:
            raise ChannelsPromotionError("视频号加热密钥配置无效", "encryption_unavailable") from error

    def encrypt_secret(self, value: str) -> str:
        if not value:
            return ""
        return "fernet:" + self._fernet().encrypt(value.encode("utf-8")).decode("ascii")

    def decrypt_secret(self, value: str) -> str:
        if not value:
            return ""
        token = value[len("fernet:"):] if value.startswith("fernet:") else value
        try:
            return self._fernet().decrypt(token.encode("ascii")).decode("utf-8")
        except (InvalidToken, UnicodeDecodeError, ValueError) as error:
            raise ChannelsPromotionError("视频号加热敏感会话无法解密，请重新授权", "encryption_invalid") from error

    @staticmethod
    def payment_launch_url(pc_sdk_info: str) -> str:
        return (
            "https://webeans.my-qcloud.com/sdk/#/consume/qrcode?"
            f"{pc_sdk_info}&theme=light&brand_color=%23e97a26"
        )

    @property
    def browser_available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            return False
        return True

    @staticmethod
    def _safe_owner(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:80] or "oa-user"

    @staticmethod
    def user_type_label(value: int | None) -> str:
        return WECOIN_USER_TYPE_LABELS.get(value, "未知账户类型")

    @classmethod
    def require_enterprise_account(cls, prepared: dict) -> None:
        user_type = cls._integer(prepared.get("user_type"))
        if user_type == WecoinUserType.CORPORATE:
            return
        nickname = str(prepared.get("nickname") or "当前账号").strip() or "当前账号"
        account_type = cls.user_type_label(user_type)
        raise ChannelsPromotionError(
            f"当前扫码登录的是{account_type}“{nickname}”；加热仅允许企业账户，请在手机端选择企业账户后重新授权",
            "enterprise_required",
        )

    @staticmethod
    def _launch_browser(playwright):
        channel = os.getenv("CHANNELS_BROWSER_CHANNEL", "chrome").strip()
        options = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
        }
        if channel:
            options["channel"] = channel
        return playwright.chromium.launch(**options)

    def _update_session(self, session_id: str, **values) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return
            state.update(values)
            state["updated_at"] = datetime.utcnow()

    def _update_capture(self, session_id: str, capture: bytes, **values) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return
            if capture and capture != state.get("capture"):
                state["capture"] = capture
                state["capture_revision"] = int(state.get("capture_revision") or 0) + 1
            state.update(values)
            state["updated_at"] = datetime.utcnow()

    def start_authorization(self, account: ChannelsAccount) -> dict:
        if not self.browser_available:
            raise ChannelsPromotionError("服务器尚未安装视频号加热浏览器组件", "browser_unavailable")
        session_id = str(uuid4())
        state = {
            "id": session_id,
            "owner_number": account.owner_number,
            "account_id": account.id,
            "account_name": account.nickname,
            "publish_auth_file": account.auth_file,
            "status": "starting",
            "message": "正在打开视频号加热授权页",
            "capture": b"",
            "capture_revision": 0,
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
        with self._lock:
            self._sessions[session_id] = state
        threading.Thread(target=self._authorization_worker, args=(session_id,), daemon=True).start()
        return self.session_status(session_id, account.owner_number)

    def session_status(self, session_id: str, owner_number: str) -> dict:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state or state["owner_number"] != owner_number:
                raise ChannelsPromotionError("加热授权会话不存在或不属于当前账号", "session_not_found")
            return {
                "id": state["id"],
                "account_id": state["account_id"],
                "status": state["status"],
                "message": state["message"],
                "capture_ready": bool(state.get("capture")),
                "capture_revision": int(state.get("capture_revision") or 0),
                "updated_at": state["updated_at"].isoformat() + "Z",
            }

    def session_capture(self, session_id: str, owner_number: str) -> bytes:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state or state["owner_number"] != owner_number:
                raise ChannelsPromotionError("加热授权会话不存在或不属于当前账号", "session_not_found")
            if not state.get("capture"):
                raise ChannelsPromotionError("加热授权画面尚未准备完成", "capture_pending")
            return bytes(state["capture"])

    def _authorization_worker(self, session_id: str) -> None:
        from playwright.sync_api import sync_playwright

        with self._lock:
            state = dict(self._sessions[session_id])
        browser = None
        try:
            publish_auth_file = Path(state["publish_auth_file"]).resolve()
            if not publish_auth_file.is_file():
                raise ChannelsPromotionError("发布授权已失效，请先重新扫码授权视频号", "publish_auth_expired")
            self.auth_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.auth_root, 0o700)
            with sync_playwright() as playwright:
                browser = self._launch_browser(playwright)
                # Publishing and promotion are independent login surfaces. A clean
                # context lets the user choose any enterprise promotion account,
                # without inheriting or requiring the publishing Channels identity.
                context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                page = context.new_page()
                page.goto(PROMOTION_LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                deadline = time.monotonic() + 240
                self._update_session(session_id, status="waiting_scan", message="请扫码并在手机端选择任意企业账户完成加热授权")
                while time.monotonic() < deadline:
                    cookie_value = ""
                    for cookie in context.cookies():
                        if cookie.get("name") == "promotewebsessionid" and cookie.get("value"):
                            cookie_value = str(cookie["value"])
                            break
                    if cookie_value:
                        try:
                            prepared = self.get_user_prepare(cookie_value)
                        except ChannelsPromotionError as error:
                            if error.code != "auth_expired":
                                raise
                        else:
                            self.require_enterprise_account(prepared)
                            auth_file = self._write_cookie_file(
                                state["owner_number"], state["account_id"], cookie_value
                            )
                            now = datetime.utcnow()
                            with SessionLocal() as db:
                                row = db.get(ChannelsPromotionAccount, state["account_id"])
                                if row is None:
                                    row = ChannelsPromotionAccount(
                                        account_id=state["account_id"],
                                        owner_number=state["owner_number"],
                                        created_at=now,
                                    )
                                    db.add(row)
                                row.auth_file = str(auth_file)
                                row.promote_uniq_id = str(prepared.get("uniq_id") or "")[:160]
                                row.nickname = str(prepared.get("nickname") or state["account_name"])[:255]
                                row.user_type = int(prepared.get("user_type") or 0)
                                row.balance_wecoin = prepared.get("balance")
                                row.status = "active"
                                row.message = "企业加热账户授权可用"
                                row.authorized_at = now
                                row.expires_at = now + timedelta(hours=72)
                                row.updated_at = now
                                db.commit()
                            self._update_session(
                                session_id,
                                status="authorized",
                                message=f"已完成企业账户“{prepared.get('nickname') or '未命名账户'}”的加热授权",
                            )
                            return
                    try:
                        capture = page.screenshot(full_page=False)
                        self._update_capture(
                            session_id,
                            capture,
                            status="waiting_scan",
                            message="请扫码并在手机端选择任意企业账户完成加热授权",
                        )
                    except Exception:
                        pass
                    page.wait_for_timeout(1500)
                self._update_session(session_id, status="expired", message="加热授权二维码已过期，请重新发起")
        except ChannelsPromotionError as error:
            self._update_session(session_id, status="failed", message=str(error))
        except Exception as error:
            self._update_session(session_id, status="failed", message=f"加热授权未完成：{str(error)[:180]}")
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    def _write_cookie_file(self, owner_number: str, account_id: str, cookie_value: str) -> Path:
        owner_dir = self.auth_root / self._safe_owner(owner_number)
        owner_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(owner_dir, 0o700)
        auth_file = owner_dir / f"{account_id}.json"
        temporary = owner_dir / f".{account_id}.{uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "version": 2,
                    "promotewebsessionid_ciphertext": self.encrypt_secret(cookie_value),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        temporary.replace(auth_file)
        os.chmod(auth_file, 0o600)
        return auth_file

    def _read_cookie_file(self, auth_file: str) -> str:
        root = self.auth_root.resolve()
        path = Path(auth_file).resolve()
        if root not in path.parents or not path.is_file():
            raise ChannelsPromotionError("加热授权已失效，请重新扫码", "auth_expired")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception as error:
            raise ChannelsPromotionError("加热授权文件不可用，请重新扫码", "auth_expired") from error
        ciphertext = str(payload.get("promotewebsessionid_ciphertext") or "")
        if ciphertext:
            value = self.decrypt_secret(ciphertext)
        else:
            value = str(payload.get("promotewebsessionid") or "")
            if value:
                # Transparently migrate legacy 0600 plaintext cookie files.
                temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
                temporary.write_text(
                    json.dumps(
                        {
                            "version": 2,
                            "promotewebsessionid_ciphertext": self.encrypt_secret(value),
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )
                os.chmod(temporary, 0o600)
                temporary.replace(path)
                os.chmod(path, 0o600)
        if not value:
            raise ChannelsPromotionError("加热授权已失效，请重新扫码", "auth_expired")
        return value

    @staticmethod
    def _base_response(payload: dict) -> tuple[int, str]:
        data = payload.get("data") if isinstance(payload, dict) else {}
        data = data if isinstance(data, dict) else {}
        base = data.get("baseResp") or data.get("base_resp") or payload.get("baseResp") or {}
        base = base if isinstance(base, dict) else {}
        raw_code = base.get("errcode", base.get("errCode", payload.get("errcode", 0)))
        try:
            code = int(raw_code or 0)
        except (TypeError, ValueError):
            code = -1
        message = str(
            base.get("errmsg")
            or base.get("errMsg")
            or payload.get("errmsg")
            or payload.get("message")
            or ""
        )
        return code, message[:240]

    @staticmethod
    def response_summary(payload: dict, promotion_id: str = "", pay_ret: int | None = None) -> str:
        code, message = ChannelsPromotionService._base_response(payload)
        summary = {"errcode": code}
        if message:
            summary["message"] = message
        if promotion_id:
            summary["promotion_id"] = promotion_id
        if pay_ret is not None:
            summary["pay_ret"] = pay_ret
        return json.dumps(summary, ensure_ascii=False)[:1200]

    def _transfer(self, method: str, cookie_value: str, payload: dict, operation: str) -> dict:
        request_id = uuid4().hex
        page_url = quote(PROMOTION_HOME_URL, safe="")
        url = f"{TRANSFER_BASE}{method}?_aid=&_rid={request_id}&_vid=&_pageUrl={page_url}"
        headers = {
            "Content-Type": "application/json",
            "Cookie": f"promotewebsessionid={cookie_value}",
            "Origin": "https://channels.weixin.qq.com",
            "Referer": PROMOTION_HOME_URL,
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
            ),
        }
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=self.request_timeout)
            response.raise_for_status()
            result = response.json()
        except (requests.Timeout, requests.ConnectionError) as error:
            uncertain = operation in {"create", "pay", "payment_session"}
            code = "platform_unknown" if uncertain else f"{operation}_failed"
            message = (
                "腾讯加热平台响应不确定，请人工核对，禁止自动重试"
                if uncertain
                else "腾讯加热平台连接超时，请稍后重新询价"
            )
            raise ChannelsPromotionError(message, code, uncertain=uncertain) from error
        except (requests.RequestException, ValueError) as error:
            raise ChannelsPromotionError(
                f"腾讯加热平台{operation}请求未完成",
                f"{operation}_failed",
            ) from error
        if not isinstance(result, dict):
            raise ChannelsPromotionError(
                f"腾讯加热平台{operation}返回格式异常",
                f"{operation}_failed",
            )
        code, message = self._base_response(result)
        if code in AUTH_ERROR_CODES:
            raise ChannelsPromotionError("加热授权已失效，请重新扫码", "auth_expired")
        if code != 0:
            raise ChannelsPromotionError(
                message or f"腾讯加热平台{operation}失败（{code}）",
                f"{operation}_failed",
                summary=self.response_summary(result),
            )
        return result

    @staticmethod
    def _path(payload: dict, *parts: str):
        current = payload
        for part in parts:
            if not isinstance(current, dict):
                return None
            current = current.get(part)
        return current

    @staticmethod
    def _integer(value) -> int | None:
        try:
            return int(float(str(value).replace(",", "")))
        except (TypeError, ValueError):
            return None

    def get_user_prepare(self, cookie_value: str) -> dict:
        result = self._transfer(
            "MMFinderPromotionDspApisvr/getUserPrepare",
            cookie_value,
            {"baseReq": {"featureFlag": 26}},
            "prepare",
        )
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        personal = data.get("personalUserInfo") if isinstance(data.get("personalUserInfo"), dict) else {}
        wecoin = data.get("userWecoinInfo") if isinstance(data.get("userWecoinInfo"), dict) else {}
        user_type = self._integer(wecoin.get("userType", wecoin.get("user_type")))
        return {
            "uniq_id": str(personal.get("uniqId") or personal.get("uniq_id") or ""),
            "nickname": str(personal.get("nickname") or ""),
            "balance": self._integer(wecoin.get("balance")),
            "user_type": user_type,
            "account_type": self.user_type_label(user_type),
            "is_enterprise": user_type == WecoinUserType.CORPORATE,
        }

    def prepare_account(self, account: ChannelsPromotionAccount) -> dict:
        if account.status != "active" or (account.expires_at and account.expires_at <= datetime.utcnow()):
            raise ChannelsPromotionError("加热授权已过期，请重新扫码", "auth_expired")
        cookie_value = self._read_cookie_file(account.auth_file)
        prepared = self.get_user_prepare(cookie_value)
        self.require_enterprise_account(prepared)
        if account.promote_uniq_id and prepared.get("uniq_id") and str(account.promote_uniq_id) != str(prepared["uniq_id"]):
            raise ChannelsPromotionError("加热授权身份已变化，请重新扫码授权", "account_mismatch")
        return {"cookie": cookie_value, **prepared}

    def quote(
        self,
        account: ChannelsPromotionAccount,
        export_id: str,
        target: str,
        budget_wecoin: int,
        duration_hours: int,
        *,
        funding_type: str = "wecoin",
        bid_mode: str = "volume",
        bid_value: float | None = None,
        start_mode: str = "immediate",
        scheduled_at: datetime | None = None,
        billing_method: str = "prepaid",
        promotion_mode: str = "smart",
        portrait_mode: str = "none",
        voucher_mode: str = "none",
    ) -> dict:
        self.validate_parameters(
            target, budget_wecoin, duration_hours,
            funding_type=funding_type, bid_mode=bid_mode, bid_value=bid_value,
            start_mode=start_mode, scheduled_at=scheduled_at, billing_method=billing_method,
            promotion_mode=promotion_mode, portrait_mode=portrait_mode, voucher_mode=voucher_mode,
        )
        prepared = self.prepare_account(account)
        result = self._transfer(
            "MMFinderPromotionDspApisvr/inquiryPromotionPrice",
            prepared["cookie"],
            {
                "exportIds": [export_id],
                "promotionQuota": str(budget_wecoin),
                "payMethod": 0,
                "promotionTarget": PROMOTION_TARGETS[target],
                "voucherIds": [],
                "currencyChoice": PROMOTION_CURRENCY_WECOIN,
            },
            "quote",
        )
        raw_need_pay = self._integer(self._path(result, "data", "payDetail", "needPayAmountInCents"))
        fallback = raw_need_pay is None
        if raw_need_pay is None:
            need_pay = budget_wecoin
        else:
            # Despite the Tencent field name, the captured Channels contract
            # reports tenths of a WeCoin here. Keep the conversion covered by
            # a fixed response sample instead of interpreting the name.
            need_pay = int(math.ceil(raw_need_pay / 10.0))
        balance = prepared.get("balance")
        if balance is not None and balance < need_pay:
            raise ChannelsPromotionError("微信豆余额不足，请充值后再试", "insufficient_balance")
        return {
            "ok": True,
            "need_pay": need_pay,
            "balance": balance,
            "quote_fallback": fallback,
            "nickname": prepared.get("nickname") or account.nickname,
            "user_type": prepared.get("user_type"),
            "account_type": prepared.get("account_type") or self.user_type_label(prepared.get("user_type")),
            "configuration": {
                "funding_type": funding_type,
                "bid_mode": bid_mode,
                "bid_value": bid_value,
                "start_mode": start_mode,
                "scheduled_at": scheduled_at.isoformat() if scheduled_at else None,
                "billing_method": billing_method,
                "promotion_mode": promotion_mode,
                "portrait_mode": portrait_mode,
                "voucher_mode": voucher_mode,
            },
        }

    @staticmethod
    def validate_parameters(
        target: str,
        budget_wecoin: int,
        duration_hours: int,
        *,
        funding_type: str = "wecoin",
        bid_mode: str = "volume",
        bid_value: float | None = None,
        start_mode: str = "immediate",
        scheduled_at: datetime | None = None,
        billing_method: str = "prepaid",
        promotion_mode: str = "smart",
        portrait_mode: str = "none",
        voucher_mode: str = "none",
    ) -> None:
        if target not in PROMOTION_TARGETS:
            raise ChannelsPromotionError("不支持的提升目标", "invalid_target")
        if budget_wecoin < 50 or budget_wecoin > 30_000_000:
            raise ChannelsPromotionError("预算需在 50—30000000 微信豆之间", "invalid_budget")
        if duration_hours not in PROMOTION_DURATIONS:
            raise ChannelsPromotionError("加热时长只支持 6、8、12 或 24 小时", "invalid_duration")
        if funding_type != "wecoin":
            raise ChannelsPromotionError(
                "当前企业账户直投链路只完成了微信豆支付校验；现金和自动选择可预选，但需腾讯返回资金账户能力后才能提交",
                "unsupported_funding",
            )
        if billing_method != "prepaid":
            raise ChannelsPromotionError(
                "实时扣费只对腾讯已开通的现金账户开放；当前微信豆账户仅支持预先扣费",
                "unsupported_billing",
            )
        if bid_mode == "cost_control" and (bid_value is None or bid_value <= 0):
            raise ChannelsPromotionError("控成本加热需要填写有效的目标出价", "invalid_bid")
        if start_mode == "scheduled":
            if scheduled_at is None:
                raise ChannelsPromotionError("请选择定时加热的开始时间", "invalid_schedule")
            if scheduled_at.timestamp() <= time.time() + 120:
                raise ChannelsPromotionError("定时加热时间至少需晚于当前时间 2 分钟", "invalid_schedule")
        if promotion_mode == "targeted":
            raise ChannelsPromotionError(
                "定向加热需要先补充性别、年龄、地域或相似账号等人群条件；当前可预选，但不能以空人群提交",
                "audience_required",
            )
        if portrait_mode == "authorized":
            raise ChannelsPromotionError(
                "使用他人肖像必须上传腾讯要求的肖像授权证明；当前可预选，上传证明后才能提交",
                "portrait_proof_required",
            )
        if voucher_mode == "max":
            raise ChannelsPromotionError(
                "最大面额优惠需要先读取并选择当前账户可用优惠券；当前账户尚未返回可用券",
                "voucher_required",
            )

    @staticmethod
    def build_create_payload(
        export_id: str,
        target: str,
        budget_wecoin: int,
        duration_hours: int,
        order_name: str,
        *,
        bid_mode: str = "volume",
        bid_value: float | None = None,
        start_mode: str = "immediate",
        scheduled_at: datetime | None = None,
        promotion_mode: str = "smart",
    ) -> dict:
        is_roi_target = target in {"deal_roi", "net_deal_roi"}
        custom_bid = bid_mode == "cost_control" and bid_value is not None
        payload = {
            "sequence": str(int(time.time() * 1000)) + uuid4().hex[:5],
            "materialExportIds": [export_id],
            "feedPromotionOrderInfo": {
                "promotionTarget": PROMOTION_TARGETS[target],
                "promotionType": PROMOTION_TYPE_SMART if promotion_mode == "smart" else PROMOTION_TYPE_TARGETED,
                "estimatedWecoinAmount": str(budget_wecoin),
                "suggest": {
                    "gender": [],
                    "ageRange": [],
                    "cityIds": [],
                    "interestTagV3": [],
                    "gameCategories": [],
                    "similarUsernameList": [],
                    "wecoinBid": str(round(float(bid_value) * 100)) if custom_bid and not is_roi_target else "0",
                    "roiBidX100": round(float(bid_value) * 100) if custom_bid and is_roi_target else None,
                    "liveUinPackageIds": [],
                    "enableSearchRelaxTargeting": True,
                    "roomInfo": {"roomId": []},
                },
                "duration": str(PROMOTION_DURATIONS[duration_hours]),
                "pricingMethod": PROMOTION_PRICING_OCPX,
                "dynamicAudit": False,
                "internalInfo": {"isInternal": False},
            },
            "voucherIds": [],
            "multiplyVoucherIds": [],
            "classification": 0,
            "projectInfo": {},
            "billingMethod": PROMOTION_BILLING_PREPAID,
            "currencyChoice": PROMOTION_CURRENCY_WECOIN,
            "orderName": order_name,
            "deviceInfo": {"deviceTypeId": 1},
            "baseReq": {"featureFlag": 26},
        }
        if start_mode == "scheduled" and scheduled_at is not None:
            payload["feedPromotionOrderInfo"]["estimatedStartts"] = int(scheduled_at.timestamp())
        return payload

    @staticmethod
    def _promotion_id(payload: dict) -> str:
        stack = [payload.get("data") if isinstance(payload, dict) else None]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    if key in {"promotionId", "promotion_id"} and value:
                        return str(value)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)
        return ""

    @staticmethod
    def _pc_sdk_info(payload: dict) -> str:
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        direct = data.get("pcSdkInfo") or data.get("pc_sdk_info")
        if direct:
            return str(direct)
        details = data.get("promotionPayDetail") or data.get("promotion_pay_detail")
        first = details[0] if isinstance(details, list) and details else {}
        if not isinstance(first, dict):
            return ""
        for key in ("cashPayInfo", "wecoinPayInfo", "cash_pay_info", "wecoin_pay_info"):
            info = first.get(key)
            if isinstance(info, dict) and (info.get("pcSdkInfo") or info.get("pc_sdk_info")):
                return str(info.get("pcSdkInfo") or info.get("pc_sdk_info"))
        return ""

    def payment_session(self, account: ChannelsPromotionAccount, promotion_id: str) -> dict:
        prepared = self.prepare_account(account)
        result = self._transfer(
            "MmFinderPromotionApiSvr/payFeedPromotion",
            prepared["cookie"],
            {
                "promotionId": promotion_id,
                "pcSdkFlag": 1,
                "baseReq": {"featureFlag": 26},
            },
            "payment_session",
        )
        pc_sdk_info = self._pc_sdk_info(result)
        if not pc_sdk_info:
            raise ChannelsPromotionError(
                "腾讯未返回支付组件信息；请先核验订单状态，仍待支付时再重试",
                "payment_session_failed",
                promotion_id=promotion_id,
                summary=self.response_summary(result, promotion_id),
            )
        return {
            "promotion_id": promotion_id,
            "pc_sdk_info": pc_sdk_info,
            "response_summary": self.response_summary(result, promotion_id),
        }

    def create_and_prepare_payment(
        self,
        account: ChannelsPromotionAccount,
        export_id: str,
        target: str,
        budget_wecoin: int,
        duration_hours: int,
        order_name: str,
        *,
        funding_type: str = "wecoin",
        bid_mode: str = "volume",
        bid_value: float | None = None,
        start_mode: str = "immediate",
        scheduled_at: datetime | None = None,
        billing_method: str = "prepaid",
        promotion_mode: str = "smart",
        portrait_mode: str = "none",
        voucher_mode: str = "none",
    ) -> dict:
        created = self.create_promotion(
            account,
            export_id,
            target,
            budget_wecoin,
            duration_hours,
            order_name,
            funding_type=funding_type,
            bid_mode=bid_mode,
            bid_value=bid_value,
            start_mode=start_mode,
            scheduled_at=scheduled_at,
            billing_method=billing_method,
            promotion_mode=promotion_mode,
            portrait_mode=portrait_mode,
            voucher_mode=voucher_mode,
        )
        promotion_id = created["promotion_id"]
        try:
            session = self.payment_session(account, promotion_id)
        except ChannelsPromotionError as error:
            error.promotion_id = promotion_id
            if error.code != "auth_expired":
                error.code = "payment_session_failed" if not error.uncertain else "platform_unknown"
            raise
        return {
            "promotion_id": promotion_id,
            "pc_sdk_info": session["pc_sdk_info"],
            "response_summary": session["response_summary"],
        }

    def create_promotion(
        self,
        account: ChannelsPromotionAccount,
        export_id: str,
        target: str,
        budget_wecoin: int,
        duration_hours: int,
        order_name: str,
        *,
        funding_type: str = "wecoin",
        bid_mode: str = "volume",
        bid_value: float | None = None,
        start_mode: str = "immediate",
        scheduled_at: datetime | None = None,
        billing_method: str = "prepaid",
        promotion_mode: str = "smart",
        portrait_mode: str = "none",
        voucher_mode: str = "none",
        request_payload: dict | None = None,
    ) -> dict:
        self.validate_parameters(
            target, budget_wecoin, duration_hours,
            funding_type=funding_type, bid_mode=bid_mode, bid_value=bid_value,
            start_mode=start_mode, scheduled_at=scheduled_at, billing_method=billing_method,
            promotion_mode=promotion_mode, portrait_mode=portrait_mode, voucher_mode=voucher_mode,
        )
        prepared = self.prepare_account(account)
        create_result = self._transfer(
            "MmFinderPromotionApiSvr/createFeedPromotion",
            prepared["cookie"],
            request_payload or self.build_create_payload(
                export_id, target, budget_wecoin, duration_hours, order_name,
                bid_mode=bid_mode, bid_value=bid_value,
                start_mode=start_mode, scheduled_at=scheduled_at,
                promotion_mode=promotion_mode,
            ),
            "create",
        )
        promotion_id = self._promotion_id(create_result)
        if not promotion_id:
            raise ChannelsPromotionError(
                "腾讯已响应创建请求，但没有返回计划 ID；请人工核对，禁止自动重试",
                "platform_unknown",
                uncertain=True,
                summary=self.response_summary(create_result),
            )
        return {
            "promotion_id": promotion_id,
            "response_summary": self.response_summary(create_result, promotion_id),
        }

    def get_order_detail(self, account: ChannelsPromotionAccount, promotion_id: str) -> dict:
        prepared = self.prepare_account(account)
        result = self._transfer(
            "MmFinderPromotionApiSvr/getFeedPromotionOrderDetail",
            prepared["cookie"],
            {"promotionId": promotion_id, "baseReq": {"featureFlag": 26}},
            "detail",
        )
        order = self._path(result, "data", "order") or {}
        order_info = order.get("orderInfo") if isinstance(order, dict) else {}
        order_info = order_info if isinstance(order_info, dict) else {}
        payment = order_info.get("paymentInfo") if isinstance(order_info.get("paymentInfo"), dict) else {}
        wecoin = payment.get("wecoinInfo") if isinstance(payment.get("wecoinInfo"), dict) else {}
        platform_status = self._integer(order_info.get("status"))
        pay_status = self._integer(wecoin.get("payStatus"))
        billing_method = self._integer(order_info.get("billingMethod"))
        paid_wecoin = 0
        if pay_status == WECOIN_PAY_PAID:
            paid_wecoin = self._integer(wecoin.get("payAmount")) or self._integer(wecoin.get("preauthAmount")) or 0
        elif not wecoin and platform_status != FEED_ORDER_UNPAID:
            paid_wecoin = self._integer(payment.get("paidWecoinAmount")) or 0

        if platform_status == FEED_ORDER_UNPAID:
            local_status = "pending_payment"
            message = "腾讯订单仍为待支付，尚未确认真实扣款"
        elif pay_status == WECOIN_PAY_WAIT_CALLBACK:
            local_status = "payment_processing"
            message = "腾讯正在确认支付回调，暂不记为已支付"
        elif platform_status in FEED_ORDER_FAILED:
            local_status = "failed"
            message = "腾讯订单审核未通过或素材已失效"
        elif platform_status in FEED_ORDER_CANCELLED:
            local_status = "cancelled"
            message = "腾讯订单已取消"
        elif pay_status == WECOIN_PAY_PAID and paid_wecoin > 0:
            local_status = "success"
            message = "腾讯已回读真实支付成功"
        elif billing_method == 1 and platform_status and platform_status > FEED_ORDER_UNPAID:
            local_status = "success"
            message = "腾讯实时扣费订单已开始执行"
        else:
            local_status = "manual_review"
            message = "腾讯订单状态尚不能证明真实支付，请人工核对"

        summary = {
            "errcode": 0,
            "promotion_id": promotion_id,
            "platform_status": platform_status,
            "wecoin_pay_status": pay_status,
            "paid_wecoin": paid_wecoin,
            "billing_method": billing_method,
        }
        return {
            "status": local_status,
            "message": message,
            "platform_status": platform_status,
            "wecoin_pay_status": pay_status,
            "cost_wecoin": paid_wecoin if local_status == "success" else None,
            "balance": prepared.get("balance"),
            "response_summary": json.dumps(summary, ensure_ascii=False),
        }

    @contextmanager
    def account_lock(self, account_id: str):
        with self._lock:
            lock = self._account_locks.setdefault(account_id, threading.Lock())
        with lock:
            yield

    def mark_expired(self, account_id: str, message: str = "加热授权已失效，请重新扫码") -> None:
        with SessionLocal() as db:
            row = db.get(ChannelsPromotionAccount, account_id)
            if row:
                row.status = "expired"
                row.message = message[:500]
                row.updated_at = datetime.utcnow()
                db.commit()

    def revoke_authorization(self, account_id: str, owner_number: str) -> None:
        with SessionLocal() as db:
            row = db.get(ChannelsPromotionAccount, account_id)
            if not row or row.owner_number != owner_number:
                return
            auth_file = row.auth_file
            row.status = "revoked"
            row.message = "已解除加热授权"
            row.updated_at = datetime.utcnow()
            db.commit()
        try:
            root = self.auth_root.resolve()
            path = Path(auth_file).resolve()
            if root in path.parents and path.is_file():
                path.unlink()
        except OSError:
            pass


channels_promotion_service = ChannelsPromotionService()
