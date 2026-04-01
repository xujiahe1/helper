# KM 文档读写指南

本文档说明如何理解 `get_doc_detail` 返回的文档结构，以及如何使用 `edit_document` 进行编辑。

---

## 文档结构概览

`get_doc_detail` 返回的 JSON 形如：

```json
{
  "type": "doc",
  "content": [
    { "_anchor": "1",   "type": "heading",   ... },
    { "_anchor": "1.1", "type": "paragraph", ... },
    { "_anchor": "1.2", "type": "listNew",   ... },
    ...
  ]
}
```

`content` 是一个**有序的平铺数组**，每个元素是一个**块节点**（block）。每个块都有 `_anchor` 字段标识位置。

---

## 块节点类型

### heading — 标题

```json
{
  "_anchor": "1",
  "type": "heading",
  "attrs": { "level": 2 },
  "content": [{ "type": "text", "text": "标题文字" }]
}
```

`level`: 1-6，对应 h1-h6。

### paragraph — 段落

```json
{
  "_anchor": "1.1",
  "type": "paragraph",
  "attrs": { "indent": 0 },
  "content": [{ "type": "text", "text": "正文内容" }]
}
```

`indent`: 缩进级别，0=无缩进。空段落不含 `content`。

### listNew — 列表项

```json
{
  "_anchor": "2.1",
  "type": "listNew",
  "attrs": {
    "listId": "d4d0fdeb",
    "listIndent": 1,
    "kind": "ordered",
    "checked": false
  },
  "content": [{ "type": "text", "text": "列表内容" }]
}
```

- `listId`：相同 listId 的连续项属于同一列表
- `listIndent`：嵌套层级（1=顶层，2=子列表...）
- `kind`：`"bullet"` 或 `"ordered"`
- `checked`：任务列表勾选状态

**新增列表项时**，使用相同的 `listId` 和 `kind` 来追加到已有列表。

### codeBlock — 代码块

```json
{
  "_anchor": "3.1",
  "type": "codeBlock",
  "attrs": { "language": "python" },
  "content": [{ "type": "text", "text": "print('hello')" }]
}
```

### expansion — 折叠块

```json
{
  "_anchor": "4.1",
  "type": "expansion",
  "attrs": { "indent": 0 },
  "content": [
    { "type": "expansion_summary", "content": [{ "type": "text", "text": "折叠标题" }] },
    {
      "type": "expansion_content",
      "content": [
        { "type": "paragraph", "content": [{ "type": "text", "text": "折叠内容" }] }
      ]
    }
  ]
}
```

- `expansion_summary`：折叠块的可见标题
- `expansion_content`：展开后显示的内容，内部包含正常的块节点

### horizontalRule — 分割线

```json
{
  "_anchor": "4.2",
  "type": "horizontalRule",
  "attrs": { "indent": 0 }
}
```

无 `content`，纯装饰性分隔。

### tooltip — 提示块

```json
{
  "_anchor": "4.3",
  "type": "tooltip",
  "attrs": {
    "indent": 0,
    "showTitle": false,
    "colorTheme": "yellow",
    "icon": "💡"
  },
  "content": [
    { "type": "tooltip_title" },
    {
      "type": "tooltip_content",
      "content": [
        { "type": "paragraph", "content": [{ "type": "text", "text": "提示内容" }] }
      ]
    }
  ]
}
```

- `colorTheme`：颜色主题，如 `"yellow"`, `"blue"`, `"green"`, `"red"`
- `icon`：前缀图标（emoji）
- `tooltip_title`：标题（`showTitle: false` 时为空节点）
- `tooltip_content`：内部包含正常的块节点

### table — 表格

```json
{
  "_anchor": "5.1",
  "type": "table",
  "content": [
    {
      "type": "tableRow",
      "content": [
        {
          "type": "tableCell",
          "attrs": { "colspan": 1, "rowspan": 1, "colwidth": [245] },
          "content": [{ "type": "paragraph", "content": [{ "type": "text", "text": "单元格" }] }]
        }
      ]
    }
  ]
}
```

- `tableCell.attrs.colspan` / `rowspan`：合并列/行
- `tableCell.attrs.colwidth`：列宽数组（像素）
- 每个 `tableCell.content` 内包含块节点（通常是 `paragraph`）

---

## 行内节点类型（出现在块的 `content` 数组内）

### text — 文本

```json
{ "type": "text", "text": "文字内容" }
```

可带 `marks` 数组表示样式：

```json
{
  "type": "text",
  "text": "加粗链接",
  "marks": [
    { "type": "bold" },
    { "type": "link", "attrs": { "href": "https://..." } }
  ]
}
```

| mark | 说明 |
|------|------|
| `bold` | 加粗 |
| `italic` | 斜体 |
| `code` | 行内代码 |
| `link` | 超链接，`attrs.href` 为 URL |
| `textStyle` | 颜色/背景，`attrs.color` / `attrs.backgroundColor` |

