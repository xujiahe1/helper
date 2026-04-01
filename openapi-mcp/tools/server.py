"""
Wave Open Platform MCP Server
提供 Wave Open API 的 MCP 工具集

鉴权信息通过 HTTP headers 传入：
- app_id: 从 headers 中的 "app_id" 获取
- app_secret: 从 headers 中的 "app_secret" 获取

每个 tool 调用前会自动获取 access_token
"""
import os
import json
import time
from typing import Optional, List, Dict, Any, Union
import argparse

from fastmcp import FastMCP, Context
from fastmcp.utilities.types import Image

from .wave_client import WaveClient, get_or_create_client
from .km_utils import KmDoc

DOMAIN = "open.hoyowave.com"
_HERE = os.path.dirname(os.path.abspath(__file__))
KM_DOC = open(os.path.join(_HERE, "km_doc.md"), "r", encoding="utf-8").read()

app_id = os.getenv("APP_ID", None)
app_secret = os.getenv("APP_SECRET", None)

parser = argparse.ArgumentParser(description="Wave Open Platform MCP Server")
parser.add_argument("--transport", choices=["stdio", "sse", "streamable-http"], default="stdio")
parser.add_argument("--host", type=str, default="0.0.0.0")
parser.add_argument("--port", type=int, default=5524)
parser.add_argument("--include-tools", type=str, default="all")
parser.add_argument(
    "--json-response",
    action="store_true",
    help="在 streamable-http 下使用 application/json 直接返回（便于浏览器/HTTP 客户端调用）",
)
parser.add_argument(
    "--stateless-http",
    action="store_true",
    help="在 streamable-http 下启用无状态模式（每个请求独立握手，避免会话管理）",
)

args = parser.parse_args()

if args.include_tools == "all":
    include_tools = ["chat", "group_chat", "group_management", "km", "calendar", "others"]
else:
    include_tools = args.include_tools.split(",")
    
    
mcp = FastMCP("mcp-wave", include_tags=include_tools)


def _get_auth_from_context(context: Context) -> tuple[str, str]:
    if app_id and app_secret:
        return app_id, app_secret

    request = context.request_context.request
    headers = request.headers

    extracted_app_id = headers.get('app_id') or headers.get('app-id') or headers.get('App-Id') or headers.get('X-App-Id')
    extracted_app_secret = headers.get('app_secret') or headers.get('app-secret') or headers.get('App-Secret') or headers.get('X-App-Secret')

    if not extracted_app_id or not extracted_app_secret:
        error_msg = (
            f"缺少 app_id 或 app_secret，请在 MCP 配置的 headers 中添加。\n"
            "配置示例:\n"
            '  "headers": {\n'
            '    "app_id": "your_app_id",\n'
            '    "app_secret": "your_app_secret"\n'
            '  }\n\n'
            f"调试信息:\n"
            f"headers: {headers}\n"
        )
        raise ValueError(error_msg)
    
    return extracted_app_id, extracted_app_secret


def _client(context: Context) -> WaveClient:
    aid, asecret = _get_auth_from_context(context)
    return get_or_create_client(aid, asecret, DOMAIN)


def _filter_empty_values(data: Any) -> Any:
    if isinstance(data, dict):
        return {
            k: _filter_empty_values(v)
            for k, v in data.items()
            if v is not None and v != "" and v != [] and v != {}
        }
    elif isinstance(data, list):
        return [_filter_empty_values(item) for item in data]
    else:
        return data


def _result(resp: dict) -> str:
    with_trace_id = {**resp, "trace_id": resp.get("trace_id", "unknown")}
    return json.dumps(_filter_empty_values(with_trace_id), indent=2, ensure_ascii=False)


_RETRYABLE_RETCODES = {10101001}
_MAX_UPDATE_RETRIES = 3
_RETRY_DELAY_SECONDS = 3.0


def _update_document_with_retry(c, doc_id: str, content_json: str) -> str:
    """更新 document 类型文档，对可重试的 retcode 自动重试。"""
    for attempt in range(_MAX_UPDATE_RETRIES):
        resp = c.post("/openapi/docs/v1/doc/document/update", {
            "doc_id": doc_id,
            "content_type": "json",
            "content": content_json,
        })
        retcode = resp.get("retcode")
        if retcode == 0 or retcode not in _RETRYABLE_RETCODES:
            return _result(resp)
        if attempt < _MAX_UPDATE_RETRIES - 1:
            time.sleep(_RETRY_DELAY_SECONDS * (attempt + 1))
    return _result(resp)


# ============================================================================
# Message 模块 Tools - 消息发送相关
# ============================================================================

