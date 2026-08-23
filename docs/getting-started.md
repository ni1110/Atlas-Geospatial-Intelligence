# Atlas 快速开始

## 1. 环境要求

- Python 3.11+
- Git
- 可选：智谱 API Key，用于真实 LLM 路由和规划

运行数据、缓存、模型和数据库默认位于仓库目录下，并被 `.gitignore` 排除。生产环境如
需强制 Windows D 盘策略，可设置 `ATLAS_REQUIRE_D_DRIVE=1`。

## 2. 克隆与安装

```bash
git clone https://github.com/ni1110/Atlas-Geospatial-Intelligence.git
cd Atlas-Geospatial-Intelligence
python -m venv .venv
```

Windows PowerShell：

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -e ".[zhipu]"
```

Linux / macOS：

```bash
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -e ".[zhipu]"
```

## 3. 模型配置

不要把 Key 写进 `.env.example`、源码、README、日志或请求体。

PowerShell：

```powershell
$secureKey = Read-Host "Zhipu API Key" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
try {
    $env:ZHIPUAI_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
$env:GEOAGENT_MODEL_PROVIDER = "zhipu"
$env:GEOAGENT_MODEL_ID = "glm-4.5-flash"
$env:GEOAGENT_MODEL_BASE_URL = "https://open.bigmodel.cn/api/paas/v4/"
```

Linux / macOS：

```bash
export ZHIPUAI_API_KEY="在当前终端安全设置"
export GEOAGENT_MODEL_PROVIDER="zhipu"
export GEOAGENT_MODEL_ID="glm-4.5-flash"
export GEOAGENT_MODEL_BASE_URL="https://open.bigmodel.cn/api/paas/v4/"
```

## 4. 启动服务

```bash
python -m geoagent.cli serve --host 127.0.0.1 --port 8000
```

访问：

- <http://127.0.0.1:8000/geoagent/demo>
- <http://127.0.0.1:8000/docs>
- <http://127.0.0.1:8000/health>

端口被占用时使用其他端口，例如 `--port 8010`。

## 5. 创建通用 Geo Job

```http
POST /v1/geo/jobs
Content-Type: application/json

{
  "user_query": "对甘肃小范围 AOI 的地形与遥感可用性进行分析",
  "region_or_aoi": {
    "bbox": [102.99885, 37.59704, 103.09885, 37.69704],
    "crs": "EPSG:4326"
  },
  "constraints": {},
  "use_live_llm": true,
  "resume": true
}
```

响应包含 `job_id`、状态地址和结果地址。使用以下接口读取状态：

```http
GET /v1/geo/jobs/{job_id}
GET /v1/geo/jobs/{job_id}/result
```

## 6. 补充缺失信息

当状态为 `NEEDS_CLARIFICATION`，状态响应会提供 `clarification.items`。提交用户回答：

```http
PATCH /v1/geo/jobs/{job_id}/clarification
Content-Type: application/json

{
  "answers": {
    "分析时间范围": "2025-04-01 至 2025-10-31",
    "遥感数据类型需求": "Sentinel-2 L2A"
  }
}
```

系统继续同一个 `job_id` 和 `goal_id`，并创建新版本 GoalContract。

## 7. 无密钥测试

```bash
pytest tests/test_public_smoke.py -q
python -m compileall -q geoagent/orchestration renewable_site_agent
```

这些检查不会发起真实 LLM 调用或大文件下载。
