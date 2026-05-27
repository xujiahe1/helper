"""KM 开放平台 HTTP API 客户端。

为什么和 wave_client 分开:
  - 域名不同(km.mihoyo.com vs open.hoyowave.com),access_token 不假设互通
  - retcode 错误码族不同(KM 的 10401xxx 文档相关码,wave 没有)
  - 调用形态不同:KM 主要是 ingest 拉取 + retrieve,wave 是发消息

实现的接口(按需扩):
  - access_token        — POST /openapi/auth/v1/access_token/internal      (KM mh0ykpik1t90)
  - 获取文档内容        — POST /openapi/docs/v1/doc/detail/get               (KM mh9d9b9clixm)
                           只支持 document(协同) + markdown,表格类型走 spreadsheet 接口
  - 获取表格文本        — POST /openapi/docs/v1/doc/spreadsheet/range/get    (KM mh9it89h08iq)
                           只取 values 二维数组的纯文本,不含图/视频/附件
  - 文档向量召回        — POST /openapi/docs/v1/doc/retrieve                 (KM mhq7lxrxj71a)
  - 文档变更列表        — POST /openapi/docs/v1/doc/changes/get              (KM mhbmbu0cwofc)

约定:
  - 复用 wave_app_id / wave_app_secret(开放平台同一个 app)
  - 但 token 独立缓存 — KM host 是否接受 wave 那边换的 token 文档没明示,稳妥起见各换各的
  - Authorization 头 = access_token 原值(不带 Bearer)
  - retcode != 0 一律抛 KMAPIError
  - 文档 OpenAPI 仅支持办公网/内网访问 — 服务器部署 OK,本地开发要走办公网
"""

from __future__ import annotations

import logging
import re
import threading
import time
import urllib.parse
from typing import Any

import httpx

from helper.config import get_settings

log = logging.getLogger(__name__)

_REFRESH_THRESHOLD_S = 30 * 60
_HTTP_TIMEOUT = httpx.Timeout(15.0, connect=5.0)


class KMAPIError(RuntimeError):
    def __init__(self, retcode: int, message: str, *, endpoint: str = "") -> None:
        self.retcode = retcode
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"km api {endpoint or ''} retcode={retcode}: {message}")


# ---------- access_token 缓存(独立于 wave) ----------