@mcp.tool(tags={'chat'})
def send_message(
    context: Context,
    receiver_id: str,
    msg_type: str,
    content: str,
    receiver_id_type: str = "user_id",
    send_type: int = 1
) -> str:
    """发送消息给用户或群聊
    
    Args:
        receiver_id: 接收者ID
            - 如果 receiver_id_type="user_id"，填用户的域账号，如 "san.zhang"
            - 如果 receiver_id_type="union_id"，填用户的 union_id
            - 如果 receiver_id_type="chat_id"，填群聊ID，如 "oc_xxx"
        msg_type: 消息类型，可选值:
            - "text": 纯文本消息
            - "card": 卡片消息（需要构建卡片JSON）
            - "image": 图片消息
            - "file": 文件消息
            - "video": 视频消息
            - "rich_text": 富文本消息
            - "markdown": Markdown消息
        content: 消息内容，JSON字符串格式
            - text类型: '{"text": "你好"}'
            - card类型: 完整的卡片JSON结构
            - image类型: '{"image_key": "xxx"}'
            - rich_text类型: 富文本JSON结构
        receiver_id_type: 接收者ID类型，可选值:
            - "user_id": 用户域账号（推荐）
            - "union_id": 用户union_id
            - "chat_id": 群聊ID
            - "department_id": 部门ID
        send_type: 发送类型，整数值:
            - 1: 普通消息/Feed消息（默认）
            - 2: 通知消息/Notice消息
            
    Returns:
        发送结果，包含 msg_id 消息ID
        
    示例:
        # 发送文本消息给用户（普通消息）
        send_message(app_id, app_secret, "san.zhang", "text", '{"text": "你好"}', "user_id", 1)
        
        # 发送通知消息到群聊
        send_message(app_id, app_secret, "oc_xxx", "text", '{"text": "重要通知"}', "chat_id", 2)
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/send", {
        "receiver_id": receiver_id,
        "receiver_id_type": receiver_id_type,
        "msg_type": msg_type,
        "content": content,
        "send_type": send_type,
    }))


@mcp.tool(tags={'chat'})
def send_text_message(
    context: Context,
    receiver_id: str,
    text: str,
    receiver_id_type: str = "user_id",
    send_type: int = 1
) -> str:
    """发送纯文本消息（简化版，无需构建JSON）
    
    Args:
        receiver_id: 接收者ID
            - 用户域账号如 "san.zhang"
            - 或群聊ID如 "oc_xxx"
        text: 要发送的文本内容，如 "你好，这是一条测试消息"
        receiver_id_type: 接收者类型
            - "user_id": 用户域账号（默认）
            - "union_id": 用户union_id
            - "chat_id": 群聊ID
        send_type: 发送类型，整数值:
            - 1: 普通消息/Feed/会话消息（默认）
            - 2: 通知消息/通知助手/通知中心/Notice消息
            
    Returns:
        发送结果，包含 msg_id
        
    示例:
        # 发送普通消息
        send_text_message(context, "san.zhang", "你好！")
        
        # 发送消息到群聊
        send_text_message(context, "oc_xxx", "群消息", "chat_id")
        
        # 发送通知消息
        send_text_message(context, "san.zhang", "重要通知", "user_id", 2)
    """
    c = _client(context)
    content_json = json.dumps({"text": text})
    return _result(c.post("/openapi/im/v1/message/send", {
        "receiver_id": receiver_id,
        "receiver_id_type": receiver_id_type,
        "msg_type": "text",
        "content": content_json,
        "send_type": send_type,
    }))


@mcp.tool(tags={'chat'})
def send_batch_message(
    context: Context,
    receiver_ids: List[str],
    msg_type: str,
    content: str,
    receiver_id_type: str = "user_id",
    send_type: int = 1
) -> str:
    """批量发送消息给多个用户
    
    Args:
        receiver_ids: 接收者ID列表，如 ["san.zhang", "si.li", "wu.wang"]
        msg_type: 消息类型，可选值:
            - "text": 纯文本消息
            - "card": 卡片消息
            - "image": 图片消息
            - "rich_text": 富文本消息
            - "markdown": Markdown消息
        content: 消息内容，JSON字符串
            - text: '{"text": "批量消息内容"}'
        receiver_id_type: 接收者ID类型
            - "user_id": 用户域账号（默认）
            - "union_id": 用户union_id
        send_type: 发送类型，整数值:
            - 1: 普通消息/Feed/会话消息（默认）
            - 2: 通知消息/通知助手/通知中心/Notice消息
        
    Returns:
        批量发送结果，包含 batch_msg_id 用于后续查询状态
        
    示例:
        send_batch_message(app_id, app_secret, ["user1", "user2"], "text", '{"text": "通知"}', "user_id", 1)
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/batch_send", {
        "receiver_ids": receiver_ids,
        "receiver_id_type": receiver_id_type,
        "msg_type": msg_type,
        "content": content,
        "send_type": send_type,
    }))


@mcp.tool(tags={'chat'})
def reply_message(
    context: Context,
    msg_id: str,
    msg_type: str,
    content: str
) -> str:
    """回复指定消息
    
    Args:
        msg_id: 要回复的消息ID，从收到的消息事件中获取
        msg_type: 回复消息类型 ("text", "card" 等)
        content: 回复内容，JSON字符串
            - text: '{"text": "这是回复内容"}'
            
    Returns:
        回复结果
        
    示例:
        reply_message(app_id, app_secret, "om_xxx", "text", '{"text": "收到，已处理"}')
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/reply", {
        "msg_id": msg_id,
        "msg_type": msg_type,
        "content": content,
    }))


@mcp.tool(tags={'chat'})
def get_message(
    context: Context,
    msg_id: str
) -> str:
    """获取消息详情
    
    Args:
        msg_id: 消息ID，如 "om_xxx"
        
    Returns:
        消息详情，包含发送者、内容、时间等信息
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/get", {"msg_id": msg_id}))


@mcp.tool(tags={'chat'})
def recall_message(context: Context, msg_id: str) -> str:
    """撤回消息
    
    Args:
        msg_id: 要撤回的消息ID
        
    Returns:
        撤回结果
        
    注意: 只能撤回机器人自己发送的消息，且有时间限制
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/recall", {"msg_id": msg_id}))


@mcp.tool(tags={'chat'})
def recall_batch_message(context: Context, batch_msg_id: str) -> str:
    """批量撤回消息
    
    Args:
        batch_msg_id: 批量消息ID，从 send_batch_message 返回结果中获取
        
    Returns:
        撤回结果
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/batch_recall", {"batch_msg_id": batch_msg_id}))


@mcp.tool(tags={'chat'})
def active_update_card(
    context: Context,
    msg_id: str,
    content: str
) -> str:
    """主动更新卡片消息内容
    
    Args:
        msg_id: 卡片消息ID
        content: 新的卡片内容，完整的卡片JSON结构
        
    Returns:
        更新结果
        
    注意: 只能更新机器人发送的卡片消息
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/card/active/update", {
        "msg_id": msg_id,
        "content": content,
    }))


@mcp.tool(tags={'chat'})
def get_msg_reaction_list(context: Context, msg_id: str) -> str:
    """获取消息的表情回应列表
    
    Args:
        msg_id: 消息ID
        
    Returns:
        表情回应列表，包含各表情的使用次数
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/reactions/get", {"msg_id": msg_id}))


@mcp.tool(tags={'chat'})
def query_batch_message_status(
    context: Context,
    batch_msg_id: str
) -> str:
    """查询批量消息发送状态
    
    Args:
        batch_msg_id: 批量消息ID，从 send_batch_message 返回
        
    Returns:
        各接收者的发送状态
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/message/batch_query", {"batch_msg_id": batch_msg_id}))


# ============================================================================
# Chat 模块 Tools - 群聊管理
# ============================================================================

@mcp.tool(tags={'group_management'})
def create_chat(
    context: Context,
    name: str,
    member_id_list: List[str]
) -> str:
    """创建群聊
    
    Args:
        name: 群聊名称，如 "项目讨论群"
        member_id_list: 群成员用户ID列表（域账号）
            如 ["san.zhang", "si.li", "wu.wang"]
            
    Returns:
        创建结果，包含 chat_id 群聊ID
        
    示例:
        create_chat(app_id, app_secret, "测试群", ["user1", "user2"])
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/chat/create", {
        "name": name,
        "member_id_list": member_id_list,
    }))


