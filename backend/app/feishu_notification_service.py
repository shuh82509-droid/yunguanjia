import json
import os
import time
from dataclasses import dataclass
from threading import Lock

import requests


@dataclass(frozen=True)
class FeishuSendResult:
    status: str
    error: str = ""
    message_id: str = ""


class FeishuNotificationService:
    """Best-effort Feishu delivery for video-request notifications.

    In-app notifications remain the source of truth. External delivery is an
    outbox consumer so a transient Feishu/network failure never rolls back a
    request workflow action.
    """

    def __init__(self) -> None:
        self.app_id = os.getenv("FEISHU_APP_ID", "").strip()
        self.app_secret = os.getenv("FEISHU_APP_SECRET", "").strip()
        self.webhook_url = os.getenv("FEISHU_VIDEO_REQUEST_WEBHOOK_URL", "").strip()
        self.receive_id_type = os.getenv("FEISHU_RECEIVE_ID_TYPE", "user_id").strip() or "user_id"
        self.public_url = os.getenv("WIS_PUBLIC_URL", "").strip()
        self.supervisor_id = os.getenv("VIDEO_REQUEST_ASSIGNER_FEISHU_ID", "").strip()
        self.recipient_map = self._load_recipient_map()
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = Lock()

    @property
    def configured(self) -> bool:
        return bool((self.app_id and self.app_secret) or self.webhook_url)

    def _load_recipient_map(self) -> dict[str, str]:
        raw = os.getenv("FEISHU_RECIPIENT_MAP_JSON", "").strip()
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(value, dict):
            return {}
        return {
            str(key).strip(): str(recipient).strip()
            for key, recipient in value.items()
            if str(key).strip() and str(recipient).strip()
        }

    def _recipient_id(self, number: str, name: str) -> str:
        for key in (number.strip(), name.strip()):
            if key and self.recipient_map.get(key):
                return self.recipient_map[key]
        if name.strip() == "何雨庭" and self.supervisor_id:
            return self.supervisor_id
        # OA employee numbers can be used directly when the Feishu app's
        # receive_id_type is configured to the matching corporate user_id.
        if self.receive_id_type == "user_id":
            return number.strip()
        return ""

    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and time.time() < self._token_expires_at:
                return self._token
            response = requests.post(
                "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self.app_id, "app_secret": self.app_secret},
                timeout=12,
            )
            response.raise_for_status()
            payload = response.json()
            if payload.get("code") != 0 or not payload.get("tenant_access_token"):
                raise RuntimeError(str(payload.get("msg") or "获取 tenant_access_token 失败"))
            self._token = str(payload["tenant_access_token"])
            self._token_expires_at = time.time() + max(60, int(payload.get("expire") or 7200) - 120)
            return self._token

    @staticmethod
    def _response_payload(response: requests.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            return {}
        return payload if isinstance(payload, dict) else {}

    @staticmethod
    def _api_error(response: requests.Response, payload: dict, fallback: str) -> str:
        raw_code = payload.get("code", payload.get("StatusCode", response.status_code))
        code = str(raw_code)
        message = str(
            payload.get("msg")
            or payload.get("message")
            or payload.get("StatusMessage")
            or fallback
        ).strip()
        if code in {"230003", "230013", "230027"}:
            message += "；请检查通知应用的可用范围、通讯录权限范围与机器人发消息权限"
        return f"飞书错误 {code}：{message}"[:1000]

    def send(self, *, recipient_number: str, recipient_name: str, title: str, message: str) -> FeishuSendResult:
        if not self.configured:
            return FeishuSendResult("skipped", "生产环境尚未配置飞书应用或群机器人")
        body = f"【{title}】\n{message}"
        if self.public_url:
            body += f"\n打开 WIS：{self.public_url}"
        try:
            recipient_id = self._recipient_id(recipient_number, recipient_name)
            if self.app_id and self.app_secret and recipient_id:
                token = self._tenant_token()
                response = requests.post(
                    "https://open.feishu.cn/open-apis/im/v1/messages",
                    params={"receive_id_type": self.receive_id_type},
                    headers={"Authorization": f"Bearer {token}"},
                    json={
                        "receive_id": recipient_id,
                        "msg_type": "text",
                        "content": json.dumps({"text": body}, ensure_ascii=False),
                    },
                    timeout=12,
                )
                payload = self._response_payload(response)
                if response.status_code >= 400 or payload.get("code") != 0:
                    raise RuntimeError(self._api_error(response, payload, "飞书消息发送失败"))
                message_id = str((payload.get("data") or {}).get("message_id") or "")
                return FeishuSendResult("sent", message_id=message_id)
            if self.webhook_url:
                response = requests.post(
                    self.webhook_url,
                    json={"msg_type": "text", "content": {"text": body}},
                    timeout=12,
                )
                payload = self._response_payload(response)
                if response.status_code >= 400 or payload.get("code", payload.get("StatusCode", 0)) != 0:
                    raise RuntimeError(self._api_error(response, payload, "飞书机器人发送失败"))
                return FeishuSendResult("sent")
            return FeishuSendResult("skipped", "未找到收件人的飞书用户 ID 映射")
        except Exception as error:
            return FeishuSendResult("failed", str(error)[:1000])


feishu_notification_service = FeishuNotificationService()
