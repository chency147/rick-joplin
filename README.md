# Rick Joplin

`rick-joplin` 是使用纯 Python 标准库的 Joplin 官方 JSON HTTP MCP 客户端与 Agent 技能。支持关键词与语义检索、笔记读写、笔记本与标签管理、图片读取、动态工具发现和顺序批量调用。

Python 3.9+，无需 pip、Node.js、npx 或独立 MCP 代理。项目采用 [MIT 许可证](LICENSE)。

## 首次使用

1. 安装 Python 3.9+，获取项目目录，在项目根目录执行下方命令。macOS/Linux 使用 `python3`；Windows 可以替换为 `python`。
2. 启动 Joplin Desktop，在设置中开启 Web Clipper 服务，复制其 Token。
3. 在 Joplin「AI」设置中启用 MCP server，并启用需要的工具。已实测版本是 **Joplin Desktop 3.7.21**；其他版本是否可用以 `doctor` 和实际工具调用为准。
4. 将 Token 放进运行脚本的进程环境，再运行诊断。

macOS/Linux（Bash 或 Zsh，交互输入不回显，也不把 Token 写进命令历史）：

```bash
printf 'Joplin Web Clipper Token: '
IFS= read -r -s JOPLIN_TOKEN
printf '\n'
export JOPLIN_TOKEN
python3 scripts/joplin_mcp.py doctor
python3 scripts/joplin_mcp.py call list_notebooks
```

Windows PowerShell：

```powershell
$credential = Get-Credential -UserName 'Joplin' -Message '将 Web Clipper Token 填入密码栏'
$env:JOPLIN_TOKEN = $credential.GetNetworkCredential().Password
Remove-Variable credential
python scripts/joplin_mcp.py doctor
python scripts/joplin_mcp.py call list_notebooks
```

`doctor` 输出连接、协议版本、工具列表以及被禁用或缺失的工具。它不修改 Joplin 设置；`ok: true` 代表诊断完成，仍应检查 `disabled_tools` 和 `missing_expected_tools`。默认端点为 `http://127.0.0.1:41184/mcp`。

环境变量只对当前进程及其子进程生效。桌面 Agent 不一定继承终端中的变量；请通过宿主的环境配置传入，或使用本地配置文件。不要将 Token 直接写入命令参数、技能文件或笔记。

## 常用命令

```bash
python3 scripts/joplin_mcp.py --version
python3 scripts/joplin_mcp.py tools
python3 scripts/joplin_mcp.py describe read_note
python3 scripts/joplin_mcp.py call search_notes --args '{"query":"Redis","limit":10}'
python3 scripts/joplin_mcp.py call semantic_search_notes --args '{"query":"以前如何排查性能问题","relevance":"normal"}'
python3 scripts/joplin_mcp.py call read_note --args '{"id":"NOTE_ID","offset":0,"max_chars":8000}'
python3 scripts/joplin_mcp.py call update_note --args-file update.json
python3 scripts/joplin_mcp.py image RESOURCE_ID --resolution high
python3 scripts/joplin_mcp.py batch --file calls.json
```

参数来自服务器的工具定义；不确定时先运行 `describe TOOL`。`call` 不限制工具名称，未来新增工具也可调用。

长正文、代码或复杂引号优先使用 UTF-8 JSON 文件及 `--args-file`；`call --stdin` 读取一个 JSON 对象，`batch --stdin` 读取数组。CLI 的 JSON stdin/stdout 使用 UTF-8。PowerShell 的引号规则与 Bash 不同，建议使用参数文件。

## 配置

可复制 [config.example.json](config.example.json) 为 `config.local.json`，继续从环境变量读取 Token：

```bash
python3 scripts/joplin_mcp.py --config config.local.json doctor
```

配置支持 `url`、`token`、`timeout`、`artifacts_dir`。如需在本地配置中保存 Token，使用如下结构（占位值不是真实凭据），并限制文件访问权限：

```json
{
  "url": "http://127.0.0.1:41184/mcp",
  "token": "YOUR_WEB_CLIPPER_TOKEN",
  "timeout": 30
}
```

| 项目 | 优先级与默认值 |
|---|---|
| URL | `--url` → `JOPLIN_MCP_URL` → 配置 `url` → `JOPLIN_URL` → 回环默认地址 |
| Token | `JOPLIN_TOKEN` → 配置 `token`；兼容旧 URL 内的 Token，但不建议使用 |
| 超时 | `--timeout` → 配置 `timeout` → 30 秒；必须是有限正数，作用于 socket I/O，不是整批操作的总时限 |
| 图片目录 | `--artifacts-dir` → 配置 `artifacts_dir` → 系统临时目录 |

`JOPLIN_URL` 可使用 Web Clipper 基础地址，脚本会补上 `/mcp`。`--url`、`--config`、`--timeout`、`--artifacts-dir` 和 `--raw` 可放在子命令前或后。远程端点应使用 HTTPS 或可信隧道，详见 [SECURITY.md](SECURITY.md)。

## 输出与失败处理

常规工具结果：

```json
{"ok":true,"tool":"read_note","data":{"id":"NOTE_ID","body":"..."},"artifacts":[]}
```

失败返回 `ok: false` 和 `error`；退出码 `1` 表示运行失败，`2` 表示输入或配置错误，`0` 表示成功。帮助和版本命令输出普通文本。

