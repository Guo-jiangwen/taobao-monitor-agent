"""指纹与去重。

两个维度，缺一不可：
1. request_fp  —— 请求级去重。同样的 endpoint+参数 在 TTL 内只真正发一次。
2. content_fp  —— 内容级指纹。内容没变就不落新快照，避免快照表被重复数据撑爆，
                  同时天然抑制「同一内容反复采集」造成的平台负载。
"""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

# 采集/投放场景里的追踪类参数，不参与指纹计算
TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "spm", "spc", "tb_token", "timestamp", "t", "callback", "_", "r",
    "app_key", "sign", "session",
}


def canonical_url(url: str) -> str:
    """规范化 URL：去 fragment、剔除追踪参数、参数按 key 排序、host 小写。"""
    parts = urlsplit(url.strip())
    keep = sorted(
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in TRACKING_PARAMS
    )
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", urlencode(keep), "")
    )


def request_fingerprint(url: str, method: str = "GET") -> str:
    base = f"{method.upper()}|{canonical_url(url)}"
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def content_fingerprint(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8", errors="replace")).hexdigest()


def short_fp(fp: str) -> str:
    return fp[:12]
