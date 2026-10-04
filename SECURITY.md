# Security

## 报告问题

发布到 GitHub 后，优先使用仓库 Security 页的私密漏洞报告功能（需要仓库维护者启用）。如果尚未提供私密渠道，可在公开 Issue 中仅说明问题类型并请求联系方式；不要附上真实 Token、笔记内容或可利用的敏感细节。

报告中注明客户端版本、Python/Joplin 版本，并尽可能使用虚构 Token、模拟 HTTP 服务和测试笔记提供复现。修复会优先进入最新补丁版本，历史版本不单独维护。

## 凭据与数据

- 使用 `JOPLIN_TOKEN` 环境变量或本地配置文件传入 Web Clipper Token。不要把 Token 放进命令行 URL、Git、Issue 或验收报告。
- `config.example.json` 不含 Token。存放真实凭据的配置建议命名为 `config.local.json`；在 POSIX 系统上将权限设为 `600`，Windows 使用用户访问控制。
- 默认服务位于 `127.0.0.1`。远程连接应使用 HTTPS 或可信的 SSH 隧道；普通远程 HTTP 会明文传输 Token 和笔记。
- 错误输出会脱敏已知配置 Token；成功的笔记读取结果会保留原文，可能包含笔记里自行保存的其他凭据。输出、日志和图片文件应按个人数据管理。
- POSIX 系统上，新生成的图片文件权限为 `600`，自动临时目录为 `700`；Windows 的文件访问权限由系统 ACL 决定。

## 操作边界

工具开关与实际访问权限由 Joplin 管理。`notebook:` 搜索条件不是访问隔离，客户端也没有按笔记本限制写入的沙箱。

客户端不自动重试请求。遇到 `outcome_unknown: true`，先核对实际数据；遇到 `server_succeeded: true`，只处理本地输出故障。批量调用不提供事务或回滚，前面已完成的操作可能保留。