@mcp.tool(tags={'group_chat'})
def get_chats(
    context: Context,
    cursor: Optional[str] = None,
    limit: int = 20
) -> str:
    """获取机器人所在的群聊列表
    
    Args:
        cursor: 分页游标，首次请求不传，后续请求传上次返回的 cursor
        limit: 每页数量，默认20，最大100
        
    Returns:
        群聊列表，包含 chat_id、群名称等信息
    """
    c = _client(context)
    body: dict = {"limit": limit}
    if cursor:
        body["cursor"] = cursor
    return _result(c.post("/openapi/im/v1/chats/get", body))


@mcp.tool(tags={'group_chat'})
def get_chat_members(
    context: Context,
    chat_id: str,
    cursor: Optional[str] = None,
    limit: int = 20
) -> str:
    """获取群成员列表
    
    Args:
        chat_id: 群聊ID，如 "oc_xxx"
        cursor: 分页游标
        limit: 每页数量，默认20
        
    Returns:
        群成员列表，包含用户ID、名称等
    """
    c = _client(context)
    body: dict = {"chat_id": chat_id, "limit": limit}
    if cursor:
        body["cursor"] = cursor
    return _result(c.post("/openapi/im/v1/chat/members/get", body))


@mcp.tool(tags={'group_management'})
def join_chat(
    context: Context,
    chat_id: str,
    uid_list: Optional[List[str]] = None,
    app_id_list: Optional[List[str]] = None
) -> str:
    """添加成员到群聊
    
    Args:
        chat_id: 群聊ID，如 "oc_xxx"
        uid_list: 要添加的用户ID列表（域账号），如 ["san.zhang", "si.li"]
        app_id_list: 要添加的应用ID列表（添加其他机器人）
        
    Returns:
        添加结果
        
    示例:
        join_chat(app_id, app_secret, "oc_xxx", ["new_user1", "new_user2"])
    """
    c = _client(context)
    body: dict = {"chat_id": chat_id}
    if uid_list:
        body["uid_list"] = uid_list
    if app_id_list:
        body["app_id_list"] = app_id_list
    return _result(c.post("/openapi/im/v1/chat/members/add", body))


@mcp.tool(tags={'group_management'})
def delete_chat_members(
    context: Context,
    chat_id: str,
    uid_list: Optional[List[str]] = None,
    app_id_list: Optional[List[str]] = None
) -> str:
    """从群聊中移除成员
    
    Args:
        chat_id: 群聊ID
        uid_list: 要移除的用户ID列表
        app_id_list: 要移除的应用ID列表
        
    Returns:
        移除结果
    """
    c = _client(context)
    body: dict = {"chat_id": chat_id}
    if uid_list:
        body["uid_list"] = uid_list
    if app_id_list:
        body["app_id_list"] = app_id_list
    return _result(c.post("/openapi/im/v1/chat/members/delete", body))


@mcp.tool(tags={'group_management'})
def disband_chat(context: Context, chat_id: str) -> str:
    """解散群聊
    
    Args:
        chat_id: 群聊ID
        
    Returns:
        解散结果
        
    注意: 只有群主或管理员可以解散群聊
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/chat/delete", {"chat_id": chat_id}))


@mcp.tool(tags={'group_management'})
def set_announcement(
    context: Context,
    chat_id: str,
    content: str,
    is_send_message: int = 1
) -> str:
    """设置群公告
    
    Args:
        chat_id: 群聊ID
        content: 公告内容，支持富文本JSON格式
            简单文本: 直接填写文本
            富文本: 使用富文本JSON结构
        is_send_message: 是否发送消息通知群成员
            - 1: 发送通知（默认）
            - 0: 不发送通知
            
    Returns:
        设置结果
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/chat/announcement/set", {
        "chat_id": chat_id,
        "content": content,
        "is_send_message": is_send_message,
    }))


@mcp.tool(tags={'group_management'})
def set_chat_manager(
    context: Context,
    chat_id: str,
    uid_list: Optional[List[str]] = None,
    app_id_list: Optional[List[str]] = None
) -> str:
    """设置群管理员
    
    Args:
        chat_id: 群聊ID
        uid_list: 要设为管理员的用户ID列表
        app_id_list: 要设为管理员的应用ID列表
        
    Returns:
        设置结果
    """
    c = _client(context)
    body: dict = {"chat_id": chat_id}
    if uid_list:
        body["uid_list"] = uid_list
    if app_id_list:
        body["app_id_list"] = app_id_list
    return _result(c.post("/openapi/im/v1/chat/manager/set", body))


@mcp.tool(tags={'group_management'})
def transfer_chat_owner(
    context: Context,
    chat_id: str,
    owner_id: str,
) -> str:
    """转让群主
    
    Args:
        chat_id: 群聊ID
        owner_id: 新群主的用户ID（域账号）
        
    Returns:
        转让结果
    """
    c = _client(context)
    return _result(c.post("/openapi/im/v1/chat/owner/transfer", {
        "chat_id": chat_id,
        "owner_id": owner_id,
    }))


@mcp.tool(tags={'group_management'})
def update_chat_config(
    context: Context,
    chat_id: str,
    add_member_permission: Optional[str] = None,
    edit_permission: Optional[str] = None
) -> str:
    """更新群聊配置
    
    Args:
        chat_id: 群聊ID
        add_member_permission: 添加成员权限
            - "all_members": 所有成员可添加
            - "only_owner": 仅群主可添加
        edit_permission: 编辑群信息权限
            - "all_members": 所有成员可编辑
            - "only_owner": 仅群主可编辑
            
    Returns:
        更新结果
    """
    c = _client(context)
    body: dict = {"chat_id": chat_id}
    if add_member_permission:
        body["add_member_permission"] = add_member_permission
    if edit_permission:
        body["edit_permission"] = edit_permission
    return _result(c.post("/openapi/im/v1/chat/config/update", body))


# ============================================================================
# Contact 模块 Tools - 通讯录/用户信息
# ============================================================================

@mcp.tool(tags={"contact"})
def get_users(
    context: Context,
    uid_list: List[str],
) -> str:
    """获取用户信息
    
    Args:
        uid_list: 用户ID列表（域账号），如 ["san.zhang", "si.li"]
        
    Returns:
        用户信息列表，包含姓名、部门、头像等
        
    示例:
        get_users(app_id, app_secret, ["san.zhang", "si.li"])
    """
    c = _client(context)
    return _result(c.post("/openapi/contact/v1/users/get", {"uid_list": uid_list}))


