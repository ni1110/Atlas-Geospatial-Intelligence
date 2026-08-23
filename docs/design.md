# Atlas 关键设计决策

## 1. 为什么需要动态任务规划

GIS 请求的差异不仅是参数不同，执行结构也不同。Buffer 只需要一个 GIS 节点；地形与
遥感可以并行；新能源选址需要数据、地形、GIS、评分、审核和报告的组合。如果使用固定
Workflow，简单任务会调用无关 Agent，复杂任务又难以局部调整。

Atlas 使用 LLM 生成请求专属 TaskGraph，并在进入执行前进行确定性治理：

- 节点必须属于已注册 Agent；
- 工具必须位于该 Agent 白名单；
- 依赖必须构成有向无环图；
- 工具调用和节点数量不得超过预算；
- 用户明确要求的领域不能被 LLM 遗漏。

这使 LLM 提供语义灵活性，同时不拥有绕过系统边界的权力。

## 2. 为什么需要 Tool 治理

地理工具可能读取外部网络、处理大文件或修改地图/工程状态。把完整工具集直接暴露给
每个 Agent 会产生越权、误调用和不可追踪的问题。

Atlas 使用三层限制：

1. Planner 只能从角色工具表选择工具；
2. Harness 在调用前基于 Capability ACL 再次授权；
3. Workspace 和 ArtifactCommitGate 限制文件落点及最终结果提交。

系统还区分 `planned_tools` 和 `actual_tool_calls`。工具名出现在计划中不代表它被执行；
实际记录只有在授权并尝试操作后生成。

## 3. 为什么需要 Checkpoint

DEM、遥感和多源空间任务可能持续较长时间。进程中断后从头执行会重复联网、重复计算，
甚至覆盖正确结果。

Atlas 将 Job、TaskGraph、Attempt、Artifact、Evidence 和 Review 持久化到 SQLite。
恢复时根据任务签名和 Artifact checksum 判断是否可复用：

- checksum 有效且 GoalContract 未变化：复用；
- 节点失败但依赖有效：局部重试或重规划；
- 输入、目标或依赖发生变化：重新执行相关节点。

Checkpoint 因而既是恢复机制，也是幂等和审计机制。

## 4. 为什么需要 Trace 与 Evidence

只保存最终地图无法回答“用了什么数据、哪个 Agent 做了什么、结果是否来自缓存”。
Atlas 为每次真实 Tool 调用记录 Agent、Task、Provider、耗时和执行类型，并将结果关联到
Artifact 与 Evidence。

执行类型明确区分：

- `LIVE_NETWORK_EXECUTED`：真实网络 Provider 调用；
- `CACHE_REUSED`：复用已校验缓存；
- `OFFLINE_LOCAL_DATA_EXECUTED`：本地真实计算；
- `SKIPPED`：能力未执行，并保留原因。

这种设计可以防止把缓存描述为实时数据，也能防止把跳过的遥感处理描述为已经生成。

## 5. 为什么采用 LLM-first + Deterministic fallback

开放式目标理解和任务拆分适合由大模型完成，但系统不能因为模型不可用而完全失效。
Atlas 正常路径调用智谱 GLM；初始化失败、超时、Provider 错误或结构治理失败时才切换到
确定性路由和规划。

两条路径分别记录 `route_mode`、`plan_mode`、调用次数、Provider、Model 和错误原因。
fallback 不会被计入 LLM 指标，也不会硬编码显示成 LLM 成功。

## 6. 为什么 Reviewer 仍不足以单独判断完成

TaskGraph 执行成功只表示引擎没有崩溃，不等于用户目标完整完成。如果用户要求地形和
遥感，但只有遥感 Evidence，任务不能标记为 PASS。

因此 Atlas 在 Reviewer 后、正式提交前增加确定性 Goal Coverage：每个 required role
必须具有成功结果以及对应 Evidence 或 Artifact。缺失任何领域证据都会阻止提交。

## 7. 为什么数据按 AOI 读取

完整 Sentinel 场景和全国尺度栅格会增加网络、存储和计算成本。Atlas 以 AOI、时间范围、
云量阈值和像元预算约束数据读取，并优先复用校验缓存。Provider 不可用时按配置顺序
fallback，同时保留来源和降级原因。

## 8. 设计取舍与限制

- 当前不是完整 GIS 桌面软件替代品；QGIS GUI-host 执行未纳入冻结验收。
- 工具注册或规划成功不等价于算法已经独立真实验收。
- Sentinel 当前公开验收以 STAC metadata 为边界，不声称完整 scene、NDVI 或 RGB。
- 新能源正式路径可能复用已验收缓存，结果会明确标记为 Cache。
- 公开 CI 不使用真实 API Key，也不执行付费或大文件 Provider 测试。
