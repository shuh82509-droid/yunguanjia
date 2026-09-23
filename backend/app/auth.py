import json
import logging
import os
import re
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException, Request
from .admission_policy import confirmed_departure


OA_API = os.getenv("OA_API_BASE_URL", "https://api.fandow.com").rstrip("/")
SESSION_COOKIE = "wis_oa_session"
logger = logging.getLogger(__name__)


def _request_json(url: str, *, method: str = "GET", payload: dict | None = None, token: str = "") -> tuple[int, dict, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = UrlRequest(url, data=body, method=method, headers=headers)
    try:
        with urlopen(request, timeout=15) as response:
            raw = response.read().decode("utf-8", errors="replace")
            try:
                data = json.loads(raw)
            except ValueError:
                data = {}
            return response.status, data, dict(response.headers)
    except HTTPError as error:
        raw = error.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except ValueError:
            data = {}
        return error.code, data, dict(error.headers)
    except (URLError, TimeoutError) as error:
        raise HTTPException(502, "OA 登录服务暂时不可用") from error


def _password_grant_payload(username: str, password: str, captcha: str = "") -> dict:
    """Build the documented payload and append the fields required by the current OA client."""
    payload = {"username": username, "password": password}
    if captcha.strip():
        payload["captcha"] = captcha.strip()
    client_id = os.getenv("OA_CLIENT_ID", "").strip()
    client_secret = os.getenv("OA_CLIENT_SECRET", "").strip()
    if client_id and client_secret:
        payload.update(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": os.getenv("OA_GRANT_TYPE", "password").strip() or "password",
            }
        )
    return payload


def _token_from(body: object) -> str:
    if not isinstance(body, dict):
        return ""
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    for source in (body, data):
        for key in ("access_token", "accessToken", "token"):
            value = source.get(key)
            if value:
                return str(value)
    return ""


def _masked_username(username: str) -> str:
    value = username.strip()
    if len(value) <= 4:
        return "***"
    return f"{value[:4]}***{value[-2:]}"


def _upstream_error_detail(status: int, body: object) -> tuple[str, str]:
    source = body if isinstance(body, dict) else {}
    nested = source.get("data") if isinstance(source.get("data"), dict) else {}
    code_value = source.get("code") or source.get("error_code") or nested.get("code") or status
    message = ""
    for container in (source, nested):
        for key in ("msg", "message", "error_description", "detail", "error"):
            value = container.get(key)
            if isinstance(value, (str, int, float)) and str(value).strip():
                message = re.sub(r"\s+", " ", str(value)).strip()[:160]
                break
        if message:
            break
    if not message or "<html" in message.lower():
        if status in (400, 401):
            message = "OA 账号或密码未通过验证"
        elif status == 403:
            message = "OA 账号无权使用当前登录方式"
        elif status == 429:
            message = "OA 登录请求过于频繁，请稍后再试"
        elif status >= 500:
            message = "OA 登录服务暂时异常"
        else:
            message = "OA 登录未完成"
    code = str(code_value)
    return f"{message}（OA 错误 {code}）", code


def _department_restriction_enabled() -> bool:
    return os.getenv("OA_ENFORCE_DEPARTMENT", "true").strip().lower() in {"1", "true", "yes", "on"}


def _allowed_departments() -> tuple[str, ...]:
    raw = os.getenv("OA_ALLOWED_DEPARTMENTS", "").strip()
    if not raw:
        raw = os.getenv("OA_ALLOWED_DEPARTMENT", "品牌营销").strip()
    return tuple(
        value.strip().casefold()
        for value in re.split(r"[,，;；\n\r]+", raw)
        if value.strip()
    )


def _explicit_user_allowed(user: dict) -> bool:
    raw = os.getenv("OA_ALLOWED_USERS", "")
    allowed = {
        value.strip().casefold()
        for value in re.split(r"[,，;\n\r]+", raw)
        if value.strip()
    }
    if not allowed:
        return False
    identifiers = {
        str(user.get(field) or "").strip().casefold()
        for field in ("number", "username", "userName", "realName", "name", "open_id")
        if str(user.get(field) or "").strip()
    }
    return bool(allowed & identifiers)


