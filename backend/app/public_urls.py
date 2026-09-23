"""Deployment-owned public URLs; never derive redirects from request headers."""
import os
from urllib.parse import urlsplit, urlunsplit, urlencode

LEGACY_PUBLIC_URL = 'https://app.fandow.top/fd-026222/wis-video-center/'

def public_root() -> str:
    value = os.getenv('WIS_PUBLIC_URL', LEGACY_PUBLIC_URL).strip()
    parsed = urlsplit(value)
    if (parsed.scheme not in {'https', 'http'} or not parsed.netloc
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('WIS_PUBLIC_URL must be an absolute public base URL without credentials, query or fragment')
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip('/')+'/', '', ''))

def qianchuan_callback_uri() -> str:
    explicit = os.getenv('QIANCHUAN_REDIRECT_URI', '').strip()
    return explicit or public_root() + 'api/qianchuan/oauth/callback'

def oauth_result_url(platform: str, authorized: bool) -> str:
    if platform not in {'qianchuan', 'adq_user'}:
        raise ValueError('Unknown authorization provider')
    return public_root() + '?' + urlencode({platform:'authorized' if authorized else 'authorization_failed'})
