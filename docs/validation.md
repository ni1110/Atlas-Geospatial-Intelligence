# Atlas 验证结果与能力边界

## 1. 验收原则

Atlas 将验收证据分为四类：

1. **真实联网**：实际请求外部 Provider，并记录来源和响应范围。
2. **本地真实执行**：使用受治理输入执行真实算法。
3. **缓存复用**：复用已通过校验的 Artifact，明确标记 Cache。
4. **跳过**：未执行的能力明确标记 Skipped，不纳入成功宣称。

## 2. 已验证路径

| 路径 | 证据 |
| --- | --- |
| 简单 Buffer | GIS Agent 执行真实本地几何运算 |
| Buffer + Intersection | 两节点依赖链、两个输入 checksum、正式 GeoJSON Artifact |
| DEM 地形分析 | AOI 数据、DEM 缓存、高程统计与坡度结果 |
| Sentinel metadata | 真实 STAC 目录查询、item/asset metadata、无完整场景下载 |
| 新能源复合链 | 数据、地形、GIS、评分与排序 Agent；复用已验收缓存 |
| 多领域开放任务 | 不同 TaskGraph；GIS、地形、遥感、报告按目标组合 |
| LLM 路由与规划 | 智谱 GLM 正常路径；确定性 fallback 故障注入 |
| 安全攻击 | 越权工具、路径穿越、跨 Workspace 写入和正式目录写入被阻断 |
| 崩溃恢复 | Checkpoint 恢复、成功节点零重复执行、Artifact checksum 复用 |

## 3. 不应扩大宣传的能力

- Aspect 路径已定义，但当前结果不足以宣称完整坡向产品。
- Spatial Join、Nearest Distance 和 Zonal Statistics 已进入治理表面，尚不能视为独立
  算法验收完成。
- Raster Window、NDVI、RGB 和可选 VLM 在冻结验收中为 Skipped。
- QGIS GUI-host、所有历史外部 Provider 工具和公网部署不在当前冻结验收范围。

## 4. 最近验证记录

- 编排层 targeted regression：`52 passed, 1 skipped`。
- 冻结前完整回归记录：`388 passed, 2 skipped, 6 deselected`。
- 正式智谱模型：`glm-4.5-flash`，Key 仅来自环境变量。
- Sentinel 策略：metadata-only，无完整 scene bytes。

上述数值是特定验收环境的记录，不代表克隆仓库后无需准备本地数据即可重跑所有 Live
测试。公开 CI 只执行不依赖密钥和大文件缓存的 smoke 测试。
