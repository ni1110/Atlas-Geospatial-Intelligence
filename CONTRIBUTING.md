# 参与 Atlas 开发

感谢你关注 Atlas。提交改动前，请先阅读
[`docs/design.md`](docs/design.md) 中的设计约束与
[`docs/validation.md`](docs/validation.md) 中的能力边界。

## 本地开发

```bash
python -m venv .venv
.venv/Scripts/activate
python -m pip install -e ".[dev,zhipu]"
python -m pytest tests/test_public_smoke.py -q
```

Linux/macOS 请使用 `source .venv/bin/activate`。公开 smoke 测试不需要 API Key，
也不会调用外部数据源。

## 提交原则

- 不提交密钥、用户原始数据、模型权重、缓存或运行时 Artifact；
- 不用 mock 结果冒充 Live Provider 验证；
- 新能力应包含测试，并标明 Offline、Live 或 Cached Evidence；
- 修改 Agent 工具权限、Workspace 或 ArtifactCommitGate 时必须补充安全测试；
- 保持提交范围聚焦，并在 PR 中写明验证命令与结果。

提交代码即表示你同意按项目的 MIT License 发布贡献。
