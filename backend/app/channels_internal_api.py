"""Durable WeChat Channels upload transport.

The creator site's private API has no compatibility guarantee.  This module
therefore captures login with Playwright and can use either an authenticated
page or an encrypted-cookie HTTP session. The final ``post_create`` request
is a durable at-most-once boundary; uncertainty is resolved by readback.

Secrets returned by the creator site (cookies, authKey and signed CDN URLs)
must never be logged or included in raised errors.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from http.cookiejar import Cookie
import tempfile
import time
from pathlib import Path
from typing import Callable
from urllib.parse import quote, urlencode, urlparse
from uuid import uuid4

import requests
import httpx

from .channels_credentials import normalize_cookies
from .channels_covers import CoverError, prepare_custom_cover, verify_uploaded_cover
from .channels_media import MediaCommandError, run_media


BASE_URL = "https://channels.weixin.qq.com"
CREATE_URL = f"{BASE_URL}/platform/post/create"
MICRO_CREATE_URL = f"{BASE_URL}/micro/content/post/create"
DEFAULT_CDN_HOST = "finder.video.qq.com"

ANNOTATION_TYPES = {
    "ai_generated": 1,
    "marketing_ad": 2,
    "fictional": 3,
    "self_shot": 5,
    "repost": 7,
    "personal_opinion": 8,
}


class ChannelsInternalApiError(RuntimeError):
    def __init__(
        self,
        message: str,
        stage: str = "internal_preflight",
        *,
        code: str = "INTERNAL_API_ERROR",
        definitive: bool = True,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.code = code
        self.definitive = definitive


class ChannelsPublishUncertain(ChannelsInternalApiError):
    def __init__(self, message: str = "已发起发布，但平台回执不确定；将只回查作品列表") -> None:
        super().__init__(
            message,
            "confirm_publish",
            code="PUBLISH_RESULT_UNCERTAIN",
            definitive=False,
        )


def _now_ms() -> str:
    return str(int(time.time() * 1000))


def _common_body(finder_id: str = "", extra: dict | None = None) -> dict:
    body = {
        "timestamp": _now_ms(),
        "_log_finder_uin": "",
        "_log_finder_id": finder_id,
        "rawKeyBuff": "",
        "pluginSessionId": None,
        "scene": 7,
        "reqScene": 7,
    }
    if extra:
        body.update(extra)
    return body


def _response_code(payload: dict) -> tuple[object | None, str]:
    """Return a failing protocol code even when the top-level code is zero."""
    observed: list[tuple[object, str]] = []
    for key in ("errCode", "errcode", "err_code", "retCode", "retcode", "code", "ret"):
        if key in payload and not isinstance(payload[key], (dict, list)):
            observed.append((payload[key], str(payload.get("errMsg") or payload.get("errmsg") or "")))
    containers = [payload]
    if isinstance(payload.get("data"), dict):
        containers.append(payload["data"])
    for container in containers:
        for key in ("baseResp", "base_resp", "BaseResponse", "baseResponse"):
            nested = container.get(key)
            if not isinstance(nested, dict):
                continue
            for code_key in ("ret", "Ret", "errCode", "errcode", "code"):
                if code_key in nested and not isinstance(nested[code_key], (dict, list)):
                    message = nested.get("errMsg") or nested.get("ErrMsg") or nested.get("errmsg") or ""
                    if isinstance(message, dict):
                        message = message.get("String") or message.get("string") or ""
                    observed.append((nested[code_key], str(message or "")))
                    break
    for code, message in observed:
        if not _is_success_code(code):
            return code, message
    return observed[0] if observed else (None, "")


def _is_success_code(code: object | None) -> bool:
    if code is None:
        return True
    if isinstance(code, bool):
        return code is True
    return str(code).strip().lower() in {"0", "ok", "success", "succeeded"}


def parse_json_response(status: int, data: object, *, stage: str, side_effect: bool = False) -> dict:
    """Common browser/HTTP semantics. Never expose arbitrary remote error text."""
    if status in {301, 302, 303, 307, 308, 401, 403}:
        raise ChannelsInternalApiError("视频号授权已失效，请重新扫码", "authorization", code="AUTH_EXPIRED")
    if not 200 <= status < 300 or not isinstance(data, dict):
        if side_effect:
            raise ChannelsPublishUncertain()
        compatible = status in {404, 405}
        raise ChannelsInternalApiError(
            "视频号接口暂时不可用" if status else "视频号接口连接中断",
            stage, code="API_UNSUPPORTED" if compatible else "API_UNAVAILABLE", definitive=compatible,
        )
    code, _message = _response_code(data)
    safe_code = str(code) if re.fullmatch(r"-?\d{1,10}", str(code)) else "UNKNOWN"
    if safe_code.isdigit() and 300330 <= int(safe_code) <= 300350:
        raise ChannelsInternalApiError("视频号授权已失效，请重新扫码", "authorization", code="AUTH_EXPIRED")
    if not _is_success_code(code):
        raise ChannelsInternalApiError(
            f"视频号平台拒绝请求（{safe_code}）", stage,
            code="PUBLISH_REJECTED" if side_effect else f"PLATFORM_{safe_code}",
        )
    if side_effect and code is None:
        raise ChannelsPublishUncertain()
    return data


class HttpJsonClient:
    """Single account HTTP session; only the captured, scoped cookie jar is sent."""

    def __init__(self, bundle: dict, *, timeout: int = 120, transport=None, on_cookies=None):
        self.on_cookies = on_cookies
        cookies = httpx.Cookies()
        for item in normalize_cookies(bundle["cookies"]):
            expires = item.get("expires", -1)
            expires = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
            domain = item["domain"]
            cookies.jar.set_cookie(Cookie(
                0, item["name"], item["value"], None, False, domain, domain.startswith("."),
                domain.startswith("."), item["path"], True, item["secure"], expires, expires is None,
                None, None, {"HttpOnly": None, "SameSite": item["sameSite"]} if item["httpOnly"]
                else {"SameSite": item["sameSite"]}, False,
            ))
        self.client = httpx.Client(
            cookies=cookies, timeout=httpx.Timeout(timeout, connect=30), follow_redirects=False,
            transport=transport, trust_env=False,
            headers={"User-Agent": bundle["user_agent"], "Accept": "application/json, text/plain, */*",
                     "Content-Type": "application/json", "Origin": BASE_URL},
        )
        self.aid = str(uuid4())

    def cookies(self) -> list[dict]:
        return normalize_cookies([{
            "name": c.name, "value": c.value, "domain": c.domain, "path": c.path,
            "expires": c.expires or -1, "secure": c.secure,
            "httpOnly": c.has_nonstandard_attr("HttpOnly"),
            "sameSite": c.get_nonstandard_attr("SameSite", "Lax"),
        } for c in self.client.cookies.jar])

    def call(self, path: str, body: dict, *, micro=False, uin="0000000000", side_effect=False, stage="internal_preflight"):
        if not path.startswith("/") or path.startswith("//") or ".." in path or "://" in path or "?" in path:
            raise ChannelsInternalApiError("视频号内部接口路径非法", stage)
        prefix = "/micro/content/cgi-bin/mmfinderassistant-bin" if micro else "/cgi-bin/mmfinderassistant-bin"
        referer = MICRO_CREATE_URL if micro else CREATE_URL
        try:
            response = self.client.post(
                BASE_URL + prefix + path, json=body,
                params={"_aid": self.aid, "_rid": uuid4().hex, "_pageUrl": referer},
                headers={"Referer": referer, "X-WECHAT-UIN": str(uin or "0000000000")},
            )
        except httpx.HTTPError:
            if side_effect:
                raise ChannelsPublishUncertain() from None
            raise ChannelsInternalApiError("视频号接口连接中断", stage, code="API_UNAVAILABLE", definitive=False) from None
        try:
            data = response.json()
        except ValueError:
            data = None
        # Rotation persistence must never turn an acknowledged publication into
        # a retry. Callback failures are intentionally not allowed to escape.
        if self.on_cookies:
            try:
                self.on_cookies(self.cookies())
            except Exception:
                pass
        return parse_json_response(response.status_code, data, stage=stage, side_effect=side_effect)

    def close(self):
        self.client.close()


class BrowserJsonClient:
    """Same-origin JSON client backed by an authenticated Playwright page."""

    def __init__(self, page) -> None:
        self.page = page
        self.aid = str(uuid4())

    def call(
        self,
        path: str,
        body: dict,
        *,
        micro: bool = False,
        uin: str = "0000000000",
        side_effect: bool = False,
        stage: str = "internal_preflight",
    ) -> dict:
        if not path.startswith("/") or path.startswith("//") or ".." in path or "://" in path or "?" in path:
            raise ChannelsInternalApiError("视频号内部接口路径非法", stage)
        prefix = "/micro/content/cgi-bin/mmfinderassistant-bin" if micro else "/cgi-bin/mmfinderassistant-bin"
        payload = {
            "prefix": prefix,
            "path": path,
            "body": body,
            "uin": str(uin or "0000000000"),
            "aid": self.aid,
            "rid": f"{int(time.time()):x}-{uuid4().hex[:8]}",
            "pageUrl": MICRO_CREATE_URL if micro else CREATE_URL,
        }
        try:
            result = self.page.evaluate(
                """async (input) => {
                  const query = new URLSearchParams({
                    _aid: input.aid,
                    _rid: input.rid,
                    _pageUrl: input.pageUrl,
                  });
                  try {
                    const response = await fetch(
                      input.prefix + input.path + '?' + query.toString(),
                      {
                        method: 'POST',
                        credentials: 'include',
                        redirect: 'manual',
                        headers: {
                          Accept: 'application/json, text/plain, */*',
                          'Content-Type': 'application/json',
                          'X-WECHAT-UIN': input.uin,
                        },
                        body: JSON.stringify(input.body),
                      },
                    );
                    const text = await response.text();
                    let data = null;
                    try { data = JSON.parse(text); } catch (_) {}
                    return { httpStatus: response.status, data, json: data !== null };
                  } catch (error) {
                    return { networkError: String(error && error.message || error || 'network error') };
                  }
                }""",
                payload,
            )
        except Exception as error:
            if side_effect:
                raise ChannelsPublishUncertain() from error
            raise ChannelsInternalApiError("视频号页面接口连接中断", stage, definitive=False) from error
        if not isinstance(result, dict) or result.get("networkError"):
            if side_effect:
                raise ChannelsPublishUncertain()
            raise ChannelsInternalApiError("视频号页面接口连接中断", stage, definitive=False)
        status = int(result.get("httpStatus") or 0)
        data = result.get("data")
        return parse_json_response(status, data, stage=stage, side_effect=side_effect)


def _data(payload: dict) -> dict:
    value = payload.get("data")
    return value if isinstance(value, dict) else {}


def extract_identity(auth_payload: dict) -> dict:
    data = _data(auth_payload)
    finder = data.get("finderUser") or data.get("userAttr") or {}
    if not isinstance(finder, dict):
        finder = {}
    if not finder and isinstance(data.get("finderList"), list) and data["finderList"]:
        finder = data["finderList"][0] if isinstance(data["finderList"][0], dict) else {}
    finder_username = str(finder.get("finderUsername") or data.get("finderUsername") or "").strip()
    uniq_id = str(finder.get("uniqId") or data.get("uniqId") or "").strip()
    nickname = str(
        finder.get("nickname")
        or finder.get("finderNickname")
        or data.get("nickname")
        or ""
    ).strip()
    return {
        "finder_username": finder_username,
        "uniq_id": uniq_id,
        "nickname": nickname,
        "external_account_id": finder_username or uniq_id,
    }


def read_authenticated_identity(page) -> dict:
    """Read only the current creator identity; never returns cookies or keys."""
    payload = BrowserJsonClient(page).call(
        "/auth/auth_data",
        _common_body(),
        stage="authorization",
    )
    return extract_identity(payload)


def probe_video(path: Path) -> dict:
    try:
        width = height = 0
        duration = 0.0
        # MP4/MOV carry dimensions and duration in their container headers.
        # Avoid decoding HEVC merely to rediscover those fields under CPU/memory
        # pressure. Formats with missing headers get one bounded normal probe.
        for header_only in (True, False):
            command = [
                "ffprobe", "-v", "error", "-threads", "1", "-select_streams", "V:0",
                "-show_entries", "stream=width,height,duration:format=duration",
                "-of", "json",
            ]
            if header_only:
                command.append("-nofind_stream_info")
            output = run_media([*command, str(path)], timeout=60)
            payload = json.loads(output)
            stream = (payload.get("streams") or [{}])[0]
            width = int(stream.get("width") or 0)
            height = int(stream.get("height") or 0)
            duration = 0.0
            for value in ((payload.get("format") or {}).get("duration"), stream.get("duration")):
                try:
                    candidate = float(value)
                    if math.isfinite(candidate) and candidate > 0:
                        duration = candidate
                        break
                except (TypeError, ValueError):
                    pass
            if width > 0 and height > 0 and duration > 0:
                break
    except MediaCommandError as error:
        message = ("服务器读取视频暂忙或超时，尚未发布；请稍后重试" if error.transient else
                   "服务器缺少视频读取工具，请联系管理员" if error.code == "MEDIA_TOOL_MISSING" else
                   "原视频文件无法识别，请检查文件是否完整且可播放")
        raise ChannelsInternalApiError(message, "validate_video", code=error.code, definitive=not error.transient) from None
    except (ValueError, TypeError, IndexError, AttributeError) as error:
        raise ChannelsInternalApiError("原视频元数据无效，请检查文件是否完整", "validate_video", code="MEDIA_INVALID") from error
    if width <= 0 or height <= 0 or duration <= 0 or path.stat().st_size <= 0:
        raise ChannelsInternalApiError("原视频尺寸或时长异常", "validate_video")
    return {"width": width, "height": height, "duration": duration, "file_size": path.stat().st_size}


def target_size(width: int, height: int) -> tuple[int, int]:
    long_side = max(width, height)
    short_side = min(width, height)
    scale = min(1.0, 1920 / long_side, 1080 / short_side)

    def even(value: float) -> int:
        return max(2, int(value) // 2 * 2)

    return even(width * scale), even(height * scale)


def extract_cover(video_path: Path, output_path: Path, duration: float) -> Path:
    at_second = max(0.0, min(3.0, duration * 0.1))
    try:
        run_media(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                "-threads", "1", "-filter_threads", "1", "-filter_complex_threads", "1",
                "-ss", f"{at_second:.3f}", "-i", str(video_path),
                "-map", "0:V:0", "-frames:v", "1", "-q:v", "2", "-threads", "1", str(output_path),
            ],
            timeout=120,
        )
    except MediaCommandError as error:
        message = "服务器生成封面暂忙或超时，尚未发布；请稍后重试" if error.transient else "无法从原视频提取封面"
        raise ChannelsInternalApiError(message, "prepare_cover", code=error.code, definitive=not error.transient) from None
    if not output_path.exists() or output_path.stat().st_size <= 0:
        raise ChannelsInternalApiError("原视频封面提取失败", "prepare_cover")
    return output_path


class ChannelsCdnUploader:
    def __init__(
        self,
        upload_params: dict,
        *,
        chunk_size: int = 8 * 1024 * 1024,
        part_retries: int = 3,
        request_timeout: int = 120,
        on_progress: Callable[[int, int], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> None:
        self.params = upload_params
        self.chunk_size = max(1, int(chunk_size))
        self.part_retries = max(1, int(part_retries))
        self.request_timeout = max(30, int(request_timeout))
        self.on_progress = on_progress
        self.should_cancel = should_cancel
        self.session = requests.Session()
        self.host = self._pick_host()
        if not str(self.params.get("authKey") or "").strip():
            raise ChannelsInternalApiError("平台未返回上传凭证", "internal_preflight")

    def _pick_host(self) -> str:
        values = self.params.get("cdnHostList") or []
        if isinstance(values, str):
            values = [values]
        values = [*values, self.params.get("cdnHost"), DEFAULT_CDN_HOST]
        for value in values:
            normalized = str(value or "").strip()
            if not normalized:
                continue
            parsed = urlparse(normalized if "://" in normalized else f"https://{normalized}")
            hostname = (parsed.hostname or "").lower()
            if hostname == "qq.com" or hostname.endswith(".qq.com"):
                return hostname
        raise ChannelsInternalApiError("平台返回的 CDN 地址不可用", "internal_preflight")

    def _x_arguments(self, filename: str, file_size: int, task_id: str, file_type_key: str) -> str:
        file_type = self.params.get(file_type_key)
        if file_type is None:
            raise ChannelsInternalApiError(f"平台缺少 {file_type_key} 上传参数", "internal_preflight")
        return urlencode({
            "apptype": self.params.get("appType", 251),
            "filetype": file_type,
            "weixinnum": str(self.params.get("uin") or ""),
            "filekey": filename,
            "filesize": file_size,
            "taskid": task_id,
            "scene": self.params.get("scene", 2),
        })

    def _headers(self, filename: str, file_size: int, task_id: str, file_type_key: str) -> dict:
        return {
            "Authorization": str(self.params["authKey"]),
            "X-Arguments": self._x_arguments(filename, file_size, task_id, file_type_key),
            "Content-MD5": "null",
            "User-Agent": "Mozilla/5.0",
        }

    @staticmethod
    def _json(response, stage: str) -> dict:
        try:
            payload = response.json()
        except Exception as error:
            raise ChannelsInternalApiError("视频号 CDN 未返回可识别结果", stage, definitive=False) from error
        if not isinstance(payload, dict):
            raise ChannelsInternalApiError("视频号 CDN 返回格式异常", stage, definitive=False)
        return payload

    @staticmethod
    def _field(payload: dict, name: str):
        if payload.get(name) not in (None, ""):
            return payload[name]
        nested = payload.get("data")
        return nested.get(name) if isinstance(nested, dict) else None

    def upload(self, path: Path, file_type_key: str, filename: str | None = None) -> dict:
        if self.should_cancel and self.should_cancel():
            raise ChannelsInternalApiError("已停止发布；尚未进入平台发布步骤", "upload_original")
        file_size = path.stat().st_size
        if file_size <= 0:
            raise ChannelsInternalApiError("上传文件为空", "upload_original")
        filename = Path(filename or path.name).name
        task_id = str(uuid4())
        part_count = max(1, math.ceil(file_size / self.chunk_size))
        part_lengths = [
            min(self.chunk_size, file_size - index * self.chunk_size)
            for index in range(part_count)
        ]
        headers = self._headers(filename, file_size, task_id, file_type_key)
        headers["Content-Type"] = "application/json"
        try:
            response = self.session.put(
                f"https://{self.host}/applyuploaddfs",
                headers=headers,
                json={"BlockSum": part_count, "BlockPartLength": part_lengths},
                timeout=self.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise ChannelsInternalApiError("视频号 CDN 初始化上传失败", "upload_original", definitive=False) from error
        apply_payload = self._json(response, "upload_original")
        upload_id = self._field(apply_payload, "UploadID")
        if not upload_id:
            raise ChannelsInternalApiError("视频号 CDN 未返回上传任务", "upload_original", definitive=False)

        part_info: list[dict] = []
        last_trans_flag = ""
        uploaded_bytes = 0
        with path.open("rb") as source:
            for part_number, part_length in enumerate(part_lengths, start=1):
                if self.should_cancel and self.should_cancel():
                    raise ChannelsInternalApiError("已停止发布；尚未进入平台发布步骤", "upload_original")
                chunk = source.read(part_length)
                if len(chunk) != part_length:
                    raise ChannelsInternalApiError("读取原视频分片失败", "upload_original")
                put_headers = self._headers(filename, file_size, task_id, file_type_key)
                put_headers["Content-Type"] = "application/octet-stream"
                put_headers["Content-MD5"] = hashlib.md5(chunk).hexdigest()
                last_error: Exception | None = None
                part_payload: dict | None = None
                for attempt in range(1, self.part_retries + 1):
                    try:
                        part_response = self.session.put(
                            f"https://{self.host}/uploadpartdfs",
                            params={
                                "PartNumber": part_number,
                                "UploadID": str(upload_id),
                                "QuickUpload": 2,
                            },
                            headers=put_headers,
                            data=chunk,
                            timeout=self.request_timeout,
                        )
                        part_response.raise_for_status()
                        part_payload = self._json(part_response, "upload_original")
                        if self._field(part_payload, "ETag"):
                            break
                        raise ChannelsInternalApiError("分片未返回 ETag", "upload_original", definitive=False)
                    except (requests.RequestException, ChannelsInternalApiError) as error:
                        last_error = error
                        if attempt < self.part_retries:
                            time.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))
                etag = self._field(part_payload or {}, "ETag")
                if not etag:
                    raise ChannelsInternalApiError(
                        f"原视频第 {part_number}/{part_count} 个分片重试后仍失败",
                        "upload_original",
                        definitive=False,
                    ) from last_error
                trans_flag = self._field(part_payload or {}, "TransFlag")
                if trans_flag not in (None, ""):
                    last_trans_flag = str(trans_flag)
                part_info.append({"PartNumber": part_number, "ETag": etag})
                uploaded_bytes += part_length
                if self.on_progress:
                    self.on_progress(uploaded_bytes, file_size)

        complete_headers = self._headers(filename, file_size, task_id, file_type_key)
        complete_headers["Content-Type"] = "application/json"
        try:
            complete_response = self.session.post(
                f"https://{self.host}/completepartuploaddfs",
                params={"UploadID": str(upload_id)},
                headers=complete_headers,
                json={"TransFlag": last_trans_flag or "0_0", "PartInfo": part_info},
                timeout=self.request_timeout,
            )
            complete_response.raise_for_status()
        except requests.RequestException as error:
            raise ChannelsInternalApiError("视频号 CDN 合并分片失败", "upload_original", definitive=False) from error
        complete_payload = self._json(complete_response, "upload_original")
        download_url = str(self._field(complete_payload, "DownloadURL") or "").strip()
        if not download_url:
            raise ChannelsInternalApiError("视频号 CDN 未返回已上传文件", "upload_original", definitive=False)
        if download_url.startswith("http://wxapp.tc.qq.com"):
            download_url = "https://finder.video.qq.com" + download_url[len("http://wxapp.tc.qq.com"):]
        elif download_url.startswith("http://"):
            download_url = "https://" + download_url[len("http://"):]
        return {
            "url": download_url,
            "file_size": file_size,
            "task_id": task_id,
            "md5sum": task_id,
        }


def _find_values(value, key: str) -> list:
    found = []
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if key in current:
                found.append(current[key])
            stack.extend(item for item in current.values() if isinstance(item, (dict, list)))
        elif isinstance(current, list):
            stack.extend(current)
    return found


def _annotation_info(payload: dict, annotation: str) -> dict:
    wanted = ANNOTATION_TYPES.get(annotation)
    if wanted is None:
        return {}
    # Verified 2026-09-05 against the authorized creator API: data.tags holds
    # the choices; one fresh data.tagKey signs the list. Older versions carried
    # a key on each choice. Never reuse keys from a different response/group.
    stack = [(payload, "")]
    while stack:
        current, inherited_key = stack.pop()
        if isinstance(current, dict):
            own_key = current.get("tagKey")
            effective_key = own_key.strip() if isinstance(own_key, str) else inherited_key
            tag_type = current.get("tagType", current.get("type"))
            if str(tag_type) == str(wanted) and effective_key:
                return {"tagType": wanted, "tagKey": effective_key}
            stack.extend((item, effective_key) for item in current.values() if isinstance(item, (dict, list)))
        elif isinstance(current, list):
            stack.extend((item, inherited_key) for item in current)
    raise ChannelsInternalApiError("视频号未返回当次有效的视频标注凭证", "internal_preflight")


class ChannelsInternalPublisher:
    def __init__(
        self,
        page=None,
        *,
        client=None,
        chunk_size: int = 8 * 1024 * 1024,
        part_retries: int = 3,
        request_timeout: int = 120,
        clip_timeout: int = 1800,
    ) -> None:
        self.client = client if client is not None else BrowserJsonClient(page)
        self.chunk_size = chunk_size
        self.part_retries = part_retries
        self.request_timeout = request_timeout
        self.clip_timeout = clip_timeout

    def preflight(
        self,
        *,
        expected_external_account_id: str = "",
        product_id: str = "",
        video_annotation: str = "none",
        annotation_shooting_time: str = "",
        annotation_shooting_location: str = "",
        annotation_repost_source: str = "",
        on_stage: Callable[[str, str], None] | None = None,
        on_identity: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
    ) -> dict:
        if on_stage:
            on_stage("internal_preflight", "正在核验视频号授权与账号一致性")
        if should_cancel and should_cancel():
            raise ChannelsInternalApiError("已停止发布；尚未进入平台发布步骤", "internal_preflight")
        auth = self.client.call("/auth/auth_data", _common_body(), stage="internal_preflight")
        identity = extract_identity(auth)
        candidates = {identity["finder_username"], identity["uniq_id"]} - {""}
        if not candidates:
            raise ChannelsInternalApiError("视频号授权已失效，请重新扫码", "authorization", code="AUTH_EXPIRED")
        if expected_external_account_id and expected_external_account_id not in candidates:
            raise ChannelsInternalApiError(
                "当前登录的视频号与任务账号不一致，已在上传前停止",
                "internal_preflight",
                code="ACCOUNT_ID_MISMATCH",
            )
        if on_identity:
            on_identity(identity["external_account_id"])
        finder_id = identity["finder_username"] or identity["uniq_id"]
        auth_data = _data(auth)
        helper = self.client.call(
            "/helper/helper_upload_params",
            _common_body(finder_id),
            stage="internal_preflight",
        )
        upload_params = {**(auth_data.get("envInfo") or {}), **_data(helper)}

        if product_id:
            limit = self.client.call(
                "/post/check_window_limit_status",
                _common_body(finder_id, {"productId": product_id}),
                micro=True,
                stage="internal_preflight",
            )
            if any(str(value) == "1" for value in _find_values(limit, "reachLimit")):
                raise ChannelsInternalApiError(
                    "该视频号当日商品发布数已达上限",
                    "internal_preflight",
                    code="PRODUCT_DAILY_LIMIT_REACHED",
                )

        tag_info: dict = {}
        if video_annotation != "none":
            tag_payload = self.client.call(
                "/post/finder_get_object_tag_list",
                _common_body(finder_id),
                micro=True,
                stage="internal_preflight",
            )
            tag_info = _annotation_info(tag_payload, video_annotation)
            if video_annotation == "self_shot":
                # The region codes are dynamic and cannot be safely guessed.
                raise ChannelsInternalApiError(
                    "自拍素材地区码需要当次平台数据，转用网页通道处理",
                    "internal_preflight",
                    code="USE_BROWSER_FOR_SELF_SHOT",
                )
            if video_annotation == "repost" and annotation_repost_source.strip():
                tag_info["repostSource"] = annotation_repost_source.strip()[:500]
            if annotation_shooting_time:
                tag_info["shootingTime"] = annotation_shooting_time.strip()[:80]
            if annotation_shooting_location:
                tag_info["shootingLocation"] = annotation_shooting_location.strip()[:255]

        trace_payload = self.client.call(
            "/post/get-finder-post-trace-key",
            _common_body(finder_id, {"objectId": ""}),
            micro=True,
            stage="internal_preflight",
        )
        trace_values = _find_values(trace_payload, "traceKey")
        trace_key = str(trace_values[0] if trace_values else "").strip()
        if not trace_key:
            raise ChannelsInternalApiError("视频号未返回发布跟踪凭证", "internal_preflight")
        return {
            "identity": identity,
            "finder_id": finder_id,
            "upload_params": upload_params,
            "tag_info": tag_info,
            "trace_key": trace_key,
        }

    def publish(
        self,
        *,
        video_path: Path,
        cover_path: Path,
        custom_cover: bool = False,
        expected_external_account_id: str = "",
        title: str,
        description: str,
        tags: list[str],
        product_id: str = "",
        product_name: str = "",
        video_annotation: str = "none",
        annotation_shooting_time: str = "",
        annotation_shooting_location: str = "",
        annotation_repost_source: str = "",
        on_stage: Callable[[str, str], None] | None = None,
        on_identity: Callable[[str], None] | None = None,
        mark_submitted: Callable[[str], None] | None = None,
        should_cancel: Callable[[], bool] | None = None,
        preflight_state: dict | None = None,
    ) -> dict:
        def report(stage: str, message: str) -> None:
            if on_stage:
                on_stage(stage, message)

        def check_cancel(stage: str) -> None:
            if should_cancel and should_cancel():
                raise ChannelsInternalApiError("已停止发布；尚未进入平台发布步骤", stage)

        clean_title = title.strip()
        if len(clean_title) > 16:
            raise ChannelsInternalApiError("视频短标题不能超过 16 个字，请修改后发布", "set_title")
        state = preflight_state or self.preflight(
            expected_external_account_id=expected_external_account_id,
            product_id=product_id,
            video_annotation=video_annotation,
            annotation_shooting_time=annotation_shooting_time,
            annotation_shooting_location=annotation_shooting_location,
            annotation_repost_source=annotation_repost_source,
            on_stage=on_stage,
            on_identity=on_identity,
            should_cancel=should_cancel,
        )
        identity = state["identity"]
        finder_id = state["finder_id"]
        upload_params = state["upload_params"]
        tag_info = state["tag_info"]
        trace_key = state["trace_key"]
        meta = probe_video(video_path)
        report("upload_original", "已通过授权预检，正在分片上传原视频")

        def progress(done: int, total: int) -> None:
            percent = min(100, max(0, int(done * 100 / max(total, 1))))
            report("upload_original", f"正在分片上传原视频：{percent}%")

        uploader = ChannelsCdnUploader(
            upload_params,
            chunk_size=self.chunk_size,
            part_retries=self.part_retries,
            request_timeout=self.request_timeout,
            on_progress=progress,
            should_cancel=should_cancel,
        )
        upload_start = int(time.time())
        selected_full = selected_profile = None
        try:
            video = uploader.upload(video_path, "videoFileType", video_path.name)
            upload_end = int(time.time())
            check_cancel("upload_cover")
            report("upload_cover", "原视频已上传，正在上传封面")
            if custom_cover:
                try:
                    with tempfile.TemporaryDirectory(prefix="channels-cover-") as folder:
                        directory = Path(folder)
                        full_path, profile_path = prepare_custom_cover(cover_path, directory)
                        selected_full = uploader.upload(full_path, "pictureFileType", full_path.name)
                        selected_profile = uploader.upload(profile_path, "pictureFileType", profile_path.name)
                        report("verify_cover", "正在核对视频号收到的主页封面与所选图片")
                        for label, uploaded, expected in (
                            ("full", selected_full, full_path), ("profile", selected_profile, profile_path),
                        ):
                            if not verify_uploaded_cover(uploaded["url"], expected, directory / f"cdn-{label}.jpg"):
                                raise CoverError("视频号收到的封面与所选图片不一致，已停止发布")
                except CoverError as error:
                    raise ChannelsInternalApiError(str(error), "set_cover", code="COVER_NOT_VERIFIED") from error
                cover = selected_profile
            else:
                cover = uploader.upload(cover_path, "pictureFileType", cover_path.name)
        finally:
            uploader.session.close()

        width, height = int(meta["width"]), int(meta["height"])
        target_width, target_height = target_size(width, height)
        clip_payload = self.client.call(
            "/post/post_clip_video",
            _common_body(finder_id, {
                "url": video["url"],
                "timeStart": 0,
                "cropDuration": 0,
                "height": height,
                "width": width,
                "x": 0,
                "y": 0,
                "clipOriginVideoInfo": {
                    "width": width,
                    "height": height,
                    "duration": meta["duration"],
                    "fileSize": meta["file_size"],
                },
                "traceInfo": {
                    "traceKey": trace_key,
                    "uploadCdnStart": upload_start,
                    "uploadCdnEnd": upload_end,
                },
                "targetWidth": target_width,
                "targetHeight": target_height,
                "type": 4,
                "useAstraThumbCover": 1,
            }),
            micro=True,
            stage="platform_processing",
        )
        ticket = _data(clip_payload)
        draft_id = str(ticket.get("draftId") or ticket.get("clipKey") or "").strip()
        if not draft_id:
            raise ChannelsInternalApiError("视频号未创建有效的视频处理任务", "platform_processing")

        report("platform_processing", "视频号正在服务端处理原视频")
        deadline = time.monotonic() + self.clip_timeout
        clip_result: dict = {}
        while time.monotonic() < deadline:
            check_cancel("platform_processing")
            report("platform_processing", "视频号仍在处理原视频，正在核验处理进度")
            response = self.client.call(
                "/post/post_clip_video_result",
                _common_body(finder_id, dict(ticket)),
                micro=True,
                stage="platform_processing",
            )
            clip_result = _data(response)
            flag = clip_result.get("flag")
            if str(flag) == "1" and clip_result.get("url"):
                break
            if str(flag) == "3":
                raise ChannelsInternalApiError("视频号服务端处理视频失败", "platform_processing", code="VIDEO_CLIP_FAILED")
            if str(flag) == "4":
                raise ChannelsInternalApiError("视频号服务端处理视频超时", "platform_processing", code="VIDEO_CLIP_TIMEOUT")
            time.sleep(5)
        else:
            raise ChannelsInternalApiError("视频号服务端处理视频超时", "platform_processing", code="VIDEO_CLIP_TIMEOUT")

        final_url = str(clip_result.get("url") or video["url"])
        final_width = int(clip_result.get("width") or target_width)
        final_height = int(clip_result.get("height") or target_height)
        final_duration = float(clip_result.get("duration") or meta["duration"])
        final_size = int(clip_result.get("fileSize") or meta["file_size"])
        # Match the creator's current contract: playback thumbnail is a video
        # frame; profile cover and its full image are separate image uploads.
        thumb_url = str(clip_result.get("thumbUrl") or clip_result.get("fullThumbUrl") or cover["url"])
        cover_url = str(selected_profile["url"] if custom_cover else clip_result.get("coverUrl") or clip_result.get("fullCoverUrl") or cover["url"])
        full_cover_url = str(selected_full["url"] if custom_cover else clip_result.get("fullCoverUrl") or cover_url)
        body_text = description.strip() or clean_title
        normalized_tags = []
        for value in tags:
            tag = str(value or "").strip().lstrip("#")
            if tag and tag not in normalized_tags:
                normalized_tags.append(tag)
                marker = f"#{tag}"
                if marker not in body_text:
                    body_text = f"{body_text} {marker}".strip()

        media = {
            "url": final_url,
            "fileSize": str(final_size),
            "thumbUrl": thumb_url,
            "fullThumbUrl": thumb_url,
            "mediaType": 4,
            "videoPlayLen": int(round(final_duration)),
            "width": final_width,
            "height": final_height,
            "md5sum": str(uuid4()),
            "coverUrl": cover_url,
            "fullCoverUrl": full_cover_url,
            "urlCdnTaskId": draft_id,
        }
        if final_width >= final_height:
            media["cardShowStyle"] = 2
            media["shareCoverUrl"] = full_cover_url
        object_desc = {
            "mpTitle": "",
            "description": body_text,
            "extReading": {"link": "", "title": ""},
            "mediaType": 4,
            "location": {},
            "topic": {"finderTopicInfo": ""},
            "event": {},
            "mentionedUser": [],
            "media": [media],
        }
        if clean_title:
            object_desc["shortTitle"] = [{"shortTitle": clean_title}]
        if product_id:
            object_desc["component"] = {
                "id": product_id,
                "type": 1,
                "title": (product_name or product_id)[:500],
                "appearedSeconds": 0,
                "appearedMode": 0,
            }
        client_id = str(uuid4())
        create_body = _common_body(finder_id, {
            "objectType": 0,
            "longitude": 0,
            "latitude": 0,
            "feedLongitude": 0,
            "feedLatitude": 0,
            "originalFlag": 0,
            "topics": normalized_tags,
            "isFullPost": 1,
            "handleFlag": 2,
            "videoClipTaskId": draft_id,
            "traceInfo": {
                "traceKey": trace_key,
                "uploadCdnStart": upload_start,
                "uploadCdnEnd": upload_end,
            },
            "objectDesc": object_desc,
            "report": {
                **ticket,
                "clipKey": ticket.get("clipKey") or draft_id,
                "draftId": draft_id,
                "height": final_height,
                "width": final_width,
                "duration": final_duration,
                "fileSize": final_size,
                "uploadCost": max(1, (upload_end - upload_start) * 1000),
            },
            "postFlag": 0,
            "mode": 1,
            "clientid": client_id,
        })
        if tag_info:
            create_body["tagInfo"] = tag_info
        check_cancel("submit_publish")
        report("submit_publish", "已完成上传与处理，正在写入防重发检查点")
        if isinstance(self.client, HttpJsonClient) and not mark_submitted:
            raise ChannelsInternalApiError("缺少防重发检查点，已停止发布", "submit_publish", code="CHECKPOINT_REQUIRED")
        if mark_submitted:
            mark_submitted(client_id)
        # Exactly one side-effecting request.  No retry belongs below this line.
        acknowledgement = self.client.call(
            "/post/post_create",
            create_body,
            micro=True,
            side_effect=True,
            stage="submit_publish",
        )
        receipt = _data(acknowledgement)
        return {
            "accepted": True,
            "confirmed": False,
            "platform_acknowledged": True,
            "publish_client_id": client_id,
            "platform_content_id": str(receipt.get("objectId") or ""),
            "platform_export_id": str(receipt.get("exportId") or ""),
            "platform_export_source": "publish_response" if receipt.get("exportId") else "",
            "external_account_id": identity["external_account_id"],
            "message": "视频号平台已受理，正在回查作品列表与公开状态",
        }
