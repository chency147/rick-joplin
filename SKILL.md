---
name: rick-joplin
description: |
  Rick 的 Joplin 笔记助手，使用纯 Python 标准库直接调用官方 MCP。
  支持关键词与语义检索、笔记读写和移动、图片读取、笔记本及标签管理、批量调用。
  当用户要搜索、查看、创建、修改、整理 Joplin 笔记，或向 Joplin 保存内容时使用。
  Use for Joplin note operations, semantic search, image attachments, notebooks, tags, and knowledge-base maintenance.
license: MIT
compatibility: Python 3.9+; Joplin Desktop with Web Clipper, JSON HTTP MCP protocol 2025-06-18 and required tools enabled; JOPLIN_TOKEN.
---

# Rick Joplin

所有 Joplin 操作通过 `scripts/joplin_mcp.py` 调用官方 `/mcp`。将本文中的 `<skill_dir>` 解析为本文件所在目录，使用绝对脚本路径。无需 pip、Node.js、npx 或独立 MCP 代理，也不需要在 Agent 中另外注册 MCP。

## 连接与诊断

- Joplin 桌面版应正在运行，Web Clipper 和「AI → Enable MCP server」已启用。
- 使用与 Web Clipper 共用的环境变量 `JOPLIN_TOKEN`。Token 不写进命令参数、日志、笔记或技能文件。
- 默认地址为 `http://127.0.0.1:41184/mcp`；用 `JOPLIN_MCP_URL` 指定其他地址，也兼容原来的 `JOPLIN_URL` 基础地址。
- 脚本读取**当前进程**的环境变量；如果 Token 配置在用户登录 Shell 中，可使用已知的登录 Shell 运行命令，例如 `zsh -lic 'python3 /absolute/path/rick-joplin/scripts/joplin_mcp.py doctor'`。不要搜索私密配置文件、打印 Token 或自动执行任意 Shell 配置。
- 可选 `--config /absolute/path/config.json` 支持 `url`、`token`、`timeout`、`artifacts_dir`。保管好含 Token 的配置文件。URL 优先级：命令参数、JOPLIN_MCP_URL、配置文件、JOPLIN_URL、默认值；Token 优先使用环境变量。

```bash
python3 <skill_dir>/scripts/joplin_mcp.py doctor
python3 <skill_dir>/scripts/joplin_mcp.py tools
python3 <skill_dir>/scripts/joplin_mcp.py describe update_note
```

`doctor` 检查握手、ping 和完整工具列表，报告被禁用与缺失的工具。`ok: true` 表示诊断完成，仍需检查 `disabled_tools` 和 `missing_expected_tools`。它不会启用工具、修改设置，或使用 REST 绕过权限。已实测 Joplin Desktop 3.7.21；客户端当前支持协议 `2025-06-18`，拒绝其他协商版本。语义检索另需 Joplin 的本地向量索引，索引是否可用由工具实际调用判断。

## 通用调用：覆盖全部 MCP 能力

工具名称、参数来自服务器。调用不使用固定工具白名单；新增工具也能通过 `call` 使用。不确定参数时先 `describe`，详细说明按需阅读 `references/tools.md`。

```bash
python3 <skill_dir>/scripts/joplin_mcp.py call list_notebooks
python3 <skill_dir>/scripts/joplin_mcp.py call search_notes --args '{"query":"Redis 性能","limit":10}'
python3 <skill_dir>/scripts/joplin_mcp.py call semantic_search_notes --args '{"query":"以前如何排查服务性能问题","relevance":"normal"}'
python3 <skill_dir>/scripts/joplin_mcp.py call read_note --args '{"id":"NOTE_ID","offset":0,"max_chars":8000}'
python3 <skill_dir>/scripts/joplin_mcp.py call update_note --args-file /absolute/path/update.json
```

对于长正文和包含引号、代码的内容，先用文件工具写入 UTF-8 JSON 文件，再使用 `--args-file`。也可以使用 `--stdin` 读取一个 JSON 对象；CLI 的 JSON stdin/stdout 固定使用 UTF-8。不要拼接未经正确转义的 Shell 字符串。

正常输出为 UTF-8 JSON：`ok`、`tool`、`data`、`artifacts`。帮助及版本命令例外。返回非零退出码表示失败；正常工具结果和错误都应检查 `ok`。`--raw` 返回原始 MCP 工具结果，只在需要协议原文时使用，仍校验基本响应结构。输入 JSON 不允许重复字段或非有限数值，嵌套深度最多 100（顶层为 0），整数最多 4300 位。

