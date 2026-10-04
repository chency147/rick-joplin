# Contributing

欢迎提交问题和改进。运行时与测试都仅使用 Python 标准库，不需要安装第三方依赖。

## 本地开发

在项目根目录运行：

```bash
python3 scripts/joplin_mcp.py --help
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Windows 可以将 `python3` 替换为 `python`。单元测试使用回环地址上的模拟 HTTP 服务，不连接实际 Joplin，也不需要 Token。

修改协议、错误处理或写入行为时，为具体的失败场景增加回归测试。保持 Python 3.9 兼容；不要增加自动写入重试、REST 权限回退或隐藏的运行时依赖。

## 提交问题

提供客户端版本、Python 与操作系统版本、Joplin 版本、复现命令及预期行为。使用占位 ID 和虚构笔记内容；去除 Token、账号、笔记正文和私人路径。安全相关问题见 [SECURITY.md](SECURITY.md)。

## 真实服务验收

真实验收会创建专用笔记本、笔记和标签，见 [README.md](README.md#测试)。请使用专用测试配置文件或测试知识库，并准备图片笔记和可用的语义索引。不要把真实验收加入公共 CI；不要提交生成的 `test-results/` 报告。

## 提交变更

说明触发问题、修复后的行为和验证结果。更新受影响的使用文档与 [CHANGELOG.md](CHANGELOG.md)。贡献代码按项目 [MIT 许可证](LICENSE)分发。
