# 文档解析流水线 HTTP 服务

把 `pipeline.py` 的完整流水线（MinerU 解析 → VLM 页内合并 → 跨页合并 → 导出 PDF/Markdown）包装为异步 REST 服务。上传文档后返回 `task_id`，轮询状态，完成后下载产物。

运行环境：conda env `newmineru`（已含 fastapi / uvicorn / mineru，零新增依赖）。

## 启动

```bash
bash start.sh            # 默认 0.0.0.0:8000，GPU 4
HOST=127.0.0.1 PORT=9000 GPU=0 bash start.sh   # 覆盖地址/端口/GPU
```

- VLM 配置自动从项目根目录 `.env` 读取（`VLM_BASE_URL` / `VLM_MODEL` / `VLM_API_KEY`）。
- **必须 `--workers 1`**：MinerU 模型为进程级共享，多 worker 会各加载一份模型且 `task_id` 不互通。并发上限 `MAX_CONCURRENCY=1`（解析串行，其余任务排队）。
- **GPU**：MinerU 默认使用 `device 0`（即 `CUDA_VISIBLE_DEVICES` 映射的第一张卡）。若默认卡被其他任务占用会 OOM，用 `GPU=N` 指定空闲卡（`start.sh` 默认 `GPU=4`）。

### 可调环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `HOST` / `PORT` | `0.0.0.0` / `8000` | 监听地址 / 端口 |
| `GPU` | `4` | MinerU 使用的 GPU（映射为 device 0） |
| `TASKS_ROOT` | `./tasks_root` | 任务数据根目录 |
| `MAX_CONCURRENCY` | `1` | 并发解析数（建议保持 1） |
| `MAX_QUEUE` | `10` | 排队任务数上限（超出返回 429） |
| `MAX_UPLOAD_MB` | `200` | 上传文件大小上限 |
| `TASK_TTL_HOURS` | `24` | 终态任务保留小时数（0 关闭清理） |
| `CLEANUP_INTERVAL` | `3600` | 清理扫描间隔（秒） |

## API

基础路径：`/api/v1`。交互式文档：`http://<host>:8000/docs`。

### POST `/api/v1/tasks` — 提交任务

支持两种提交方式（二选一）：
- **`file`**：multipart 文件上传
- **`file_path`**：服务端可访问的本地文件路径（`application/x-www-form-urlencoded` 表单字段），适用于外部程序与本服务同机/共享文件系统、直接传路径的场景

| 字段 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `file` | 文件 | 二选一 | multipart 上传 `.doc` / `.pdf`，≤200MB |
| `file_path` | str | 二选一 | 本地 PDF/.doc 绝对路径（`-d "file_path=/data/x.pdf"`） |
| `enable_vlm` | bool | `true` | VLM 页内合并 |
| `enable_cross_page` | bool | `true` | 跨页合并 |
| `export_pdf` | bool | `true` | 导出 PDF |
| `export_md` | bool | `true` | 导出 Markdown |
| `export_mode` | str | `confirmed` | `confirmed` / `detailed` |
| `log_level` | str | `INFO` | 日志级别 |
| `vlm_base_url` / `vlm_model` / `vlm_api_key` | str | 取 `.env` | 本次任务覆盖 VLM 配置 |

返回 `202`：

```json
{
  "task_id": "3f2a1c9e...",
  "status": "pending",
  "params": {"file_name": "report.pdf", "enable_vlm": true, "enable_cross_page": true,
             "export_pdf": true, "export_md": true, "export_mode": "confirmed", "log_level": "INFO"},
  "detail_url": "/api/v1/tasks/3f2a1c9e..."
}
```

### GET `/api/v1/tasks/{task_id}` — 查询状态

