## Wave Open Platform MCP（`mcp-server-wave`）

提供 Wave Open API 的 MCP 工具集（消息、群聊、知识库文档/表格、日历、应用配置等），支持工具过滤，可以只暴露部分工具。

### 鉴权方式（必须）

- **方式 A：环境变量（适用于 stdio）**
  - `APP_ID`
  - `APP_SECRET`
- **方式 B：HTTP Headers（适用于 SSE / streamable-http）**
  - `app_id`
  - `app_secret`

### 安装（本地运行时需要）

在本目录安装依赖：

```bash
pip install -r requirements.txt
```

### MCP 配置（mcp_config）怎么写

下面示例以常见的 `mcpServers` 配置结构为例；你也可以按你当前 Agent 的配置文件格式嵌入对应 server 节点。

#### echo 内配置
```json
{
  "transport": "echo",
  "name": "openapi-mcp",
  "args": [
    "--include-tools",
    "chat,group_management,group_chat,km,calendar,others"
  ],
  "env": {
    "APP_ID": "你的_app_id",
    "APP_SECRET": "你的_app_secret"
  }
}
```

### `--include-tools`（工具集过滤）

`openapi-mcp/tools/server.py` 会用 `FastMCP(..., include_tags=include_tools)` 按 **tag** 过滤工具。

- `--include-tools all`：使用代码里的默认 tag 列表
- `--include-tools chat,km,...`：只暴露指定 tag 的工具

### ToolTags（tag 含义）

- **chat**：消息发送/回复/撤回/卡片更新/表情回应等
- **group_management**：群管理（建群、拉人、踢人、解散、公告、管理员、转让群主、配置）
- **group_chat**：群查询（机器人所在群列表、群成员列表）
- **km**：知识库/文档/表格（创建、更新、追加、表格读写、工作表管理、检索等）
- **calendar**：日历（创建日历、事件增删查）
- **others**：应用可见范围、JS 鉴权票据、出口 IP 等其他能力

### Tools 列表（表格）

| 工具名 | tags | 描述 |
| --- | --- | --- |
| `send_message` | `chat` | 发送消息（支持 text/card/image/file/video/rich_text/markdown 等，content 为 JSON 字符串） |
| `send_text_message` | `chat` | 发送纯文本消息（简化版，无需手写 JSON） |
| `send_batch_message` | `chat` | 批量发送消息给多个用户 |
| `reply_message` | `chat` | 回复指定消息 |
| `get_message` | `chat` | 获取消息详情 |
| `recall_message` | `chat` | 撤回消息 |
| `recall_batch_message` | `chat` | 批量撤回消息 |
| `active_update_card` | `chat` | 主动更新卡片消息内容 |
| `get_msg_reaction_list` | `chat` | 获取消息的表情回应列表 |
| `query_batch_message_status` | `chat` | 查询批量消息发送状态 |
| `get_chats` | `group` | 获取机器人所在的群聊列表 |
| `get_chat_members` | `group` | 获取群成员列表 |
| `create_chat` | `group_management` | 创建群聊 |
| `join_chat` | `group_management` | 添加成员到群聊 |
| `delete_chat_members` | `group_management` | 从群聊中移除成员 |
| `disband_chat` | `group_management` | 解散群聊 |
| `set_announcement` | `group_management` | 设置群公告 |
| `set_chat_manager` | `group_management` | 设置群管理员 |
| `transfer_chat_owner` | `group_management` | 转让群主 |
| `update_chat_config` | `group_management` | 更新群聊配置（如添加成员/编辑权限） |
| `create_document` | `km` | 创建文档（HTML/Text） |
| `create_markdown` | `km` | 创建 Markdown 文档 |
| `get_doc_detail` | `km` | 获取文档详情 |
| `update_document` | `km` | 更新文档 |
| `update_markdown` | `km` | 更新 Markdown 文档 |
| `append_document` | `km` | 追加文档内容 |
| `get_spreadsheet_resource` | `km` | 读取表格单元格数据（需要 `sheet_id` + `range_address`） |
| `create_spreadsheet` | `km` | 创建表格文档 |
| `update_spreadsheet_resource` | `km` | 更新表格数据（HTTP 手动请求） |
| `append_spreadsheet_resource` | `km` | 追加表格数据（HTTP 手动请求） |
| `get_spreadsheet_sheets` | `km` | 获取工作表列表（HTTP 手动请求） |
| `add_spreadsheet_sheet` | `km` | 添加工作表 |
| `insert_spreadsheet_rows` | `km` | 插入表格行（HTTP 手动请求） |
| `get_doc_children` | `km` | 获取文档子节点列表（遍历文档树） |
| `retrieve` | `km` | 在知识库中检索文档内容（语义搜索） |
| `get_public_url` | `chat,group_chat,km` | 获取文件公开 URL |
| `upload_file` | `chat,group_chat,km` | 上传文件到 Wave 平台（file_path 必须是 MCP 运行机器本地路径） |
| `create_calendar` | `calendar` | 创建日历（需要 `user_token`） |
| `get_calendar_list` | `calendar` | 获取日历列表（需要 `user_token`） |
| `create_calendar_event` | `calendar` | 创建日历事件（需要 `user_token`） |
| `get_calendar_event_info` | `calendar` | 获取日历事件详情（需要 `user_token`） |
| `get_calendar_event_list` | `calendar` | 获取日历事件列表（需要 `user_token`） |
| `cancel_calendar_event` | `calendar` | 取消日历事件（需要 `user_token`） |
| `get_js_auth_ticket` | `others` | 获取 JS 鉴权票据 |
| `get_outbound_ip` | `others` | 获取应用出口 IP 列表 |
| `set_app_visibility` | `others` | 设置应用可见范围 |
| `get_users` | `others` | 获取用户信息（通讯录） |
| `convert_user_ids` | `others` | 转换用户 ID 类型（user_id/open_id 等） |