## 读取、编辑与组织

1. 先搜索或列出笔记本获取真实 ID，不猜测 ID，不只凭可能重复的标题修改笔记。创建后直接使用返回的 ID 验证；关键词索引异步更新，真实验收中约延迟10秒，刚创建的笔记搜索不到不代表创建失败，不要因此重复创建。
2. 笔记本返回平面列表，可由 `parent_id` 重建完整路径；遇到重名必须使用路径或 ID 明确目标。
3. 精确名称、版本号和错误码使用 `search_notes`；描述性问题使用 `semantic_search_notes`。语义结果是片段，需要时用 `read_note` 查看原文。
4. 小修改优先使用 `update_note` 的 `append`、`prepend`、`replace_text`。精确替换要求唯一匹配；不唯一时补充上下文。
5. 用 `notebook_id` 移动笔记。创建知识整理笔记时显式指定目标笔记本，避免写入默认位置。
6. `delete_note` 将笔记移入回收站，不是永久删除；仅在用户授权范围内调用。
7. 个人知识库整理保留原始记录，在独立主题笔记中写结论并用 `[标题](:/NOTE_ID)` 引用来源。使用 ID 和 `updated_time` 跟踪资料，不能凭重复日期标题合并内容。
8. MCP 工具权限是操作类型权限；查询中的笔记本过滤不等于服务端访问隔离。

## 图片

```bash
python3 <skill_dir>/scripts/joplin_mcp.py image RESOURCE_ID --resolution high
python3 <skill_dir>/scripts/joplin_mcp.py image RESOURCE_ID --output /absolute/path/new-image.png
```

默认将 MCP 图片 Base64 解码为本地文件，仅返回路径、MIME 和大小。**随后用 Agent 的图片/文件读取工具打开返回的 `artifacts[].path`，才能实际查看图片。**通用 `call read_image` 同样会保存图片。指定输出文件不得已存在，脚本不会覆盖它。

分辨率 high 也受 Joplin 工具本身的缩放限制，不能据此保证小字识别准确。不要把 `--raw` 的大段 Base64 当作文本交给模型；`--raw` 与 `image --output` 不可同时使用。

## 批量调用

先创建 JSON 数组，所有项目的结构与 JSON 可序列化性在发送任何调用前都会校验；具体工具参数的语义和权限由服务端判断：

```json
[
  {"label":"笔记本","tool":"list_notebooks","arguments":{}},
  {"label":"标签","tool":"list_tags","arguments":{}}
]
```

```bash
python3 <skill_dir>/scripts/joplin_mcp.py batch --file /absolute/path/calls.json
```

一次进程只初始化一次并复用连接，按顺序执行。默认遇到失败停止；`--continue-on-error` 继续后续项目。结果包含成功、失败和跳过数量。批量不是事务，不会自动回滚已成功的修改。项目之间不支持结果变量引用；有依赖的操作分阶段执行。

## 失败处理与边界

- 请求不自动重试。写请求遇到连接故障、超时、HTTP 408/5xx、内部 RPC 错误、不明确的协议响应或工具 `isError` 时报告 `outcome_unknown: true`；工具报错不保证回滚，先核对实际笔记，再决定是否重试。未知工具按可能写入处理。
- `server_succeeded: true` 表示工具成功响应已到达，但本地输出处理失败；只修复本地输出问题，不重复写入。批量中这类项目仍计为失败。
- 错误输出会脱敏已知配置 Token；成功读取的笔记原文可能包含用户自行保存的其他凭据，不能直接转发到日志或公开文档。
- HTTP 403 需检查 Token 与 MCP 开关；工具 `isError` 需检查具体错误和工具开关；不要静默退回 REST。
- 当前 `search_notes` 单次最多 100 条且没有分页参数。结果 `total` 大于返回数量时不能声称已经遍历全库。
- `list_notebooks` 返回笔记数量但不包含共享成员权限。当前 MCP 没有附件上传、任意 PDF 下载、笔记本删除、共享权限管理、完整导出等工具。
- 与旧技能相比，待办到期时间设置和某些链接检查不是当前 MCP 的独立功能；不要宣称所有旧命令都能直接替代。
- 新增或不同内容类型会尽量保留，原始协议可用 `--raw` 查看。本客户端针对 Joplin 的 JSON HTTP MCP，不是通用 SSE/stdio MCP SDK。