```json
{
  "task_id": "3f2a1c9e...",
  "status": "succeeded",
  "created_at": "2026-08-04T10:00:00Z",
  "started_at": "2026-08-04T10:00:01Z",
  "finished_at": "2026-08-04T10:03:12Z",
  "params": {"file_name": "report.pdf", "...": "..."},
  "result": {
    "file_stem": "report",
    "page_count": 12, "block_count": 1280,
    "figure_count": 6, "table_count": 3, "char_count": 45600,
    "artifacts": {
      "layout_json": {"filename": "report_layout.json", "path": "output/report_layout.json",
                      "url": "/files/3f2a1c9e.../output/report_layout.json",
                      "download_url": "/api/v1/tasks/3f2a1c9e.../download/layout_json",
                      "size_bytes": 2456711},
      "markdown": {"filename": "report_confirmed.md", "...": "..."},
      "pdf": {"filename": "report_confirmed.pdf", "...": "..."},
      "middle_json": {"filename": "report_middle.json", "...": "..."}
    }
  },
  "error": null,
  "log_url": "/api/v1/tasks/3f2a1c9e.../log"
}
```

`status`：`pending` → `running` → `succeeded` / `failed`。失败时 `error` 含异常信息。

### GET `/api/v1/tasks/{task_id}/result` — 按文件结构返回全套解析结果

任务成功后调用，递归遍历输出目录，**每个文件按其结构返回路径与内容**：
- JSON 文件（`*_layout.json` / `*_middle.json` / `content_list*.json`）→ `content` 为解析后的 JSON 对象
- Markdown / 文本 → `content` 为全文
- PDF / 图片 → 不内嵌，返回 `url`（经 `/files/...` 访问）+ `size_bytes`

```json
{
  "task_id": "3f2a...",
  "status": "succeeded",
  "summary": {"file_stem": "report", "page_count": 12, "block_count": 1280,
              "figure_count": 6, "table_count": 3, "char_count": 45600},
  "files": [
    {"path": "output/report_layout.json", "size_bytes": 4560,
     "url": "/files/3f2a.../output/report_layout.json", "content": [...]},
    {"path": "output/report_confirmed.md", "size_bytes": 883,
     "url": "/files/3f2a.../output/report_confirmed.md", "content": "![](images/...)"},
    {"path": "output/report/auto/images/xx.jpg", "size_bytes": 16456,
     "url": "/files/3f2a.../output/report/auto/images/xx.jpg"}
  ]
}
```

参数 `include_content=false` 时只返回文件清单（path/url/size，不内嵌内容），适合大文档仅需定位文件。

### GET `/api/v1/tasks/{task_id}/download/{artifact}` — 下载产物

`artifact` ∈ `layout_json` / `markdown` / `pdf` / `middle_json`。未导出或未完成返回相应错误码。

### 其他

- `GET /api/v1/tasks?limit=50` — 任务列表
- `GET /api/v1/tasks/{task_id}/log?tail=2000` — 任务日志（纯文本）
- `DELETE /api/v1/tasks/{task_id}` — 排队任务取消；运行中任务返回 409；终态任务删除
- `GET /healthz` — 健康检查
- `GET /metrics` — Prometheus 指标（`ENABLE_METRICS=false` 关闭）

## 外部访问

服务监听 `0.0.0.0:8000`，同一局域网内的其他机器通过服务器内网 IP 访问：

```bash
BASE=http://10.154.24.43:8000
curl $BASE/healthz
```

（客户端 `client.py` 默认就连接 `http://10.154.24.43:8000`，无需再指定 `--url`。）

## 一键客户端（client.py）

```bash
python client.py /path/to/doc.pdf --output /save/dir        # 上传 + 按文件结构保存全套结果
python client.py /path/to/doc.pdf --name renamed.pdf        # 上传时指定文件名
python client.py /path/to/doc.pdf --by-path                 # 服务端可见路径，直接传路径
python client.py /path/to/doc.pdf --no-vlm --no-cross-page  # 关 VLM 快速验证
python client.py /path/to/doc.pdf --url http://127.0.0.1:8000   # 覆盖默认地址
```

上传时显式携带文件名（`--name`，默认取源文件 basename），解析完成后按文件结构把全套结果（layout/middle JSON、Markdown、PDF、图片）保存到 `--output` 目录。