@mcp.tool(tags={"others"})
def convert_user_ids(
    context: Context,
    uid_list: List[str],
) -> str:
    """转换用户ID类型（user_id <-> open_id）
    
    Args:
        uid_list: 用户ID列表
        
    Returns:
        ID转换结果，包含 user_id 和 open_id 的对应关系
    """
    c = _client(context)
    return _result(c.post("/openapi/contact/v1/user/id_convert", {"uid_list": uid_list}))


# ============================================================================
# Docs 模块 Tools - 文档管理
# ============================================================================

@mcp.tool(tags={"km"})
def create_document(
    context: Context,
    knowledge_id: str,
    title: str,
    owner_id: str,
    parent_doc_id: Optional[str] = None,
) -> str:
    """创建文档
    
    Args:
        knowledge_id: 知识库ID，永远由数字组成，如 "371412"
        title: 文档标题，如 "项目周报"
        owner_id: 文档所有者用户ID（域账号）
        parent_doc_id: 父文档ID（创建在某文档下），是由mh开头的字符串，如 "mhdinvixorfg"
        
    Returns:
        创建结果，包含 doc_id 和 doc_url
    """
    c = _client(context)
    body: dict = {
        "knowledge_id": knowledge_id,
        "title": title,
        "owner_id": owner_id,
        "content": "",
        "is_super_wide": False,
        "uid_type": "user_id",
    }
    if parent_doc_id:
        body["parent_doc_id"] = parent_doc_id
    return _result(c.post("/openapi/docs/v1/doc/document/add", body))


@mcp.tool(tags={"km"})
def copy_document(
    context: Context,
    doc_id: str,
) -> str:
    """原地复制文档，在同一知识库、同一父目录下创建一份副本

    Args:
        doc_id: 源文档ID

    Returns:
        复制结果，包含新文档的 doc_id 和 doc_url
    """
    c = _client(context)

    resp = c.post("/openapi/docs/v1/doc/detail/get", {"doc_id": doc_id})
    data = resp.get("data")
    if data is None or data.get("info") is None:
        return _result(resp)

    info = data["info"]
    doc_type = info.get("doc_type", "document")
    owner_id = info.get("owner", {}).get("id", "")

    body: dict = {
        "knowledge_id": info.get("knowledge_id"),
        "title": info.get("title", "Untitled") + " (副本)",
        "owner_id": owner_id,
        "content": info.get("content", ""),
        "uid_type": "user_id",
    }
    if info.get("parent_doc_id"):
        body["parent_doc_id"] = info["parent_doc_id"]

    if doc_type == "markdown":
        return _result(c.post("/openapi/docs/v1/doc/markdown/add", body))
    else:
        body['content_type'] = "json"
        return _result(c.post("/openapi/docs/v1/doc/document/add", body))


@mcp.tool(tags={"km"})
def move_document(
    context: Context,
    doc_id: str,
    knowledge_id: str,
    title: Optional[str] = None,
    parent_doc_id: Optional[str] = None,
) -> str:
    """移动文档到指定知识库（在目标位置创建副本，需手动删除原文档）

    Args:
        doc_id: 源文档ID
        knowledge_id: 目标知识库ID，永远由数字组成，如 "371412"
        title: 新文档标题，不传则使用原文档标题
        parent_doc_id: 目标父文档ID，是由mh开头的字符串，如 "mhdinvixorfg"

    Returns:
        移动结果，包含新文档的 doc_id 和 doc_url
    """
    c = _client(context)

    resp = c.post("/openapi/docs/v1/doc/detail/get", {"doc_id": doc_id})
    data = resp.get("data")
    if data is None or data.get("info") is None:
        return _result(resp)

    info = data["info"]
    doc_type = info.get("doc_type", "document")
    owner_id = info.get("owner", {}).get("id", "")

    body: dict = {
        "knowledge_id": knowledge_id,
        "title": title or info.get("title", "Untitled"),
        "owner_id": owner_id,
        "content": info.get("content", ""),
        "uid_type": "user_id",
    }
    if parent_doc_id:
        body["parent_doc_id"] = parent_doc_id

    if doc_type == "markdown":
        return _result(c.post("/openapi/docs/v1/doc/markdown/add", body))
    else:
        body['content_type'] = "json"
        return _result(c.post("/openapi/docs/v1/doc/document/add", body))


@mcp.tool(tags={"km"})
def get_doc_detail(
    context: Context,
    doc_id: str,
    format: str = "json",
) -> str:
    """获取文档详情

    Args:
        doc_id: 文档ID
        format: 返回格式
            - "json": 完整的 KM JSON 结构（默认），适合需要编辑文档时使用
            - "plain_text": 纯文本格式，只保留文字内容和图片/视频引用标记，token 占用极小，适合只需阅读内容时使用

    Returns:
        清理后的 KM JSON 文档内容（已去除编辑器冗余字段，图片已填充下载地址）
    """
    try:
        c = _client(context)
        resp = c.post("/openapi/docs/v1/doc/detail/get", {
            "doc_id": doc_id
        })
        data = resp.get("data")
        if data is None:
            return _result(resp)
        info = data.get("info")
        if info is None:
            return _result(resp)

        km = KmDoc.from_json(info["content"])
        if format == "plain_text":
            km.clean().simplify().add_anchors()
            return km.to_plain_text()
        km.fill_card_urls(doc_id, c.token, DOMAIN).clean().simplify().add_anchors()
        return km.to_json()
    except Exception as e:
        print(e)
        return json.dumps({"error": str(e), "trace_id": resp.get("trace_id")}, ensure_ascii=False)


@mcp.tool(tags={"km"})
def download_image(
    ref_path: str,
    save_path: Optional[str] = None,
) -> Image | str:
    """下载图片并读入上下文，支持三种路径格式：

    - wave://  — get_doc_detail 返回的内部引用，如 wave://abc12345/image.png
    - http(s):// — 网页图片 URL
    - 本地路径 — MCP 所在机器的文件路径，如 /tmp/screenshot.png

    Args:
        ref_path: 图片路径（wave://、http(s)://、或本地路径）
        save_path: 本地保存路径。不传则将图片读入上下文供 AI 直接查看
    """
    file_bytes, msg = KmDoc.download_ref(ref_path, save_path)
    if file_bytes is None:
        return msg

    filename = msg
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else 'png'
    fmt_map = {'jpg': 'jpeg', 'jpeg': 'jpeg', 'png': 'png', 'gif': 'gif', 'webp': 'webp'}
    fmt = fmt_map.get(ext, 'png')
    return Image(data=file_bytes, format=fmt)