def _organization_from_user(user: dict) -> tuple[str, str]:
    """Return stable department and center labels from the varying OA payload shapes."""
    group_name = str(user.get("groupName") or "").strip()
    department = str(user.get("parentDept") or user.get("department") or "").strip()
    center = str(user.get("center") or user.get("deptName") or "").strip()
    if group_name and not department and not center:
        for separator in ("-", "·", "/"):
            left, found, right = group_name.partition(separator)
            if found and "部" in left and "中心" in right:
                department, center = left.strip(), right.strip()
                break
    if group_name and not department and "部" in group_name and "中心" not in group_name:
        department = group_name
    if group_name and not center and "中心" in group_name:
        center = group_name
    return department[:255], center[:255]


def _database_user_allowed(user: dict) -> bool | None:
    """Resolve administrator-managed grants without making the OA service stateful."""
    try:
        from .database import SessionLocal
        from .models import AdminGrant, ModuleAccessGrant, OaAccessGrant

        number = str(user.get("number") or user.get("userId") or user.get("id") or "").strip()
        name = str(user.get("realName") or user.get("name") or "").strip()
        identifiers = []
        if number:
            identifiers.append(f"number:{number.upper()}")
        if name:
            identifiers.append(f"name:{name.casefold()}")
        if not identifiers:
            return None
        with SessionLocal() as db:
            inactive_match = False
            for identifier in identifiers:
                grant = db.get(OaAccessGrant, identifier)
                if not grant:
                    continue
                if not grant.active:
                    inactive_match = True
                    continue
                changed = False
                department, center = _organization_from_user(user)
                related_rows = [grant]
                for model in (AdminGrant, ModuleAccessGrant):
                    related = None
                    for item in identifiers:
                        candidate = db.get(model, item)
                        if candidate:
                            related = candidate
                            break
                    if related:
                        related_rows.append(related)
                for row in related_rows:
                    if number and not row.user_number:
                        row.user_number = number[:80]
                        changed = True
                    if name and not row.real_name:
                        row.real_name = name[:120]
                        changed = True
                    if department and row.department != department:
                        row.department = department
                        changed = True
                    if center and row.center != center:
                        row.center = center
                        changed = True
                if changed:
                    from datetime import datetime

                    grant.last_verified_at = datetime.utcnow()
                    db.commit()
                return True
            if inactive_match:
                return False
    except Exception:
        logger.exception("Could not read the OA access whitelist")
    return None


def user_allowed(user: dict) -> bool:
    if confirmed_departure(user):
        return False
    status = str(user.get("status") or "").strip().lower()
    if status != "normal":
        return False
    # Automatic department access is the primary rule: an active colleague in an
    # allowed department must not be locked out by a stale per-person exception
    # row. Individual grants remain available for colleagues outside that scope.
    if _department_restriction_enabled():
        keywords = _allowed_departments()
        department = " ".join(
            str(user.get(field) or "")
            for field in ("groupName", "parentDept", "deptName", "department", "center")
        ).casefold()
        if keywords and any(keyword in department for keyword in keywords):
            return True
    database_decision = _database_user_allowed(user)
    if database_decision is not None:
        return database_decision
    if _explicit_user_allowed(user):
        return True
    if not _department_restriction_enabled():
        return True
    keywords = _allowed_departments()
    if not keywords:
        return True
    department = " ".join(
        str(user.get(field) or "")
        for field in ("groupName", "parentDept", "deptName", "department", "center")
    ).casefold()
    return any(keyword in department for keyword in keywords)


def _assert_user_allowed(user: dict) -> None:
    if confirmed_departure(user):
        raise HTTPException(403, {"code": "CONFIRMED_DEPARTURE", "message": "该成员已确认离职，不能进入中枢或业务工作台"})
    if str(user.get("status") or "").strip().lower() != "normal":
        raise HTTPException(403, "当前 OA 账号已冻结或状态异常，无法进入")
    if not user_allowed(user):
        raise HTTPException(403, "当前 OA 账号不在允许访问的部门范围内")


