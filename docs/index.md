# Atlas

Atlas 是一个以自然语言为入口的分层式多 Agent 地理空间分析系统。它通过总控 Agent
理解目标、动态生成 TaskGraph，并调度数据、GIS、地形、遥感、新能源选址、地图报告
与审核 Agent 完成可追溯分析。

## 文档导航

- [系统架构](architecture.md)：组件职责、Agent 边界和数据流。
- [任务工作流](workflow.md)：基础 GIS、地形遥感和新能源选址执行过程。
- [设计决策](design.md)：动态规划、Tool 治理、Checkpoint 和 Trace 的设计原因。
- [快速开始](getting-started.md)：安装、配置、启动和 API 示例。
- [验证与边界](validation.md)：已验证能力、待完善能力和不应宣传的能力。

## 统一入口

正式通用入口为：

```text
POST /v1/geo/jobs
GET  /v1/geo/jobs/{job_id}
GET  /v1/geo/jobs/{job_id}/result
PATCH /v1/geo/jobs/{job_id}/clarification
```

新能源选址保留为系统中的专业复合能力，其兼容入口为
`POST /v1/site-selection/jobs`。

## 核心原则

1. LLM 负责目标理解和动态规划，确定性逻辑只在失败或显式禁用时降级接管。
2. 不同问题生成不同 TaskGraph，不把系统退化为固定 Workflow。
3. Agent 只能访问白名单工具和自己的工作空间。
4. 计划调用、实际调用、缓存复用和跳过状态分别记录。
5. 最终结果必须具有 Artifact、Evidence、来源和审核结论。
6. 不将已注册但未真实验证的工具描述为已完成能力。
