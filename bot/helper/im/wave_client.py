"""Wave 开放平台 HTTP API 客户端 — bot 出站走这个,不走 MCP。

为什么不走 MCP: openapi-mcp 只支持登录用户身份,不支持服务端凭据。bot 是
后台 daemon,必须用应用自身的 app_id+app_secret 自换 access_token。

实现的接口(按需扩,不预先一锅端):
  - access_token        — POST /openapi/auth/v1/access_token/internal      (KM mh0ykpik1t90)
  - 发消息              — POST /openapi/im/v1/message/send                  (KM mhssk6r38l8m)
  - 回复消息            — POST /openapi/im/v1/message/reply                 (KM mhv7k60z40lk)
  - 拉表情回复成员      — POST /openapi/im/v1/message/reaction/members/get  (KM mha49c0oqjko)
  - id 互转             — POST /openapi/contact/v1/user/id_convert          (KM mhlj2ir6m65i)
  - 用户信息(批量)    — POST /openapi/contact/v1/users/get                (KM mh1o9t9i2rmy)

身份信息走 users/get(直接拿 name / display_status),不再走 IAM。

约定:
  - Authorization 头 = access_token 原值(不要加 "Bearer " 前缀,KM 文档原样如此)
  - body content 必须 JSON-stringify 后传(text 也是 {"text": "..."} 序列化)
  - retcode != 0 一律抛 WaveAPIError,带 retcode + message 方便上层 fail-fast
  - access_token 续期阈值: 剩余 < 30 分钟就重新换(KM 文档明示这个窗口可双 token 共存)

不在这层管的:
  - 重试 / 限流回退 — 让调用方根据 retcode 决定(限流是 10101016)
  - 请求去重 — 调用方传 request_id(send_message 暴露了这个 kwarg)
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from typing import Any

import httpx

from helper.config import get_settings

log = logging.getLogger(__name__)

# 距离过期 < 30min 就提前换。KM 文档允许双活 token,这里宽松一点。
_REFRESH_THRESHOLD_S = 30 * 60

# 总超时(连接 + 读)。Wave 内网通常 <1s,留余量。
_HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)


class WaveAPIError(RuntimeError):
    def __init__(self, retcode: int, message: str, *, endpoint: str = "") -> None:
        self.retcode = retcode
        self.message = message
        self.endpoint = endpoint
        super().__init__(f"wave api {endpoint or ''} retcode={retcode}: {message}")


# ---------- access_token 缓存 ----------

class _TokenCache:
    """模块级单例。token + 过期时间戳(秒,绝对)。线程安全。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str = ""
        self._expire_at: int = 0  # Unix epoch 秒

    def get(self) -> str:
        s = get_settings()
        if not (s.wave_app_id and s.wave_app_secret):
            raise WaveAPIError(
                -1,
                "WAVE_APP_ID / WAVE_APP_SECRET not configured",
                endpoint="access_token",
            )

        with self._lock:
            now = int(time.time())
            if self._token and (self._expire_at - now) > _REFRESH_THRESHOLD_S:
                return self._token

            url = f"{s.wave_open_api_base_url}/openapi/auth/v1/access_token/internal"
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
                raise WaveAPIError(-1, f"http error: {e}", endpoint="access_token") from e

            retcode = int(body.get("retcode", -1))
            if retcode != 0:
                raise WaveAPIError(
                    retcode, str(body.get("message", "")), endpoint="access_token"
                )

            data = body.get("data") or {}
            token = data.get("access_token")
            expire = data.get("expire")
            if not isinstance(token, str) or not token:
                raise WaveAPIError(-1, "missing access_token in response", endpoint="access_token")
            # expire 文档说是绝对秒级时间戳。但有些示例像 "16654695359" 看起来超大,
            # 兼容两种:若 expire < now 当成相对秒数(now + expire),否则当绝对。
            try:
                expire_int = int(expire)
            except (TypeError, ValueError):
                expire_int = now + 7200
            if expire_int < now:
                expire_int = now + expire_int  # 当相对秒数处理
            self._token = token
            self._expire_at = expire_int
            log.info(
                "wave access_token refreshed, expire_at=%s (in %ds)",
                expire_int,
                expire_int - now,
            )
            return token