class _TokenCache:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str = ""
        self._expire_at: int = 0

    def get(self) -> str:
        s = get_settings()
        if not (s.wave_app_id and s.wave_app_secret):
            raise KMAPIError(
                -1,
                "WAVE_APP_ID / WAVE_APP_SECRET not configured (KM 复用 wave 凭据)",
                endpoint="access_token",
            )
        with self._lock:
            now = int(time.time())
            if self._token and (self._expire_at - now) > _REFRESH_THRESHOLD_S:
                return self._token

            url = f"{s.km_open_api_base_url}/openapi/auth/v1/access_token/internal"
            payload = {"app_id": s.wave_app_id, "app_secret": s.wave_app_secret}
            try:
                resp = httpx.post(
                    url,
                    json=payload,
                    timeout=_HTTP_TIMEOUT,
                    headers={"Content-Type": "application/json; charset=utf-8"},
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as e:
                raise KMAPIError(-1, f"http error: {e}", endpoint="access_token") from e

            retcode = int(body.get("retcode", -1))
            if retcode != 0:
                raise KMAPIError(
                    retcode, str(body.get("message", "")), endpoint="access_token"
                )
            data = body.get("data") or {}
            token = data.get("access_token")
            expire = data.get("expire")
            if not isinstance(token, str) or not token:
                raise KMAPIError(-1, "missing access_token in response", endpoint="access_token")
            try:
                expire_int = int(expire)
            except (TypeError, ValueError):
                expire_int = now + 7200
            if expire_int < now:
                expire_int = now + expire_int
            self._token = token
            self._expire_at = expire_int
            log.info(
                "km access_token refreshed, expire_at=%s (in %ds)",
                expire_int,
                expire_int - now,
            )
            return token


_cache = _TokenCache()


def get_access_token() -> str:
    return _cache.get()


# ---------- 请求基础设施 ----------

def _post(
    path: str,
    *,
    params: dict[str, Any] | None = None,
    json_body: dict[str, Any],
) -> dict[str, Any]:
    s = get_settings()
    url = f"{s.km_open_api_base_url}{path}"
    headers = {
        "Authorization": get_access_token(),
        "Content-Type": "application/json; charset=utf-8",
    }
    try:
        resp = httpx.post(
            url, params=params, json=json_body, headers=headers, timeout=_HTTP_TIMEOUT
        )
        resp.raise_for_status()
        body = resp.json()
    except httpx.HTTPError as e:
        raise KMAPIError(-1, f"http error: {e}", endpoint=path) from e
    retcode = int(body.get("retcode", -1))
    if retcode != 0:
        raise KMAPIError(retcode, str(body.get("message", "")), endpoint=path)
    return body.get("data") or {}


# ---------- 链接解析 ----------

# enc_id 形如 mhxxxxxxxxxx,纯小写字母+数字。
_ENC_ID_RE = re.compile(r"/doc/(mh[a-z0-9]+)")


def parse_km_url(url: str) -> tuple[str, str | None]:
    """从 KM 链接抠 (enc_id, sheet_id|None)。

    支持:
      - https://km.mihoyo.com/doc/{enc_id}
      - https://km.mihoyo.com/doc/{enc_id}?sheetId={sheet_id}
      - https://km.mihoyo.com/m/doc/{enc_id}            (移动端)
      - 同时容忍 query / fragment 里的 sheetId

    不匹配返 ("", None)。
    """
    if not url:
        return "", None
    m = _ENC_ID_RE.search(url)
    if not m:
        return "", None
    enc_id = m.group(1)

    sheet_id: str | None = None
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    if "sheetId" in qs:
        sheet_id = qs["sheetId"][0]
    elif parsed.fragment:
        # 偶尔 sheetId 在 fragment 里(如分享链接)
        frag_qs = urllib.parse.parse_qs(parsed.fragment)
        if "sheetId" in frag_qs:
            sheet_id = frag_qs["sheetId"][0]
    return enc_id, sheet_id


# ---------- 文档读取 ----------

def get_doc_detail(
    doc_id: str,
    *,
    target_time_ms: int | None = None,
    doc_tenant_id: str | None = None,
    uid_type: str = "user_id",
) -> dict[str, Any]:
    """取协同文档/Markdown 的元信息+正文。

    返回 data.info,关键字段:
      doc_id / parent_doc_id / workspace_id / knowledge_id / title /
      owner:{id,id_type,tenant_id} / doc_type / content (string) /
      create_time / update_time (毫秒字符串) / last_modifier / is_archived /
      doc_tenant_id / is_sensitive

    错误:
      10401305 应用对文档无权限(没给可管理) / 10401307 不支持的文档类型
      (传给非 document/markdown 类型时报这个) / 10101016 限流
    """
    body: dict[str, Any] = {"doc_id": doc_id}
    if target_time_ms is not None:
        body["target_time"] = target_time_ms
    if doc_tenant_id:
        body["doc_tenant_id"] = doc_tenant_id
    data = _post(
        "/openapi/docs/v1/doc/detail/get",
        params={"uid_type": uid_type},
        json_body=body,
    )
    return data.get("info") or {}


def get_spreadsheet_range(
    doc_id: str,
    sheet_id: str,
    *,
    range_address: str | None = None,
    version_id: str | None = None,
    doc_tenant_id: str | None = None,
) -> list[list[Any]]:
    """取普通表格(spreadsheet)指定 sheet 的文本内容。

    - range_address 不传 = 整 sheet(从文档行为推测;不行就要显式传 'A1:ZZ10000' 类)
    - 不返回单元格里的图/视频/附件
    - 时间型单元格返回的是"1900.1.1 起的天数"(数值)
    - 应用需对表格有"可管理"权限

    返 values: list[list[Any]],每行是一组单元格值(可能 None)
    """
    body: dict[str, Any] = {"doc_id": doc_id, "sheet_id": sheet_id}
    if range_address:
        body["range_address"] = range_address
    if version_id:
        body["version_id"] = version_id
    if doc_tenant_id:
        body["doc_tenant_id"] = doc_tenant_id
    data = _post(
        "/openapi/docs/v1/doc/spreadsheet/range/get",
        json_body=body,
    )
    # data.resource 是 JSON 字符串 {"values": [[...]]}
    resource_str = data.get("resource") or "{}"
    try:
        import json as _json
        parsed = _json.loads(resource_str)
    except (ValueError, TypeError):
        log.warning("km spreadsheet resource not valid json: %r", resource_str[:200])
        return []
    values = parsed.get("values")
    return values if isinstance(values, list) else []


# ---------- 文档向量召回 ----------

def retrieve_docs(
    query: str,
    *,
    knowledge_id_list: list[int] | None = None,
    doc_id_list: list[str] | None = None,
    doc_tenant_id: str | None = None,
    top_k: int | None = None,
    score_threshold: float | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """KM 向量召回。query ≤ 8000 字。

    scope.knowledge_id_list / scope.doc_id_list 至少传一个(强烈建议),否则全租户搜性能差。
    返回 {invalid_scope:{...}, result:[{meta:{doc_url,doc_id,title}, score, content}]}
    """
    if not query:
        return {"result": []}
    scope: dict[str, Any] = {}
    if knowledge_id_list:
        scope["knowledge_id_list"] = knowledge_id_list
    if doc_id_list:
        scope["doc_id_list"] = doc_id_list
    if doc_tenant_id:
        scope["doc_tenant_id"] = doc_tenant_id
    body: dict[str, Any] = {"scope": scope, "query": query[:8000]}
    setting: dict[str, Any] = {}
    if top_k is not None:
        setting["top_k"] = top_k
    if score_threshold is not None:
        setting["score_threshold"] = score_threshold
    if setting:
        body["setting"] = setting
    if user_id:
        body["user_info"] = {"uid": user_id}
    return _post("/openapi/docs/v1/doc/retrieve", json_body=body)


# ---------- 文档变更 ----------

def get_doc_changes(
    *,
    knowledge_id: int | None = None,
    doc_id_list: list[str] | None = None,
    start_time_ms: int,
    end_time_ms: int,
    doc_type: str | None = None,
    change_type: str | None = None,
    doc_tenant_id: str | None = None,
    cursor: str | None = None,
    limit: int = 200,
) -> dict[str, Any]:
    """文档变更列表。窗口 ≤ 24h,scope 二选一(knowledge_id 或 doc_id_list ≤500)。

    doc_type ∈ richtext/markdown/spreadsheet/mind_map/document/smart_spreadsheet/
                folder/shortcut/file/whiteboard/unravel/meeting_note
    change_type ∈ create/update/delete/status_change

    返 {items:[...], pagenation:{next_cursor}, invalid_scope}
    """
    scope: dict[str, Any] = {}
    if knowledge_id is not None:
        scope["knowledge_id"] = knowledge_id
    if doc_id_list:
        scope["doc_id_list"] = doc_id_list
    if doc_tenant_id:
        scope["doc_tenant_id"] = doc_tenant_id
    body: dict[str, Any] = {
        "scope": scope,
        "start_time": start_time_ms,
        "end_time": end_time_ms,
    }
    if doc_type:
        body["doc_type"] = doc_type
    if change_type:
        body["change_type"] = change_type
    params: dict[str, Any] = {"limit": limit}
    if cursor:
        params["cursor"] = cursor
    return _post(
        "/openapi/docs/v1/doc/changes/get",
        params=params,
        json_body=body,
    )