- `outcome_unknown: true`：写请求可能已经执行，先核对真实笔记，不能直接重复创建、追加或修改。此标记覆盖连接故障、超时、HTTP 408/5xx、内部 RPC 错误、不明确的协议响应，以及写类工具的 `isError` 响应；工具报错不保证回滚。
- `server_succeeded: true`：工具成功响应已到达，但本地输出处理失败；只处理本地文件或输出问题，不重复写入。
- 新工具尚不能确定是否只读时，按可能写入处理。错误标记不意味着服务端提供事务或回滚。

所有请求都没有自动重试。成功读取会返回完整原文，不会脱敏笔记里自行保存的账号或其他凭据。异常响应中的字段类型、请求 ID 及 `result`/`error` 会严格校验；`--raw` 也不会绕过基本协议校验。

默认图片输出为本地路径，需由 Agent 的图片读取工具打开。显式 `image --output new.png` 不覆盖已有文件。`--raw` 返回原始工具响应，包括图片 Base64；不能与 `image --output` 同时使用。

## 批量调用

`calls.json` 示例：

```json
[
  {"tool":"list_notebooks","arguments":{},"label":"笔记本"},
  {"tool":"list_tags","arguments":{},"label":"标签"}
]
```

所有项目的结构与 JSON 可序列化性在发送任何调用前校验；具体工具参数的语义和权限仍由 Joplin 判断。一个进程只初始化一次并复用 HTTP 连接，按顺序执行。默认失败即停止；`--continue-on-error` 可继续后续项目。

批量没有事务或回滚，前面的成功写入不会撤销。结果包含成功、失败与跳过数量；本地输出失败仍计为失败，需同时检查该项的 `server_succeeded`。项目之间不支持结果变量引用，有依赖的操作分阶段执行。

## 安装为技能

将整个目录复制到宿主的技能搜索路径。在 Pi 中可放到 `~/.pi/agent/skills/rick-joplin`，也可以链接源码目录：

```bash
mkdir -p ~/.pi/agent/skills
ln -s /absolute/path/rick-joplin ~/.pi/agent/skills/rick-joplin
```

修改后执行 `/reload` 或开启新会话；可通过 `/skill:rick-joplin` 显式调用。若已有旧 Joplin 技能，先将旧目录移出扫描路径或在资源配置中排除，避免名称冲突。Windows 或不支持符号链接的环境可以复制整个目录。

技能说明见 [SKILL.md](SKILL.md)，工具参数见 [references/tools.md](references/tools.md)。客户端也可以独立运行，不依赖 Agent 宿主。

## 测试

模拟 HTTP 服务与 CLI 测试，不需要 Joplin 或 Token：

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

GitHub Actions 配置覆盖 Ubuntu 的 Python 3.9–3.14，以及 macOS/Windows 的 Python 3.14。CI 只运行模拟测试，不读取私人笔记，也不注入真实 Token。

可选真实服务验收：

```bash
python3 tests/live_acceptance.py --output test-results/acceptance.local.json
```

真实验收会创建名称以 `rick-joplin 验收` 开头的专用笔记本、测试笔记与标签。清理前会核对测试笔记的标题标记和所属笔记本；标签清理失败不会跳过测试笔记的删除尝试。成功清理的测试笔记移入回收站，MCP 没有笔记本删除工具，空测试笔记本会保留。连接中断或创建后未取得 ID 时，可能仍有测试项目需要手动核对；脚本会记录清理错误，不按猜测的 ID 删除笔记。建议使用测试知识库。

报告路径必须是尚不存在的新文件；脚本会在任何 Joplin 操作前创建报告，不覆盖既有文件。POSIX 系统上报告文件权限为 `600`。

图片验收需要已有的图片笔记；语义检索验收需要可用索引及相关笔记，空知识库不会全项通过。既有笔记和图片仅只读。缺失验收项会记录在报告中。`test-results/` 是本地生成数据，不随源码发布。

当前验证结果与历史真实验收范围见 [TEST-REPORT.md](TEST-REPORT.md)，独立复审结论见 [AUDIT.md](AUDIT.md)。贡献流程见 [CONTRIBUTING.md](CONTRIBUTING.md)，版本变化见 [CHANGELOG.md](CHANGELOG.md)。

## 能力边界

基线 Joplin 3.7.21 的 11 个 MCP 工具均可调用。客户端仅支持 Joplin 的 JSON HTTP 响应与协议 `2025-06-18`，不实现通用 SSE/stdio MCP SDK；遇到其他协议版本会明确报错。

当前 MCP 不提供笔记本删除、共享权限管理、附件上传、任意 PDF 下载、完整全库导出或搜索分页。搜索单次最多返回 100 条，不能把一次搜索当作全库遍历。语义检索依赖 Joplin 本地索引；笔记本过滤不构成访问隔离。客户端没有旧 REST 回退。

输入和响应 JSON 不接受重复对象字段、非有限数值或过深嵌套。嵌套深度上限为 100（顶层深度为 0），整数最多 4300 位。单次 HTTP 响应上限为 32 MiB；工具发现最多 100 页、10000 项，拒绝重复名称或异常分页游标。

Rick Joplin 是独立社区项目，与 Joplin 官方没有隶属关系。