@mcp.tool(tags={"km"}, description=f"""基于锚点编辑文档中的某个 block。
先通过 get_doc_detail 获取文档内容，每个顶层 block 都有一个 _anchor 字段（多层级字符串，如 "3.2.1"）。
然后用本工具对指定锚点执行操作。

Args:
    doc_id: 文档ID
    anchor: 目标 block 的 _anchor 值（如 "3.2.1"）
    action: 操作类型：
        - replace : 用 content 替换该 block
        - insert_after : 在该 block 后插入 content
        - insert_before : 在该 block 前插入 content
        - delete : 删除该 block（content 可留空）
    content: 单个 block 对象或 block 数组。delete 时可留空。

Returns:
    更新结果


{KM_DOC}
""")
def edit_document(
    context: Context,
    doc_id: str,
    anchor: str,
    action: str,
    content: Union[Dict[str, Any], List[Dict[str, Any]]],
) -> str:
    c = _client(context)

    resp = c.post("/openapi/docs/v1/doc/detail/get", {
        "doc_id": doc_id,
        "uid_type": "user_id",
    })
    data = resp.get("data")
    if data is None or data.get("info") is None:
        return json.dumps({"error": "无法获取文档内容"}, ensure_ascii=False)

    km = KmDoc.from_json(data["info"]["content"])
    km.fill_card_urls(doc_id, c.token, DOMAIN).clean().simplify().add_anchors()

    km.apply_edit(anchor, action, content)

    km.strip_anchors().upload_new_images(c.token, DOMAIN).restore()
    updated_json = km.to_json()

    return _update_document_with_retry(c, doc_id, updated_json)


@mcp.tool(tags={"km"})
def batch_edit_document(
    context: Context,
    doc_id: str,
    edits: list[dict],
) -> str:
    """对同一文档执行多个编辑操作（一次读写）。

    相比多次调用 edit_document，batch_edit_document 只拉取和更新文档一次，
    大幅减少 API 调用次数和延迟。

    Args:
        doc_id: 文档ID
        edits: 编辑操作数组，按顺序执行。每个元素是一个 dict，包含：
            - anchor: 目标 block 的 _anchor 值
            - action: replace / insert_after / insert_before / delete
            - content: block 对象或 block 数组（delete 时可省略）

    Returns:
        更新结果

    注意：edits 按给定顺序依次执行。每次 apply_edit 后锚点会重新编号，
    因此后续 edit 的 anchor 值应基于原始文档的锚点（系统在每步之间会
    自动重新编号）。如果需要从上到下依次修改，建议按从后往前的顺序排列
    edits，这样前面的编辑不会影响后续 anchor 的位置。
    """
    c = _client(context)

    resp = c.post("/openapi/docs/v1/doc/detail/get", {
        "doc_id": doc_id,
        "uid_type": "user_id",
    })
    data = resp.get("data")
    if data is None or data.get("info") is None:
        return json.dumps({"error": "无法获取文档内容"}, ensure_ascii=False)

    km = KmDoc.from_json(data["info"]["content"])
    km.fill_card_urls(doc_id, c.token, DOMAIN).clean().simplify().add_anchors()

    for edit in edits:
        km.apply_edit(edit["anchor"], edit["action"], edit.get("content"))
        km.strip_anchors().add_anchors()

    km.strip_anchors().upload_new_images(c.token, DOMAIN).restore()
    updated_json = km.to_json()

    return _update_document_with_retry(c, doc_id, updated_json)


@mcp.tool(tags={"km"})
def get_spreadsheet_resource(
    context: Context,
    doc_id: str,
    sheet_id: str,
    range_address: str = "A1:Z100",
) -> str:
    """获取表格数据
    
    从 Wave 表格文档中读取指定工作表的单元格数据。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
        sheet_id: 工作表ID（必填）
            - 格式如 "5qRDtgp"
            - 每个工作表都有唯一的 sheet_id
            - 可通过表格 URL 或其他接口获取
        range_address: 数据范围，使用 A1 notation 格式
            - 格式："{起始单元格}:{结束单元格}"
            - 示例：
              - "A1:C10" - 读取 A1 到 C10 区域
              - "A1:Z100" - 读取 A1 到 Z100 区域（默认）
              - "B2:E50" - 读取 B2 到 E50 区域
        
    Returns:
        表格数据，JSON 格式包含:
        - resource: 单元格数据的 JSON 字符串
          - 通常是二维数组格式：[[row1_col1, row1_col2], [row2_col1, row2_col2]]
        
    示例:
        # 读取指定工作表的数据
        get_spreadsheet_resource(app_id, app_secret, "mholb6rrf1wm", 
                                 "5qRDtgp", "A1:D10")
        
        # 读取大范围数据
        get_spreadsheet_resource(app_id, app_secret, "doc_xxx", 
                                 "sheet_xxx", "A1:Z100")
        
    注意:
        - 需要对表格文档有读权限
        - sheet_id 是必填参数，每个工作表都有唯一的 ID
        - range_address 使用 Excel 风格的 A1 notation
    """
    c = _client(context)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/range/get", {
        "doc_id": doc_id,
        "sheet_id": sheet_id,
        "range_address": range_address,
    }))


@mcp.tool(tags={"km"})
def create_spreadsheet(
    context: Context,
    knowledge_id: str,
    title: str,
    owner_id: str,
    parent_doc_id: Optional[str] = None,
) -> str:
    """创建表格文档
    
    Args:
        knowledge_id: 知识库ID，如 "371412"
        title: 表格标题，如 "数据统计表"
        owner_id: 表格所有者用户ID（域账号）
        parent_doc_id: 父文档ID（创建在某文档下），如 "mhdinvixorfg"
    Returns:
        创建结果，包含 doc_id 和 doc_url
        
    示例:
        create_spreadsheet(app_id, app_secret, "371412", "数据统计表", "san.zhang")
    """
    c = _client(context)
    body: dict = {
        "knowledge_id": knowledge_id,
        "title": title,
        "owner_id": owner_id,
        "uid_type": "user_id",
    }
    if parent_doc_id:
        body["parent_doc_id"] = parent_doc_id
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/add", body))


