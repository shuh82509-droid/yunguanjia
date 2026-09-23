import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Callable
from uuid import uuid4

import requests
from sqlalchemy import select

from .channels_internal_api import (
    ChannelsInternalApiError,
    ChannelsInternalPublisher,
    ChannelsPublishUncertain,
    HttpJsonClient,
    extract_cover,
    probe_video,
    read_authenticated_identity,
)
from .channels_credentials import ChannelsCredentialsError, decrypt_credentials, encrypt_credentials
from .config import settings
from .database import SessionLocal
from .models import ChannelsAccount, ChannelsDelivery
from .oss_service import oss_service


LOGIN_URL = "https://channels.weixin.qq.com/login.html"
PLATFORM_URL = "https://channels.weixin.qq.com/platform"
PUBLISH_URL = "https://channels.weixin.qq.com/platform/post/create"
POST_LIST_URL = "https://channels.weixin.qq.com/platform/post/list"
PRODUCT_CACHE_TTL_SECONDS = 10 * 60
PRODUCT_CACHE_MAX_STALE_SECONDS = 7 * 24 * 60 * 60
PRODUCT_REFRESH_ATTEMPTS = 3

logger = logging.getLogger(__name__)

VIDEO_ANNOTATION_LABELS = {
    "none": "无需标注",
    "ai_generated": "含AI生成内容",
    "fictional": "内容为虚构剧情，仅供娱乐",
    "personal_opinion": "个人观点，仅供参考",
    "marketing_ad": "内容包含营销广告",
    "self_shot": "内容为自行拍摄",
    "repost": "内容为转载",
}


class ChannelsError(RuntimeError):
    def __init__(self, message: str, stage: str = "", *, code: str = "", definitive: bool = True, transport: str = "") -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.definitive = definitive
        self.transport = transport


class ChannelsCancelled(ChannelsError):
    """Raised only before the irreversible platform publish click."""