## 静态文件

任务目录通过 `GET /files/{task_id}/...` 直接访问，供 Markdown 图片等定位：
- 原始上传：`/files/{task_id}/input/<file>`
- 解析产物：`/files/{task_id}/output/...`

**注意**：Markdown 中的图片引用是 `images/xxx.jpg` 相对路径，实际图片位于 `/files/{task_id}/output/<stem>/auto/images/xxx.jpg`，客户端需自行拼接（该映射在 v1 不做自动改写）。

## 完整调用示例

```bash
BASE=http://127.0.0.1:8000

# 1. 健康检查
curl -s $BASE/healthz

# 2. 提交任务（默认完整流水线：VLM 页内 + 跨页，耗时分钟级）
TASK=$(curl -s -X POST $BASE/api/v1/tasks \
  -F "file=@/path/to/document.pdf" | jq -r .task_id)
echo $TASK

# 3. 轮询至 succeeded
for i in $(seq 1 60); do
  ST=$(curl -s $BASE/api/v1/tasks/$TASK | jq -r .status)
  echo "$i: $ST"
  [ "$ST" = "succeeded" ] || [ "$ST" = "failed" ] && break
  sleep 10
done

# 4. 下载产物
curl -s -o result.md   $BASE/api/v1/tasks/$TASK/download/markdown
curl -s -o result.pdf  $BASE/api/v1/tasks/$TASK/download/pdf
curl -s -o layout.json $BASE/api/v1/tasks/$TASK/download/layout_json

# 5. 排障
curl -s $BASE/api/v1/tasks/$TASK/log | tail -50
```

## 快速验证链路（关 VLM）

```bash
TASK=$(curl -s -X POST $BASE/api/v1/tasks \
  -F "file=@/path/to/sample.pdf" \
  -F "enable_vlm=false" -F "enable_cross_page=false" | jq -r .task_id)
curl -s $BASE/api/v1/tasks/$TASK | jq .status
```

## 外部程序通过本地路径提交（无文件上传）

```bash
# 直接传服务端可见的本地 PDF 路径（urlencoded 表单）
TASK=$(curl -s -X POST $BASE/api/v1/tasks \
  -d "file_path=/data/documents/report.pdf&enable_vlm=false&enable_cross_page=false" | jq -r .task_id)

# 轮询成功后，按文件结构取全套解析结果
curl -s "$BASE/api/v1/tasks/$TASK/result" | jq .
```

Python 调用示例：

```python
import requests

BASE = "http://127.0.0.1:8000"

# 提交（传本地路径）
r = requests.post(f"{BASE}/api/v1/tasks",
                  data={"file_path": "/data/documents/report.pdf",
                        "enable_vlm": "true", "enable_cross_page": "true"})
task_id = r.json()["task_id"]

# 轮询
while True:
    st = requests.get(f"{BASE}/api/v1/tasks/{task_id}").json()["status"]
    if st in ("succeeded", "failed"):
        break
    time.sleep(5)

# 按文件结构取全套结果
result = requests.get(f"{BASE}/api/v1/tasks/{task_id}/result").json()
for f in result["files"]:
    print(f["path"], f.get("content") or f["url"])
```

## 已知限制（v1）

- **无鉴权**：`/files` 会暴露上传源文件与产物，仅适用于内网/可信环境；对外部署需加 token 中间件。
- **不支持 `.docx`**：仅 `.doc` / `.pdf`。如需支持，改 `parse_doc.py` 把 `.docx` 也交给 LibreOffice 转换并加入白名单。
- 任务数据默认保留 24h 后自动清理（`TASK_TTL_HOURS` 调整，`0` 关闭）。

## 任务目录结构

```
tasks_root/
  {task_id}/
    params.json        # 任务参数（不含 VLM api_key）
    input/             # 上传源文件
    output/            # run_pipeline 输出目录（layout/md/pdf/confirmed_images/...）
    logs/task.log      # 本任务 print / std-logging / loguru 合并日志
```
