import hashlib
import secrets
from datetime import datetime, timedelta
from urllib.parse import urlencode
from uuid import uuid4

from fastapi import HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import JianyingDevice, JianyingPairing


PAIRING_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
PAIRING_TTL_MINUTES = 10


def _secret_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _user_number(user: dict) -> str:
    value = str(user.get("number") or user.get("userId") or user.get("id") or "").strip()
    if not value:
        raise HTTPException(403, "OA 账号缺少稳定工号，无法连接剪映助手")
    return value[:80]


def _user_name(user: dict) -> str:
    return str(user.get("realName") or user.get("name") or _user_number(user))[:120]


def normalized_code(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def external_app_url(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "").split(",")[0].strip()
    scheme = forwarded_proto or request.url.scheme or "https"
    host = request.headers.get("host", "").strip() or request.url.netloc
    prefix = request.headers.get("x-forwarded-prefix", "").strip().rstrip("/")
    return f"{scheme}://{host}{prefix}/"


def create_pairing(db: Session, user: dict, app_url: str) -> dict:
    now = datetime.utcnow()
    owner_number = _user_number(user)
    db.query(JianyingPairing).filter(
        JianyingPairing.owner_number == owner_number,
        JianyingPairing.status == "pending",
    ).update({"status": "replaced", "updated_at": now})
    code = "".join(secrets.choice(PAIRING_ALPHABET) for _ in range(10))
    pairing = JianyingPairing(
        id=str(uuid4()),
        owner_number=owner_number,
        owner_name=_user_name(user),
        code_hash=_secret_hash(code),
        status="pending",
        expires_at=now + timedelta(minutes=PAIRING_TTL_MINUTES),
        created_at=now,
        updated_at=now,
    )
    db.add(pairing)
    db.commit()
    api_base = app_url.rstrip("/") + "/api"
    scheme_url = "wis-jianying://pair?" + urlencode({"code": code, "server": api_base})
    return {
        "id": pairing.id,
        "code": code,
        "status": pairing.status,
        "expires_at": pairing.expires_at.isoformat(timespec="seconds") + "Z",
        "scheme_url": scheme_url,
        "api_base": api_base,
    }


def claim_pairing(db: Session, code: str, device_name: str) -> dict:
    now = datetime.utcnow()
    normalized = normalized_code(code)
    if len(normalized) != 10:
        raise HTTPException(400, "配对码格式不正确")
    pairing = db.scalar(
        select(JianyingPairing).where(JianyingPairing.code_hash == _secret_hash(normalized))
    )
    if not pairing or pairing.status != "pending":
        raise HTTPException(404, "配对码不存在或已使用")
    if pairing.expires_at <= now:
        pairing.status = "expired"
        pairing.updated_at = now
        db.commit()
        raise HTTPException(410, "配对码已过期，请在素材库重新生成")

    raw_token = "wjy_" + secrets.token_urlsafe(36)
    device = JianyingDevice(
        id=str(uuid4()),
        owner_number=pairing.owner_number,
        owner_name=pairing.owner_name,
        device_name=(device_name.strip() or "Windows 剪映助手")[:120],
        token_hash=_secret_hash(raw_token),
        active=True,
        last_seen_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(device)
    db.flush()
    pairing.status = "claimed"
    pairing.device_id = device.id
    pairing.claimed_at = now
    pairing.updated_at = now
    db.commit()
    return {
        "access_token": raw_token,
        "token_type": "bearer",
        "device": device_out(device),
        "owner": {"number": device.owner_number, "name": device.owner_name},
    }


def device_from_request(request: Request, db: Session) -> JianyingDevice:
    authorization = request.headers.get("authorization", "").strip()
    if not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "剪映助手尚未配对")
    token = authorization[7:].strip()
    if not token.startswith("wjy_") or len(token) < 32:
        raise HTTPException(401, "剪映助手令牌无效")
    device = db.scalar(
        select(JianyingDevice).where(
            JianyingDevice.token_hash == _secret_hash(token),
            JianyingDevice.active.is_(True),
        )
    )
    if not device:
        raise HTTPException(401, "剪映助手连接已失效，请重新配对")
    now = datetime.utcnow()
    if not device.last_seen_at or now - device.last_seen_at >= timedelta(minutes=5):
        device.last_seen_at = now
        device.updated_at = now
        db.commit()
    request.state.user = device_user(device)
    return device


def device_user(device: JianyingDevice) -> dict:
    return {
        "number": device.owner_number,
        "realName": device.owner_name,
        "groupName": "WIS 剪映助手",
        "status": "normal",
    }


def device_out(device: JianyingDevice) -> dict:
    return {
        "id": device.id,
        "device_name": device.device_name,
        "active": device.active,
        "last_seen_at": device.last_seen_at.isoformat(timespec="seconds") + "Z" if device.last_seen_at else None,
        "created_at": device.created_at.isoformat(timespec="seconds") + "Z",
    }