@mcp.tool(tags={"km"})
def update_spreadsheet_resource(
    context: Context,
    doc_id: str,
    sheet_id: str,
    range: str,
    range_address: str,
    values: str,
) -> str:
    """更新表格数据
    
    更新指定范围的表格单元格数据。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
        sheet_id: 工作表ID（必填）
            - 格式如 "5qRDtgp"
            - 可通过 get_spreadsheet_sheets 获取
        range: 完整范围，包含工作表名
            - 格式："{工作表名}!{起始单元格}:{结束单元格}"
            - 示例："工作表1!A1:B5"
        range_address: 纯范围地址，不包含工作表名
            - 格式："{起始单元格}:{结束单元格}"
            - 示例："A1:C3"
        values: 要更新的数据，JSON 字符串格式的二维数组
            - 格式：'[["row1_col1", "row1_col2"], ["row2_col1", "row2_col2"]]'
            - 示例：'[["姓名", "年龄"], ["张三", "25"], ["李四", "30"]]'
    Returns:
        更新结果
        
    示例:
        # 更新表格数据
        update_spreadsheet_resource(app_id, app_secret, "mholb6rrf1wm", 
                                    "5qRDtgp", "工作表1!A1:B5", "A1:C3",
                                    '[["姓名", "年龄"], ["张三", "25"], ["李四", "30"]]')
        
    注意:
        - 需要对表格文档有写权限
        - resource 字段需要是 JSON 字符串格式：'{"values": [[...]]}'
    """
    c = _client(context)
    values_list = json.loads(values)
    if not isinstance(values_list, list):
        raise ValueError("values 必须是二维数组格式")
    resource_json = json.dumps({"values": values_list}, ensure_ascii=False)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/update", {
        "doc_id": doc_id,
        "range": range,
        "sheet_id": sheet_id,
        "range_address": range_address,
        "resource": resource_json,
    }))


@mcp.tool(tags={"km"})
def append_spreadsheet_resource(
    context: Context,
    doc_id: str,
    sheet_id: str,
    range: str,
    range_address: str,
    values: str,
) -> str:
    """追加表格数据
    
    在指定范围后追加新的表格数据。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
        sheet_id: 工作表ID（必填）
            - 格式如 "5qRDtgp"
            - 可通过 get_spreadsheet_sheets 获取
        range: 完整范围，包含工作表名
            - 格式："{工作表名}!{起始单元格}:{结束单元格}"
            - 示例："工作表1!A1:B5"
        range_address: 纯范围地址，不包含工作表名
            - 格式："{起始单元格}:{结束单元格}"
            - 示例："A1:C3"
        values: 要追加的数据，JSON 字符串格式的二维数组
            - 格式：'[["row1_col1", "row1_col2"], ["row2_col1", "row2_col2"]]'
            - 示例：'[["王五", "28"], ["赵六", "35"]]'
    Returns:
        追加结果
        
    示例:
        # 追加表格数据
        append_spreadsheet_resource(app_id, app_secret, "mholb6rrf1wm", 
                                    "5qRDtgp", "工作表1!A1:B5", "A1:C3",
                                    '[["王五", "28"], ["赵六", "35"]]')
        
    注意:
        - 需要对表格文档有写权限
        - resource 字段需要是 JSON 字符串格式：'{"values": [[...]]}'
    """
    c = _client(context)
    values_list = json.loads(values)
    if not isinstance(values_list, list):
        raise ValueError("values 必须是二维数组格式")
    resource_json = json.dumps({"values": values_list}, ensure_ascii=False)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/append", {
        "doc_id": doc_id,
        "range": range,
        "sheet_id": sheet_id,
        "range_address": range_address,
        "resource": resource_json,
    }))


@mcp.tool(tags={"km"})
def get_spreadsheet_sheets(
    context: Context,
    doc_id: str,
) -> str:
    """获取表格工作表列表
    
    获取表格文档中所有工作表的列表，包括每个工作表的 sheet_id 和名称。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
    Returns:
        工作表列表，包含每个工作表的信息:
        - sheet_id: 工作表ID（用于后续操作）
        - sheet_name: 工作表名称
        - 其他工作表属性
        
    示例:
        # 获取表格的所有工作表
        get_spreadsheet_sheets(app_id, app_secret, "mholb6rrf1wm")
        
    注意:
        - 需要对表格文档有读权限
        - 返回的 sheet_id 可用于 get_spreadsheet_resource、update_spreadsheet_resource 等操作
    """
    c = _client(context)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/sheets/list", {"doc_id": doc_id}))


@mcp.tool(tags={"km"})
def add_spreadsheet_sheet(
    context: Context,
    doc_id: str,
    sheet_name: str,
) -> str:
    """添加工作表
    
    在表格文档中添加新的工作表。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
        sheet_name: 新工作表的名称
            - 示例："数据表2"、"Sheet2"
    Returns:
        添加结果，包含新工作表的 sheet_id
        
    示例:
        # 添加新工作表
        add_spreadsheet_sheet(app_id, app_secret, "mholb6rrf1wm", "数据表2")
        
    注意:
        - 需要对表格文档有写权限
        - 工作表名称不能与现有工作表重复
    """
    c = _client(context)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/sheets/add", {
        "doc_id": doc_id,
        "sheet_name": sheet_name,
    }))


@mcp.tool(tags={"km"})
def insert_spreadsheet_rows(
    context: Context,
    doc_id: str,
    sheet_id: str,
    range: str,
    range_address: str,
    values: str,
) -> str:
    """插入表格行
    
    在指定位置插入新的表格行。
    
    Args:
        doc_id: 表格文档ID
            - 格式如 "mholb6rrf1wm"
            - 可从表格 URL 中获取
        sheet_id: 工作表ID（必填）
            - 格式如 "5qRDtgp"
            - 可通过 get_spreadsheet_sheets 获取
        range: 完整范围，包含工作表名
            - 格式："{工作表名}!{起始单元格}:{结束单元格}"
            - 示例："工作表1!A1:B5"
        range_address: 插入位置，纯范围地址（单元格或范围）
            - 格式："{起始单元格}" 或 "{起始单元格}:{结束单元格}"
            - 示例："A3" 或 "A1:C3"
            - 注意：新行会插入到指定位置
        values: 要插入的数据，JSON 字符串格式的二维数组
            - 格式：'[["row1_col1", "row1_col2"], ["row2_col1", "row2_col2"]]'
            - 示例：'[["新数据1", "值1"], ["新数据2", "值2"]]'
    Returns:
        插入结果
        
    示例:
        # 在指定位置插入新行
        insert_spreadsheet_rows(app_id, app_secret, "mholb6rrf1wm", 
                                "5qRDtgp", "工作表1!A1:B5", "A3",
                                '[["新数据1", "值1"], ["新数据2", "值2"]]')
        
    注意:
        - 需要对表格文档有写权限
        - resource 字段需要是 JSON 字符串格式：'{"values": [[...]]}'
    """
    c = _client(context)
    values_list = json.loads(values)
    if not isinstance(values_list, list):
        raise ValueError("values 必须是二维数组格式")
    resource_json = json.dumps({"values": values_list}, ensure_ascii=False)
    return _result(c.post("/openapi/docs/v1/doc/spreadsheet/rows/insert", {
        "doc_id": doc_id,
        "range": range,
        "sheet_id": sheet_id,
        "range_address": range_address,
        "resource": resource_json,
    }))


