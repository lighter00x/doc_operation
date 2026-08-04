"""
文档解析流水线 HTTP 服务

启动: bash start.sh  (默认 0.0.0.0:8000)

主要端点:
  POST   /api/v1/tasks                 上传文档，异步创建解析任务
  GET    /api/v1/tasks/{task_id}       查询任务状态
  GET    /api/v1/tasks                 任务列表
  GET    /api/v1/tasks/{task_id}/download/{artifact}  下载产物
  GET    /api/v1/tasks/{task_id}/log   任务日志
  DELETE /api/v1/tasks/{task_id}       取消/删除任务
  GET    /healthz                      健康检查
  /files/{task_id}/...                 任务目录静态访问（图片等）
"""
import json
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ⚠️ import 顺序敏感: config 先 load_dotenv，再导入 service (其内部 import pipeline)
from config import settings
from service import TaskManager, QueueFullError

ALLOWED_EXTENSIONS = {".doc", ".pdf"}
ARTIFACT_KEYS = ("layout_json", "markdown", "pdf", "middle_json")
ARTIFACT_MEDIA = {
    "layout_json": "application/json; charset=utf-8",
    "markdown": "text/markdown; charset=utf-8",
    "pdf": "application/pdf",
    "middle_json": "application/json; charset=utf-8",
}


# ────────────────────────────────────────────────────────────────
# pydantic 响应模型
# ────────────────────────────────────────────────────────────────

class ArtifactEntry(BaseModel):
    filename: str
    path: str
    url: str
    download_url: str
    size_bytes: int


class ResultSummary(BaseModel):
    file_stem: str
    page_count: int
    block_count: int
    figure_count: int
    table_count: int
    char_count: int
    artifacts: dict[str, ArtifactEntry] = {}


class TaskParams(BaseModel):
    file_name: str
    enable_vlm: bool
    enable_cross_page: bool
    export_pdf: bool
    export_md: bool
    export_mode: str
    log_level: str


class TaskCreated(BaseModel):
    task_id: str
    status: str
    params: TaskParams
    detail_url: str


class TaskStatus(BaseModel):
    task_id: str
    status: str
    created_at: datetime
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    params: TaskParams
    result: Optional[ResultSummary] = None
    error: Optional[str] = None
    log_url: Optional[str] = None


class TaskList(BaseModel):
    total: int
    active: int
    queued: int
    tasks: list[dict]


class Health(BaseModel):
    status: str
    active_tasks: int
    queued_tasks: int
    max_concurrency: int
    uptime_seconds: int
    version: str


# ────────────────────────────────────────────────────────────────
# 应用与任务管理器
# ────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    task_manager.shutdown()


