# Joplin MCP 工具参考

基线：Joplin Desktop 3.7.21。运行时以 `tools`、`describe TOOL` 返回的定义为准。使用 `call TOOL --args-file FILE` 可以完整传递服务器支持的参数。

| 工具 | 参数 | 返回与说明 |
|---|---|---|
| `search_notes` | `query` 必填；`limit` 1–100，默认20 | `results` 含 ID、标题、笔记本 ID、更新时间、正文片段；`total` 为匹配总数。无分页参数。 |
| `semantic_search_notes` | `query` 必填；可选 `notebook_id` 或 `tag_id`（不可同时传）；`relevance` 为 strict/normal/loose | 返回相关块的 `note_id`、`chunk_text`、`chunk_index`、score 等。依赖本机向量索引。 |
| `read_note` | `id` 必填；`offset`，`max_chars` | 返回正文、标题、笔记本、标签、时间及 `has_more`。当前实现按 JavaScript 字符串索引切分；offset 是服务器返回的索引，不是 UTF-8 字节数。中文和 emoji 阅读应沿用服务器的分段元数据，不用 Python 的字节长度推算。 |
| `read_image` | 图片资源 `id` 必填；`resolution` low/medium/high，默认medium | 返回 MCP 图片内容。当前 Joplin 缩放最大尺寸分别128/256/512像素。客户端保存图片并输出路径。不是任意附件下载。 |
| `list_notebooks` | 无 | 返回 `id`、`title`、`parent_id`、`note_count`。父级为空时为根笔记本。 |
| `list_tags` | 无 | 返回已关联笔记的标签 ID 与标题；不含空标签。 |
| `create_note` | `title` 必填；`body`、`notebook_id`、`is_todo` | 返回新笔记 ID。省略 notebook_id 会使用默认笔记本。 |
| `update_note` | `id` 必填；`title`、`body`、`append`、`prepend`、`replace_text: {find,replace}`、`notebook_id`、`todo_completed` | 更新提供的字段。body 优先于局部操作；replace_text 必须唯一匹配；notebook_id 可移动笔记。当前没有设置 todo_due 的参数。 |
| `delete_note` | `id` 必填 | 移入回收站，可在 Joplin 恢复。 |
| `manage_tags` | `note_id` 必填；`add`、`remove` 为标签名称数组 | 至少提供一个非空操作数组。add 中未知标签自动创建；返回 added/removed/tags。 |
| `create_notebook` | `title` 必填；可选 `parent_id` | 创建一个笔记本，不自动解析整条路径。返回 id/title/parent_id。 |

## 常见输入

创建笔记：

```json
{"title":"性能优化经验","body":"# 结论\n\n## 来源\n[原始记录](:/NOTE_ID)","notebook_id":"FOLDER_ID"}
```

局部更新：

```json
{"id":"NOTE_ID","replace_text":{"find":"唯一匹配的原文","replace":"新内容"}}
```

移动笔记并标记待办：

```json
{"id":"NOTE_ID","notebook_id":"TARGET_FOLDER_ID","todo_completed":true}
```

语义检索：

```json
{"query":"以前如何排查服务性能问题","notebook_id":"FOLDER_ID","relevance":"normal"}
```

搜索过滤语法：

```json
{"query":"notebook:\"工作记录\" Redis","limit":20}
```

`notebook:` 为 Joplin 搜索语法的笔记本标题过滤，不要将其当成严格路径或权限隔离。优先通过具体 ID 操作确定的笔记。所有写入是否允许由 Joplin 的工具开关和笔记自身状态决定。

## 输出与错误

正常工具结果：

```json
{"ok":true,"tool":"read_note","data":{"id":"NOTE_ID","body":"..."},"artifacts":[]}
```

图片的 data/artifacts 包含本地 `path`，不包含 Base64。多个 MCP 内容块按顺序保留；未来未知类型也会保留。附加 MCP 元数据放在 `mcp_metadata`。

`--raw` 返回 `result` 中的原始 MCP 内容，不做图片落盘和正文解析。它仍校验基本协议结构与 `isError`，不把畸形内容当作成功响应。

错误 kind：input/configuration、transport、http、protocol、rpc、tool、output/local。错误对象的键和值会脱敏已知配置 Token；成功读取的笔记正文保留原文。CLI 的 JSON stdin/stdout 使用 UTF-8。

- `outcome_unknown: true`：可能写入的调用遇到连接故障、超时、HTTP 408/5xx、内部 RPC 错误、不明确的协议响应或工具 `isError`。工具报错不保证回滚；先核对服务端状态，不能直接重复写入。
- `server_succeeded: true`：成功工具响应已返回，但本地输出失败。只修复输出，不重复执行工具。批量统计将该项计为失败。

未知工具按可能写入处理。所有请求都不自动重试；批量不是事务。输入和响应 JSON 的重复字段、非标准数值、溢出数字及过深嵌套会被拒绝。嵌套深度最多 100（顶层为 0），整数最多 4300 位。单次 HTTP 响应最多 32 MiB，工具发现最多 100 页、10000 项。

当前支持 MCP 协议 `2025-06-18`。不支持的协商版本在初始化阶段报错，不会继续调用工具。

工具禁用时仍可能出现在 tools/list；名称存在不能证明已经启用。`doctor` 依据当前 Joplin 的 Disabled tool 描述报告开关状态，实际调用错误是最终依据。