class ChannelsService:
    """Per-user Video Channels browser sessions, adapted from the supplied MIT project."""

    def __init__(self) -> None:
        self.auth_root = Path(os.getenv("CHANNELS_AUTH_DIR", "/data/channels-auth"))
        self._sessions: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._browser_locks: dict[str, threading.Lock] = {}
        self._product_cache: dict[str, dict] = {}
        self._product_jobs: dict[str, dict] = {}
        self._authorization_slot = threading.Lock()

    @staticmethod
    def _require_browser_capacity() -> None:
        """Leave thread headroom for the API; never raise the container limit silently."""
        try:
            root = Path("/sys/fs/cgroup")
            limit_text = (root / "pids.max").read_text().strip()
            if limit_text != "max":
                available = int(limit_text) - int((root / "pids.current").read_text().strip())
                if available < 96:
                    raise ChannelsError("视频号登录资源暂忙，请稍后重新扫码；现有推送不受影响", "authorization", code="BROWSER_BUSY")
        except (OSError, ValueError):
            pass

    @property
    def browser_available(self) -> bool:
        try:
            import playwright.sync_api  # noqa: F401
        except Exception:
            return False
        return True

    @staticmethod
    def _launch_publish_browser(playwright):
        """Launch a codec-capable browser so the platform can generate a poster.

        The source video is still uploaded byte-for-byte. This only changes the
        browser used to render the Video Channels creator page.
        """
        channel = os.getenv("CHANNELS_BROWSER_CHANNEL", "chrome").strip()
        options = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        }
        if channel:
            options["channel"] = channel
        return playwright.chromium.launch(**options)

    def _account_profile_dir(self, auth_file: Path) -> Path:
        """Return an account-scoped Chrome profile kept beside the auth state."""
        root = self.auth_root.resolve()
        profile_dir = auth_file.with_suffix(".profile").resolve()
        if root not in profile_dir.parents:
            raise ChannelsError("视频号授权目录异常，请重新扫码")
        return profile_dir

    @staticmethod
    def _restore_context_state(context, auth_file: Path) -> None:
        """Seed a persistent profile from the last verified Playwright state."""
        try:
            state = json.loads(auth_file.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as error:
            raise ChannelsError("视频号授权文件无法读取，请重新扫码") from error
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if isinstance(cookies, list) and cookies:
            context.add_cookies(cookies)
        origin_values: dict[str, dict[str, str]] = {}
        for origin in state.get("origins", []) if isinstance(state, dict) else []:
            if not isinstance(origin, dict) or not str(origin.get("origin") or "").strip():
                continue
            values = {
                str(item.get("name")): str(item.get("value") or "")
                for item in origin.get("localStorage", [])
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            }
            if values:
                origin_values[str(origin["origin"])] = values
        if origin_values:
            encoded_origins = json.dumps(origin_values, ensure_ascii=True).replace("</", "<\\/")
            context.add_init_script(
                f"""(() => {{
                  const valuesByOrigin = {encoded_origins};
                  const values = valuesByOrigin[location.origin];
                  if (!values) return;
                  for (const [key, value] of Object.entries(values)) localStorage.setItem(key, value);
                }})()""",
            )

    @staticmethod
    def _persist_context_state(context, auth_file: Path) -> None:
        """Atomically refresh cookies/local storage after every authorized visit."""
        temporary = auth_file.with_suffix(auth_file.suffix + ".tmp")
        try:
            context.storage_state(path=str(temporary))
            os.replace(temporary, auth_file)
            os.chmod(auth_file, 0o600)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def _launch_account_session(self, playwright, auth_file: Path, *, allow_fallback: bool = True):
        """Launch one durable Chrome profile per account, with a safe legacy fallback."""
        profile_dir = self._account_profile_dir(auth_file)
        profile_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(profile_dir, 0o700)
        channel = os.getenv("CHANNELS_BROWSER_CHANNEL", "chrome").strip()
        options = {
            "headless": True,
            "viewport": {"width": 1440, "height": 1000},
            "locale": "zh-CN",
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-background-timer-throttling",
                "--disable-backgrounding-occluded-windows",
                "--disable-renderer-backgrounding",
            ],
        }
        if channel:
            options["channel"] = channel
        context = None
        try:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                **options,
            )
            self._restore_context_state(context, auth_file)
            self._restore_encrypted_session(context, auth_file)
            return context.browser, context, True
        except Exception as error:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass
            if isinstance(error, (ChannelsCredentialsError, ChannelsError)):
                raise
            if not allow_fallback:
                raise ChannelsError("视频号登录浏览器暂时无法启动，请稍后重新扫码", "authorization", code="BROWSER_START_FAILED") from None
            logger.warning("channels_persistent_profile_fallback profile=%s", profile_dir.name)
            browser = self._launch_publish_browser(playwright)
            context = browser.new_context(
                storage_state=str(auth_file), viewport={"width": 1440, "height": 1000}, locale="zh-CN",
            )
            self._restore_encrypted_session(context, auth_file)
            return browser, context, False

    def _restore_encrypted_session(self, context, auth_file):
        with SessionLocal() as db:
            row = db.scalar(select(ChannelsAccount).where(
                ChannelsAccount.auth_file == str(auth_file), ChannelsAccount.status == "active",
            ))
            if not row or not row.channel_cookies_ciphertext:
                return
            bundle = decrypt_credentials(self.auth_root, row.channel_cookies_ciphertext)
            context.clear_cookies()
            context.add_cookies(bundle["cookies"])
            context._wis_credentials = (row.id, row.channel_cookies_ciphertext, bundle)

    def _refresh_encrypted_session(self, context, auth_file):
        captured = getattr(context, "_wis_credentials", None)
        if not isinstance(captured, tuple) or len(captured) != 3:
            return
        account_id, revision, bundle = captured
        encrypted, session = encrypt_credentials(self.auth_root, context.cookies(), bundle["user_agent"])
        with SessionLocal() as db:
            db.query(ChannelsAccount).filter(
                ChannelsAccount.id == account_id, ChannelsAccount.status == "active",
                ChannelsAccount.auth_file == str(auth_file), ChannelsAccount.channel_cookies_ciphertext == revision,
            ).update({"channel_cookies_ciphertext": encrypted, "session_cookie_ciphertext": session,
                      "cookies_updated_at": datetime.utcnow()}, synchronize_session=False)
            db.commit()

    def _close_account_session(self, browser, context, auth_file: Path) -> None:
        if context:
            try:
                self._refresh_encrypted_session(context, auth_file)
            except Exception:
                logger.warning("channels_encrypted_state_refresh_failed")
            try:
                self._persist_context_state(context, auth_file)
            except Exception as error:
                logger.warning("channels_state_refresh_failed auth=%s", auth_file.name)
            try:
                context.close()
            except Exception:
                pass
        if browser:
            try:
                if browser.is_connected():
                    browser.close()
            except Exception:
                pass

    def remove_authorization_files(self, auth_file_value: str) -> None:
        """Remove only the explicitly revoked account state and its durable profile."""
        root = self.auth_root.resolve()
        auth_file = Path(auth_file_value).resolve()
        if root not in auth_file.parents:
            return
        profile_dir = auth_file.with_suffix(".profile").resolve()
        if auth_file.is_file():
            auth_file.unlink()
        if root in profile_dir.parents and profile_dir.is_dir():
            shutil.rmtree(profile_dir)

    @staticmethod
    def _safe_owner(value: str) -> str:
        return re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")[:80] or "oa-user"

    def status(self) -> dict:
        return {
            "configured": self.browser_available,
            "message": "可扫码授权并发布原视频" if self.browser_available else "服务器尚未安装视频号浏览器组件",
            "capabilities": {
                "qr_authorization": self.browser_available,
                "original_video_publish": self.browser_available,
                "batch_publish": True,
                "per_user_isolation": True,
                "direct_internal_api": True,
            },
            "publish_transport": settings.channels_publish_transport,
            "browser_fallback": settings.channels_internal_api_browser_fallback,
        }

    def start_authorization(self, owner_number: str, owner_name: str) -> dict:
        if not self.browser_available:
            raise ChannelsError("服务器尚未安装视频号浏览器组件")
        session_id = str(uuid4())
        state = {
            "id": session_id,
            "owner_number": owner_number,
            "owner_name": owner_name,
            "status": "starting",
            "message": "正在打开视频号授权页",
            "qr": b"",
            "capture_hash": "",
            "capture_revision": 0,
            "account_id": "",
            "interaction_required": False,
            "capture_mode": "qr",
            "account_choices": [],
            "account_choice_targets": {},
            "commands": [],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(seconds=240),
        }
        with self._lock:
            # A repeated click/retry must reuse the owner's active QR session,
            # not create another browser and consume the remaining API threads.
            for existing in self._sessions.values():
                if (existing["owner_number"] == owner_number
                        and existing["status"] not in {"authorized", "failed", "expired"}
                        and existing["expires_at"] > datetime.utcnow()):
                    session_id = existing["id"]
                    break
            else:
                self._sessions[session_id] = state
                existing = None
        if existing is None:
            try:
                threading.Thread(target=self._authorization_worker, args=(session_id,), daemon=True).start()
            except RuntimeError:
                self._update_session(session_id, status="failed", message="视频号登录资源暂忙，请稍后重新扫码")
        return self.session_status(session_id, owner_number)

    def session_status(self, session_id: str, owner_number: str) -> dict:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state or state["owner_number"] != owner_number:
                raise ChannelsError("授权会话不存在或不属于当前账号")
            return {
                "id": state["id"],
                "status": state["status"],
                "message": state["message"],
                "qr_ready": bool(state["qr"]),
                "capture_revision": int(state.get("capture_revision") or 0),
                "account_id": state["account_id"],
                "interaction_required": bool(state.get("interaction_required")),
                "capture_mode": state.get("capture_mode") or "qr",
                "account_choices": [
                    {
                        "choice_id": str(item.get("choice_id") or ""),
                        "label": str(item.get("label") or "")[:80],
                        "detail": str(item.get("detail") or "")[:120],
                    }
                    for item in list(state.get("account_choices") or [])
                ],
                "updated_at": state["updated_at"].isoformat() + "Z",
            }

    def queue_interaction(
        self,
        session_id: str,
        owner_number: str,
        x: float,
        y: float,
        action: str = "click",
        delta_y: float = 0,
        choice_id: str = "",
    ) -> dict:
        if not 0 <= x <= 1 or not 0 <= y <= 1:
            raise ChannelsError("点击位置无效，请重试")
        if action not in {"click", "scroll", "select", "select_account", "scroll_accounts"}:
            raise ChannelsError("不支持的电脑端操作")
        normalized_action = {
            "select_account": "select",
            "scroll_accounts": "scroll",
        }.get(action, action)
        if normalized_action == "scroll" and not -3000 <= delta_y <= 3000:
            raise ChannelsError("滚动距离无效，请重试")
        with self._lock:
            state = self._sessions.get(session_id)
            if not state or state["owner_number"] != owner_number:
                raise ChannelsError("授权会话不存在或不属于当前账号")
            expires_at = state.get("expires_at")
            if isinstance(expires_at, datetime) and expires_at <= datetime.utcnow():
                state.update(
                    status="expired",
                    message="授权会话已过期，请重新扫码",
                    interaction_required=False,
                    account_choices=[],
                    account_choice_targets={},
                    updated_at=datetime.utcnow(),
                )
                raise ChannelsError("授权会话已过期，请重新扫码")
            if not state.get("interaction_required"):
                raise ChannelsError("当前授权页面不需要电脑端选择")
            target = None
            if normalized_action == "select":
                if state.get("capture_mode") != "account_list":
                    raise ChannelsError("账号候选项已失效，请刷新后重新选择")
                target = dict((state.get("account_choice_targets") or {}).get(choice_id) or {})
                if not target:
                    raise ChannelsError("账号候选项已失效，请刷新后重新选择")
            elif state.get("capture_mode") != "account_choice":
                raise ChannelsError("当前页面无需截图点击，请从账号列表中选择")
            commands = state.setdefault("commands", [])
            command = {
                "type": normalized_action,
                "x": float(x),
                "y": float(y),
                "delta_y": float(delta_y),
                "choice_id": choice_id,
            }
            if target:
                command["target"] = target
            # A touchpad can emit dozens of wheel events in a fraction of a
            # second. Sending every event through Playwright made the remote
            # picker feel several seconds behind the user's hand. Keep one
            # accumulated scroll command; clicks remain strictly ordered.
            if normalized_action == "scroll" and commands and commands[-1].get("type") == "scroll":
                previous = commands[-1]
                previous.update(
                    type="scroll",
                    x=float(x),
                    y=float(y),
                    delta_y=max(-3000.0, min(3000.0, float(previous.get("delta_y") or 0) + float(delta_y))),
                )
            else:
                commands.append(command)
            del commands[:-5]
            if normalized_action == "scroll":
                state.update(
                    status="waiting_choice",
                    message="正在滚动账号列表并刷新画面",
                    interaction_required=True,
                    updated_at=datetime.utcnow(),
                )
            else:
                state.update(
                    status="selecting_account",
                    message="已识别你的选择，正在登录该视频号",
                    interaction_required=False,
                    account_choices=[],
                    account_choice_targets={},
                    updated_at=datetime.utcnow(),
                )
        return self.session_status(session_id, owner_number)

    @staticmethod
    def _account_choice_items(page) -> tuple[list[dict], dict[str, dict]]:
        """Discover every visible/virtualized account without exposing private selectors."""
        script = r"""
        async () => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
          const visible = node => {
            const box = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return box.width >= 80 && box.height >= 28 && box.bottom > 0 && box.top < innerHeight
              && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const titlePattern = /选择视频号登录|请选择视频号|选择要登录的视频号/;
          const title = [...document.querySelectorAll('h1,h2,h3,h4,p,span,div')]
            .find(node => visible(node) && titlePattern.test(normalize(node.innerText || node.textContent)));
          if (!title) return [];
          const root = title.closest('[role="dialog"],dialog,[class*="dialog" i],[class*="modal" i]') || document.body;
          const scrollables = [...root.querySelectorAll('*')].filter(node => {
            const style = getComputedStyle(node);
            return node.scrollHeight > node.clientHeight + 8
              && /(auto|scroll)/.test(style.overflowY)
              && node.clientHeight >= 80;
          }).sort((left, right) =>
            (right.scrollHeight - right.clientHeight) - (left.scrollHeight - left.clientHeight)
          );
          const scroller = scrollables[0] || root;
          const found = new Map();
          const collect = () => {
            const nodes = [...root.querySelectorAll('button,[role="button"],a,li,div')].filter(visible);
            for (const node of nodes) {
              const box = node.getBoundingClientRect();
              const text = normalize(node.innerText || node.textContent);
              if (!text || text.length > 160 || titlePattern.test(text) || /使用其他账号登录/.test(text)) continue;
              if (box.width < 80 || box.height < 28 || box.height > 160) continue;
              const role = node.getAttribute('role') || '';
              const semantic = !!node.querySelector('img') || role === 'button'
                || ['BUTTON', 'A', 'LI'].includes(node.tagName) || getComputedStyle(node).cursor === 'pointer';
              if (!semantic) continue;
              const nestedSame = [...node.querySelectorAll('button,[role="button"],a,li,div')]
                .some(child => child !== node && visible(child)
                  && normalize(child.innerText || child.textContent) === text
                  && (child.querySelector('img') || child.getAttribute('role') === 'button'
                    || ['BUTTON', 'A', 'LI'].includes(child.tagName) || getComputedStyle(child).cursor === 'pointer'));
              if (nestedSame) continue;
              const rawLines = String(node.innerText || node.textContent || '').split(/\n+/).map(normalize).filter(Boolean);
              const lines = rawLines.length ? rawLines : [text];
              const rolePattern = /^(管理员|运营者|创作者|视频号|协作者)$/;
              let label = lines.find(line => !titlePattern.test(line) && !rolePattern.test(line)) || text;
              label = normalize(label.split(/管理员|运营者|创作者|协作者|\|/)[0]) || text;
              const detail = normalize(lines.filter(line => line !== label).join(' ')
                || text.slice(label.length) || '视频号账号');
              const businessId = normalize(node.getAttribute('data-id') || node.getAttribute('data-finder-id') || '');
              if (!found.has(text)) found.set(text, { text, label: label.slice(0, 80), detail: detail.slice(0, 120), business_id: businessId });
            }
          };
          const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
          const step = Math.max(60, Math.floor((scroller.clientHeight || innerHeight) * 0.7));
          let previousTop = -1;
          let unchanged = 0;
          scroller.scrollTop = 0;
          for (let pass = 0; pass < 80; pass += 1) {
            await wait(60);
            collect();
            const currentMax = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
            if (scroller.scrollTop >= currentMax - 2) break;
            const nextTop = Math.min(currentMax, scroller.scrollTop + step);
            if (nextTop === previousTop) unchanged += 1; else unchanged = 0;
            if (unchanged >= 2) break;
            previousTop = scroller.scrollTop;
            scroller.scrollTop = nextTop;
          }
          scroller.scrollTop = 0;
          scroller.dispatchEvent(new Event('scroll', { bubbles: true }));
          await wait(40);
          return [...found.values()].slice(0, 80);
        }
        """
        public: list[dict] = []
        private: dict[str, dict] = {}
        frame_indexes: set[int] = set()
        for frame_index, frame in enumerate(page.frames):
            try:
                items = frame.evaluate(script) or []
            except Exception:
                continue
            for item in items:
                text = re.sub(r"\s+", " ", str(item.get("text") or "")).strip()
                label = re.sub(r"\s+", " ", str(item.get("label") or "")).strip()
                detail = re.sub(r"\s+", " ", str(item.get("detail") or "")).strip()
                if not text or not label:
                    continue
                stable_value = str(item.get("business_id") or text)
                choice_id = hashlib.sha256(f"{frame_index}\0{stable_value}".encode("utf-8")).hexdigest()[:24]
                if choice_id in private:
                    continue
                public.append({"choice_id": choice_id, "label": label[:80], "detail": detail[:120]})
                private[choice_id] = {"text": text[:240], "frame_index": frame_index}
                frame_indexes.add(frame_index)
                if len(public) >= 80:
                    break
            if len(public) >= 80:
                break
        if public:
            logger.info(
                "account_choices_discovered count=%s frame_indexes=%s",
                len(public), sorted(frame_indexes),
            )
        return public, private

    @staticmethod
    def _click_account_choice(page, target: dict) -> tuple[bool, str]:
        frame_index = int(target.get("frame_index", -1))
        text = re.sub(r"\s+", " ", str(target.get("text") or "")).strip()
        if not text or frame_index < 0 or frame_index >= len(page.frames):
            return False, "ACCOUNT_CHOICE_CHANGED"
        frame = page.frames[frame_index]
        marker = f"wis-{uuid4().hex}"
        script = r"""
        async ({ expected, marker }) => {
          const normalize = value => String(value || '').replace(/\s+/g, ' ').trim();
          const wait = ms => new Promise(resolve => setTimeout(resolve, ms));
          const visible = node => {
            const box = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return box.width >= 80 && box.height >= 28 && box.bottom > 0 && box.top < innerHeight
              && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const titlePattern = /选择视频号登录|请选择视频号|选择要登录的视频号/;
          const title = [...document.querySelectorAll('h1,h2,h3,h4,p,span,div')]
            .find(node => visible(node) && titlePattern.test(normalize(node.innerText || node.textContent)));
          if (!title) return { matched: 0 };
          const root = title.closest('[role="dialog"],dialog,[class*="dialog" i],[class*="modal" i]') || document.body;
          const scrollables = [...root.querySelectorAll('*')].filter(node => {
            const style = getComputedStyle(node);
            return node.scrollHeight > node.clientHeight + 8 && /(auto|scroll)/.test(style.overflowY) && node.clientHeight >= 80;
          }).sort((left, right) => (right.scrollHeight - right.clientHeight) - (left.scrollHeight - left.clientHeight));
          const scroller = scrollables[0] || root;
          const step = Math.max(60, Math.floor((scroller.clientHeight || innerHeight) * 0.7));
          scroller.scrollTop = 0;
          for (let pass = 0; pass < 80; pass += 1) {
            await wait(60);
            const exact = [...root.querySelectorAll('button,[role="button"],a,li,div')]
              .filter(node => visible(node) && normalize(node.innerText || node.textContent) === expected);
            const leaves = exact.filter(node => !exact.some(other => other !== node && node.contains(other)));
            if (leaves.length === 1) {
              leaves[0].setAttribute('data-wis-select-marker', marker);
              leaves[0].scrollIntoView({ block: 'center' });
              return { matched: 1 };
            }
            if (leaves.length > 1) return { matched: leaves.length };
            const maxScroll = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
            if (scroller.scrollTop >= maxScroll - 2) break;
            scroller.scrollTop = Math.min(maxScroll, scroller.scrollTop + step);
          }
          return { matched: 0 };
        }
        """
        try:
            result = frame.evaluate(script, {"expected": text, "marker": marker}) or {}
            if int(result.get("matched") or 0) != 1:
                return False, "ACCOUNT_CHOICE_CHANGED"
            locator = frame.locator(f'[data-wis-select-marker="{marker}"]')
            if locator.count() != 1:
                return False, "ACCOUNT_CHOICE_CHANGED"
            locator.click(timeout=6000)
            try:
                frame.evaluate(
                    "marker => document.querySelector(`[data-wis-select-marker=\"${marker}\"]`)?.removeAttribute('data-wis-select-marker')",
                    marker,
                )
            except Exception:
                pass
            return True, ""
        except Exception:
            return False, "ACCOUNT_CHOICE_CLICK_FAILED"

    def session_qr(self, session_id: str, owner_number: str) -> bytes:
        with self._lock:
            state = self._sessions.get(session_id)
            if not state or state["owner_number"] != owner_number:
                raise ChannelsError("授权会话不存在或不属于当前账号")
            if not state["qr"]:
                raise ChannelsError("二维码仍在生成，请稍后刷新")
            return state["qr"]

    def _update_session(self, session_id: str, **values) -> None:
        with self._lock:
            state = self._sessions.get(session_id)
            if state:
                state.update(values, updated_at=datetime.utcnow())

    def _update_capture(self, session_id: str, image: bytes, **values) -> bool:
        """Store a browser capture only when its bytes actually changed.

        The authorization UI polls frequently. Keeping a monotonic revision lets
        the frontend retain the currently painted image instead of replacing it
        with the same screenshot every poll, which caused visible flashing.
        """
        digest = hashlib.sha256(image).hexdigest() if image else ""
        changed = False
        with self._lock:
            state = self._sessions.get(session_id)
            if not state:
                return False
            if image and digest != str(state.get("capture_hash") or ""):
                state["qr"] = image
                state["capture_hash"] = digest
                state["capture_revision"] = int(state.get("capture_revision") or 0) + 1
                changed = True
            state.update(values, updated_at=datetime.utcnow())
        return changed

    def _capture_needed(self, session_id: str, mode: str) -> bool:
        with self._lock:
            state = self._sessions.get(session_id)
            return bool(
                state
                and (
                    not state.get("qr")
                    or str(state.get("capture_mode") or "qr") != mode
                )
            )

    @staticmethod
    def _capture_qr_image(page) -> bytes:
        """Capture the QR element instead of shrinking the entire login page."""
        selectors = (
            '[class*="qrcode" i] canvas',
            '[class*="qrcode" i] img',
            '[class*="qr-code" i] canvas',
            '[class*="qr-code" i] img',
            '[class*="qrcode" i]',
            '[class*="qr-code" i]',
            'img[src^="data:image"]',
            'canvas',
            'img',
        )
        candidates: list[tuple[int, float, object]] = []
        for frame in page.frames:
            for priority, selector in enumerate(selectors):
                try:
                    matches = frame.locator(selector)
                    for index in range(min(matches.count(), 12)):
                        node = matches.nth(index)
                        if not node.is_visible():
                            continue
                        box = node.bounding_box()
                        if not box:
                            continue
                        width = float(box.get("width") or 0)
                        height = float(box.get("height") or 0)
                        ratio = width / height if height else 0
                        if min(width, height) < 72 or max(width, height) > 520 or not 0.72 <= ratio <= 1.38:
                            continue
                        candidates.append((priority, -(width * height), node))
                except Exception:
                    continue
        for _, __, node in sorted(candidates, key=lambda item: (item[0], item[1])):
            try:
                image = node.screenshot(type="png")
                if image and len(image) > 256:
                    return image
            except Exception:
                continue
        return page.screenshot(full_page=True)

    @staticmethod
    def _account_choice_visible(page) -> bool:
        pattern = re.compile("选择视频号登录|使用其他账号登录|请选择视频号")
        for frame in page.frames:
            try:
                locator = frame.get_by_text(pattern).first
                if locator.count() and locator.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _scroll_account_choice(page, mouse_x: float, mouse_y: float, delta_y: float) -> bool:
        """Scroll the account picker itself before falling back to page wheel.

        Video Channels renders the picker as a nested fixed panel. A generic
        page wheel can leave that panel untouched, so first find the smallest
        visible scroll container under the pointer and update its scrollTop.
        """
        payload = {"x": float(mouse_x), "y": float(mouse_y), "delta": float(delta_y)}
        script = r"""
        ({ x, y, delta }) => {
          const seen = new Set();
          const candidates = [];
          const add = (node, priority) => {
            if (!(node instanceof HTMLElement) || seen.has(node)) return;
            seen.add(node);
            const rect = node.getBoundingClientRect();
            if (rect.width < 40 || rect.height < 40 || rect.bottom < 0 || rect.right < 0) return;
            if (node.scrollHeight <= node.clientHeight + 4) return;
            const contains = x >= rect.left && x <= rect.right && y >= rect.top && y <= rect.bottom;
            candidates.push({ node, priority: contains ? priority : priority + 1000, area: rect.width * rect.height });
          };
          for (const hit of document.elementsFromPoint(x, y)) {
            let node = hit;
            let depth = 0;
            while (node && node !== document.documentElement) {
              add(node, depth++);
              node = node.parentElement;
            }
          }
          for (const node of document.querySelectorAll('*')) add(node, 200);
          candidates.sort((a, b) => a.priority - b.priority || a.area - b.area);
          for (const { node } of candidates) {
            const before = node.scrollTop;
            node.scrollTop = Math.max(0, Math.min(node.scrollHeight - node.clientHeight, before + delta));
            node.dispatchEvent(new Event('scroll', { bubbles: true }));
            if (Math.abs(node.scrollTop - before) > 1) {
              return { moved: true, before, after: node.scrollTop };
            }
          }
          const target = document.elementFromPoint(x, y);
          if (target) target.dispatchEvent(new WheelEvent('wheel', { deltaY: delta, bubbles: true, cancelable: true }));
          return { moved: false };
        }
        """
        try:
            result = page.evaluate(script, payload) or {}
            if bool(result.get("moved")):
                return True
        except Exception:
            pass
        page.mouse.move(mouse_x, mouse_y)
        page.mouse.wheel(0, delta_y)
        return False

    def _pop_interaction(self, session_id: str) -> dict | None:
        with self._lock:
            state = self._sessions.get(session_id)
            commands = state.get("commands", []) if state else []
            return commands.pop(0) if commands else None

    def _authorization_worker(self, session_id: str) -> None:
        # Only one QR verification chain at a time in this worker. The short
        # queue does not hold a browser; waiting does not expire a fresh QR.
        self._update_session(session_id, status="waiting_slot", message="正在等待安全登录通道，请稍候")
        if not self._authorization_slot.acquire(timeout=30):
            self._update_session(session_id, status="failed", message="其他扫码授权正在进行，请稍后重新扫码", qr=b"", interaction_required=False)
            return
        try:
            self._update_session(session_id, expires_at=datetime.utcnow() + timedelta(seconds=240))
            self._run_authorization_session(session_id)
        finally:
            self._authorization_slot.release()

    def _run_authorization_session(self, session_id: str) -> None:
        from playwright.sync_api import sync_playwright

        with self._lock:
            state = dict(self._sessions[session_id])
        browser = None
        phase = "open_qr"
        try:
            self.auth_root.mkdir(parents=True, exist_ok=True)
            os.chmod(self.auth_root, 0o700)
            with sync_playwright() as playwright:
                self._require_browser_capacity()
                browser = playwright.chromium.launch(
                    headless=True,
                    args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                page = context.new_page()
                page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                self._update_session(session_id, status="waiting_scan", message="请使用微信扫描二维码并确认登录")
                deadline = time.monotonic() + 240
                while time.monotonic() < deadline:
                    if "/platform" in page.url and "/login" not in page.url:
                        phase = "validate_scan"
                        self._update_session(session_id, status="verifying", message="已完成扫码，正在校验账号并保存登录状态", interaction_required=False, account_choices=[], account_choice_targets={})
                        page.goto(PLATFORM_URL, wait_until="domcontentloaded", timeout=60000)
                        page.wait_for_timeout(2500)
                        account_id = str(uuid4())
                        owner_dir = self.auth_root / self._safe_owner(state["owner_number"])
                        owner_dir.mkdir(parents=True, exist_ok=True)
                        os.chmod(owner_dir, 0o700)
                        auth_file = owner_dir / f"{account_id}.json"
                        self._persist_context_state(context, auth_file)

                        # A redirect into /platform is not sufficient evidence:
                        # WeChat can leave the login QR inside a child frame. Reopen
                        # the saved state in a clean context before activating it.
                        validation_context = browser.new_context(
                            storage_state=str(auth_file), viewport={"width": 1280, "height": 900}, locale="zh-CN",
                        )
                        validation_page = validation_context.new_page()
                        try:
                            self._goto_creator_page(validation_page, PLATFORM_URL, timeout_ms=60000)
                            validation_page.wait_for_timeout(1800)
                            if self._authorization_expired(validation_page) or "/platform" not in str(validation_page.url or ""):
                                raise ChannelsError("扫码后授权校验未通过，请重新扫码并确认登录", "authorization")
                            self._persist_context_state(validation_context, auth_file)
                        except Exception:
                            try:
                                validation_context.close()
                            except Exception:
                                pass
                            try:
                                auth_file.unlink()
                            except OSError:
                                pass
                            raise
                        nickname = "已授权视频号"
                        avatar_url = ""
                        external_account_id = ""
                        nickname_node = validation_page.locator("div.finder-card h2.finder-nickname").first
                        if nickname_node.count():
                            nickname = (nickname_node.inner_text(timeout=3000) or nickname).strip()
                        avatar_node = validation_page.locator("div.finder-card img.avatar").first
                        if avatar_node.count():
                            avatar_url = avatar_node.get_attribute("src") or ""
                        try:
                            identity = read_authenticated_identity(validation_page)
                            external_account_id = str(identity.get("external_account_id") or "")[:160]
                        except ChannelsInternalApiError:
                            # The browser transport remains available when the
                            # private identity endpoint changes. The first
                            # successful internal preflight will backfill it.
                            external_account_id = ""
                        validation_context.close()
                        # The QR browser previously stayed alive while a second
                        # Chrome process verified persistence. Under pids.max=256
                        # that exhausted threads and also broke ordinary API reads.
                        self._close_scan_browser(browser, context)
                        browser = None
                        context = None
                        phase = "verify_profile"
                        # A persistent profile must survive an actual close and
                        # reopen, and return the same stable identity, before the
                        # browserless credential bundle can become active.
                        verified, encrypted, session_cipher = self._verify_persistent_authorization(
                            playwright, auth_file, external_account_id,
                        )
                        external_account_id = verified["external_account_id"]
                        nickname = verified.get("nickname") or nickname
                        phase = "save_account"
                        account_id, resumed_count = self._save_authorization(
                            state, account_id, auth_file, nickname, avatar_url, external_account_id,
                            encrypted, session_cipher,
                        )
                        self._update_session(
                            session_id,
                            status="authorized",
                            message=(
                                f"已授权：{nickname}；已自动恢复 {resumed_count} 条发布或回流任务"
                                if resumed_count else f"已授权：{nickname}"
                            ),
                            account_id=account_id,
                            qr=b"",
                            interaction_required=False,
                            capture_mode="qr",
                            account_choices=[],
                            account_choice_targets={},
                        )
                        return
                    if self._account_choice_visible(page):
                        choices, choice_targets = self._account_choice_items(page)
                        command = self._pop_interaction(session_id)
                        if command:
                            viewport = page.viewport_size or {"width": 1280, "height": 900}
                            mouse_x = float(command.get("x") or 0) * float(viewport["width"])
                            mouse_y = float(command.get("y") or 0) * float(viewport["height"])
                            page.mouse.move(mouse_x, mouse_y)
                            if command.get("type") == "scroll":
                                self._scroll_account_choice(
                                    page,
                                    mouse_x,
                                    mouse_y,
                                    float(command.get("delta_y") or 0),
                                )
                                # scrollTop changes synchronously; a short settle
                                # avoids the old 700ms penalty per wheel step.
                                page.wait_for_timeout(320)
                                refreshed_choices, refreshed_targets = self._account_choice_items(page)
                                if refreshed_choices:
                                    self._update_session(
                                        session_id,
                                        status="waiting_choice",
                                        message="请选择要登录的视频号",
                                        interaction_required=True,
                                        capture_mode="account_list",
                                        account_choices=refreshed_choices,
                                        account_choice_targets=refreshed_targets,
                                    )
                                else:
                                    self._update_capture(
                                        session_id,
                                        page.screenshot(full_page=False),
                                        status="waiting_choice",
                                        message="账号列表已滚动，请继续选择",
                                        interaction_required=True,
                                        capture_mode="account_choice",
                                        account_choices=[],
                                        account_choice_targets={},
                                    )
                                continue
                            if command.get("type") == "select":
                                clicked, failure_code = self._click_account_choice(
                                    page, dict(command.get("target") or {}),
                                )
                                if not clicked:
                                    logger.warning(
                                        "account_choice_click_failed session_id=%s code=%s",
                                        session_id, failure_code,
                                    )
                                    refreshed_choices, refreshed_targets = self._account_choice_items(page)
                                    if refreshed_choices:
                                        self._update_session(
                                            session_id,
                                            status="waiting_choice",
                                            message="账号候选项已失效，请重新选择",
                                            interaction_required=True,
                                            capture_mode="account_list",
                                            account_choices=refreshed_choices,
                                            account_choice_targets=refreshed_targets,
                                        )
                                    else:
                                        logger.info(
                                            "account_choice_fallback session_id=%s reason=%s",
                                            session_id, failure_code,
                                        )
                                        self._update_capture(
                                            session_id,
                                            page.screenshot(full_page=False),
                                            status="waiting_choice",
                                            message="账号列表结构已变化，请在原始页面中选择",
                                            interaction_required=True,
                                            capture_mode="account_choice",
                                            account_choices=[],
                                            account_choice_targets={},
                                        )
                                    continue
                                logger.info(
                                    "account_choice_selected session_id=%s choice_id=%s",
                                    session_id, str(command.get("choice_id") or ""),
                                )
                            else:
                                page.mouse.click(mouse_x, mouse_y)
                            self._update_session(
                                session_id,
                                status="selecting_account",
                                message="已提交选择，正在等待视频号确认登录",
                                interaction_required=False,
                                account_choices=[],
                                account_choice_targets={},
                            )
                            click_deadline = time.monotonic() + 8
                            while time.monotonic() < click_deadline:
                                if not self._account_choice_visible(page):
                                    break
                                page.wait_for_timeout(250)
                            if self._account_choice_visible(page):
                                refreshed_choices, refreshed_targets = self._account_choice_items(page)
                                if refreshed_choices:
                                    self._update_session(
                                        session_id,
                                        status="waiting_choice",
                                        message="未完成选择，请重新选择视频号",
                                        interaction_required=True,
                                        capture_mode="account_list",
                                        account_choices=refreshed_choices,
                                        account_choice_targets=refreshed_targets,
                                    )
                                else:
                                    self._update_capture(
                                        session_id,
                                        page.screenshot(full_page=False),
                                        status="waiting_choice",
                                        message="未完成选择，请点击账号名称或头像区域后重试",
                                        interaction_required=True,
                                        capture_mode="account_choice",
                                        account_choices=[],
                                        account_choice_targets={},
                                    )
                            continue
                        logger.info(
                            "account_choice_page_detected session_id=%s frame_count=%s",
                            session_id, len(page.frames),
                        )
                        if choices:
                            self._update_session(
                                session_id,
                                status="waiting_choice",
                                message="请选择要登录的视频号",
                                interaction_required=True,
                                capture_mode="account_list",
                                account_choices=choices,
                                account_choice_targets=choice_targets,
                            )
                        else:
                            logger.info(
                                "account_choice_fallback session_id=%s reason=empty_choices",
                                session_id,
                            )
                            fallback_values = {
                                "status": "waiting_choice",
                                "message": "请选择要登录的视频号",
                                "interaction_required": True,
                                "capture_mode": "account_choice",
                                "account_choices": [],
                                "account_choice_targets": {},
                            }
                            if self._capture_needed(session_id, "account_choice"):
                                self._update_capture(session_id, page.screenshot(full_page=False), **fallback_values)
                            else:
                                self._update_session(session_id, **fallback_values)
                        page.wait_for_timeout(260)
                        continue
                    self._update_capture(
                        session_id,
                        self._capture_qr_image(page),
                        status="waiting_scan",
                        message="请使用微信扫描二维码并确认登录",
                        interaction_required=False,
                        capture_mode="qr",
                        account_choices=[],
                        account_choice_targets={},
                    )
                    page.wait_for_timeout(2000)
                self._update_session(
                    session_id,
                    status="expired",
                    message="二维码已过期，请重新发起授权",
                    interaction_required=False,
                    account_choices=[],
                    account_choice_targets={},
                )
        except Exception as error:
            location = traceback.extract_tb(error.__traceback__)[-1] if error.__traceback__ else None
            logger.warning("channels_authorization_failed phase=%s type=%s location=%s trace=%s",
                           phase, type(error).__name__,
                           f"{Path(location.filename).name}:{location.lineno}" if location else "unknown",
                           session_id[:8])
            self._update_session(
                session_id,
                status="failed",
                message=(str(error)[:240] if isinstance(error, (ChannelsCredentialsError, ChannelsError, ChannelsInternalApiError))
                         else "视频号授权校验未完成，请重新扫码或联系管理员"),
                interaction_required=False,
                qr=b"",
                capture_mode="qr",
                account_choices=[],
                account_choice_targets={},
                commands=[],
            )
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    @staticmethod
    def _close_scan_browser(browser, context) -> None:
        # Close before launching a persistent browser, not only in worker finally.
        try:
            context.close()
        finally:
            browser.close()

    def _verify_persistent_authorization(self, playwright, auth_file, expected):
        identity = {}
        encrypted = session_cipher = ""
        for _ in range(2):
            browser = context = None
            try:
                self._require_browser_capacity()
                browser, context, persistent = self._launch_account_session(playwright, auth_file, allow_fallback=False)
                if not persistent:
                    raise ChannelsError("登录目录无法持久保存，请联系管理员后重新扫码", "authorization")
                page = context.new_page()
                self._goto_creator_page(page, PLATFORM_URL, timeout_ms=60000)
                if self._authorization_expired(page):
                    raise ChannelsError("重新打开登录目录后授权失效，请重新扫码", "authorization")
                identity = read_authenticated_identity(page)
                actual = identity.get("external_account_id") or ""
                if not actual or (expected and actual != expected):
                    raise ChannelsError("扫码账号校验不一致，未保存授权，请重新扫码", "authorization")
                expected = actual
                encrypted, session_cipher = encrypt_credentials(
                    self.auth_root, context.cookies(), str(page.evaluate("navigator.userAgent")),
                )
            finally:
                self._close_account_session(browser, context, auth_file)
        return identity, encrypted, session_cipher

    def _save_authorization(self, state, account_id, auth_file, nickname, avatar_url, external_id, encrypted, session_cipher):
        with self._browser_lock(account_id, external_account_id=external_id):
            with SessionLocal() as db:
                account = db.scalar(select(ChannelsAccount).where(
                    ChannelsAccount.owner_number == state["owner_number"],
                    ChannelsAccount.external_account_id == external_id,
                ).order_by(ChannelsAccount.created_at.asc()))
                now = datetime.utcnow()
                if account is None:
                    account = ChannelsAccount(id=account_id, owner_number=state["owner_number"], created_at=now)
                    db.add(account)
                account.owner_name = state["owner_name"]
                account.nickname = nickname[:255]
                account.avatar_url = avatar_url[:2048]
                account.external_account_id = external_id
                account.auth_file = str(auth_file)
                account.status = "active"
                account.message = "授权可用，已保存加密直连凭据"
                account.channel_cookies_ciphertext = encrypted
                account.session_cookie_ciphertext = session_cipher
                account.cookies_updated_at = account.last_verified_at = now
                account.authorized_at = account.updated_at = now
                db.flush()
                resumed = self._resume_tasks_after_authorization(db, account)
                saved_id = account.id
                db.commit()
                return saved_id, resumed

    @staticmethod
    def _resume_tasks_after_authorization(db, account: ChannelsAccount) -> int:
        """Move tasks off expired copies of the same owned Channels account.

        A fresh QR authorization creates a new encrypted browser state file.
        Historical deliveries must follow that new account row; otherwise both
        public confirmation and daily metrics remain permanently attached to
        the expired file even after the colleague has scanned again.
        """
        expired_ids = list(db.scalars(
            select(ChannelsAccount.id).where(
                ChannelsAccount.id != account.id,
                ChannelsAccount.owner_number == account.owner_number,
                (ChannelsAccount.external_account_id == account.external_account_id)
                if account.external_account_id else (
                    (ChannelsAccount.nickname == account.nickname) & (ChannelsAccount.external_account_id == "")
                ),
                ChannelsAccount.status == "expired",
            )
        ).all())
        tasks = db.scalars(
            select(ChannelsDelivery).where(
                ChannelsDelivery.account_id.in_([account.id, *expired_ids]),
                ChannelsDelivery.deleted_at.is_(None),
                ChannelsDelivery.status.in_({"failed", "submitted", "success"}),
            )
        ).all()
        now = datetime.utcnow()
        resumed = 0
        for task in tasks:
            task.account_id = account.id
            task.account_name = account.nickname
            error_text = f"{task.error_message or ''} {task.message or ''}"
            authorization_failure = (
                task.status == "failed"
                and task.submitted_at is None
                and task.publish_clicked_at is None
                and not task.platform_content_id
                and (
                    task.failure_stage in {"authorization", "open_publish_page"}
                    or "授权已失效" in error_text
                    or "授权已过期" in error_text
                )
            )
            if authorization_failure:
                task.status = "pending"
                task.failure_stage = ""
                task.error_message = ""
                task.message = "授权已恢复，等待继续发布原视频"
            elif task.status == "submitted":
                task.failure_stage = "confirm_publish"
                task.message = "授权已恢复，等待继续核验公开展示状态"
                if "授权已" in (task.metrics_message or ""):
                    task.metrics_message = "授权已恢复，等待继续回流"
            elif task.status == "success" and "授权已" in (task.metrics_message or ""):
                task.metrics_message = "授权已恢复，等待继续回流"
            task.updated_at = now
            resumed += 1
        return resumed

    def _validated_auth_file(self, account: ChannelsAccount) -> Path:
        root = self.auth_root.resolve()
        auth_file = Path(account.auth_file).resolve()
        if root not in auth_file.parents or not auth_file.is_file():
            raise ChannelsError("该视频号授权文件已失效，请重新扫码")
        return auth_file

    @staticmethod
    def _authorization_expired(page) -> bool:
        """Detect a logged-out creator page even when its URL is unchanged."""
        try:
            if "/login" in str(page.url or "").lower():
                return True
        except Exception:
            pass
        pattern = re.compile(r"微信扫码登录|扫码登录|登录视频号助手|请使用微信扫码")
        try:
            roots = [page, *[frame for frame in page.frames if frame != page.main_frame]]
        except Exception:
            roots = [page]
        for root in roots:
            try:
                frame_url = str(root.url or "").lower()
            except Exception:
                frame_url = ""
            if (
                "/login" in frame_url
                or "open.weixin.qq.com/connect/qrconnect" in frame_url
                or "login.weixin.qq.com" in frame_url
            ):
                return True
        for root in roots:
            try:
                marker = root.get_by_text(pattern).first
                if marker.count() and marker.is_visible():
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _transient_navigation_error(error: Exception) -> bool:
        """Return true when WeChat replaced the page while Playwright was reading it."""
        raw = str(error or "").lower()
        return any(
            marker in raw
            for marker in (
                "execution context was destroyed",
                "frame was detached",
                "navigation",
                "page.goto: timeout",
                "target page, context or browser has been closed",
            )
        )

    @staticmethod
    def _wait_for_page_settle(page, timeout_ms: int = 15000) -> None:
        """Best-effort settle wait; creator pages can keep loading after useful DOM exists."""
        try:
            page.wait_for_load_state("domcontentloaded", timeout=timeout_ms)
        except Exception:
            pass
        try:
            page.wait_for_timeout(900)
        except Exception:
            pass

    @classmethod
    def _goto_creator_page(cls, page, url: str, timeout_ms: int = 60000) -> None:
        """Navigate through WeChat redirects without waiting on long-lived page requests."""
        last_error: Exception | None = None
        for attempt in range(2):
            try:
                page.goto(url, wait_until="commit", timeout=timeout_ms)
            except Exception as error:
                last_error = error
                try:
                    current_url = str(page.url or "")
                except Exception:
                    current_url = ""
                if not cls._transient_navigation_error(error) or "channels.weixin.qq.com" not in current_url:
                    if attempt == 1:
                        raise
                    continue
            cls._wait_for_page_settle(page)
            return
        if last_error:
            raise last_error

    @classmethod
    def _goto_creator_post_list(cls, page) -> None:
        """The initial deep link can redirect home while account auth hydrates."""
        cls._goto_creator_page(page, POST_LIST_URL, timeout_ms=60000)
        marker = page.get_by_text('视频管理', exact=True)
        if cls._first_visible([marker], timeout_ms=5000):
            return
        # Use the real navigation after hydration, never confuse the homepage
        # (or an unloaded micro frontend) with an empty creator list.
        content = cls._first_visible([page.get_by_role('link', name='内容管理', exact=True)], timeout_ms=5000)
        if content:
            content.click()
        video = cls._first_visible([page.get_by_role('link', name='视频', exact=True)], timeout_ms=5000)
        if video:
            video.click()
        if not cls._first_visible([marker], timeout_ms=15000):
            raise ChannelsError('视频号作品列表尚未加载完成；保留待核验状态，不会重复发布', 'readback')

    @contextmanager
    def _browser_lock(self, account_id: str, *, external_account_id: str = ""):
        # Browser readback and direct publishing share the same external-account
        # boundary, including legacy duplicate authorization rows.
        key = external_account_id
        if not key:
            with SessionLocal() as db:
                row = db.get(ChannelsAccount, account_id)
                key = (row.external_account_id if row else "") or account_id
        digest = hashlib.sha256(key.encode()).hexdigest()
        with self._lock:
            lock = self._browser_locks.setdefault(digest, threading.Lock())
        with lock:
            if os.name != "posix":
                yield
                return
            import fcntl
            lock_dir = self.auth_root / ".account-locks"
            lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            with (lock_dir / (digest + ".lock")).open("a") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _download_with_resume(
        url: str,
        destination: Path,
        stage: str,
        report: Callable[[str, str], None],
        label: str,
        attempts: int = 4,
    ) -> None:
        """Download an OSS object with bounded range-resume and progress heartbeats."""
        last_error: Exception | None = None
        last_report_at = 0.0
        last_reported_bytes = -1
        for attempt in range(1, max(1, attempts) + 1):
            offset = destination.stat().st_size if destination.exists() else 0
            headers = {"Accept-Encoding": "identity", "Connection": "keep-alive"}
            if offset:
                headers["Range"] = f"bytes={offset}-"
            try:
                with requests.get(url, stream=True, timeout=(20, 120), headers=headers) as response:
                    if response.status_code == 416 and offset:
                        total_match = re.search(r"\*/(\d+)", response.headers.get("Content-Range", ""))
                        if total_match and int(total_match.group(1)) == offset:
                            return
                    response.raise_for_status()
                    resumed = bool(offset and response.status_code == 206)
                    if offset and not resumed:
                        offset = 0
                    content_length = int(response.headers.get("Content-Length") or 0)
                    expected_size = offset + content_length if content_length else 0
                    mode = "ab" if resumed else "wb"
                    with destination.open(mode) as output:
                        for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                            if not chunk:
                                continue
                            output.write(chunk)
                            current_size = destination.stat().st_size
                            now = time.monotonic()
                            if (
                                current_size - last_reported_bytes >= 16 * 1024 * 1024
                                or now - last_report_at >= 8
                            ):
                                if expected_size:
                                    percent = min(99, int(current_size * 100 / expected_size))
                                    message = f"正在读取{label}：{percent}%"
                                else:
                                    message = f"正在读取{label}：{current_size / 1024 / 1024:.0f} MB"
                                if attempt > 1:
                                    message += f"（断点续传第 {attempt} 次）"
                                report(stage, message)
                                last_report_at = now
                                last_reported_bytes = current_size
                    final_size = destination.stat().st_size
                    if expected_size and final_size < expected_size:
                        raise IOError(f"下载提前结束：{final_size}/{expected_size}")
                    if final_size <= 0:
                        raise IOError("下载结果为空")
                    report(stage, f"{label}读取完成：{final_size / 1024 / 1024:.1f} MB")
                    return
            except Exception as error:
                last_error = error
                if attempt >= attempts:
                    break
                report(stage, f"{label}连接中断，正在从已完成位置续传（第 {attempt + 1}/{attempts} 次）")
                time.sleep(min(6, attempt * 1.5))
        raise ChannelsError(f"{label}下载多次中断，请稍后重试：{str(last_error)[:160]}", stage) from last_error

    @staticmethod
    def _transient_upload_error(error: object) -> bool:
        value = str(error or "").lower()
        return any(token in value for token in (
            "网络出错", "网络错误", "网络异常", "连接中断", "加载失败", "重新上传",
            "upload failed", "connection", "net::", "timeout", "timed out",
        ))

    @classmethod
    def _dismiss_non_publish_dialogs(cls, page, publish_root, timeout_ms: int = 1800) -> int:
        """Close product/notice overlays without ever acknowledging publication."""
        closed = 0
        deadline = time.monotonic() + max(0, timeout_ms) / 1000
        roots = cls._cover_roots(page, publish_root)
        while time.monotonic() <= deadline:
            changed = False
            for root in roots:
                try:
                    dialogs = root.locator('[role="dialog"], .weui-desktop-dialog, .sale-visible-dialog')
                    for index in range(min(dialogs.count(), 12)):
                        dialog = dialogs.nth(index)
                        if not dialog.is_visible():
                            continue
                        text_value = re.sub(r"\s+", " ", str(dialog.inner_text() or "")).strip()
                        if re.search(r"确认发表|确认发布|继续发表|继续发布", text_value):
                            continue
                        if not re.search(r"商品|链接|橱窗|营销|选择|提示|知道了|网络|重试|封面", text_value):
                            continue
                        button = cls._first_visible([
                            dialog.get_by_role("button", name=re.compile(r"^(?:确定|确认|完成|知道了|我知道了|继续|关闭|重试|重新上传)$")),
                            dialog.get_by_text(re.compile(r"^(?:确定|确认|完成|知道了|我知道了|继续|关闭|重试|重新上传)$"), exact=True),
                        ], timeout_ms=150)
                        if not button:
                            continue
                        button.click(timeout=3000)
                        closed += 1
                        changed = True
                        page.wait_for_timeout(250)
                except Exception:
                    continue
            if not changed:
                break
        return closed

    @staticmethod
    def _upload_progress_percent(page) -> int | None:
        try:
            value = page.evaluate(r"""
                () => {
                  const visible = node => {
                    const box = node.getBoundingClientRect();
                    const style = getComputedStyle(node);
                    return box.width > 1 && box.height > 1 && style.display !== 'none' && style.visibility !== 'hidden';
                  };
                  let best = -1;
                  for (const node of document.querySelectorAll('[class*="progress"], [class*="upload"], [role="progressbar"]')) {
                    if (!visible(node)) continue;
                    const values = [node.innerText, node.textContent, node.getAttribute('aria-valuenow')];
                    for (const raw of values) {
                      const match = String(raw || '').match(/(?:^|\s)(\d{1,3})\s*%/);
                      if (match) best = Math.max(best, Math.min(100, Number(match[1])));
                    }
                  }
                  return best >= 0 ? best : null;
                }
            """)
            return int(value) if value is not None else None
        except Exception:
            return None

    def _capture_page_diagnostic(self, page, label: str) -> str:
        """Keep a bounded, protected screenshot and structural trace for support."""
        trace_id = f"{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
        diagnostic_root = (self.auth_root / "diagnostics").resolve()
        auth_root = self.auth_root.resolve()
        if auth_root not in diagnostic_root.parents:
            return ""
        try:
            diagnostic_root.mkdir(parents=True, exist_ok=True)
            os.chmod(diagnostic_root, 0o700)
            cutoff = time.time() - 7 * 24 * 60 * 60
            for stale in diagnostic_root.glob("channels-*"):
                try:
                    if stale.is_file() and stale.stat().st_mtime < cutoff:
                        stale.unlink()
                except OSError:
                    continue
            stem = diagnostic_root / f"channels-{trace_id}-{self._safe_owner(label)}"
            screenshot_path = stem.with_suffix(".png")
            metadata_path = stem.with_suffix(".json")
            page.screenshot(path=str(screenshot_path), full_page=False)
            frame_urls = []
            for frame in page.frames:
                try:
                    frame_urls.append(str(frame.url or "").split("?", 1)[0][:500])
                except Exception:
                    continue
            buttons = []
            try:
                buttons = [
                    re.sub(r"\s+", " ", str(value or "")).strip()[:120]
                    for value in page.locator("button").all_inner_texts()
                    if str(value or "").strip()
                ][:40]
            except Exception:
                pass
            metadata_path.write_text(json.dumps({
                "trace_id": trace_id,
                "captured_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
                "label": label,
                "url": str(page.url or "").split("?", 1)[0][:500],
                "frame_urls": frame_urls[:40],
                "visible_buttons": buttons,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
            os.chmod(screenshot_path, 0o600)
            os.chmod(metadata_path, 0o600)
            return trace_id
        except Exception as error:
            logger.warning("channels_diagnostic_capture_failed error=%s", str(error)[:240])
            return ""

    @staticmethod
    def _publish_root_and_input(page):
        roots = [page]
        roots.extend(frame for frame in page.frames if frame != page.main_frame)
        for root in roots:
            locator = root.locator('input[type="file"][accept*="video"], input[type="file"]').first
            if locator.count():
                return root, locator
        return page, page.locator('input[type="file"][accept*="video"], input[type="file"]').first

    def _open_publish_page(self, page):
        """Open the real creator page even when WeChat redirects the deep link home."""
        last_error = ""
        for attempt in range(3):
            try:
                # Enter from the creator home first. The direct /post/create
                # route intermittently returns an empty micro-frontend frame.
                self._goto_creator_page(page, PLATFORM_URL, timeout_ms=60000)
                if self._authorization_expired(page):
                    raise ChannelsError("视频号授权已过期，请重新扫码授权", "open_publish_page")
                deadline = time.monotonic() + 65
                clicked_entry = False
                direct_fallback = False
                started_at = time.monotonic()
                while time.monotonic() < deadline:
                    try:
                        if self._authorization_expired(page):
                            raise ChannelsError("视频号授权已过期，请重新扫码授权", "open_publish_page")
                        publish_root, file_input = self._publish_root_and_input(page)
                        if file_input.count():
                            file_input.wait_for(state="attached", timeout=10000)
                            return publish_root, file_input
                        if not clicked_entry:
                            for root in [page, *[frame for frame in page.frames if frame != page.main_frame]]:
                                publish_entry = self._first_visible([
                                    root.locator("button.weui-desktop-btn").filter(has_text=re.compile(r"^发表视频$")),
                                    root.get_by_role("button", name=re.compile(r"^发表视频$")),
                                    root.get_by_text("发表视频", exact=True),
                                ], timeout_ms=250)
                                if publish_entry:
                                    publish_entry.click()
                                    clicked_entry = True
                                    break
                        if not clicked_entry and not direct_fallback and time.monotonic() - started_at >= 12:
                            # Bounded compatibility fallback for accounts whose
                            # home card is hidden by platform experiments.
                            self._goto_creator_page(page, PUBLISH_URL, timeout_ms=60000)
                            direct_fallback = True
                    except ChannelsError:
                        raise
                    except Exception as error:
                        last_error = str(error)
                        if self._transient_navigation_error(error):
                            self._wait_for_page_settle(page, timeout_ms=10000)
                            continue
                        raise
                    page.wait_for_timeout(1000)
                trace_id = self._capture_page_diagnostic(page, "open-publish-page")
                suffix = f"（诊断编号 {trace_id}）" if trace_id else ""
                raise ChannelsError(f"视频号发布页在 65 秒内未生成上传控件{suffix}，请稍后重试", "open_publish_page")
            except ChannelsError:
                raise
            except Exception as error:
                last_error = str(error)
                if self._transient_navigation_error(error):
                    self._wait_for_page_settle(page, timeout_ms=10000)
                    if self._authorization_expired(page):
                        raise ChannelsError("视频号授权已过期，请重新扫码授权", "open_publish_page")
                if attempt < 2:
                    page.wait_for_timeout(1200)
                    continue
        detail = "视频号平台暂未进入发布页，请刷新授权后重试"
        if last_error and not self._transient_navigation_error(RuntimeError(last_error)) and "Timeout" not in last_error:
            detail = f"{detail}：{last_error[:160]}"
        raise ChannelsError(detail, "open_publish_page")

    @staticmethod
    def _parse_product_row(text_value: str, image_url: str = "") -> dict | None:
        lines = [line.strip() for line in (text_value or "").splitlines() if line.strip()]
        if not lines:
            return None
        identifier = ""
        for index, line in enumerate(lines):
            if line == "ID" and index + 1 < len(lines) and re.fullmatch(r"\d{6,}", lines[index + 1]):
                identifier = lines[index + 1]
                break
            match = re.search(r"(?:^|\b)ID\s*[:：]?\s*(\d{6,})(?:\b|$)", line, re.I)
            if match:
                identifier = match.group(1)
                break
        if not identifier:
            identifier = next((line for line in lines[1:] if re.fullmatch(r"\d{8,}", line)), "")
        if not identifier:
            return None
        price = None
        for line in lines:
            match = re.search(r"[¥￥]\s*([\d,.]+)", line)
            if match:
                try:
                    price = float(match.group(1).replace(",", ""))
                except ValueError:
                    pass
                break
        name = next((line for line in lines if line not in {"ID", "自营"} and line != identifier and not line.startswith(("¥", "￥"))), lines[0])
        return {"id": identifier, "name": name, "price_yuan": price, "image_url": image_url}

    @staticmethod
    def _first_visible(candidates: list, timeout_ms: int = 20000):
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while time.monotonic() <= deadline:
            for locator in candidates:
                try:
                    for index in range(min(locator.count(), 12)):
                        candidate = locator.nth(index)
                        if candidate.is_visible():
                            return candidate
                except Exception:
                    continue
            time.sleep(0.25)
        return None

    @staticmethod
    def _product_search_input(page):
        return page.locator(
            'input[placeholder*="商品名称/编码搜索"], '
            'input[placeholder*="商品名称"], '
            'input[placeholder*="商品编码"], '
            'input[placeholder*="商品ID"], '
            'input[placeholder*="商品 ID"]'
        )

    @classmethod
    def _open_product_dialog(cls, page) -> None:
        search = cls._product_search_input(page)
        if cls._first_visible([search], timeout_ms=500):
            return

        link_trigger = cls._first_visible([
            page.get_by_text("选择链接", exact=True),
            page.get_by_text("添加链接", exact=True),
            page.get_by_text(re.compile(r"选择.*链接|添加.*链接")),
        ], timeout_ms=30000)
        if not link_trigger:
            raise ChannelsError("视频号发布页未显示“选择链接”入口，请稍后重试", "product_dialog")
        link_trigger.click()

        product_option = cls._first_visible([
            page.get_by_text("商品", exact=True),
            page.get_by_role("button", name=re.compile(r"^商品$")),
            page.locator('[role="menuitem"], [role="option"]').filter(has_text=re.compile(r"^商品$")),
        ], timeout_ms=20000)
        if not product_option:
            raise ChannelsError("视频号链接菜单未加载商品选项，系统可稍后自动重试", "product_dialog")
        product_option.click()

        if cls._first_visible([search], timeout_ms=1500):
            return
        choose_product = cls._first_visible([
            page.get_by_text("选择需要添加的商品", exact=True),
            page.get_by_text("选择商品", exact=True),
            page.get_by_text("添加商品", exact=True),
            page.get_by_text(re.compile(r"选择.*商品|添加.*商品")),
            page.get_by_role("button", name=re.compile(r"选择.*商品|添加.*商品")),
        ], timeout_ms=25000)
        if choose_product:
            choose_product.click()
        elif not cls._first_visible([search], timeout_ms=500):
            raise ChannelsError("视频号商品入口暂未加载完成，系统可稍后自动重试", "product_dialog")

        if not cls._first_visible([search], timeout_ms=30000):
            raise ChannelsError("视频号商品列表未加载完成，系统可稍后自动重试", "product_list")

    @staticmethod
    def _filter_products(page, keyword: str) -> None:
        if not keyword.strip():
            return
        search = ChannelsService._product_search_input(page).first
        search.fill(keyword.strip())
        filter_button = page.get_by_role("button", name="筛选", exact=True).last
        filter_button.click()
        page.wait_for_timeout(1200)

    @staticmethod
    def _button_visually_disabled(button) -> bool:
        """Account for Video Channels buttons disabled through CSS, not HTML."""
        if button.is_disabled():
            return True
        aria_disabled = (button.get_attribute("aria-disabled") or "").strip().lower()
        class_name = (button.get_attribute("class") or "").strip().lower()
        return (
            aria_disabled == "true"
            or "btn_disabled" in class_name
            or "is-disabled" in class_name
            or "ant-pagination-disabled" in class_name
        )

    @staticmethod
    def _normalized_short_title(title: str, filename: str) -> str:
        value = (title.strip() or Path(filename).stem).strip()
        if len(value) < 6:
            value = f"{value}视频素材"
        return value[:16]

    @classmethod
    def _fill_short_title(cls, page, publish_root, title: str, filename: str) -> str:
        """Write and read back the platform short title instead of silently
        allowing Video Channels to derive it from the uploaded filename.
        """
        expected = cls._normalized_short_title(title, filename)
        last_error: Exception | None = None
        for attempt in range(1, 4):
            cls._dismiss_non_publish_dialogs(page, publish_root, timeout_ms=900)
            field = cls._first_visible([
                publish_root.locator('input[placeholder*="短标题"]'),
                page.locator('input[placeholder*="短标题"]'),
                publish_root.locator('input[placeholder*="填写短标题"]'),
                page.locator('input[placeholder*="填写短标题"]'),
                publish_root.locator('input[maxlength="16"]'),
                page.locator('input[maxlength="16"]'),
            ], timeout_ms=15000)
            if not field:
                last_error = RuntimeError("短标题输入框未显示")
                page.wait_for_timeout(600 * attempt)
                continue
            try:
                field.scroll_into_view_if_needed(timeout=3000)
            except Exception:
                pass
            try:
                field.fill(expected, timeout=10000)
            except Exception as error:
                last_error = error
                # The creator page occasionally leaves an invisible overlay
                # above an otherwise editable input. Use the native value
                # setter only as a bounded fallback, then require exact readback.
                try:
                    field.evaluate(
                        """(element, value) => {
                          const descriptor = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                          descriptor.set.call(element, value);
                          element.dispatchEvent(new Event('input', { bubbles: true }));
                          element.dispatchEvent(new Event('change', { bubbles: true }));
                        }""",
                        expected,
                    )
                except Exception as fallback_error:
                    last_error = fallback_error
            page.wait_for_timeout(300)
            try:
                observed = str(field.input_value(timeout=3000) or "").strip()
            except Exception as error:
                last_error = error
                observed = ""
            if observed == expected:
                return expected
            last_error = RuntimeError(f"短标题回读为：{observed or '空'}")
            page.wait_for_timeout(600 * attempt)
        raise ChannelsError(
            f"短标题多次写入仍未能完整回读（期望：{expected}）：{str(last_error)[:160]}",
            "fill_content",
        ) from last_error

    @classmethod
    def _delivery_search_candidates(cls, title: str, filename: str) -> list[str]:
        """Return the values that can actually appear in the creator list.

        The publishing page writes a six-to-sixteen-character short title.
        Searching only the original (often much longer) task title made real
        posts invisible to readback and left them in ``submitted`` forever.
        """
        values = [
            cls._normalized_short_title(title, filename),
            title.strip()[:32],
            Path(filename).stem.strip()[:32],
        ]
        return list(dict.fromkeys(value for value in values if value))

    @classmethod
    def _find_delivery_in_creator_list(
        cls,
        page,
        candidates: list[str],
        platform_content_id: str = "",
        platform_export_id: str = "",
        max_pages: int = 20,
        excluded_platform_ids: set[str] | None = None,
    ):
        """Find a post across creator-list pages without treating absence as zero."""
        excluded_tokens = {
            token
            for value in (excluded_platform_ids or set())
            for token in cls._platform_identity_tokens(value)
        }
        identity_tokens = (
            cls._platform_identity_tokens(platform_content_id)
            | cls._platform_identity_tokens(platform_export_id)
        ) - excluded_tokens
        for page_index in range(max_pages):
            cls._wait_for_page_settle(page, timeout_ms=10000)
            for content_token in identity_tokens:
                try:
                    link = page.locator(f'a[href*="{content_token}"]').first
                    if link.count() and link.is_visible():
                        return link, content_token
                except Exception:
                    pass
            for candidate in candidates:
                try:
                    matches = page.get_by_text(candidate, exact=False)
                    for index in range(min(matches.count(), 20)):
                        node = matches.nth(index)
                        if node.is_visible():
                            try:
                                href = str(node.evaluate("node => (node.closest('a') || node.closest('tr,[class*=item],[class*=card],[class*=row]')?.querySelector('a'))?.href || ''"))
                            except Exception:
                                href = ""
                            if excluded_tokens and any(token and token in href for token in excluded_tokens):
                                continue
                            return node, candidate
                except Exception as error:
                    if cls._transient_navigation_error(error):
                        cls._wait_for_page_settle(page, timeout_ms=10000)
                        continue
                    raise
            if page_index + 1 >= max_pages:
                break
            try:
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(350)
            except Exception:
                pass
            next_button = cls._first_visible([
                page.get_by_role("button", name=re.compile(r"下一页|下页")),
                page.locator('[aria-label*="下一页"], [title*="下一页"]'),
                page.locator('.ant-pagination-next button, .weui-desktop-pagination__next'),
            ], timeout_ms=1000)
            if not next_button or cls._button_visually_disabled(next_button):
                break
            next_button.click()
            page.wait_for_timeout(900)
        return None, ""

    @staticmethod
    def _publication_state(row_text: str) -> str:
        """Classify the creator-list state without inventing homepage success."""
        value = re.sub(r"\s+", " ", str(row_text or "")).strip()
        if re.search(r"审核不通过|审核失败|发布失败|发表失败|未通过|已驳回|违规|已删除", value):
            return "failed"
        if re.search(r"审核中|待审核|等待审核|处理中|发布中|发表中|排队中", value):
            return "pending"
        if re.search(r"私密|仅自己可见", value):
            return "private"
        if re.search(r"已发布|已发表|公开|播放\s*[:：]?\s*[\d,.]+|观看\s*[:：]?\s*[\d,.]+", value):
            return "published"
        return "listed"

    @staticmethod
    def _filter_cached_products(items: list[dict], keyword: str) -> list[dict]:
        normalized = keyword.strip().casefold()
        if not normalized:
            return list(items)
        return [
            item for item in items
            if normalized in str(item.get("id") or "").casefold()
            or normalized in str(item.get("name") or "").casefold()
        ]

    @staticmethod
    def _friendly_product_error(error: Exception) -> tuple[str, str]:
        stage = error.stage if isinstance(error, ChannelsError) else "product_read"
        raw = str(error).strip()
        lowered = raw.lower()
        if "授权" in raw or "/login" in raw:
            return "视频号授权已失效，请重新扫码授权后再读取商品", "authorization"
        if "timeout" in lowered or "超时" in raw:
            return "视频号页面响应较慢，系统自动重试后仍未读到商品，请稍后再试", stage
        if "net::" in lowered or "connection" in lowered or "网络" in raw:
            return "视频号平台网络连接波动，系统自动重试后仍未完成", stage
        if raw:
            return raw[:240], stage
        return "视频号商品读取未完成，请稍后重试", stage

    @staticmethod
    def _product_cache_file(auth_file: Path) -> Path:
        return auth_file.with_name(f"{auth_file.stem}.products.json")

    def _load_product_cache_file(self, account_id: str, auth_file: Path) -> None:
        cache_file = self._product_cache_file(auth_file)
        try:
            payload = json.loads(cache_file.read_text(encoding="utf-8"))
            items = payload.get("items")
            fetched_at = float(payload.get("fetched_at_epoch") or 0)
            if not isinstance(items, list) or fetched_at <= time.time() - PRODUCT_CACHE_MAX_STALE_SECONDS:
                return
            cache = {
                "items": [item for item in items if isinstance(item, dict) and item.get("id")],
                "fetched_at": fetched_at,
                "read_at": str(payload.get("read_at") or ""),
                "complete": bool(payload.get("complete")),
                "expected_total": int(payload.get("expected_total") or 0),
                "page_count": int(payload.get("page_count") or 0),
            }
            with self._lock:
                self._product_cache.setdefault(account_id, cache)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return

    def _save_product_cache_file(self, auth_file: Path, cache: dict) -> None:
        cache_file = self._product_cache_file(auth_file)
        temp_file = cache_file.with_name(f"{cache_file.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temp_file.write_text(json.dumps({
                "items": cache.get("items", []),
                "fetched_at_epoch": cache.get("fetched_at", time.time()),
                "read_at": cache.get("read_at", ""),
                "complete": bool(cache.get("complete")),
                "expected_total": int(cache.get("expected_total") or 0),
                "page_count": int(cache.get("page_count") or 0),
            }, ensure_ascii=False), encoding="utf-8")
            os.chmod(temp_file, 0o600)
            temp_file.replace(cache_file)
        except OSError:
            try:
                temp_file.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _product_expected_total(page) -> int:
        """Read a shop total only from product-specific copy, never from prices or IDs."""
        try:
            body_text = "\n".join(page.locator("body").all_inner_texts())
        except Exception:
            return 0
        patterns = (
            r"共\s*(\d{1,6})\s*(?:件|个|条)\s*商品",
            r"商品\s*(?:列表)?\s*[（(]\s*(\d{1,6})\s*[)）]",
            r"全部商品\s*[（(]?\s*(\d{1,6})\s*[)）]?",
        )
        for pattern in patterns:
            match = re.search(pattern, body_text)
            if match:
                return int(match.group(1))
        return 0

    @staticmethod
    def _window_product_to_item(product: dict) -> dict | None:
        identifier = str(product.get("productId") or product.get("outProductId") or "").strip()
        name = str(product.get("title") or product.get("name") or "").strip()
        if not identifier or not name:
            return None
        price_value = product.get("sellingPrice")
        if price_value is None:
            price_value = product.get("marketPrice")
        try:
            price_yuan = round(float(price_value) / 100, 2) if price_value is not None else None
        except (TypeError, ValueError):
            price_yuan = None
        images = product.get("imgUrls") or product.get("imageUrls") or []
        image_url = str(images[0]) if isinstance(images, list) and images else ""
        return {"id": identifier, "name": name, "price_yuan": price_yuan, "image_url": image_url}

    def _read_window_products_api(self, page, snapshot: dict, progress=None) -> tuple[list[dict], int, int]:
        """Fully paginate the same read-only API used by the visible product dialog."""
        url = str(snapshot.get("url") or "")
        request_body = dict(snapshot.get("request_body") or {})
        response_payload = dict(snapshot.get("response_payload") or {})
        if not url or not request_body or not response_payload:
            raise ChannelsError("完整商品接口上下文缺失，改用页面列表读取", "product_api")

        items: list[dict] = []
        seen_ids: set[str] = set()
        seen_buffers: set[str] = set()
        expected_total = 0
        completed_pages = 0
        for page_number in range(1, 101):
            data = response_payload.get("data") or {}
            if int(data.get("code") or 0) != 0:
                raise ChannelsError("微信小店完整商品接口返回异常，请稍后重试", "product_api")
            raw_products = data.get("products") or []
            for product in raw_products if isinstance(raw_products, list) else []:
                if not isinstance(product, dict):
                    continue
                parsed = self._window_product_to_item(product)
                if parsed and parsed["id"] not in seen_ids:
                    seen_ids.add(parsed["id"])
                    items.append(parsed)
            completed_pages = page_number
            expected_total = max(expected_total, int(data.get("productCountWithFilter") or 0))
            if progress:
                progress(len(items), page_number, expected_total)

            continue_flag = int(data.get("continueFlag") or 0)
            last_buffer = str(data.get("lastBuffer") or "")
            if not continue_flag:
                break
            if not last_buffer or last_buffer in seen_buffers:
                raise ChannelsError("微信小店商品翻页标记异常，系统不会把残缺列表当作完整结果", "product_api")
            seen_buffers.add(last_buffer)
            request_body["lastBuffer"] = last_buffer
            request_body["timestamp"] = str(int(time.time() * 1000))
            response_payload = page.evaluate(
                """async ({ url, payload }) => {
                    const response = await fetch(url, {
                      method: 'POST',
                      credentials: 'include',
                      headers: { 'content-type': 'application/json' },
                      body: JSON.stringify(payload),
                    });
                    const text = await response.text();
                    if (!response.ok) throw new Error(`HTTP ${response.status}: ${text.slice(0, 160)}`);
                    return JSON.parse(text);
                }""",
                {"url": url, "payload": request_body},
            )
            page.wait_for_timeout(120)
        else:
            raise ChannelsError("微信小店商品超过 100 页，已停止读取以避免无限循环", "product_api")

        if expected_total and len(items) < expected_total:
            raise ChannelsError(
                f"微信小店共有 {expected_total} 个商品，本次仅读取到 {len(items)} 个，系统不会覆盖完整缓存",
                "product_api",
            )
        return items, expected_total, completed_pages

    @staticmethod
    def _product_row_locator(page):
        return page.locator(
            "tr.ant-table-row, "
            "tr.weui-desktop-table__row, "
            "[role='row'][class*='table'][class*='row'], "
            "[class*='product'][class*='row']"
        )

    def _collect_visible_product_rows(self, page, items: list[dict], seen_ids: set[str]) -> list[str]:
        rows = self._product_row_locator(page)
        page_ids: list[str] = []
        for index in range(min(rows.count(), 300)):
            row = rows.nth(index)
            try:
                if not row.is_visible():
                    continue
                image_url = ""
                images = row.locator("img")
                if images.count():
                    image_url = images.first.get_attribute("src") or ""
                parsed = self._parse_product_row(row.inner_text(), image_url)
            except Exception:
                continue
            if not parsed:
                continue
            page_ids.append(parsed["id"])
            if parsed["id"] not in seen_ids:
                seen_ids.add(parsed["id"])
                items.append(parsed)
        return page_ids

    @staticmethod
    def _scroll_product_list(page) -> bool:
        candidates = page.locator(
            ".ant-table-body, .weui-desktop-table__body, "
            "[class*='product'][class*='scroll'], [class*='table'][class*='scroll'], "
            "[role='dialog'] [class*='scroll']"
        )
        moved = False
        for index in range(min(candidates.count(), 12)):
            candidate = candidates.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                before = candidate.evaluate("node => ({ top: node.scrollTop, height: node.scrollHeight, client: node.clientHeight })")
                if int(before.get("height") or 0) <= int(before.get("client") or 0) + 2:
                    continue
                candidate.evaluate("node => { node.scrollTop = Math.min(node.scrollHeight, node.scrollTop + Math.max(node.clientHeight * 0.85, 360)); }")
                after = candidate.evaluate("node => node.scrollTop")
                moved = moved or float(after or 0) > float(before.get("top") or 0) + 1
            except Exception:
                continue
        return moved

    @classmethod
    def _next_product_button(cls, page):
        return cls._first_visible([
            page.locator("li.ant-pagination-next"),
            page.locator(".weui-desktop-pagination__next, .weui-desktop-pagination__next-btn"),
            page.locator("button[aria-label='下一页'], button[title='下一页'], button[aria-label='Next page'], button[title='Next page']"),
            page.get_by_role("button", name=re.compile(r"下一页|下页|Next", re.I)),
            page.locator("[class*='pagination'][class*='next']"),
        ], timeout_ms=1200)

    def _read_product_rows(self, page, progress=None) -> tuple[list[dict], int, int]:
        items: list[dict] = []
        seen_ids: set[str] = set()
        previous_page_marker = ""
        expected_total = self._product_expected_total(page)
        completed_pages = 0
        for page_number in range(1, 101):
            rows = self._product_row_locator(page)
            try:
                rows.first.wait_for(state="visible", timeout=20000)
            except Exception:
                empty = page.get_by_text(re.compile(r"暂无商品|暂无数据|没有数据|未找到商品")).first
                if empty.count() and empty.is_visible():
                    break
                raise ChannelsError("商品表格已打开，但列表内容未加载完成", "product_list")

            page_ids: list[str] = self._collect_visible_product_rows(page, items, seen_ids)
            # Some Video Channels shops virtualize one long list instead of
            # exposing conventional pagination. Walk the scroll area until it
            # reaches the bottom and retain every newly rendered row.
            stable_scrolls = 0
            for _scroll_round in range(160):
                count_before = len(items)
                if not self._scroll_product_list(page):
                    break
                page.wait_for_timeout(280)
                page_ids.extend(self._collect_visible_product_rows(page, items, seen_ids))
                if progress:
                    progress(len(items), page_number, expected_total)
                stable_scrolls = stable_scrolls + 1 if len(items) == count_before else 0
                if expected_total and len(items) >= expected_total:
                    break
                if stable_scrolls >= 3:
                    break

            marker = "|".join(page_ids)
            if marker and marker == previous_page_marker:
                break
            previous_page_marker = marker
            completed_pages = page_number
            if progress:
                progress(len(items), page_number, expected_total)
            if expected_total and len(items) >= expected_total:
                break
            next_button = self._next_product_button(page)
            if not next_button or self._button_visually_disabled(next_button):
                break
            first_row_before = rows.first.inner_text().strip()
            next_button.click()
            page_changed = False
            change_deadline = time.monotonic() + 15
            while time.monotonic() < change_deadline:
                page.wait_for_timeout(350)
                next_rows = page.locator("tr.ant-table-row")
                try:
                    if next_rows.count() and next_rows.first.inner_text().strip() != first_row_before:
                        page_changed = True
                        break
                except Exception:
                    continue
            if not page_changed:
                raise ChannelsError("商品分页未能进入下一页，完整列表读取已暂停，请稍后重试", "product_pagination")
        if expected_total and len(items) < expected_total:
            raise ChannelsError(
                f"微信小店共有 {expected_total} 个商品，本次仅完整读取到 {len(items)} 个，系统不会用残缺列表覆盖缓存",
                "product_pagination",
            )
        return items, expected_total, completed_pages

    def _read_products_live(self, account, progress=None) -> tuple[list[dict], int, int]:
        from playwright.sync_api import sync_playwright

        auth_file = self._validated_auth_file(account)
        with self._browser_lock(account.id):
            browser = None
            try:
                with sync_playwright() as playwright:
                    browser = self._launch_publish_browser(playwright)
                    context = browser.new_context(
                        storage_state=str(auth_file),
                        viewport={"width": 1440, "height": 1000},
                        locale="zh-CN",
                    )
                    page = context.new_page()
                    product_api_snapshot: dict = {}

                    def capture_product_api(response) -> None:
                        if "get_all_window_products" not in response.url:
                            return
                        try:
                            product_api_snapshot.update({
                                "url": response.url,
                                "request_body": json.loads(response.request.post_data or "{}"),
                                "response_payload": response.json(),
                            })
                        except Exception:
                            return

                    page.on("response", capture_product_api)
                    publish_root, _file_input = self._open_publish_page(page)
                    self._open_product_dialog(publish_root)
                    api_deadline = time.monotonic() + 8
                    while not product_api_snapshot and time.monotonic() < api_deadline:
                        page.wait_for_timeout(250)
                    if product_api_snapshot:
                        return self._read_window_products_api(page, product_api_snapshot, progress=progress)
                    items, expected_total, page_count = self._read_product_rows(publish_root, progress=progress)
                    if items and not expected_total:
                        raise ChannelsError(
                            "视频号只展示了商品首屏，但未返回可核验的完整总数；系统不会把首屏当作完整小店",
                            "product_pagination",
                        )
                    return items, expected_total, page_count
            except ChannelsError:
                raise
            except Exception as error:
                message, stage = self._friendly_product_error(error)
                raise ChannelsError(message, stage) from error
            finally:
                if browser:
                    try:
                        browser.close()
                    except Exception:
                        pass

    def _product_refresh_worker(self, account_id: str, auth_file_value: str) -> None:
        account = SimpleNamespace(id=account_id, auth_file=auth_file_value)
        last_message = ""
        last_stage = "product_read"
        for attempt in range(1, PRODUCT_REFRESH_ATTEMPTS + 1):
            with self._lock:
                self._product_jobs[account_id] = {
                    "state": "loading",
                    "attempt": attempt,
                    "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                    "message": f"正在读取视频号商品，第 {attempt}/{PRODUCT_REFRESH_ATTEMPTS} 次尝试",
                    "error": "",
                    "stage": "open_publish_page",
                }
            started = time.monotonic()
            try:
                def report_progress(count: int, page_number: int, expected_total: int) -> None:
                    total_copy = f"/{expected_total}" if expected_total else ""
                    with self._lock:
                        self._product_jobs[account_id] = {
                            "state": "loading",
                            "attempt": attempt,
                            "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                            "message": f"正在读取完整商品列表：已读取 {count}{total_copy} 个 · 第 {page_number} 页",
                            "error": "",
                            "stage": "product_pagination",
                        }

                items, expected_total, page_count = self._read_products_live(account, progress=report_progress)
                now = time.time()
                read_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
                cache = {
                    "items": items,
                    "fetched_at": now,
                    "read_at": read_at,
                    "complete": True,
                    "expected_total": expected_total,
                    "page_count": page_count,
                }
                with self._lock:
                    self._product_cache[account_id] = cache
                    self._product_jobs[account_id] = {
                        "state": "ready",
                        "attempt": attempt,
                        "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                        "message": f"已完整读取 {len(items)} 个商品",
                        "error": "",
                        "stage": "completed",
                    }
                self._save_product_cache_file(Path(auth_file_value), cache)
                logger.info(
                    "Channels product refresh completed account_id=%s count=%s elapsed=%.2f attempt=%s",
                    account_id, len(items), time.monotonic() - started, attempt,
                )
                return
            except Exception as error:
                last_message, last_stage = self._friendly_product_error(error)
                logger.warning(
                    "Channels product refresh failed account_id=%s stage=%s attempt=%s error=%s",
                    account_id, last_stage, attempt, last_message,
                )
                if attempt < PRODUCT_REFRESH_ATTEMPTS:
                    with self._lock:
                        self._product_jobs[account_id] = {
                            "state": "loading",
                            "attempt": attempt,
                            "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                            "message": f"第 {attempt} 次未完成，系统将在几秒后自动重试",
                            "error": last_message,
                            "stage": last_stage,
                        }
                    time.sleep(2 if attempt == 1 else 5)
        with self._lock:
            self._product_jobs[account_id] = {
                "state": "error",
                "attempt": PRODUCT_REFRESH_ATTEMPTS,
                "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                "message": "商品读取暂未完成",
                "error": last_message,
                "stage": last_stage,
            }

    def _start_product_refresh(self, account: ChannelsAccount) -> bool:
        auth_file = self._validated_auth_file(account)
        with self._lock:
            active = self._product_jobs.get(account.id, {}).get("state") == "loading"
            if active:
                return False
            self._product_jobs[account.id] = {
                "state": "loading",
                "attempt": 0,
                "max_attempts": PRODUCT_REFRESH_ATTEMPTS,
                "message": "已进入后台读取队列",
                "error": "",
                "stage": "queued",
            }
        threading.Thread(
            target=self._product_refresh_worker,
            args=(account.id, str(auth_file)),
            daemon=True,
        ).start()
        return True

    def products_status(self, account: ChannelsAccount, keyword: str = "", force_refresh: bool = False) -> dict:
        auth_file = self._validated_auth_file(account)
        with self._lock:
            cache_known = account.id in self._product_cache
        if not cache_known:
            self._load_product_cache_file(account.id, auth_file)

        with self._lock:
            cache = dict(self._product_cache.get(account.id, {}))
            job = dict(self._product_jobs.get(account.id, {}))
        cache_items = list(cache.get("items", []))
        fetched_at = float(cache.get("fetched_at") or 0)
        expected_total = int(cache.get("expected_total") or 0)
        cache_complete = bool(cache.get("complete")) and (not cache_items or expected_total > 0)
        fresh = bool(cache_items or fetched_at) and cache_complete and fetched_at > time.time() - PRODUCT_CACHE_TTL_SECONDS
        active = job.get("state") == "loading"
        should_start = (
            force_refresh
            or (not cache and not job)
            or (cache and not fresh and not active and job.get("state") != "error")
        )
        if should_start and not active:
            self._start_product_refresh(account)
            with self._lock:
                job = dict(self._product_jobs.get(account.id, {}))
            active = True

        filtered = self._filter_cached_products(cache_items, keyword)
        if fresh and not force_refresh:
            state = "ready"
        elif cache_items and active:
            state = "refreshing"
        elif cache_items:
            state = "ready"
        elif active:
            state = "loading"
        else:
            state = "error"
        error = str(job.get("error") or "")
        message = str(job.get("message") or "")
        if state == "ready" and cache_items:
            message = f"已完整读取 {len(cache_items)} 个商品" if cache_complete else f"当前显示 {len(cache_items)} 个历史商品，正在补齐完整列表"
        elif state == "refreshing" and cache_items and not cache_complete:
            message = f"当前显示 {len(cache_items)} 个历史商品，正在补齐完整列表"
        return {
            "state": state,
            "items": filtered,
            "total": len(filtered),
            "cached_total": len(cache_items),
            "cached": bool(cache),
            "stale": bool(cache) and not fresh,
            "message": message,
            "error": error,
            "stage": str(job.get("stage") or ""),
            "attempt": int(job.get("attempt") or 0),
            "max_attempts": int(job.get("max_attempts") or PRODUCT_REFRESH_ATTEMPTS),
            "read_at": str(cache.get("read_at") or ""),
            "complete": cache_complete,
            "expected_total": expected_total,
            "page_count": int(cache.get("page_count") or 0),
            "retry_after_ms": 2000 if state in {"loading", "refreshing"} else 0,
        }

    def products(self, account: ChannelsAccount, keyword: str = "") -> list[dict]:
        """Synchronous compatibility helper used by diagnostics and older callers."""
        auth_file = self._validated_auth_file(account)
        with self._lock:
            cache = dict(self._product_cache.get(account.id, {}))
        cached_items = list(cache.get("items", []))
        cache_complete = bool(cache.get("complete")) and (not cached_items or int(cache.get("expected_total") or 0) > 0)
        if cache and cache_complete and float(cache.get("fetched_at") or 0) > time.time() - PRODUCT_CACHE_TTL_SECONDS:
            return self._filter_cached_products(cached_items, keyword)
        items, expected_total, page_count = self._read_products_live(account)
        now = time.time()
        cache = {
            "items": items,
            "fetched_at": now,
            "read_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            "complete": True,
            "expected_total": expected_total,
            "page_count": page_count,
        }
        with self._lock:
            self._product_cache[account.id] = cache
        self._save_product_cache_file(auth_file, cache)
        return self._filter_cached_products(items, keyword)

    @staticmethod
    def _product_identity_matches(text: str, product_id: str, product_name: str = "") -> bool:
        normalized = re.sub(r"\s+", "", text or "").lower()
        if product_id and re.search(rf"(?<!\d){re.escape(product_id.lower())}(?!\d)", normalized):
            return True
        name = re.sub(r"\s+", "", product_name or "").lower()
        return bool(name and len(name) >= 4 and name[: min(18, len(name))] in normalized)

    @classmethod
    def _product_attached(cls, page, product_id: str, product_name: str = "") -> bool:
        """Verify the selected product outside the transient picker dialog."""
        script = r"""
        ({ productId, productName }) => {
          const clean = value => (value || '').replace(/\s+/g, '').toLowerCase();
          const id = clean(productId);
          const name = clean(productName).slice(0, 18);
          const visible = node => {
            const rect = node.getBoundingClientRect();
            const style = getComputedStyle(node);
            return rect.width > 1 && rect.height > 1 && style.visibility !== 'hidden' && style.display !== 'none';
          };
          const selectors = [
            '[class*="link"]', '[class*="product"]', '[class*="goods"]',
            '[class*="commodity"]', '[class*="attach"]'
          ];
          for (const node of document.querySelectorAll(selectors.join(','))) {
            if (!visible(node) || node.closest('[role="dialog"], .ant-modal, .weui-desktop-dialog')) continue;
            const text = clean(node.innerText || node.textContent || '');
            if ((id && text.includes(id)) || (name.length >= 4 && text.includes(name))) return true;
          }
          return false;
        }
        """
        try:
            return bool(page.evaluate(script, {"productId": product_id, "productName": product_name}))
        except Exception:
            return False

    def _select_product(self, page, product_id: str, product_name: str = "") -> None:
        if not product_id.strip():
            return
        if self._product_attached(page, product_id, product_name):
            return
        self._open_product_dialog(page)
        self._filter_products(page, product_id)
        rows = page.locator('tr.ant-table-row, [role="row"], [class*="product-item"], [class*="goods-item"]')
        matching = rows.filter(has_text=product_id).first
        try:
            matching.wait_for(state="visible", timeout=35000)
        except Exception as error:
            raise ChannelsError(
                f"商品 {product_id} 未在该视频号橱窗中找到，可能已下架或当前账号无权使用",
                "bind_product",
            ) from error
        if not re.search(rf"(?<!\d){re.escape(product_id)}(?!\d)", matching.inner_text()):
            raise ChannelsError(f"商品 {product_id} 查询结果不一致，请重新选择商品", "bind_product")
        radio = matching.locator('input.ant-radio-input, input[type="radio"], [role="radio"]').first
        if radio.count():
            radio.click(force=True)
        else:
            matching.click()
        # Selecting a product changes the platform button label from
        # "添加" to "添加(1)". Match both states so Playwright does not wait
        # for a label that no longer exists after the radio is selected.
        add_button = page.get_by_role(
            "button",
            name=re.compile(r"^(?:添加|确定|完成|保存)(?:\(\d+\))?$"),
            exact=False,
        ).last
        add_button.wait_for(state="visible", timeout=45000)
        deadline = time.monotonic() + 45
        while self._button_visually_disabled(add_button) and time.monotonic() < deadline:
            page.wait_for_timeout(400)
        if self._button_visually_disabled(add_button):
            raise ChannelsError(f"商品 {product_id} 已找到，但平台未允许添加，请重新选择", "bind_product")
        add_button.click()
        search = self._product_search_input(page).first
        verify_deadline = time.monotonic() + 45
        while time.monotonic() < verify_deadline:
            if (not search.count()) or (not search.is_visible()):
                page.wait_for_timeout(500)
                self._dismiss_non_publish_dialogs(page, page, timeout_ms=2200)
                if self._product_attached(page, product_id, product_name):
                    return
                # The picker closing is itself a platform acknowledgement; a
                # later publish validation remains the final safeguard.
                return
            if self._product_attached(page, product_id, product_name):
                self._dismiss_non_publish_dialogs(page, page, timeout_ms=2200)
                return
            page.wait_for_timeout(500)
        if self._product_attached(page, product_id, product_name):
            self._dismiss_non_publish_dialogs(page, page, timeout_ms=2200)
            return
        raise ChannelsError(
            f"商品 {product_id} 已选中，但平台未返回绑定结果；请稍后点击重试",
            "bind_product",
        )

    @staticmethod
    def _cover_roots(page, publish_root) -> list:
        """Return every live DOM scope where WeChat may portal the cover editor."""
        roots = [publish_root, page]
        try:
            roots.extend(frame for frame in page.frames if frame != page.main_frame)
        except Exception:
            pass
        unique = []
        for root in roots:
            if root is not None and not any(root is existing for existing in unique):
                unique.append(root)
        return unique

    @classmethod
    def _cover_image_input(cls, page, publish_root):
        selector = (
            'input[type="file"][accept*="image"], input[type="file"][accept*="jpg"], '
            'input[type="file"][accept*="jpeg"], input[type="file"][accept*="png"], '
            'input[type="file"][accept*="webp"]'
        )
        for root in cls._cover_roots(page, publish_root):
            try:
                inputs = root.locator(selector)
                for index in range(min(inputs.count(), 12) - 1, -1, -1):
                    candidate = inputs.nth(index)
                    candidate.wait_for(state="attached", timeout=100)
                    return candidate
            except Exception:
                continue
        return None

    @classmethod
    def _wait_cover_image_input(cls, page, publish_root, timeout_ms: int):
        deadline = time.monotonic() + max(timeout_ms, 0) / 1000
        while time.monotonic() <= deadline:
            image_input = cls._cover_image_input(page, publish_root)
            if image_input:
                return image_input
            page.wait_for_timeout(350)
        return None

    @classmethod
    def _find_cover_trigger(cls, page, publish_root, pattern: re.Pattern, timeout_ms: int):
        candidates = []
        for root in cls._cover_roots(page, publish_root):
            candidates.extend([
                root.get_by_role("button", name=pattern),
                root.locator('button, [role="button"], a, label').filter(has_text=pattern),
                root.locator('[aria-label*="封面"], [title*="封面"], [data-testid*="cover"]'),
                root.get_by_text(pattern),
            ])
        return cls._first_visible(candidates, timeout_ms=timeout_ms)

    @staticmethod
    def _click_cover_trigger(page, trigger, cover_path: Path) -> bool:
        """Click a cover control and capture direct file-chooser implementations."""
        try:
            with page.expect_file_chooser(timeout=1800) as chooser_info:
                trigger.click()
            chooser_info.value.set_files(str(cover_path))
            return True
        except Exception as error:
            # Most WeChat versions open an in-page dialog instead of a native
            # chooser. The click has still completed when only the expectation
            # timed out, so the caller can continue looking inside that dialog.
            raw = str(error).lower()
            if "timeout" not in raw:
                raise
            return False

    @classmethod
    def _apply_custom_cover_once(cls, page, publish_root, cover_path: Path) -> None:
        """Upload a caller-provided image through all known WeChat cover UIs."""
        image_input = cls._wait_cover_image_input(page, publish_root, timeout_ms=1200)
        uploaded = False
        if not image_input:
            outer_pattern = re.compile(
                r"设置封面|编辑封面|修改封面|选择封面|更换封面|"
                r"上传封面|自定义封面|视频封面|封面设置"
            )
            trigger = cls._find_cover_trigger(page, publish_root, outer_pattern, timeout_ms=90000)
            if not trigger:
                raise ChannelsError("视频号发布页暂未显示自定义封面入口，请稍后重试", "set_cover")
            uploaded = cls._click_cover_trigger(page, trigger, cover_path)
            if not uploaded:
                image_input = cls._wait_cover_image_input(page, publish_root, timeout_ms=5000)

        if not uploaded and not image_input:
            inner_pattern = re.compile(r"上传封面|自定义封面|本地上传|选择图片|更换封面|点击更换")
            upload_trigger = cls._find_cover_trigger(page, publish_root, inner_pattern, timeout_ms=20000)
            if upload_trigger:
                uploaded = cls._click_cover_trigger(page, upload_trigger, cover_path)
            if not uploaded:
                image_input = cls._wait_cover_image_input(page, publish_root, timeout_ms=20000)

        if not uploaded:
            if not image_input:
                raise ChannelsError("已打开视频号封面设置，但平台未生成本地图片上传控件，请稍后重试", "set_cover")
            try:
                image_input.set_input_files(str(cover_path))
            except Exception as error:
                raise ChannelsError(f"封面文件未能交给视频号页面：{error}", "set_cover") from error

        page.wait_for_timeout(1000)
        confirm_pattern = re.compile(r"^下一步$|^确定$|^确认$|^完成$|^保存$|^使用$|^应用$|^选取$|^裁剪完成$")
        confirm_candidates = []
        for root in cls._cover_roots(page, publish_root):
            confirm_candidates.extend([
                root.get_by_role("button", name=confirm_pattern).last,
                root.get_by_text(confirm_pattern, exact=True).last,
            ])
        confirm = cls._first_visible(confirm_candidates, timeout_ms=30000)
        if not confirm:
            # Newer Channels pages auto-apply direct file selections without a
            # second confirmation control and clears input.files immediately
            # after consuming the image. set_input_files returning successfully
            # is therefore the browser-side receipt; explicit platform errors
            # are still checked below, and publish itself still requires a
            # platform submission receipt before the task can advance.
            page.wait_for_timeout(1800)
        else:
            try:
                confirm_label = str(confirm.inner_text() or "").strip()
            except Exception:
                confirm_label = ""
            confirm.click()
            page.wait_for_timeout(1200)
            if confirm_label == "下一步":
                final_pattern = re.compile(r"^确定$|^确认$|^完成$|^保存$|^使用$|^应用$|^裁剪完成$")
                final_candidates = []
                for root in cls._cover_roots(page, publish_root):
                    final_candidates.extend([
                        root.get_by_role("button", name=final_pattern).last,
                        root.get_by_text(final_pattern, exact=True).last,
                    ])
                final_confirm = cls._first_visible(final_candidates, timeout_ms=30000)
                if not final_confirm:
                    raise ChannelsError("视频号封面已进入裁剪步骤，但没有显示最终保存按钮；已停止发布", "set_cover")
                final_confirm.click()
                page.wait_for_timeout(1200)
        cover_error = cls._first_visible([
            root.get_by_text(re.compile(r"封面上传失败|图片上传失败|尺寸不符合|图片格式不支持|请重新上传"))
            for root in cls._cover_roots(page, publish_root)
        ], timeout_ms=800)
        if cover_error:
            try:
                detail = cover_error.inner_text().strip()
            except Exception:
                detail = "封面未通过视频号校验"
            raise ChannelsError(f"视频号未接受自定义封面：{detail[:160]}", "set_cover")

    @staticmethod
    def _transient_cover_error(error: Exception) -> bool:
        value = str(error or "").lower()
        return any(token in value for token in (
            "网络出错", "网络错误", "网络异常", "请重新上传", "连接中断",
            "timeout", "timed out", "temporarily unavailable", "upload failed",
        ))

    @classmethod
    def _dismiss_cover_retry_notice(cls, page, publish_root) -> None:
        retry = cls._first_visible([
            root.get_by_role("button", name=re.compile(r"重新上传|重试|再试一次|确定|知道了"))
            for root in cls._cover_roots(page, publish_root)
        ], timeout_ms=900)
        if retry:
            try:
                retry.click()
            except Exception:
                pass

    @classmethod
    def _apply_custom_cover(cls, page, publish_root, cover_path: Path, attempts: int = 3) -> None:
        """Retry only transient cover-network failures; structural failures stop.

        The platform intermittently returns ``网络出错，请重新上传`` for the
        same valid image. Retrying here is safe because the video has not been
        submitted yet and the operation only replaces the draft cover.
        """
        last_error: Exception | None = None
        for attempt in range(1, max(1, attempts) + 1):
            try:
                cls._apply_custom_cover_once(page, publish_root, cover_path)
                return
            except ChannelsError as error:
                last_error = error
                if error.stage != "set_cover" or not cls._transient_cover_error(error) or attempt >= attempts:
                    raise
                cls._dismiss_cover_retry_notice(page, publish_root)
                page.wait_for_timeout(1200 * attempt)
        if last_error:
            raise last_error

    @staticmethod
    def _annotation_control_text(control) -> str:
        for reader in (
            lambda: control.inner_text(),
            lambda: control.input_value(),
            lambda: control.get_attribute("value"),
        ):
            try:
                value = reader()
                if isinstance(value, str) and value.strip():
                    return value.strip()
            except Exception:
                continue
        return ""

    @classmethod
    def _find_video_annotation_control(cls, page, publish_root, timeout_ms: int = 90000):
        label = cls._first_visible([
            publish_root.get_by_text(re.compile(r"^视频标注$"), exact=True),
            page.get_by_text(re.compile(r"^视频标注$"), exact=True),
        ], timeout_ms=timeout_ms)
        if not label:
            raise ChannelsError("视频号发布页未显示“视频标注”入口，已停止发布", "set_video_annotation")
        try:
            label.scroll_into_view_if_needed()
        except Exception:
            pass

        current_values = "|".join(re.escape(value) for value in ["选择视频标注", *VIDEO_ANNOTATION_LABELS.values()])
        container = label
        for _ in range(6):
            try:
                container = container.locator("xpath=..")
            except Exception:
                break
            native = cls._first_visible([container.locator("select")], timeout_ms=250)
            if native:
                return native, True
            custom = cls._first_visible([
                container.locator('[role="combobox"]'),
                container.locator('input[placeholder*="视频标注"]'),
                container.get_by_text(re.compile(rf"^(?:{current_values})$"), exact=True),
                container.locator('[class*="select"], [class*="Select"]'),
            ], timeout_ms=250)
            if custom:
                return custom, False
        raise ChannelsError("视频号发布页显示了“视频标注”，但无法定位选择框，已停止发布", "set_video_annotation")

    @classmethod
    def _fill_video_annotation_detail(
        cls,
        page,
        publish_root,
        selectors: str,
        label_pattern: str,
        value: str,
        field_name: str,
        required: bool = True,
    ) -> None:
        if not value.strip() and not required:
            return
        candidates = [publish_root.locator(selectors), page.locator(selectors)]
        try:
            detail_label = cls._first_visible([
                publish_root.get_by_text(re.compile(label_pattern)),
                page.get_by_text(re.compile(label_pattern)),
            ], timeout_ms=1200)
            if detail_label:
                parent = detail_label.locator("xpath=..")
                candidates.insert(0, parent.locator("input, textarea"))
        except Exception:
            pass
        field = cls._first_visible(candidates, timeout_ms=15000)
        if not field:
            if required:
                raise ChannelsError(f"选择视频标注后未显示“{field_name}”输入框，已停止发布", "set_video_annotation")
            return
        try:
            field.fill(value.strip())
            observed = field.input_value().strip()
        except Exception as error:
            raise ChannelsError(f"视频号未接受{field_name}：{error}", "set_video_annotation") from error
        if not observed:
            raise ChannelsError(f"{field_name}填写后未能回读，已停止发布", "set_video_annotation")

    @classmethod
    def _apply_video_annotation(
        cls,
        page,
        publish_root,
        annotation: str,
        shooting_time: str = "",
        shooting_location: str = "",
        repost_source: str = "",
    ) -> str:
        target = VIDEO_ANNOTATION_LABELS.get(annotation)
        if not target:
            raise ChannelsError("视频标注类型无效，已停止发布", "set_video_annotation")
        control, native = cls._find_video_annotation_control(page, publish_root)
        if native:
            try:
                control.select_option(label=target)
                observed = control.evaluate(
                    "el => el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : ''"
                )
            except Exception as error:
                raise ChannelsError(f"无法选择视频标注“{target}”：{error}", "set_video_annotation") from error
        else:
            observed = cls._annotation_control_text(control)
            if target not in observed:
                last_error: Exception | None = None
                opened = False
                selected_natively = False
                for attempt in range(1, 4):
                    cls._dismiss_non_publish_dialogs(page, publish_root, timeout_ms=1400)
                    try:
                        native_now = False
                        if attempt > 1:
                            control, native_now = cls._find_video_annotation_control(
                                page, publish_root, timeout_ms=5000,
                            )
                        if native_now:
                            control.select_option(label=target)
                            observed = control.evaluate(
                                "el => el.selectedOptions && el.selectedOptions[0] ? el.selectedOptions[0].textContent.trim() : ''"
                            )
                            selected_natively = True
                            opened = True
                            break
                        control.click(timeout=8000)
                        opened = True
                        break
                    except Exception as error:
                        last_error = error
                        try:
                            control.evaluate("element => element.click()")
                            opened = True
                            break
                        except Exception as fallback_error:
                            last_error = fallback_error
                    page.wait_for_timeout(500 * attempt)
                if not opened:
                    raise ChannelsError(
                        f"无法打开视频标注选择框：{str(last_error)[:180]}",
                        "set_video_annotation",
                    ) from last_error
                if not selected_natively:
                    option = cls._first_visible([
                        page.get_by_role("option", name=target, exact=True),
                        page.get_by_text(target, exact=True),
                        publish_root.get_by_role("option", name=target, exact=True),
                        publish_root.get_by_text(target, exact=True),
                    ], timeout_ms=15000)
                    if not option:
                        raise ChannelsError(f"视频号未提供视频标注“{target}”，已停止发布", "set_video_annotation")
                    option.click(timeout=8000)
                    page.wait_for_timeout(500)
                    control, _ = cls._find_video_annotation_control(page, publish_root, timeout_ms=5000)
                    observed = cls._annotation_control_text(control)
        if not isinstance(observed, str) or target not in observed:
            raise ChannelsError(
                f"视频标注选择后未能回读为“{target}”（当前：{str(observed)[:80] or '空'}），已停止发布",
                "set_video_annotation",
            )

        if annotation == "self_shot":
            cls._fill_video_annotation_detail(
                page, publish_root,
                'input[placeholder*="拍摄时间"], input[type="datetime-local"], input[type="date"]',
                r"拍摄时间", shooting_time, "拍摄时间",
            )
            cls._fill_video_annotation_detail(
                page, publish_root,
                'input[placeholder*="拍摄地点"], input[placeholder*="地点"]',
                r"拍摄地点|拍摄位置", shooting_location, "拍摄地点",
            )
        elif annotation == "repost":
            cls._fill_video_annotation_detail(
                page, publish_root,
                'input[placeholder*="转载来源"], textarea[placeholder*="转载来源"]',
                r"转载来源", repost_source, "转载来源", required=False,
            )
        return target

    @classmethod
    def _publish_acknowledgement(cls, url: str, status: int, payload: object) -> dict | None:
        """Accept only a semantic publish receipt, never a generic 2xx request."""
        normalized_url = str(url or "").lower().split("?", 1)[0]
        if not 200 <= int(status) < 300 or "channels.weixin.qq.com" not in normalized_url:
            return None
        if any(token in normalized_url for token in ("/post/list", "/post/query", "mmdata", "report-perf")):
            return None
        if not re.search(r"post[_/-]?create|post[_/-]?publish|publish[_/-]?post|/publish(?:/|$)", normalized_url):
            return None
        if not isinstance(payload, (dict, list)):
            return None

        platform_content_id = ""
        platform_export_id = ""
        semantic_codes: list[object] = []
        stack = [payload]
        while stack:
            current = stack.pop()
            if isinstance(current, dict):
                for key, value in current.items():
                    normalized_key = str(key).replace("_", "").lower()
                    if normalized_key == "exportid" and value:
                        platform_export_id = platform_export_id or str(value)
                        platform_content_id = platform_content_id or platform_export_id
                    elif normalized_key in {"objectid", "postid", "videoid", "feedid"} and value:
                        platform_content_id = platform_content_id or str(value)
                    elif normalized_key in {"errcode", "retcode", "ret", "code"} and not isinstance(value, (dict, list)):
                        semantic_codes.append(value)
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(current, list):
                stack.extend(current)

        def is_success_code(value: object) -> bool:
            if isinstance(value, bool):
                return value is True
            normalized = str(value).strip().lower() if value is not None else ""
            return normalized in {"0", "ok", "success", "succeeded"}

        explicit_code = semantic_codes[0] if semantic_codes else None
        if semantic_codes and not any(is_success_code(value) for value in semantic_codes):
            return None
        if not semantic_codes and not (platform_content_id or platform_export_id):
            return None
        return {
            "status": int(status),
            "semantic_code": "" if explicit_code is None else str(explicit_code)[:80],
            "platform_content_id": platform_content_id[:160],
            "platform_export_id": platform_export_id[:160],
            "endpoint": normalized_url[:500],
        }

    def publish(
        self,
        account: ChannelsAccount,
        object_key: str,
        filename: str,
        title: str,
        description: str,
        tags: list[str],
        product_id: str = "",
        product_name: str = "",
        cover_object_key: str = "",
        cover_filename: str = "",
        video_annotation: str = "none",
        annotation_shooting_time: str = "",
        annotation_shooting_location: str = "",
        annotation_repost_source: str = "",
        on_stage: Callable[[str, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        mark_submitted: Callable[[str], None] | None = None,
        on_identity: Callable[[str], None] | None = None,
    ) -> dict:
        transport = settings.channels_publish_transport
        if transport not in {"auto", "internal_api", "browser_ui", "direct_internal_api"}:
            transport = "auto"
        allowlist = {x.strip() for x in settings.channels_direct_account_ids.split(",") if x.strip()}
        if account.id in allowlist:
            transport = "direct_internal_api"
        elif (transport == "auto" and not allowlist
              and account.channel_cookies_ciphertext and account.external_account_id):
            # Auto uses an already-authorized HTTP session when available. A
            # nonempty rollout allowlist or explicit browser setting remains an
            # operator override. Identity/revoke checks still happen after lock.
            transport = "direct_internal_api"
        checkpoint = False

        def checkpoint_once(client_id):
            nonlocal checkpoint
            checkpoint = True  # A callback can commit and then fail: never fall back.
            if not mark_submitted:
                raise ChannelsError("缺少防重发检查点，已停止发布", "submit_publish", code="CHECKPOINT_REQUIRED")
            mark_submitted(client_id)

        def surfaced(error, used_transport):
            return ChannelsError(str(error), error.stage, code=error.code,
                                 definitive=error.definitive, transport=used_transport)
        kwargs = {
            "account": account,
            "object_key": object_key,
            "filename": filename,
            "title": title,
            "description": description,
            "tags": tags,
            "product_id": product_id,
            "product_name": product_name,
            "cover_object_key": cover_object_key,
            "cover_filename": cover_filename,
            "video_annotation": video_annotation,
            "annotation_shooting_time": annotation_shooting_time,
            "annotation_shooting_location": annotation_shooting_location,
            "annotation_repost_source": annotation_repost_source,
            "on_stage": on_stage,
            "should_cancel": should_cancel,
            "mark_submitted": checkpoint_once,
            "on_identity": on_identity,
        }
        if transport == "direct_internal_api":
            try:
                result = self._publish_with_direct_internal_api(**kwargs)
                result["publish_transport"] = "direct_internal_api"
                return result
            except ChannelsInternalApiError as error:
                can_fallback = (
                    settings.channels_internal_api_browser_fallback and not checkpoint
                    and error.stage == "internal_preflight"
                    and error.code in {"DIRECT_CREDENTIALS_MISSING", "API_UNSUPPORTED", "USE_BROWSER_FOR_SELF_SHOT"}
                )
                if not can_fallback:
                    raise surfaced(error, "direct_internal_api") from None
                transport = "auto"
                if on_stage:
                    on_stage("internal_preflight", "直连凭据或接口暂不支持，已在提交前切换原上传通道")
        if transport in {"auto", "internal_api"}:
            try:
                result = self._publish_with_internal_api(**kwargs)
                result["publish_transport"] = "internal_api"
                return result
            except ChannelsPublishUncertain as error:
                raise surfaced(error, "internal_api") from None
            except ChannelsInternalApiError as error:
                if (transport != "auto" or checkpoint or error.stage != "internal_preflight"
                    or error.code not in {"API_UNSUPPORTED", "USE_BROWSER_FOR_SELF_SHOT"}
                    or not error.definitive):
                    raise surfaced(error, "internal_api") from None
                if on_stage:
                    on_stage("internal_preflight", "新上传通道预检未通过，正在上传前切换网页通道")
        result = self._publish_with_browser_ui(**kwargs)
        result["publish_transport"] = "browser_ui"
        return result

    def _publish_with_direct_internal_api(self, **kwargs) -> dict:
        account = kwargs["account"]
        with self._browser_lock(account.id):
            # Refresh after waiting for the lock: revoke or reauthorization may
            # have invalidated the detached object carried by a queued worker.
            with SessionLocal() as db:
                fresh = db.get(ChannelsAccount, account.id)
                if not fresh or fresh.status != "active" or fresh.owner_number != account.owner_number:
                    raise ChannelsInternalApiError("视频号授权不可用，请重新扫码", "authorization", code="AUTH_EXPIRED")
                if not fresh.channel_cookies_ciphertext or not fresh.external_account_id:
                    raise ChannelsInternalApiError("该账号尚未保存直连凭据，请重新扫码授权", code="DIRECT_CREDENTIALS_MISSING")
                db.expunge(fresh)
            account = fresh
            kwargs["account"] = account
            try:
                bundle = decrypt_credentials(self.auth_root, account.channel_cookies_ciphertext)
            except ChannelsCredentialsError as error:
                raise ChannelsInternalApiError(str(error), "authorization", code="AUTH_EXPIRED") from None
            previous_cookies = bundle["cookies"]
            revision = account.channel_cookies_ciphertext

            def persist_rotation(cookies):
                nonlocal revision, previous_cookies
                if cookies == previous_cookies:
                    return
                encrypted, session = encrypt_credentials(self.auth_root, cookies, bundle["user_agent"])
                with SessionLocal() as db:
                    # Conditional UPDATE also protects against a revoke committed
                    # between a SELECT and this write.
                    changed = db.query(ChannelsAccount).filter(
                        ChannelsAccount.id == account.id, ChannelsAccount.status == "active",
                        ChannelsAccount.auth_file == account.auth_file,
                        ChannelsAccount.channel_cookies_ciphertext == revision,
                    ).update({"channel_cookies_ciphertext": encrypted, "session_cookie_ciphertext": session,
                              "cookies_updated_at": datetime.utcnow()}, synchronize_session=False)
                    db.commit()
                    if changed:
                        revision, previous_cookies = encrypted, cookies

            client = HttpJsonClient(bundle, timeout=settings.channels_internal_request_timeout_seconds, on_cookies=persist_rotation)
            try:
                publisher = ChannelsInternalPublisher(
                    client=client, chunk_size=settings.channels_upload_chunk_bytes,
                    part_retries=settings.channels_upload_part_retries,
                    request_timeout=settings.channels_internal_request_timeout_seconds,
                    clip_timeout=settings.channels_clip_timeout_seconds,
                )
                preflight = publisher.preflight(
                    expected_external_account_id=account.external_account_id,
                    **{key: kwargs[key] for key in ("product_id", "video_annotation", "annotation_shooting_time",
                       "annotation_shooting_location", "annotation_repost_source", "on_stage", "on_identity", "should_cancel")},
                )
                with SessionLocal() as db:
                    db.query(ChannelsAccount).filter(
                        ChannelsAccount.id == account.id, ChannelsAccount.status == "active",
                        ChannelsAccount.channel_cookies_ciphertext == revision,
                    ).update({"last_verified_at": datetime.utcnow()}, synchronize_session=False)
                    db.commit()
                return self._publish_prepared(publisher, preflight, **kwargs)
            except (ChannelsInternalApiError, ChannelsError):
                raise
            except Exception as error:
                logger.warning("channels_direct_runtime_failed type=%s", type(error).__name__)
                raise ChannelsInternalApiError("视频号直连通道未完成，请查看任务阶段后重试或重新授权", "internal_runtime", code="DIRECT_RUNTIME_ERROR", definitive=False) from None
            finally:
                client.close()

    def _publish_with_internal_api(
        self,
        account: ChannelsAccount,
        object_key: str,
        filename: str,
        title: str,
        description: str,
        tags: list[str],
        product_id: str = "",
        product_name: str = "",
        cover_object_key: str = "",
        cover_filename: str = "",
        video_annotation: str = "none",
        annotation_shooting_time: str = "",
        annotation_shooting_location: str = "",
        annotation_repost_source: str = "",
        on_stage: Callable[[str, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        mark_submitted: Callable[[str], None] | None = None,
        on_identity: Callable[[str], None] | None = None,
    ) -> dict:
        from playwright.sync_api import sync_playwright

        auth_file = self._validated_auth_file(account)
        progress_report = on_stage or (lambda _stage, _message: None)
        browser = None
        context = None
        passed_preflight = False
        with self._browser_lock(account.id):
            try:
                with sync_playwright() as playwright:
                    browser, context, _persistent = self._launch_account_session(playwright, auth_file)
                    context.set_default_timeout(45000)
                    context.set_default_navigation_timeout(60000)
                    page = context.new_page()
                    self._goto_creator_page(page, PUBLISH_URL, timeout_ms=60000)
                    page.wait_for_timeout(1200)
                    if self._authorization_expired(page):
                        raise ChannelsInternalApiError(
                            "视频号授权已过期，请重新扫码授权",
                            "authorization",
                            code="AUTH_EXPIRED",
                        )
                    publisher = ChannelsInternalPublisher(
                        page,
                        chunk_size=settings.channels_upload_chunk_bytes,
                        part_retries=settings.channels_upload_part_retries,
                        request_timeout=settings.channels_internal_request_timeout_seconds,
                        clip_timeout=settings.channels_clip_timeout_seconds,
                    )
                    preflight_state = publisher.preflight(
                        expected_external_account_id=account.external_account_id or "",
                        product_id=product_id,
                        video_annotation=video_annotation,
                        annotation_shooting_time=annotation_shooting_time,
                        annotation_shooting_location=annotation_shooting_location,
                        annotation_repost_source=annotation_repost_source,
                        on_stage=on_stage,
                        on_identity=on_identity,
                        should_cancel=should_cancel,
                    )
                    passed_preflight = True
                    return self._publish_prepared(
                        publisher, preflight_state, account=account, object_key=object_key,
                        filename=filename, title=title, description=description, tags=tags,
                        product_id=product_id, product_name=product_name, cover_object_key=cover_object_key,
                        cover_filename=cover_filename, video_annotation=video_annotation,
                        annotation_shooting_time=annotation_shooting_time,
                        annotation_shooting_location=annotation_shooting_location,
                        annotation_repost_source=annotation_repost_source, on_stage=on_stage,
                        on_identity=on_identity, mark_submitted=mark_submitted, should_cancel=should_cancel,
                    )
            except ChannelsInternalApiError:
                raise
            except (ChannelsCancelled, ChannelsError):
                raise
            except Exception as error:
                logger.warning("channels_browser_runtime_failed preflight=%s type=%s", passed_preflight, type(error).__name__)
                if should_cancel and should_cancel():
                    raise ChannelsCancelled(
                        "已按要求停止发布；尚未进入平台发布步骤",
                        "internal_preflight" if not passed_preflight else "upload_original",
                    ) from error
                raise ChannelsInternalApiError(
                    "视频号新上传通道未完成",
                    "internal_preflight" if not passed_preflight else "internal_runtime",
                    definitive=False,
                ) from error
            finally:
                self._close_account_session(browser, context, auth_file)

    def _publish_prepared(
        self, publisher, preflight_state, *, account, object_key, filename, title, description, tags,
        product_id="", product_name="", cover_object_key="", cover_filename="", video_annotation="none",
        annotation_shooting_time="", annotation_shooting_location="", annotation_repost_source="",
        on_stage=None, on_identity=None, mark_submitted=None, should_cancel=None,
    ):
        progress_report = on_stage or (lambda _stage, _message: None)
        download_url = oss_service.url_for(object_key, download=True, expires_in=3600)
        if not download_url:
            raise ChannelsError("无法读取原视频下载地址", "download_original")
        if on_stage:
            on_stage("download_original", "授权预检已通过，正在从素材库读取原视频")
        with tempfile.TemporaryDirectory(prefix="wis-channels-api-") as temp_dir:
            local_path = Path(temp_dir) / Path(filename).name
            self._download_with_resume(
                download_url,
                local_path,
                "download_original",
                progress_report,
                "素材库原视频",
            )
            meta = probe_video(local_path)
            if should_cancel and should_cancel():
                raise ChannelsCancelled(
                    "已按要求停止发布；尚未进入平台发布步骤",
                    "download_original",
                )
            if cover_object_key:
                cover_url = oss_service.url_for(cover_object_key, download=True, expires_in=3600)
                if not cover_url:
                    raise ChannelsError("无法读取已选择的视频封面", "download_cover")
                cover_path = Path(temp_dir) / Path(cover_filename or cover_object_key).name
                self._download_with_resume(
                    cover_url,
                    cover_path,
                    "download_cover",
                    progress_report,
                    "自定义封面",
                    attempts=3,
                )
            else:
                cover_path = extract_cover(
                    local_path,
                    Path(temp_dir) / "finder_video_cover.jpg",
                    meta["duration"],
                )
            return publisher.publish(
                video_path=local_path,
                cover_path=cover_path,
                custom_cover=bool(cover_object_key),
                expected_external_account_id=account.external_account_id or "",
                title=title,
                description=description,
                tags=tags,
                product_id=product_id,
                product_name=product_name,
                video_annotation=video_annotation,
                annotation_shooting_time=annotation_shooting_time,
                annotation_shooting_location=annotation_shooting_location,
                annotation_repost_source=annotation_repost_source,
                on_stage=on_stage,
                on_identity=on_identity,
                mark_submitted=mark_submitted,
                should_cancel=should_cancel,
                preflight_state=preflight_state,
            )


    def _publish_with_browser_ui(
        self,
        account: ChannelsAccount,
        object_key: str,
        filename: str,
        title: str,
        description: str,
        tags: list[str],
        product_id: str = "",
        product_name: str = "",
        cover_object_key: str = "",
        cover_filename: str = "",
        video_annotation: str = "none",
        annotation_shooting_time: str = "",
        annotation_shooting_location: str = "",
        annotation_repost_source: str = "",
        on_stage: Callable[[str, str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        mark_submitted: Callable[[str], None] | None = None,
        on_identity: Callable[[str], None] | None = None,
    ) -> dict:
        """Publish the untouched OSS source file. No compression or transcoding is performed."""
        from playwright.sync_api import sync_playwright

        stage = "download_original"
        def report(next_stage: str, message: str) -> None:
            nonlocal stage
            stage = next_stage
            if on_stage:
                on_stage(next_stage, message)

        def check_cancel() -> None:
            if should_cancel and should_cancel():
                raise ChannelsCancelled("已按要求停止发布；尚未点击视频号平台的发布按钮", stage)

        check_cancel()
        auth_file = self._validated_auth_file(account)
        download_url = oss_service.url_for(object_key, download=True, expires_in=3600)
        if not download_url:
            raise ChannelsError("无法读取原视频下载地址", stage)
        report("download_original", "正在从素材库读取原视频；不压缩、不转码")
        with tempfile.TemporaryDirectory(prefix="wis-channels-") as temp_dir:
            local_path = Path(temp_dir) / Path(filename).name
            self._download_with_resume(
                download_url, local_path, "download_original", report, "素材库原视频",
            )
            check_cancel()
            cover_path: Path | None = None
            if cover_object_key:
                cover_url = oss_service.url_for(cover_object_key, download=True, expires_in=3600)
                if not cover_url:
                    raise ChannelsError("无法读取已选择的视频封面", "download_cover")
                cover_path = Path(temp_dir) / Path(cover_filename or cover_object_key).name
                self._download_with_resume(
                    cover_url, cover_path, "download_cover", report, "自定义封面", attempts=3,
                )
                check_cancel()
            source_size = max(local_path.stat().st_size, 1)
            # Large originals need substantially longer than a fixed 30-second browser wait.
            upload_wait_seconds = min(1800, max(420, int(source_size / (2 * 1024 * 1024)) + 300))
            browser = None
            context = None
            with self._browser_lock(account.id):
                try:
                    with sync_playwright() as playwright:
                        browser, context, _persistent = self._launch_account_session(playwright, auth_file)
                        context.set_default_timeout(45000)
                        context.set_default_navigation_timeout(60000)
                        page = context.new_page()
                        body = description.strip()
                        if tags:
                            body = (body + " " + " ".join(f"#{tag.lstrip('#')}" for tag in tags)).strip()
                        annotation_label = VIDEO_ANNOTATION_LABELS.get(video_annotation, video_annotation)
                        editor_wait_ms = min(
                            600000,
                            max(120000, int(source_size / (1024 * 1024)) * 1000 + 120000),
                        )

                        def fill_publish_form() -> None:
                            check_cancel()
                            editor = publish_root.locator("div.input-editor").first
                            editor.wait_for(state="visible", timeout=editor_wait_ms)
                            self._dismiss_non_publish_dialogs(page, publish_root, timeout_ms=1200)
                            report("fill_content", "原视频已被页面接收，正在填写标题与正文")
                            editor.fill(body, timeout=30000)
                            self._fill_short_title(page, publish_root, title, filename)
                            if product_id:
                                report("bind_product", f"正在核验并关联商品：{product_name or product_id}")
                                self._select_product(publish_root, product_id, product_name)
                            report("set_video_annotation", f"正在选择并核验视频标注：{annotation_label}")
                            self._apply_video_annotation(
                                page,
                                publish_root,
                                video_annotation,
                                annotation_shooting_time,
                                annotation_shooting_location,
                                annotation_repost_source,
                            )
                            check_cancel()

                        report("open_publish_page", "正在打开视频号发布页")
                        publish_root, file_input = self._open_publish_page(page)
                        check_cancel()
                        report("upload_original", f"正在上传原视频，最长等待 {upload_wait_seconds // 60} 分钟")
                        file_input.set_input_files(str(local_path), timeout=45000)
                        check_cancel()
                        fill_publish_form()
                        report("wait_platform", "正在等待视频号完成原视频处理并开放发布按钮")
                        page.wait_for_timeout(1200)
                        publish_button = publish_root.get_by_role("button", name=re.compile("发表|发布")).last
                        publish_button.wait_for(state="visible", timeout=120000)
                        deadline = time.monotonic() + upload_wait_seconds
                        upload_error = publish_root.get_by_text(re.compile("上传失败|视频处理失败|格式不支持|网络异常|重新上传")).first
                        upload_attempt = 1
                        last_progress = -1
                        last_progress_report_at = 0.0
                        while self._button_visually_disabled(publish_button) and time.monotonic() < deadline:
                            check_cancel()
                            if self._authorization_expired(page):
                                raise ChannelsError("视频号授权已过期，请重新扫码授权", "upload_original")
                            if upload_error.count() and upload_error.is_visible():
                                detail = upload_error.inner_text().strip()
                                if self._transient_upload_error(detail) and upload_attempt < 3:
                                    upload_attempt += 1
                                    report(
                                        "upload_original",
                                        f"上传连接中断，正在自动重连并重新续传（第 {upload_attempt}/3 次）",
                                    )
                                    self._dismiss_non_publish_dialogs(page, publish_root, timeout_ms=1800)
                                    retry_button = self._first_visible([
                                        publish_root.get_by_role("button", name=re.compile(r"重新上传|重试|再试一次")),
                                        publish_root.get_by_text(re.compile(r"重新上传|重试|再试一次"), exact=True),
                                    ], timeout_ms=1800)
                                    if retry_button:
                                        try:
                                            retry_button.click(timeout=5000)
                                        except Exception:
                                            pass
                                    publish_root, file_input = self._publish_root_and_input(page)
                                    if not file_input.count():
                                        publish_root, file_input = self._open_publish_page(page)
                                    check_cancel()
                                    file_input.set_input_files(str(local_path), timeout=45000)
                                    check_cancel()
                                    fill_publish_form()
                                    publish_button = publish_root.get_by_role("button", name=re.compile("发表|发布")).last
                                    publish_button.wait_for(state="visible", timeout=120000)
                                    upload_error = publish_root.get_by_text(
                                        re.compile("上传失败|视频处理失败|格式不支持|网络异常|重新上传")
                                    ).first
                                    deadline = time.monotonic() + upload_wait_seconds
                                    last_progress = -1
                                    continue
                                raise ChannelsError(f"视频号未完成原视频处理：{detail[:160]}", "upload_original")
                            progress = self._upload_progress_percent(page)
                            now = time.monotonic()
                            if progress is not None and (
                                progress >= last_progress + 5 or now - last_progress_report_at >= 20
                            ):
                                report(
                                    "upload_original",
                                    f"正在上传原视频：{progress}%（连接保持中，第 {upload_attempt}/3 次）",
                                )
                                last_progress = progress
                                last_progress_report_at = now
                            page.wait_for_timeout(2000)
                        if self._button_visually_disabled(publish_button):
                            raise ChannelsError(
                                f"原视频已传到视频号页面，但等待 {upload_wait_seconds // 60} 分钟后仍未开放发布按钮",
                                "wait_platform",
                            )
                        cover_warning = ""
                        if cover_path:
                            check_cancel()
                            report("set_cover", "原视频处理完成，正在上传并设置自定义封面")
                            try:
                                self._apply_custom_cover(page, publish_root, cover_path)
                                report("wait_platform", "自定义封面已提交，正在等待视频号重新开放发布按钮")
                                cover_deadline = time.monotonic() + 120
                                while self._button_visually_disabled(publish_button) and time.monotonic() < cover_deadline:
                                    check_cancel()
                                    page.wait_for_timeout(1000)
                                if self._button_visually_disabled(publish_button):
                                    raise ChannelsError("封面已上传，但视频号在 2 分钟内未重新开放发布按钮", "set_cover")
                            except ChannelsError as error:
                                # An explicit custom cover is not optional. Never
                                # silently publish a different image on UI failure.
                                raise ChannelsError("自定义封面未设置成功，已停止发布：" + str(error)[:400], "set_cover") from error
                        publish_responses: list[str] = []
                        accepted_responses: list[dict] = []

                        def remember_publish_response(response) -> None:
                            try:
                                request = response.request
                                url = response.url
                                if request.method != "POST" or "channels.weixin.qq.com" not in url:
                                    return
                                if "helper/hepler_merlin_mmdata" in url or "report-perf" in url:
                                    return
                                if not any(token in url.lower() for token in ("post", "publish", "video", "finder")):
                                    return
                                detail = f"{response.status} {url.split('?')[0]}"
                                # Never retain response bodies or request JSON:
                                # they can contain auth keys and signed CDN URLs.
                                publish_responses.append(detail[:700])
                                del publish_responses[:-20]
                                try:
                                    parsed = response.json()
                                except Exception:
                                    parsed = None
                                acknowledgement = self._publish_acknowledgement(url, int(response.status), parsed)
                                if acknowledgement:
                                    accepted_responses.append(acknowledgement)
                            except Exception:
                                return

                        page.on("response", remember_publish_response)
                        # This is the final reversible boundary.  After the
                        # click, platform acceptance must be read back instead
                        # of being represented as a successful cancellation.
                        check_cancel()
                        report("submit_publish", "平台已允许发布，正在提交")
                        check_cancel()
                        publish_client_id = str(uuid4())
                        if mark_submitted:
                            mark_submitted(publish_client_id)
                        publish_button.click()
                        page.wait_for_timeout(1200)
                        dialogs = publish_root.locator('[role="dialog"], .weui-desktop-dialog')

                        def acknowledge_publish_dialog() -> bool:
                            for dialog_index in range(dialogs.count()):
                                dialog = dialogs.nth(dialog_index)
                                if not dialog.is_visible():
                                    continue
                                dialog_text = dialog.inner_text().strip()
                                if not re.search(r"发表|发布|商品|审核", dialog_text):
                                    continue
                                confirm_button = dialog.get_by_role(
                                    "button",
                                    name=re.compile(r"确认发表|确认发布|继续发表|继续发布|确定|确认|发表|发布"),
                                ).last
                                if confirm_button.count() and confirm_button.is_visible():
                                    confirm_button.click()
                                    page.wait_for_timeout(1000)
                                    return True
                            return False

                        acknowledge_publish_dialog()
                        report("confirm_publish", "已点击发布，正在等待视频号成功回执")
                        success = publish_root.get_by_text(re.compile("发表成功|发布成功|已发布|发布完成")).first
                        publish_error = publish_root.get_by_text(re.compile("发布失败|发表失败|审核失败|请重试")).first
                        confirm_deadline = time.monotonic() + 180
                        accepted_hint = False
                        explicit_success = False
                        while time.monotonic() < confirm_deadline:
                            acknowledge_publish_dialog()
                            if success.count() and success.is_visible():
                                accepted_hint = True
                                explicit_success = True
                                break
                            if publish_error.count() and publish_error.is_visible():
                                detail = publish_error.inner_text().strip()
                                raise ChannelsError(f"视频号拒绝发布：{detail[:160]}", "confirm_publish")
                            page.wait_for_timeout(2000)
                        if not accepted_hint and not accepted_responses:
                            trace_id = self._capture_page_diagnostic(page, "confirm-publish")
                            logger.warning(
                                "channels_publish_receipt_missing trace_id=%s response_count=%s",
                                trace_id or "none", len(publish_responses),
                            )
                            suffix = f"（诊断编号 {trace_id}）" if trace_id else ""
                            raise ChannelsError(
                                f"已点击发布，但未读取到平台受理或成功回执{suffix}；已转待核验，不会重复上传",
                                "confirm_publish",
                            )
                        return {
                            "accepted": True,
                            # The creator page can only prove platform receipt.
                            # Public status is promoted exclusively by readback.
                            "confirmed": False,
                            "platform_acknowledged": bool(explicit_success or accepted_responses),
                            "platform_content_id": next((item["platform_content_id"] for item in accepted_responses if item["platform_content_id"]), ""),
                            "platform_export_id": next((item["platform_export_id"] for item in accepted_responses if item["platform_export_id"]), ""),
                            "platform_export_source": "publish_response" if any(item["platform_export_id"] for item in accepted_responses) else "",
                            "publish_client_id": publish_client_id,
                            "cover_warning": cover_warning,
                            "message": (
                                "视频号页面已提示发布成功，正在核验内容列表与公开状态"
                                if explicit_success
                                else "视频号平台已受理，正在核验内容列表与公开展示状态"
                            ) + ("；自定义封面待后续核验" if cover_warning else ""),
                        }
                except ChannelsError:
                    raise
                except Exception as error:
                    raw = str(error)
                    if "Timeout" in raw or "timeout" in raw.lower():
                        stage_labels = {
                            "download_original": "读取素材库原视频",
                            "download_cover": "读取自定义封面",
                            "open_publish_page": "打开发布页",
                            "upload_original": "上传原视频",
                            "fill_content": "填写发布内容",
                            "bind_product": "关联商品",
                            "set_video_annotation": "设置视频标注",
                            "set_cover": "设置自定义封面",
                            "wait_platform": "等待平台处理视频",
                            "submit_publish": "提交发布",
                            "confirm_publish": "等待发布回执",
                        }
                        label = stage_labels.get(stage, "处理发布任务")
                        raise ChannelsError(f"{label}阶段响应超时，请稍后重试；原视频未压缩、未转码", stage) from error
                    if "net::" in raw or "Connection" in raw:
                        raise ChannelsError("视频号平台网络连接中断，请稍后点击重试", stage) from error
                    raise ChannelsError(f"视频号发布未完成：{raw[:220]}", stage) from error
                finally:
                    self._close_account_session(browser, context, auth_file)

    @staticmethod
    def _metric_value(text: str, labels: tuple[str, ...]) -> int | None:
        for label in labels:
            match = re.search(rf"{re.escape(label)}\s*[:：]?\s*([\d,.]+)\s*([万wW]?)", text)
            if not match:
                continue
            value = float(match.group(1).replace(",", ""))
            if match.group(2):
                value *= 10000
            return max(0, int(round(value)))
        return None

    @staticmethod
    def _collect_post_api_records(response, records: list[dict]) -> None:
        """Collect transient post-list objects without logging platform data."""
        try:
            url = str(response.url or "").lower()
            if (
                not 200 <= int(response.status) < 300
                or "channels.weixin.qq.com" not in url
                or not any(token in url for token in ("post", "finder", "feed", "video", "list"))
            ):
                return
            content_type = str(response.headers.get("content-type") or "").lower()
            if "json" not in content_type:
                return
            payload = response.json()
            stack = [payload]
            while stack and len(records) < 800:
                current = stack.pop()
                if isinstance(current, dict):
                    lowered = {str(key).lower() for key in current}
                    if lowered.intersection({
                        "exportid", "export_id", "objectid", "object_id", "postid", "post_id",
                        "videoid", "video_id", "title", "desc", "description", "objectdesc",
                        "viewcount", "playcount", "readcount", "likecount", "commentcount",
                    }):
                        records.append(current)
                    stack.extend(value for value in current.values() if isinstance(value, (dict, list)))
                elif isinstance(current, list):
                    stack.extend(current)
        except Exception:
            return

    @staticmethod
    def _record_value(record: dict, aliases: tuple[str, ...]):
        normalized = {str(key).replace("_", "").lower(): value for key, value in record.items()}
        for alias in aliases:
            value = normalized.get(alias.replace("_", "").lower())
            if value is not None and value != "":
                return value
        return None

    @classmethod
    def _api_publication_state(cls, record: dict | None) -> str:
        # Live creator-list contract: status 1 is a normal published post and
        # visibleType 1 is public. Missing/unknown values are NOT public.
        if not record:
            return 'listed'
        status = cls._record_value(record, ('status',))
        visible = cls._record_value(record, ('visibleType',))
        if type(status) in (int, str) and type(visible) in (int, str) and str(status) == '1' and str(visible) == '1':
            return 'published'
        return 'listed'

    @staticmethod
    def _platform_identity_tokens(value: object) -> set[str]:
        normalized = str(value or "").strip()
        if not normalized:
            return set()
        return {normalized, normalized.split("/")[-1]}

    @classmethod
    def _matching_post_api_record(
        cls,
        records: list[dict],
        candidates: list[str],
        platform_content_id: str,
        platform_export_id: str = "",
        excluded_platform_ids: set[str] | None = None,
    ) -> dict | None:
        content_tokens = (
            cls._platform_identity_tokens(platform_content_id)
            | cls._platform_identity_tokens(platform_export_id)
        )
        excluded_tokens = {
            token
            for value in (excluded_platform_ids or set())
            for token in cls._platform_identity_tokens(value)
        }
        normalized_candidates = [candidate.casefold() for candidate in candidates if candidate.strip()]
        best: tuple[int, dict] | None = None
        best_identity = ""
        ambiguous = False
        for record in records:
            export_id = str(cls._record_value(record, ("exportId", "export_id")) or "")
            generic_id = str(cls._record_value(record, (
                "objectId", "object_id", "postId", "post_id", "videoId", "video_id",
            )) or "")
            text_fields = [
                str(cls._record_value(record, (alias,)) or "")
                for alias in ("title", "desc", "description", "objectDesc", "name", "filename")
            ]
            text_value = " ".join(text_fields).casefold()
            record_tokens = cls._platform_identity_tokens(export_id) | cls._platform_identity_tokens(generic_id)
            if record_tokens & excluded_tokens:
                continue
            id_match = bool(content_tokens and record_tokens & content_tokens)
            title_match = bool(normalized_candidates and any(candidate in text_value for candidate in normalized_candidates))
            # An export ID merely proves that the API object is a post. It is
            # not identity evidence for this delivery. The previous +10-only
            # fallback could assign one real post to several batch tasks.
            if not id_match and not title_match:
                continue
            score = (100 if id_match else 0) + (40 if title_match else 0) + (10 if export_id else 0)
            if score and (best is None or score > best[0]):
                best = (score, record)
                best_identity = export_id or generic_id
                ambiguous = False
            elif best and score == best[0] and (export_id or generic_id) != best_identity:
                ambiguous = True
        return best[1] if best and not ambiguous else None

    @classmethod
    def _api_record_metrics(cls, record: dict | None) -> dict:
        if not record:
            return {}

        def integer(*aliases: str) -> int | None:
            raw = cls._record_value(record, aliases)
            try:
                return max(0, int(float(str(raw).replace(",", "")))) if raw is not None and raw != "" else None
            except (TypeError, ValueError):
                return None

        return {
            "view_count": integer("viewCount", "playCount", "readCount"),
            "like_count": integer("likeCount", "likedCount"),
            "comment_count": integer("commentCount"),
            "share_count": integer("shareCount", "forwardCount"),
            "order_count": integer("orderCount", "dealOrderCount"),
            "gmv_fen": integer("gmvFen", "payAmountFen", "orderAmountFen"),
        }

    @classmethod
    def _verified_export_id(cls, record: dict | None, row_data: dict, href: str) -> tuple[str, str]:
        if record:
            value = cls._record_value(record, ("exportId", "export_id"))
            if value:
                return str(value), "creator_api"
        for key in ("export_id", "exportId"):
            value = (row_data or {}).get(key)
            if value:
                return str(value), "creator_dom"
        match = re.search(r"(?:[?&]|/)export(?:_|-)?id[=/]([A-Za-z0-9_-]+)", href, re.I)
        return (match.group(1), "creator_url") if match else ("", "")

    def _verify_selected_cover(self, record: dict | None, object_key: str) -> dict:
        if not object_key:
            return {"cover_status": "not_requested", "cover_message": "使用视频默认封面"}
        from .channels_covers import CoverError, prepare_custom_cover, verify_uploaded_cover
        uncertain = {"cover_status": "unverified", "cover_message": "自定义封面尚未完成图片一致性核验"}
        if not record:
            return uncertain
        desc = record.get("desc") or record.get("objectDesc") or {}
        media = desc.get("media") if isinstance(desc, dict) else None
        if not isinstance(media, list) or len(media) != 1 or not isinstance(media[0], dict):
            return uncertain
        # A playback thumb is deliberately NOT a fallback for a missing cover.
        profile_url = str(media[0].get("coverUrl") or "")
        if not profile_url:
            return uncertain
        try:
            with tempfile.TemporaryDirectory(prefix="channels-cover-readback-") as folder:
                directory = Path(folder)
                original = directory / "selected-image"
                source_url = oss_service.url_for(object_key, download=True, expires_in=600)
                if not source_url:
                    return uncertain
                self._download_with_resume(source_url, original, "verify_cover", lambda *_: None, "所选封面", attempts=1)
                full, profile = prepare_custom_cover(original, directory)
                same = verify_uploaded_cover(profile_url, profile, directory / "observed-profile.jpg")
                full_url = str(media[0].get("fullCoverUrl") or "")
                if same and full_url:
                    same = verify_uploaded_cover(full_url, full, directory / "observed-full.jpg")
                return {
                    "cover_status": "verified" if same else "mismatch",
                    "cover_message": "平台主页封面与所选图片一致" if same else "平台主页封面与所选图片不一致，封面未生效；不会重复发布",
                }
        except Exception:
            # Network/codec failures mean unknown, never success or image mismatch.
            return uncertain

    def read_delivery(
        self,
        account: ChannelsAccount,
        title: str,
        filename: str,
        platform_content_id: str = "",
        platform_export_id: str = "",
        excluded_platform_ids: set[str] | None = None,
        cover_object_key: str = "",
    ) -> dict:
        """Read the creator list before retrying or collecting metrics.

        Missing fields stay None: an unavailable platform field must never be
        represented as a real zero.
        """
        from playwright.sync_api import sync_playwright

        auth_file = self._validated_auth_file(account)
        browser = None
        context = None
        with self._browser_lock(account.id):
            try:
                with sync_playwright() as playwright:
                    browser, context, _persistent = self._launch_account_session(playwright, auth_file)
                    page = context.new_page()
                    api_records: list[dict] = []
                    page.on("response", lambda response: self._collect_post_api_records(response, api_records))
                    self._goto_creator_post_list(page)
                    page.wait_for_timeout(3500)
                    if self._authorization_expired(page):
                        raise ChannelsError("视频号授权已失效，请重新扫码", "readback")
                    candidates = self._delivery_search_candidates(title, filename)
                    node, matched = self._find_delivery_in_creator_list(
                        page,
                        candidates,
                        platform_content_id=platform_content_id,
                        platform_export_id=platform_export_id,
                        excluded_platform_ids=excluded_platform_ids,
                    )
                    api_record = self._matching_post_api_record(
                        api_records,
                        candidates,
                        platform_content_id,
                        platform_export_id,
                        excluded_platform_ids=excluded_platform_ids,
                    )
                    if node is None:
                        if api_record:
                            export_id, export_source = self._verified_export_id(api_record, {}, "")
                            api_metrics = self._api_record_metrics(api_record)
                            generic_id = str(self._record_value(api_record, (
                                "objectId", "object_id", "postId", "post_id", "videoId", "video_id",
                            )) or "")
                            api_state = self._api_publication_state(api_record)
                            return {
                                "found": True,
                                "published": api_state == "published",
                                "publication_state": api_state,
                                "matched": "creator_api",
                                "platform_content_id": generic_id,
                                "platform_export_id": export_id,
                                "platform_export_source": export_source,
                                **api_metrics,
                                **self._verify_selected_cover(api_record, cover_object_key),
                                "message": "已从视频号作品接口确认公开发布" if api_state == "published" else "已从视频号内容接口找到记录，正在继续核验公开展示状态",
                            }
                        return {
                            "found": False,
                            "message": f"已检查内容列表前 20 页，仍未找到“{candidates[0]}”；稍后继续核验",
                        }
                    row_data = node.evaluate("""
                        node => {
                          const row = node.closest('tr,[class*=item],[class*=card],[class*=row]') || node.parentElement;
                          const link = node.closest('a') || row?.querySelector('a');
                          return {
                            text: (row?.innerText || node.innerText || '').replace(/\\s+/g, ' ').trim(),
                            href: link?.href || '',
                             export_id: row?.getAttribute('data-export-id') || row?.dataset?.exportId
                               || row?.querySelector('[data-export-id]')?.getAttribute('data-export-id') || '',
                             cover_present: !!(row?.querySelector('img[src]:not([src=""]), [style*="background-image"]')),
                           };
                        }
                    """)
                    text_value = str((row_data or {}).get("text") or "")
                    href = str((row_data or {}).get("href") or "")
                    id_match = re.search(
                        r"(?:object|export|post|video)(?:[_-]?id)?[=/]([A-Za-z0-9_-]+)",
                        href,
                        re.I,
                    )
                    read_content_id = (
                        platform_content_id
                        if platform_content_id and platform_content_id.split("/")[-1] in href
                        else (id_match.group(1) if id_match else "")
                    )
                    if not api_record:
                        api_record = self._matching_post_api_record(
                            api_records,
                            candidates,
                            read_content_id,
                            platform_export_id,
                            excluded_platform_ids=excluded_platform_ids,
                        )
                    api_metrics = self._api_record_metrics(api_record)
                    api_content_id = str(self._record_value(api_record or {}, (
                        "objectId", "object_id", "postId", "post_id", "videoId", "video_id",
                    )) or "")
                    export_id, export_source = self._verified_export_id(api_record, row_data or {}, href)
                    dom_metrics = {
                        "view_count": self._metric_value(text_value, ("播放", "观看", "浏览")),
                        "like_count": self._metric_value(text_value, ("点赞", "喜欢")),
                        "comment_count": self._metric_value(text_value, ("评论",)),
                        "share_count": self._metric_value(text_value, ("分享", "转发")),
                        "order_count": self._metric_value(text_value, ("订单", "成交订单")),
                    }
                    publication_state = self._publication_state(text_value)
                    if publication_state == "listed":
                        publication_state = self._api_publication_state(api_record)
                    state_messages = {
                        "published": "已从视频号内容列表确认公开发布",
                        "pending": "视频号内容列表已受理，仍在等待平台审核",
                        "private": "视频号内容已生成，但当前不是公开展示状态",
                        "failed": "视频号内容列表显示发布或审核失败",
                        "listed": "视频号内容列表已找到记录，公开展示状态待核验",
                    }
                    return {
                        "found": True,
                        "published": publication_state == "published",
                        "publication_state": publication_state,
                        "matched": matched,
                        "platform_content_id": read_content_id or api_content_id,
                        "platform_export_id": export_id,
                        "platform_export_source": export_source,
                        "platform_content_url": href,
                        "cover_present": bool((row_data or {}).get("cover_present")),
                        **self._verify_selected_cover(api_record, cover_object_key),
                        "view_count": dom_metrics["view_count"] if dom_metrics["view_count"] is not None else api_metrics.get("view_count"),
                        "like_count": dom_metrics["like_count"] if dom_metrics["like_count"] is not None else api_metrics.get("like_count"),
                        "comment_count": dom_metrics["comment_count"] if dom_metrics["comment_count"] is not None else api_metrics.get("comment_count"),
                        "share_count": dom_metrics["share_count"] if dom_metrics["share_count"] is not None else api_metrics.get("share_count"),
                        "order_count": dom_metrics["order_count"] if dom_metrics["order_count"] is not None else api_metrics.get("order_count"),
                        "gmv_fen": api_metrics.get("gmv_fen"),
                        "message": state_messages[publication_state],
                    }
            except ChannelsError:
                raise
            except Exception as error:
                if self._transient_navigation_error(error) or "timeout" in str(error).lower():
                    raise ChannelsError(
                        "视频号主页加载较慢，本次未完成回流；数据保持待回流状态，请稍后重试",
                        "readback",
                    ) from error
                raise ChannelsError(f"视频号数据回读失败：{str(error)[:220]}", "readback") from error
            finally:
                self._close_account_session(browser, context, auth_file)


channels_service = ChannelsService()