app = FastAPI(
    title="文档解析流水线服务",
    description="MinerU 解析 → VLM 页内合并 → 跨页合并 → 导出 PDF/Markdown（异步任务）",
    version=settings.version,
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

task_manager = TaskManager(
    tasks_root=settings.tasks_root,
    vlm=settings.vlm,
    max_concurrency=settings.max_concurrency,
    max_queue=settings.max_queue,
    ttl_hours=settings.task_ttl_hours,
    cleanup_interval_seconds=settings.cleanup_interval_seconds,
)

# 静态访问任务目录（markdown 图片、原始产物）
app.mount("/files", StaticFiles(directory=str(settings.tasks_root)), name="files")

if settings.enable_metrics:
    from prometheus_fastapi_instrumentator import Instrumentator

    Instrumentator().instrument(app).expose(app)


# ────────────────────────────────────────────────────────────────
# 工具
# ────────────────────────────────────────────────────────────────

class UploadTooLarge(Exception):
    pass


def _magic_ok(path: Path, ext: str) -> bool:
    """魔数校验: PDF 头 %PDF-；.doc (OLE2) 头 D0CF11E0A1B11AE1。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except OSError:
        return False
    if ext == ".pdf":
        return head[:5] == b"%PDF-"
    if ext == ".doc":
        return head[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    return False


async def _stream_to_disk(file: UploadFile, dest: Path, max_bytes: int) -> int:
    size = 0
    with open(dest, "wb") as f:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise UploadTooLarge()
            f.write(chunk)
    return size


def _task_status(r) -> dict:
    params = {"file_name": r.file_name, **r.params}
    params.pop("vlm_override", None)
    return {
        "task_id": r.task_id,
        "status": r.status,
        "created_at": r.created_at,
        "started_at": r.started_at,
        "finished_at": r.finished_at,
        "params": params,
        "result": r.result,
        "error": r.error,
        "log_url": f"/api/v1/tasks/{r.task_id}/log",
    }


# ────────────────────────────────────────────────────────────────
# 路由
# ────────────────────────────────────────────────────────────────

@app.post("/api/v1/tasks", status_code=202, response_model=TaskCreated, tags=["tasks"])
async def create_task(
    file: Optional[UploadFile] = File(None),
    file_path: Optional[str] = Form(None),
    enable_vlm: bool = Form(True),
    enable_cross_page: bool = Form(True),
    export_pdf: bool = Form(True),
    export_md: bool = Form(True),
    export_mode: Literal["confirmed", "detailed"] = Form("confirmed"),
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = Form("INFO"),
    vlm_base_url: Optional[str] = Form(None),
    vlm_model: Optional[str] = Form(None),
    vlm_api_key: Optional[str] = Form(None),
):
    """创建解析任务。

    两种提交方式（二选一）:
      A. file_path: 服务端可直接访问的本地 PDF/.doc 路径（urlencoded Form 字段）
      B. file:       multipart 文件上传
    """
    if file is not None and file_path:
        raise HTTPException(status_code=400, detail="file 与 file_path 只能二选一")
    if file is None and not file_path:
        raise HTTPException(status_code=400, detail="必须提供 file（上传）或 file_path（本地路径）")

    params = {
        "enable_vlm": enable_vlm,
        "enable_cross_page": enable_cross_page,
        "export_pdf": export_pdf,
        "export_md": export_md,
        "export_mode": export_mode,
        "log_level": log_level,
    }
    if vlm_base_url or vlm_model or vlm_api_key:
        params["vlm_override"] = {
            "base_url": vlm_base_url,
            "model": vlm_model,
            "api_key": vlm_api_key,
        }

    if file_path:
        # ── 模式 A: 本地路径 ──────────────────────────────
        fp = Path(file_path)
        filename = os.path.basename(str(fp))
        ext = os.path.splitext(filename)[1].lower()
        if not filename or ext not in ALLOWED_EXTENSIONS:
            raise HTTPException(status_code=400, detail=f"仅支持 .doc / .pdf 文件，收到: {ext or '(无扩展名)'}")
        if not fp.is_file():
            raise HTTPException(status_code=400, detail=f"文件不存在或不可读: {file_path}")
        if fp.stat().st_size > settings.max_upload_bytes:
            raise HTTPException(status_code=413, detail=f"文件超过大小限制 {settings.max_upload_bytes // 1024 // 1024}MB")

        try:
            record = task_manager.reserve(filename, params)
        except QueueFullError as e:
            raise HTTPException(status_code=429, detail=str(e))

        record.input_path = fp  # 直接引用源路径，不复制
        if not _magic_ok(fp, ext):
            task_manager.delete(record.task_id, force=True)
            raise HTTPException(status_code=400, detail="文件内容与扩展名不符（魔数校验失败）")

        task_manager.enqueue(record)

        resp_params = {"file_name": filename, **params}
        resp_params.pop("vlm_override", None)
        return TaskCreated(
            task_id=record.task_id,
            status="pending",
            params=resp_params,
            detail_url=f"/api/v1/tasks/{record.task_id}",
        )

    # ── 模式 B: multipart 文件上传 ───────────────────────
    filename = os.path.basename((file.filename or "").replace("\\", "/"))
    ext = os.path.splitext(filename)[1].lower()
    if not filename or ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(status_code=400, detail=f"仅支持 .doc / .pdf 文件，收到: {ext or '(无扩展名)'}")

    try:
        record = task_manager.reserve(filename, params)
    except QueueFullError as e:
        raise HTTPException(status_code=429, detail=str(e))

    try:
        await _stream_to_disk(file, record.input_path, settings.max_upload_bytes)
    except UploadTooLarge:
        task_manager.delete(record.task_id, force=True)
        raise HTTPException(
            status_code=413,
            detail=f"文件超过大小限制 {settings.max_upload_bytes // 1024 // 1024}MB",
        )

    if not _magic_ok(record.input_path, ext):
        task_manager.delete(record.task_id, force=True)
        raise HTTPException(status_code=400, detail="文件内容与扩展名不符（魔数校验失败）")

    task_manager.enqueue(record)

    resp_params = {"file_name": filename, **params}
    resp_params.pop("vlm_override", None)
    return TaskCreated(
        task_id=record.task_id,
        status="pending",
        params=resp_params,
        detail_url=f"/api/v1/tasks/{record.task_id}",
    )


@app.get("/api/v1/tasks/{task_id}", response_model=TaskStatus, tags=["tasks"])
def get_task(task_id: str):
    r = task_manager.get(task_id)
    if not r:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    return _task_status(r)


@app.get("/api/v1/tasks", response_model=TaskList, tags=["tasks"])
def list_tasks(limit: int = 50):
    limit = max(1, min(limit, 500))
    s = task_manager.stats()
    return {
        "total": s["total"],
        "active": s["active"],
        "queued": s["queued"],
        "tasks": task_manager.list_tasks(limit),
    }


# 文本类型文件直接返回内容；二进制（pdf/图片）返回 URL。阈值以上大文件只给 URL。
_TEXT_EXTS = {".json", ".md", ".txt", ".log"}
_MAX_TEXT_CONTENT_BYTES = 50 * 1024 * 1024


def _collect_output_files(record, include_content: bool = True) -> list[dict]:
    """递归遍历任务输出目录，按文件结构返回每个文件（path + 内容或 URL）。"""
    out_dir = record.task_dir / "output"
    if not out_dir.exists():
        return []
    files = []
    for p in sorted(out_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = str(p.relative_to(record.task_dir))
        entry = {
            "path": rel,
            "size_bytes": p.stat().st_size,
            "url": f"/files/{record.task_id}/{rel.replace(os.sep, '/')}",
        }
        ext = p.suffix.lower()
        if (
            include_content
            and ext in _TEXT_EXTS
            and p.stat().st_size <= _MAX_TEXT_CONTENT_BYTES
        ):
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                raw = None
            if raw is not None:
                if ext == ".json":
                    try:
                        entry["content"] = json.loads(raw)
                    except Exception:
                        entry["content"] = raw
                else:
                    entry["content"] = raw
        files.append(entry)
    return files


@app.get("/api/v1/tasks/{task_id}/result", tags=["tasks"])
def get_task_result(task_id: str, include_content: bool = True):
    """按文件结构返回全套解析结果。

    include_content=true（默认）: 文本/JSON 文件内嵌内容；二进制（pdf/图片）给 URL。
    include_content=false: 只返回文件清单（path/url/size），适合大文档或仅需定位。
    """
    r = task_manager.get(task_id)
    if not r:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    if r.status != "succeeded" or not r.result:
        raise HTTPException(status_code=409, detail=f"任务未完成（当前状态 {r.status}）")

    summary = {k: v for k, v in r.result.items() if k != "artifacts"}
    files = _collect_output_files(r, include_content=include_content)
    return {
        "task_id": r.task_id,
        "status": r.status,
        "summary": summary,
        "files": files,
    }


@app.get("/api/v1/tasks/{task_id}/download/{artifact}", tags=["tasks"])
def download_artifact(task_id: str, artifact: str):
    if artifact not in ARTIFACT_KEYS:
        raise HTTPException(status_code=400, detail=f"未知产物类型: {artifact}（可选 {', '.join(ARTIFACT_KEYS)}）")
    r = task_manager.get(task_id)
    if not r:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    if r.status != "succeeded" or not r.result:
        raise HTTPException(status_code=409, detail=f"任务未完成（当前状态 {r.status}）")
    entry = (r.result.get("artifacts") or {}).get(artifact)
    if not entry:
        raise HTTPException(status_code=404, detail=f"产物 {artifact} 不存在（可能未导出）")
    path = r.task_dir / entry["path"]
    if not path.exists():
        raise HTTPException(status_code=404, detail="产物文件不存在")
    return FileResponse(
        path,
        media_type=ARTIFACT_MEDIA.get(artifact, "application/octet-stream"),
        filename=entry["filename"],
    )


@app.get("/api/v1/tasks/{task_id}/log", tags=["tasks"])
def get_log(task_id: str, tail: int = 2000):
    r = task_manager.get(task_id)
    if not r:
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    log_path = r.task_dir / "logs" / "task.log"
    if not log_path.exists():
        return PlainTextResponse("")
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return PlainTextResponse("")
    tail = max(1, min(tail, 100000))
    return PlainTextResponse("\n".join(lines[-tail:]))


@app.delete("/api/v1/tasks/{task_id}", tags=["tasks"])
def delete_task(task_id: str):
    state = task_manager.cancel(task_id)
    if state == "not_found":
        raise HTTPException(status_code=404, detail="任务不存在或已被清理")
    if state == "running":
        raise HTTPException(status_code=409, detail="任务正在执行，无法取消")
    if state == "cancelled":
        task_manager.delete(task_id)
        return {"status": "cancelled", "deleted": True}
    # terminal (succeeded/failed) → 直接删除
    deleted = task_manager.delete(task_id)
    return {"status": "deleted", "deleted": deleted}


@app.get("/healthz", response_model=Health, tags=["system"])
def healthz():
    s = task_manager.stats()
    uptime = int((datetime.now(timezone.utc) - task_manager._started_at).total_seconds())
    return {
        "status": "ok",
        "active_tasks": s["active"],
        "queued_tasks": s["queued"],
        "max_concurrency": settings.max_concurrency,
        "uptime_seconds": uptime,
        "version": settings.version,
    }
