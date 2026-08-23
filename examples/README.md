# Atlas API 示例

本目录提供不依赖额外前端工程的最小调用示例。启动服务后，可直接使用
[`requests.http`](requests.http) 在支持 HTTP Client 的编辑器中逐条执行。

## 启动服务

```bash
python -m geoagent.cli serve --port 8000
```

浏览器入口：

- Atlas 主 Demo：<http://127.0.0.1:8000/geoagent/demo>
- 新能源选址页面：<http://127.0.0.1:8000/>
- OpenAPI：<http://127.0.0.1:8000/docs>

## 调用顺序

1. `POST /v1/geo/jobs` 创建任务；
2. 从响应中复制 `job_id`；
3. `GET /v1/geo/jobs/{job_id}` 查看状态；
4. 成功后调用 `GET /v1/geo/jobs/{job_id}/result` 获取正式结果；
5. 若状态为 `NEEDS_CLARIFICATION`，按响应给出的澄清接口补充信息。

真实大模型主路径需要在本机设置 `ZHIPUAI_API_KEY`。不要把密钥写入示例、
配置文件、命令历史截图或 Issue。
