"""Private, versioned Channels credentials. Never include this data in API output."""

import json
import os
import re
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


class ChannelsCredentialsError(RuntimeError):
    pass


def public_channels_message(value: str | None) -> str:
    """Suppress old diagnostic payloads at the API boundary without editing history."""
    message = str(value or "")
    positions = [message.find(marker) for marker in ("；平台回执：", " request=", "；可见按钮：", "；页面：")]
    found = [position for position in positions if position >= 0]
    if found:
        message = message[:min(found)] + "（历史接口详情已隐藏）"
    if re.search(r"(?:sessionid|authkey|authorization|cookie|encfilekey|token)[\s\"':=]+", message, re.I):
        return "平台请求未完成，请查看任务阶段；敏感接口详情已隐藏"
    return message


def normalize_cookies(cookies: list[dict]) -> list[dict]:
    result = []
    for item in cookies:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").lower()
        host = domain.lstrip(".")
        if host != "weixin.qq.com" and not host.endswith(".weixin.qq.com"):
            continue
        if not item.get("name") or not isinstance(item.get("value"), str):
            continue
        result.append({
            "name": str(item["name"]), "value": item["value"], "domain": domain,
            "path": str(item.get("path") or "/"), "expires": item.get("expires", -1),
            "secure": bool(item.get("secure", False)), "httpOnly": bool(item.get("httpOnly", False)),
            "sameSite": item.get("sameSite") if item.get("sameSite") in {"Strict", "Lax", "None"} else "Lax",
        })
    return result


def _fernet(root: Path, *, create: bool) -> Fernet:
    configured = os.getenv("CHANNELS_SESSION_FERNET_KEY", "").strip()
    if configured:
        try:
            return Fernet(configured.encode("ascii"))
        except Exception:
            raise ChannelsCredentialsError("视频号凭据加密配置无效，请联系管理员") from None
    key_path = root / ".session-fernet.key"
    if not key_path.is_file() and create:
        root.mkdir(parents=True, exist_ok=True)
        # Atomic link publishes a complete key; concurrent initializations cannot
        # overwrite one another. Keep the durable key beside the protected profiles.
        fd, name = tempfile.mkstemp(prefix=".session-key-", dir=root)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(Fernet.generate_key())
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(name, 0o600)
            try:
                os.link(name, key_path)
            except FileExistsError:
                pass
        finally:
            Path(name).unlink(missing_ok=True)
    try:
        return Fernet(key_path.read_bytes().strip())
    except Exception:
        raise ChannelsCredentialsError("视频号凭据密钥不可用，请恢复密钥或重新扫码") from None


def encrypt_credentials(root: Path, cookies: list[dict], user_agent: str) -> tuple[str, str]:
    clean = normalize_cookies(cookies)
    session = next((x["value"] for x in clean if x["name"] == "sessionid" and x["value"]), "")
    if not session or not user_agent.strip():
        raise ChannelsCredentialsError("未取得完整视频号登录凭据，请重新扫码")
    cipher = _fernet(root, create=True)
    bundle = {"version": 1, "cookies": clean, "user_agent": user_agent.strip()}
    return (cipher.encrypt(json.dumps(bundle, ensure_ascii=False).encode()).decode(),
            cipher.encrypt(session.encode()).decode())


def decrypt_credentials(root: Path, ciphertext: str) -> dict:
    if not ciphertext:
        raise ChannelsCredentialsError("该账号尚未保存直连登录凭据，请重新扫码授权")
    try:
        bundle = json.loads(_fernet(root, create=False).decrypt(ciphertext.encode()))
        if bundle.get("version") != 1 or not bundle.get("user_agent"):
            raise ValueError()
        bundle["cookies"] = normalize_cookies(bundle.get("cookies") or [])
        if not any(x["name"] == "sessionid" and x["value"] for x in bundle["cookies"]):
            raise ValueError()
        return bundle
    except (InvalidToken, ValueError, TypeError, AttributeError):
        raise ChannelsCredentialsError("视频号登录凭据无法解密，请重新扫码授权") from None