_cache = _TokenCache()


def get_access_token() -> str:
    """返回当前有效 access_token,自动续期。"""
    return _cache.get()


# ---------- 请求基础设施 ----------

def _post(path: str, *, params: dict[str, Any] | None = None, json_body: dict[str, Any]) -> dict[str, Any]:
    """带 access_token 的 POST,自动 retcode 校验。

    400/5xx 抛 httpx.HTTPError → 包成 WaveAPIError。retcode != 0 抛 WaveAPIError。
    """
    s = get_settings()
    url = f"{s.wave_open_api_base_url}{path}"
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
        raise WaveAPIError(-1, f"http error: {e}", endpoint=path) from e
    retcode = int(body.get("retcode", -1))
    if retcode != 0:
        raise WaveAPIError(retcode, str(body.get("message", "")), endpoint=path)
    return body.get("data") or {}


# ---------- 发消息 ----------

def send_message(
    receiver_id: str,
    *,
    msg_type: str = "text",
    content: str | dict[str, Any],
    receiver_id_type: str = "user_id",
    send_type: int = 1,
    receiver_topic_id: str | None = None,
    receiver_tenant_id: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    """给 user_id / union_id / chat_id 发会话消息或通知。

    - send_type=1 会话消息(机器人能力),=2 通知(通知助手能力)。强烈建议显式传。
    - content 接受 dict(自动 JSON 序列化)或 str(已序列化好);Wave 协议要求 content 是 string。
    - request_id 用于 8h 内去重;不传会自动生成 uuid。50 字符上限。
    - 返回 data 字典,关键字段: msg_id, create_time, content。

    msg_type=text 例: send_message("liang.xue", content={"text": "hello"})
    """
    if isinstance(content, dict):
        content_str = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    else:
        content_str = content

    body: dict[str, Any] = {
        "receiver_id": receiver_id,
        "receiver_id_type": receiver_id_type,
        "msg_type": msg_type,
        "content": content_str,
        "send_type": send_type,
    }
    if receiver_topic_id:
        body["receiver_topic_id"] = receiver_topic_id
    if receiver_tenant_id:
        body["receiver_tenant_id"] = receiver_tenant_id
    rid = request_id or uuid.uuid4().hex
    return _post(
        "/openapi/im/v1/message/send",
        params={"request_id": rid[:50]},
        json_body=body,
    )


# ---------- union_id ↔ user_id 互转 ----------

def convert_user_ids(
    uid_list: list[str],
    *,
    uid_type: str = "user_id",
    user_tenant_id: str | None = None,
) -> dict[str, Any]:
    """user_id(域账号) ↔ union_id 互转。

    - uid_type="user_id"(默认): 用域账号查 union_id;此时 user_tenant_id 默认补米哈游租户。
    - uid_type="union_id": 用 union_id 查域账号。
    - 单次最多 500 个。
    - 返回 {"uid_pairs": [{"union_id":..., "user_id":...}], "invalid_uid_list": [...]}
    """
    if not uid_list:
        return {"uid_pairs": [], "invalid_uid_list": []}
    s = get_settings()
    body: dict[str, Any] = {"uid_list": uid_list}
    if uid_type == "user_id":
        body["user_tenant_id"] = user_tenant_id or s.wave_user_tenant_id
    return _post(
        "/openapi/contact/v1/user/id_convert",
        params={"uid_type": uid_type},
        json_body=body,
    )


def open_id_to_domain_account(open_id: str) -> str:
    """便捷封装: 单个 union_id → 域账号。查不到返空串(不抛)。"""
    if not open_id:
        return ""
    data = convert_user_ids([open_id], uid_type="union_id")
    for pair in data.get("uid_pairs") or []:
        if pair.get("union_id") == open_id:
            return pair.get("user_id") or ""
    return ""


# ---------- 用户信息(批量) — 替代 IAM 的姓名/部门来源 ----------

def get_users_info(
    uid_list: list[str],
    *,
    uid_type: str = "user_id",
    user_tenant_id: str | None = None,
) -> list[dict[str, Any]]:
    """批量取用户身份: 域账号/union_id → 姓名 / 英文名 / 邮箱 / 状态 / 头像。

    单次最多 100。返回的每条形如:
      {"union_id":"ou_xxx","user_id":"jiahe.xu","name":"徐嘉禾","nick_name":"...",
       "en_name":"...","avatar":"...","email":"...","display_status":"activated",
       "tenant_id":"ot_..."}

    用途: raw_input 落库后,后台 enrich identity_cache 的 name 字段;
    给用户发卡片时拿姓名做问候;权威决策规约里的 author 显示成"姓名(域账号)"。
    """
    if not uid_list:
        return []
    s = get_settings()
    body: dict[str, Any] = {"uid_list": uid_list}
    if uid_type == "user_id":
        body["user_tenant_id"] = user_tenant_id or s.wave_user_tenant_id
    data = _post(
        "/openapi/contact/v1/users/get",
        params={"uid_type": uid_type},
        json_body=body,
    )
    users = data.get("users")
    return list(users) if isinstance(users, list) else []


# ---------- 回复消息 ----------

def reply_message(
    msg_id: str,
    *,
    msg_type: str = "text",
    content: str | dict[str, Any],
    enable_feedback: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """对指定会话消息(om_xxx)做回复。bot 必须在该消息所在会话中。

    - msg_id: 被回复的消息 ID,Wave 会话消息 ID,34 字符,以 om_ 开头
    - enable_feedback=True 会在 bot 这条回复下面挂"👍/👎"按钮;
      用户点击会推送 im.msg.feedback.action_v1 事件(无须额外订阅)
    - 仅 1.30+ 客户端能看到反馈按钮,低版本会忽略
    - request_id 8h 内幂等;不传自动 uuid
    """
    if isinstance(content, dict):
        content_str = json.dumps(content, ensure_ascii=False, separators=(",", ":"))
    else:
        content_str = content

    body: dict[str, Any] = {
        "msg_id": msg_id,
        "msg_type": msg_type,
        "content": content_str,
    }
    if enable_feedback:
        body["feedback_config"] = {"enable_feedback": True}
    rid = request_id or uuid.uuid4().hex
    return _post(
        "/openapi/im/v1/message/reply",
        params={"request_id": rid[:50]},
        json_body=body,
    )


# ---------- 拉表情回复(👍/👎 + 其它) ----------

def get_message_reactions(
    msg_id: str,
    *,
    reaction_id: str | None = None,
    uid_type: str = "user_id",
    cursor: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """拉一条消息的表情回复人员列表(分页)。

    - reaction_id 不传 = 拉所有 reaction(按时间倒序);传了就只看这一种(emoji_ok / emoji_thumbsup ...)
    - limit 上限 50
    - 返回 {items:[{operator:{id,id_type,tenant_id}, action_time, reaction_info:{reaction_id}}],
            pagenation:{next_cursor}}

    用途: 主动拉某条 bot 回复下的反馈人员;
    被动接收点赞点踩走 im.msg.feedback.action_v1 事件,不用主动拉。
    """
    params: dict[str, Any] = {"uid_type": uid_type, "limit": limit}
    if cursor:
        params["cursor"] = cursor
    body: dict[str, Any] = {"msg_id": msg_id}
    if reaction_id:
        body["reaction_id"] = reaction_id
    return _post(
        "/openapi/im/v1/message/reaction/members/get",
        params=params,
        json_body=body,
    )