def login_with_password(username: str, password: str, captcha: str = "") -> tuple[str, dict]:
    username = username.strip().upper()
    status, body, headers = _request_json(
        f"{OA_API}/authentication/password-grant",
        method="POST",
        payload=_password_grant_payload(username, password, captcha),
    )
    authorization = str(headers.get("Authorization") or headers.get("authorization") or "").strip()
    header_token = authorization.split(None, 1)[1] if authorization.lower().startswith("bearer ") else authorization
    token = _token_from(body) or header_token
    if status < 200 or status >= 300 or not token:
        detail, code = _upstream_error_detail(status, body)
        logger.warning(
            "OA password grant rejected status=%s code=%s user=%s token_returned=%s",
            status,
            code,
            _masked_username(username),
            bool(token),
        )
        response_code = str((body or {}).get("code") or "") if isinstance(body, dict) else ""
        if response_code == "400":
            raise HTTPException(428, detail)
        raise HTTPException(502 if status >= 500 else 401, detail)
    user = user_from_token(token, login_flow=True)
    _assert_user_allowed(user)
    return token, user


def send_login_captcha(username: str) -> dict:
    normalized = username.strip().upper()
    status, body, _ = _request_json(
        f"{OA_API}/authentication/captcha",
        method="POST",
        payload={"username": normalized},
    )
    code = str((body or {}).get("code") or "0") if isinstance(body, dict) else ""
    if status < 200 or status >= 300 or code not in {"", "0"}:
        detail, _ = _upstream_error_detail(status, body)
        raise HTTPException(502 if status >= 500 else 400, detail)
    return body if isinstance(body, dict) else {}


def user_from_token(token: str, *, login_flow: bool = False) -> dict:
    status, body, _ = _request_json(f"{OA_API}/auth/userinfo", token=token)
    user = body.get("data") if isinstance(body, dict) else None
    if status < 200 or status >= 300 or body.get("code") != 0 or not isinstance(user, dict):
        if login_flow:
            detail, code = _upstream_error_detail(status, body)
            logger.warning("OA userinfo rejected during login status=%s code=%s", status, code)
            raise HTTPException(
                502 if status >= 500 else 401,
                f"OA 登录凭证已获取，但用户信息校验失败：{detail}",
            )
        raise HTTPException(401, "登录状态已失效")
    return user


def require_user(request: Request) -> dict:
    cached_user = getattr(request.state, "user", None)
    if isinstance(cached_user, dict):
        if confirmed_departure(cached_user):
            _assert_user_allowed(cached_user)
        return cached_user
    # Nginx overwrites these headers with values emitted by the successful
    # internal auth_request. They are not accepted directly from clients.
    verified_number = (unquote(request.headers.get("x-verified-employee-no", "")).strip()
                       if os.getenv("TRUST_VERIFIED_PROXY_HEADERS") == "1" else "")
    if verified_number:
        verified_name = unquote(request.headers.get("x-verified-user-name", "")).strip()
        department = unquote(request.headers.get("x-verified-department", "")).strip()
        center = unquote(request.headers.get("x-verified-center", "")).strip()
        dept_name = "-".join(value for value in (department, center) if value)
        user = {
            "number": verified_number,
            "username": verified_number,
            "realName": verified_name or verified_number,
            "name": verified_name or verified_number,
            "parentDept": department,
            "center": center,
            "deptName": dept_name,
            "groupName": dept_name,
            "status": "normal",
        }
        _assert_user_allowed(user)
        request.state.user = user
        return user
    candidates = [
        request.cookies.get(SESSION_COOKIE, ""),
        request.headers.get("x-oa-token", ""),
        request.cookies.get("authorization", ""),
        request.cookies.get("oa-authorization", ""),
    ]
    tokens: list[str] = []
    for raw in candidates:
        if not raw:
            continue
        token = unquote(raw)
        if token.lower().startswith("bearer "):
            token = token[7:]
        if token and token not in tokens:
            tokens.append(token)
    if not tokens:
        raise HTTPException(401, "未登录")
    service_error: HTTPException | None = None
    for token in tokens:
        try:
            user = user_from_token(token)
            _assert_user_allowed(user)
            request.state.user = user
            return user
        except HTTPException as error:
            if isinstance(error.detail, dict) and error.detail.get("code") == "CONFIRMED_DEPARTURE":
                raise
            if error.status_code == 502:
                service_error = error
    if service_error:
        raise service_error
    raise HTTPException(401, "登录状态已失效")


def cookie_value(token: str) -> str:
    return quote(token, safe="")