@mcp.tool(tags={"km"})
def get_doc_children(
    context: Context,
    knowledge_id: str,
    parent_doc_id: Optional[str] = None,
    uid_type: str = "user_id",
) -> str:
    """获取文档子节点列表
    
    用于遍历知识库的文档树结构，获取指定文档下的所有子文档。
    
    Args:
        knowledge_id: 知识库ID（必填）
            - 知识库ID可以从知识库URL中获取
            - 也可以通过其他接口获取
        parent_doc_id: 父文档ID（可选）
            - 不传则获取知识库根目录下的文档列表
            - 传入文档ID则获取该文档下的子文档列表
            - 格式如 "doc_xxx"
        uid_type: 用户ID类型，可选值:
            - "user_id": 用户域账号（默认）
            - "open_id": 用户open_id
            
    Returns:
        子文档列表，包含每个文档的:
        - doc_id: 文档ID
        - title: 文档标题
        - doc_type: 文档类型（document/markdown/spreadsheet等）
        - create_time: 创建时间
        - update_time: 更新时间
        
    示例:
        # 获取知识库根目录下的文档
        get_doc_children(app_id, app_secret, "kb_xxx")
        
        # 获取某个文档下的子文档
        get_doc_children(app_id, app_secret, "kb_xxx", "doc_xxx")
    """
    c = _client(context)
    body: dict = {
        "knowledge_id": knowledge_id,
        "uid_type": uid_type,
    }
    if parent_doc_id:
        body["parent_doc_id"] = parent_doc_id
    return _result(c.post("/openapi/docs/v1/doc/children/get", body))


@mcp.tool(tags={"km"})
def retrieve(
    context: Context,
    query: str,
    knowledge_id_list: Optional[List[str]] = None,
    doc_id_list: Optional[List[str]] = None,
    top_k: int = 5,
    score_threshold: float = 0.0,
) -> str:
    """在知识库中检索文档内容（语义搜索）
    
    基于自然语言查询，在指定的知识库或文档范围内进行语义检索，返回最相关的文档片段。
    
    Args:
        query: 检索查询文本，自然语言描述
            - 例如："如何配置机器人权限"
            - 例如："Wave API 的认证方式"
            - 支持中英文查询
        knowledge_id_list: 知识库ID列表（可选）
            - 指定要检索的知识库范围
            - 永远由数字组成，如 "371412"
            - 格式如 ["1500", "2033"]
            - 不传则在有权限的所有知识库中检索
        doc_id_list: 文档ID列表（可选）
            - 指定要检索的文档范围
            - 是由mh开头的字符串，如 "mhdinvixorfg"
            - 格式如 ["mhkuh002j0l3", "mys5p2z944r7"]
            - 可与 knowledge_id_list 同时使用
        top_k: 返回结果数量
            - 返回相关度最高的前 K 个结果
            - 取值范围：1-30
        score_threshold: 相关度阈值
            - 只返回相关度分数 >= 该值的结果
            - 取值范围：0.0-1.0
            
    Returns:
        检索结果列表，每个结果包含:
        - meta: 元信息
          - doc_id: 文档ID
          - doc_url: 文档链接
          - title: 文档标题
        - score: 相关度分数（0-1，越高越相关）
        - content: 匹配的文档片段内容
        - invalid_scope: 无效的检索范围（如果有）
        
    示例:
        # 在指定知识库中检索
        retrieve(app_id, app_secret, "如何使用消息API", 
                knowledge_id_list=["kb_xxx"])
        
        # 在指定文档中检索
        retrieve(app_id, app_secret, "机器人配置步骤",
                doc_id_list=["doc_xxx", "doc_yyy"])
        
        # 高相关度检索（只返回分数>0.7的结果）
        retrieve(app_id, app_secret, "Wave API认证",
                top_k=10, score_threshold=0.7)
    
    注意:
        - 需要对知识库或文档有读权限
        - 检索基于语义理解，不是简单的关键词匹配
        - 返回的 content 是文档的相关片段，不是完整文档
    """
    c = _client(context)
    scope: dict = {}
    if knowledge_id_list:
        scope["knowledge_id_list"] = knowledge_id_list
    if doc_id_list:
        scope["doc_id_list"] = doc_id_list
    return _result(c.post("/openapi/docs/v1/doc/retrieve", {
        "scope": scope,
        "query": query,
        "setting": {
            "top_k": top_k,
            "score_threshold": score_threshold,
        },
    }))


# ============================================================================
# App 模块 Tools - 应用管理
# ============================================================================

@mcp.tool(tags={"others"})
def set_app_visibility(
    context: Context,
    uids: List[str],
    op_type: str = "add",
) -> str:
    """设置应用可见范围
    
    Args:
        uids: 用户ID列表（域账号）
        op_type: 操作类型
            - "add": 添加可见用户
            - "delete": 移除可见用户
            
    Returns:
        设置结果
    """
    c = _client(context)
    return _result(c.post("/openapi/app/v1/visibility/set", {
        "uids": uids,
        "op_type": op_type,
    }))


# ============================================================================
# Calendar 模块 Tools - 日历管理
# ============================================================================

@mcp.tool(tags={"calendar"})
def create_calendar(
    context: Context,
    user_token: str,
    title: str,
    description: Optional[str] = None,
    public_scope: str = "private",
) -> str:
    """创建日历
    
    Args:
        user_token: 用户token（需要用户授权获取）
        title: 日历名称，如 "工作日历"
        description: 日历描述
        public_scope: 公开范围
            - "private": 私有（默认）
            - "public": 公开
            
    Returns:
        创建结果，包含 calendar_id
        
    注意: 日历操作需要用户授权，需要先获取 user_token
    """
    c = _client(context)
    body: dict = {
        "title": title,
        "public_scope": public_scope,
    }
    if description:
        body["description"] = description
    return _result(c.post("/openapi/calendar/v1/calendar/create", body, token_override=user_token))


@mcp.tool(tags={"calendar"})
def get_calendar_list(
    context: Context,
    user_token: str,
) -> str:
    """获取日历列表
    
    Args:
        user_token: 用户token
        
    Returns:
        用户的日历列表
    """
    c = _client(context)
    return _result(c.post("/openapi/calendar/v1/calendars/get", {}, token_override=user_token))