### mathematics — LaTeX 公式

```json
{
  "type": "mathematics",
  "attrs": { "data-content": "E = mc^2" }
}
```

`data-content` 为 LaTeX 表达式字符串。行内嵌入在 paragraph 等块节点的 `content` 中。

### image_ref — 图片

图片以简化格式呈现，不需要接触底层 card 结构：

```json
{
  "type": "image_ref",
  "ref_id": "c87a4029",
  "filename": "image.png",
  "width": "812px",
  "height": "362px",
  "url": "wave://c87a4029/image.png"
}
```

- `ref_id`：关联缓存中的原始数据，编辑时保留不动
- `url` 支持三种格式：
  - `wave://xxx/xxx` — 内部引用，可通过 `download_image` 工具下载查看
  - `https://...` — 网页图片 URL
  - 本地路径 — MCP 所在机器的文件路径
- 新增图片时，将 `url` 设为网页 URL 或本地路径即可，系统会自动中转上传
- 保留原有 `image_ref` 不动 = 图片不变
- 删除 `image_ref` = 删除图片

### km_link — KM 文档链接

```json
{
  "type": "km_link",
  "ref_id": "a8b405d4",
  "kmid": "mhgc2frzxkxa",
  "title": "Release Notes"
}
```

- `kmid`：KM 文档 ID（`mh` 开头）
- `title`：显示标题
- 保留 `ref_id` 不动 = 链接不变

### video_ref — 视频

```json
{
  "type": "video_ref",
  "ref_id": "c6bd0c6c",
  "filename": "demo.mp4",
  "size": 387118191,
  "url": "wave://c6bd0c6c/demo.mp4"
}
```

视频为块级节点，直接出现在 `doc.content` 数组中（不嵌套在其他块内）。

- `url` 支持与 `image_ref` 相同的三种格式：`wave://`、`https://`、本地路径
- 新增视频时，将 `url` 设为本地文件路径即可，系统自动中转上传到 KM 存储
- 视频文件较大，**仅支持本地路径上传**，不支持从网络 URL 下载后中转
- 保留原有 `video_ref` 不动 = 视频不变

## 编辑方式

适合修改文档中的**局部内容**。先 `get_doc_detail` 查看文档，再对指定锚点执行操作。

**参数**：
- `doc_id`：文档 ID
- `anchor`：目标块的 `_anchor` 值（字符串，如 `"3.2.1"`）
- `action`：操作类型
- `content`：JSON 对象（单个 block）或数组（多个 block）。`delete` 时可不传

**action 类型**：

| action | 效果 |
|--------|------|
| `replace` | 替换锚点处的整个 block |
| `insert_after` | 在锚点后插入新 block |
| `insert_before` | 在锚点前插入新 block |
| `delete` | 删除锚点处的 block |

**示例**：在 `_anchor: "2.3"` 后追加一个段落：

```
edit_document(
  doc_id="mhxxx",
  anchor="2.3",
  action="insert_after",
  content={
    "type": "paragraph",
    "attrs": {"indent": 0},
    "content": [{"type": "text", "text": "新增的段落内容"}]
  }
)
```

**示例**：追加列表项（使用相同 listId）：

```
edit_document(
  doc_id="mhr43oruy20m",
  anchor="2.1.7",
  action="insert_after",
  content={
    "type": "listNew",
    "attrs": {"listId": "70d41d44", "listIndent": 1, "kind": "ordered", "checked": false},
    "content": [{"type": "text", "text": "新增列表项"}]
  }
)
```

**注意**：
- `edit_document` 内部会自动拉取最新文档、执行编辑、提交更新
- 锚点值来自最近一次 `get_doc_detail` 的输出，修改后锚点会变化，需重新获取

## 编辑要点

1. **保留不变的内容**：不需要修改的 block 原样保留，特别是 `image_ref`、`video_ref`、`km_link` 的 `ref_id`
2. **新增列表项**：使用同一 `listId` + 相同 `kind` 追加，系统自动编号
3. **新增图片**：创建 `image_ref` 节点，不填 `ref_id`，填入上传后获得的 URL：
   ```json
   {"type": "image_ref", "filename": "new.png", "width": "400px", "height": "300px", "url": "<上传URL>"}
   ```
4. **新增 KM 链接**：创建 `km_link` 节点，不填 `ref_id`：
   ```json
   {"type": "km_link", "kmid": "mhxxxxxx", "title": "文档标题"}
   ```
5. **@提及**：在文本中直接写 `@姓名`，如果该用户之前被提及过则自动还原
6. **文本样式**：通过 `marks` 数组添加，如加粗 `[{"type": "bold"}]`、链接 `[{"type": "link", "attrs": {"href": "..."}}]`
