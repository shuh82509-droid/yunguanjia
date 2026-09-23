import hmac
import os

from fastapi import HTTPException, Request


def require_workstation(request: Request) -> dict:
    expected = os.getenv("WIS_WORKSTATION_API_TOKEN", "").strip()
    if len(expected) < 32:
        raise HTTPException(503, "混剪工作台集成尚未配置")

    provided = str(request.headers.get("x-wis-workstation-token") or "").strip()
    authorization = str(request.headers.get("authorization") or "").strip()
    if not provided and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    if not provided or not hmac.compare_digest(provided, expected):
        raise HTTPException(401, "混剪工作台集成凭证无效")

    return {
        "number": "SERVICE-WIS-REMIX",
        "realName": "WIS混剪工作台",
        "status": "normal",
        "service": True,
    }
