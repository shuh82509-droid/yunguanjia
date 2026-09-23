"""Channels profile covers: normalized derivatives and content-based verification.

Video thumbnails are not profile covers. Never use the presence of an <img>
as evidence that the user's selected cover was applied. Signed URLs stay private.
"""
from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import urlsplit

import requests
from .channels_media import MediaCommandError, run_media

MAX_IMAGE_BYTES = 20 * 1024 * 1024


class CoverError(RuntimeError):
    pass


def _run(args: list[str]) -> bytes:
    try:
        if args[0] == "ffmpeg":
            args = [args[0], "-nostdin", "-filter_threads", "1", "-filter_complex_threads", "1", *args[1:]]
        return run_media(args, timeout=60)
    except MediaCommandError as error:
        if error.transient:
            raise CoverError("服务器图片处理资源繁忙，封面尚未完成处理；请稍后重试") from None
        raise CoverError("封面图片无法处理，请使用有效的 JPG、PNG 或 WEBP 图片") from error


def prepare_custom_cover(source: Path, directory: Path) -> tuple[Path, Path]:
    """Keep the original intact; upload a full image and a 3:4 profile derivative."""
    if not source.is_file() or not 0 < source.stat().st_size <= MAX_IMAGE_BYTES:
        raise CoverError("封面图片为空或超过 20MB")
    try:
        probe = json.loads(_run([
            "ffprobe", "-v", "error", "-threads", "1", "-select_streams", "v:0", "-show_entries",
            "stream=width,height", "-of", "json", str(source),
        ]))
        stream = probe["streams"][0]
        width, height = int(stream["width"]), int(stream["height"])
        if min(width, height) < 2 or max(width, height) > 20000 or width * height > 40_000_000:
            raise ValueError("dimensions")
    except (KeyError, IndexError, ValueError, TypeError) as error:
        raise CoverError("封面图片尺寸无效或过大") from error
    full = directory / "channels-selected-full.jpg"
    profile = directory / "channels-selected-profile.jpg"
    for target, transform in (
        (full, "scale='min(1920,iw)':'min(1920,ih)':force_original_aspect_ratio=decrease,setsar=1"),
        (profile, "crop='min(iw,ih*3/4)':'min(ih,iw*4/3)',scale=960:1280,setsar=1"),
    ):
        _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-threads", "1",
              "-i", str(source), "-vf", transform, "-frames:v", "1", "-q:v", "2",
              "-threads", "1", str(target)])
        if not target.is_file() or not target.stat().st_size:
            raise CoverError("封面图片生成失败")
    return full, profile


def image_signature(path: Path) -> dict:
    pixels = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
                   "-i", str(path), "-vf", "scale=16:16", "-frames:v", "1",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-threads", "1", "pipe:1"])
    gray = _run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-threads", "1",
                 "-i", str(path), "-vf", "scale=9:8", "-frames:v", "1",
                 "-f", "rawvideo", "-pix_fmt", "gray", "-threads", "1", "pipe:1"])
    if len(pixels) != 768 or len(gray) != 72:
        raise CoverError("封面图片无法核验")
    bits = [gray[y * 9 + x] > gray[y * 9 + x + 1] for y in range(8) for x in range(8)]
    return {"rgb": list(pixels), "edges": bits}


def signatures_match(expected: dict, observed: dict) -> bool:
    a, b = expected.get("rgb", []), observed.get("rgb", [])
    x, y = expected.get("edges", []), observed.get("edges", [])
    if len(a) != 768 or len(b) != 768 or len(x) != 64 or len(y) != 64:
        return False
    # Both structure and colour must agree, allowing ordinary CDN recompression.
    return sum(abs(i - j) for i, j in zip(a, b)) / 768 <= 10 and sum(i != j for i, j in zip(x, y)) <= 8


def _safe_platform_image_url(url: str) -> bool:
    try:
        p = urlsplit(url)
        host = (p.hostname or "").lower()
        return (p.scheme == "https" and not p.username and not p.password
                and p.port in (None, 443)
                and any(host.endswith(suffix) for suffix in (".qq.com", ".qpic.cn")))
    except ValueError:
        return False


def download_platform_image(url: str, target: Path) -> None:
    """Unauthenticated, bounded download; validate every redirect and hide URLs."""
    for _ in range(4):
        if not _safe_platform_image_url(url):
            raise CoverError("平台封面地址无法安全核验")
        try:
            with requests.get(url, timeout=(5, 15), stream=True, allow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    url = response.headers.get("Location", "")
                    continue
                response.raise_for_status()
                if int(response.headers.get("Content-Length", "0")) > MAX_IMAGE_BYTES:
                    raise CoverError("平台封面图片过大")
                size = 0
                with target.open("wb") as output:
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > MAX_IMAGE_BYTES:
                            raise CoverError("平台封面图片过大")
                        output.write(chunk)
                if not size:
                    raise CoverError("平台封面图片为空")
                return
        except (requests.RequestException, ValueError) as error:
            raise CoverError("暂时无法读取平台封面，尚未验证图片一致性") from error
    raise CoverError("平台封面地址重定向过多")


def verify_uploaded_cover(url: str, expected: Path, downloaded: Path) -> bool:
    download_platform_image(url, downloaded)
    return signatures_match(image_signature(expected), image_signature(downloaded))
