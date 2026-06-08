"""Wave 开放平台 — 用户中文名查询。

按域账号批量查中文姓名, 用于在 ask 路径里把 asker_domain 反查成 canonical
中文名 (落 EntityAlias.source='auto'), 让 scope=entity:<中文名> 的 directive
能在该 asker 提问时被注入 system prompt 用户偏好段。

OpenAPI: POST /openapi/contact/v1/users/get?uid_type=user_id
请求体: {"uid_list": ["yuqing.chen", ...]} (单次最多 200)
返回: data.users[] 含 user_id / name (中文名) / en_name / union_id 等
"""

from __future__ import annotations

import logging

from helper.im.wave_client import WaveAPIError, _post

log = logging.getLogger(__name__)


def get_user_chinese_names(domains: list[str]) -> dict[str, str]:
    """域账号 → 中文名 批量查询。

    返回: {domain: chinese_name}, 查不到 / 没权限 / 失败的 domain 不在 dict 里。
    异常 → 返空 dict, 不抛, 不阻塞调用方主链路。
    """
    if not domains:
        return {}
    uid_list = list(dict.fromkeys(d for d in domains if d))[:200]
    if not uid_list:
        return {}
    try:
        data = _post(
            "/openapi/contact/v1/users/get",
            params={"uid_type": "user_id"},
            json_body={"uid_list": uid_list},
        )
    except WaveAPIError as e:
        log.warning("wave users/get failed: %s", e)
        return {}
    except Exception:  # noqa: BLE001
        log.exception("wave users/get unexpected error")
        return {}
    out: dict[str, str] = {}
    for u in data.get("users") or []:
        uid = (u.get("user_id") or "").strip()
        name = (u.get("name") or "").strip()
        if uid and name:
            out[uid] = name
    return out


def ensure_alias_for_domain(domain: str) -> str:
    """域账号 → canonical 中文名, 落 entity_alias 缓存, 返回 canonical。

    没 alias 记录就走 wave 拉一次中文名, 落 source='auto'。
    查不到 / 失败 → 返空字符串, 调用方按 fallback 处理 (不再注 entity_refs)。
    """
    if not domain:
        return ""
    names = get_user_chinese_names([domain])
    canon = names.get(domain, "")
    if not canon:
        return ""
    from helper.memory.alias import add_alias
    add_alias(domain, canon, source="auto")
    return canon
