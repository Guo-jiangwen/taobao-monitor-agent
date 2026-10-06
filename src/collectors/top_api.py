"""淘宝开放平台 / 淘宝客采集器。

签名规则（TOP 协议 v2.0）：
  1. 除 sign / session 外的所有参数按 key 字典序升序排列；
  2. 拼成 key1value1key2value2...；
  3. 首尾拼接 app_secret，做 MD5 并转大写，得到 sign；
  4. 把 sign 加入参数，按 key 升序拼 query 请求。

合规前提（部署前必须确认）：
  - AppKey 已在淘宝开放平台备案，应用已申请对应 API 的调用权限；
  - 服务器出口 IP 已加入应用白名单；
  - 采集频率严格按 source.json 里声明的 qps / daily_quota；
  - 只拉取自己有授权关系的商品与公开商品素材，不做任何绕过鉴权的抓取。
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from .base import BaseCollector
from ..core.models import MonitorTarget, RawRecord


def top_sign(params: dict[str, str], app_secret: str) -> str:
    base = "".join(f"{k}{params[k]}" for k in sorted(params))
    return hashlib.md5((base + app_secret).encode("utf-8")).hexdigest().upper()


class TaobaoTopCollector(BaseCollector):
    adapter_name = "top_api"

    def _secret(self) -> str:
        for env in self.source.credentials_env:
            val = os.getenv(env)
            if val:
                return val
        if self.source.compliance == "synthetic":
            return "synthetic"
        raise RuntimeError(f"缺少凭证：请设置环境变量 {self.source.credentials_env}")

    def build_request(self, target: Any) -> str:
        params = dict(self.source.params)
        params.update(
            {
                "method": self.source.api or "taobao.tbk.dg.material.optional",
                "app_key": params.get("app_key", ""),
                "num": "1",
                "page_no": "1",
                "item_ids": getattr(target, "item_id", ""),
                "timestamp": "",
            }
        )
        params = {k: v for k, v in params.items() if v not in (None, "")}
        params["timestamp"] = self._timestamp()
        params["sign"] = top_sign(params, self._secret())
        query = urllib.parse.urlencode({k: params[k] for k in sorted(params)})
        return f"{self.source.endpoint.rstrip('/')}?{query}"

    def _timestamp(self) -> str:
        from datetime import datetime

        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    def parse(self, payload: dict[str, Any], target: Any) -> RawRecord:
        item = payload.get("item") or {}
        skus = item.get("skus") or []
        return RawRecord(
            item_id=str(item.get("item_id") or getattr(target, "item_id", "")),
            source_id=self.source.id,
            title=str(item.get("title", "")),
            price=_to_float(item.get("price")),
            original_price=_to_float(item.get("original_price") or item.get("price")),
            stock=_to_int(item.get("stock")),
            listing_status=item.get("status", "unknown"),
            main_image=item.get("main_image", ""),
            promotion=item.get("promotion", ""),
            review_count=_to_int(item.get("review_count")),
            rating=_to_float(item.get("rating")),
            sku_list=[str(s.get("sku_id", "")) for s in skus],
            raw=payload,
        )

    def _do_fetch(self, url: str) -> dict[str, Any]:
        from ..core.risk import FetchError

        req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as err:
            raise FetchError(err.code, err.read().decode("utf-8", errors="replace")) from err
        except Exception as err:
            raise FetchError(0, "", exc=err) from err

        body = json.loads(raw) if raw.strip() else {}
        code = str(body.get("code", body.get("error_response", {}).get("code", 1)))
        if code not in ("0", "200"):
            err_body = json.dumps(body.get("error_response", body), ensure_ascii=False)
            raise FetchError(200, err_body)
        return body.get("result", body)


_UA = "TaobaoMonitorAgent/0.1 (+compliance: official_api)"


def _to_float(v: Any) -> float | None:
    try:
        return round(float(str(v).replace("元", "").strip()), 2)
    except (TypeError, ValueError):
        return None


def _to_int(v: Any) -> int | None:
    try:
        return int(float(str(v)))
    except (TypeError, ValueError):
        return None