@mcp.tool(tags={"calendar"})
def create_calendar_event(
    context: Context,
    user_token: str,
    calendar_id: str,
    title: str,
    start_date: str,
    end_date: str,
    description: Optional[str] = None,
    is_all_day: bool = True,
) -> str:
    """创建日历事件
    
    Args:
        user_token: 用户token
        calendar_id: 日历ID
        title: 事件标题，如 "项目会议"
        start_date: 开始日期，格式 "2025-02-06"
        end_date: 结束日期，格式 "2025-02-06"
        description: 事件描述
        is_all_day: 是否全天事件，默认 True
        
    Returns:
        创建结果，包含 event_id
        
    示例:
        create_calendar_event(app_id, app_secret, user_token, "cal_xxx", 
                             "周会", "2025-02-06", "2025-02-06", "每周例会")
    """
    c = _client(context)
    body: dict = {
        "calendar_id": calendar_id,
        "title": title,
        "is_all_day": is_all_day,
        "start_time": {"date": start_date},
        "end_time": {"date": end_date},
    }
    if description:
        body["description"] = description
    return _result(c.post("/openapi/calendar/v1/event/create", body, token_override=user_token))


@mcp.tool(tags={"calendar"})
def get_calendar_event_info(
    context: Context,
    user_token: str,
    event_id: str,
) -> str:
    """获取日历事件详情
    
    Args:
        user_token: 用户token
        event_id: 事件ID
        
    Returns:
        事件详情
    """
    c = _client(context)
    return _result(c.post("/openapi/calendar/v1/event/get", {"event_id": event_id}, token_override=user_token))


@mcp.tool(tags={"calendar"})
def get_calendar_event_list(
    context: Context,
    user_token: str,
    calendar_id: str,
    start_time: str,
    end_time: str,
) -> str:
    """获取日历事件列表
    
    Args:
        user_token: 用户token
        calendar_id: 日历ID
        start_time: 开始时间，格式 "2025-02-06 00:00"
        end_time: 结束时间，格式 "2025-02-06 23:59"
        
    Returns:
        时间范围内的事件列表
    """
    c = _client(context)
    return _result(c.post("/openapi/calendar/v1/events/get", {
        "calendar_id": calendar_id,
        "start_time": start_time,
        "end_time": end_time,
    }, token_override=user_token))


@mcp.tool(tags={"calendar"})
def cancel_calendar_event(
    context: Context,
    user_token: str,
    event_id: str,
) -> str:
    """取消日历事件
    
    Args:
        user_token: 用户token
        event_id: 事件ID
        
    Returns:
        取消结果
    """
    c = _client(context)
    return _result(c.post("/openapi/calendar/v1/event/cancel", {"event_id": event_id}, token_override=user_token))


@mcp.tool(tags={'minutes'})
def get_meeting_minutes(
    context: Context,
    cursor: str,
    limit: int = 20,
) -> str:
    """获取当前用户的会议纪要列表
    
    Args:
        cursor: 分页游标 ，首次访问不填，后续翻页须使用返回值里的next_cursor
        limit: 每页数量，默认20，最大20
        
    Returns:
        会议纪要列表
    """
    c = _client(context)
    return _result(c.post("/openapi/minutes/v1/minutes/get", {
        "cursor": cursor,
        "limit": limit,
    }))


@mcp.tool(tags={'minutes'})
def get_meeting_minutes_transcript(
    context: Context,
    minute_id: str,
) -> str:
    """获取会议纪要的转录文本
    
    Args:
        minute_id: 会议纪要ID
    """
    c = _client(context)
    return _result(c.post("/openapi/minutes/v1/minutes/transcript/get", {"minute_id": minute_id}))

# ============================================================================
# File 模块 Tools - 文件管理
# ============================================================================

@mcp.tool(tags={"chat", "group_chat"})
def get_public_url(context: Context, file_key: str) -> str:
    """获取文件公开URL
    
    Args:
        file_key: 文件key，从上传文件或消息中获取
        
    Returns:
        文件的公开访问URL
    """
    c = _client(context)
    return _result(c.post("/openapi/file/v1/public_url/get", {"file_key": file_key}))


@mcp.tool(tags={"chat", "group_chat"})
def upload_file(
    context: Context,
    file_path: str,
) -> str:
    """上传文件到 Wave 平台
    
    将本地文件上传到 Wave 服务器，获取 file_key 用于后续发送文件消息等操作。
    注意文件需要在MCP所在机器的本地路径，不能是网络路径。如果你是订阅的远程MCP, 则无法使用。
    
    Args:
        file_path: 本地文件路径
            - 支持绝对路径，如 "/Users/xxx/documents/report.pdf"
            - 支持相对路径，如 "./files/image.png"
            - 支持各种文件类型：文档、图片、视频、压缩包等
            
    Returns:
        上传结果，包含:
        - file_key: 文件唯一标识，用于发送消息时引用
        - 其他元信息
        
    示例:
        # 上传PDF文件
        upload_file(app_id, app_secret, "/path/to/document.pdf")
        
        # 上传图片
        upload_file(app_id, app_secret, "./images/screenshot.png")
        
    注意:
        - 文件大小有限制，大文件请使用分片上传接口
        - 上传成功后返回的 file_key 可用于 send_message 发送文件消息
    """
    c = _client(context)
    with open(file_path, 'rb') as f:
        resp = c.upload("/openapi/file/v1/upload", f)
    return _result(resp)


# ============================================================================
# JsAuth 模块 Tools - JS鉴权
# ============================================================================

@mcp.tool(tags={"others"})
def get_js_auth_ticket(context: Context) -> str:
    """获取JS鉴权票据
    
    Args:
    Returns:
        JS鉴权票据，用于前端JS SDK初始化
    """
    c = _client(context)
    return _result(c.post("/openapi/jssdk/v1/ticket/get", {}))


# ============================================================================
# Common 模块 Tools - 通用功能
# ============================================================================

@mcp.tool(tags={"others"})
def get_outbound_ip(context: Context) -> str:
    """获取应用出口IP列表
    
    Args:
    Returns:
        应用服务器的出口IP列表，用于配置防火墙白名单
    """
    c = _client(context)
    return _result(c.post("/openapi/common/v1/outbound_ip", {}))


# ============================================================================
# 服务启动
# ============================================================================

def main():
    """主入口"""
    if args.transport in ["sse", "streamable-http"]:
        mcp.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            json_response=args.json_response,
            stateless_http=args.stateless_http,
        )
    else:
        mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
