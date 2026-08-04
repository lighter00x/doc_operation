"""
后台任务管理：TaskManager

职责:
  - 任务注册表 (进程内 dict + 锁)
  - ThreadPoolExecutor(max_workers=1) 作为串行队列 (MinerU 模型进程级共享)
  - 每任务日志隔离: print / std-logging / loguru 全部写入 {task_dir}/logs/task.log
  - 对 pipeline._configure_logging 打猴子补丁，规避 basicConfig(force=True) 在常驻
    服务中反复清空 root logger 的问题
  - layout 巨 dict 只算摘要，绝不进任务状态/响应
  - 终态任务 TTL 清理
"""
import contextlib
import json
import logging
import os
import shutil
import threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loguru import logger

# ⚠️ 顺序敏感: config 在顶部 load_dotenv，必须先于 import pipeline
from config import settings, VLMConfig

import pipeline  # noqa: E402  此时 .env 已加载，VLM_* 已就位


# ────────────────────────────────────────────────────────────────
# 日志基础设施
# ────────────────────────────────────────────────────────────────

# 线程局部: worker 当前任务的共享文件对象 (print/std-logging/loguru 共用)
_shared_log_file = threading.local()


def _set_log_path(path: Optional[Path]):
    _shared_log_file.path = path


def _get_log_path() -> Optional[Path]:
    return getattr(_shared_log_file, "path", None)


class _SharedFileHandler(logging.Handler):
    """把 std-logging 记录写到与 print/loguru 共享的同一个文件对象，
    避免多个句柄各自维护文件 offset 造成互相覆盖。"""

    def __init__(self, file_obj):
        super().__init__()
        self._file = file_obj

    def emit(self, record):
        try:
            self._file.write(self.format(record) + "\n")
            self._file.flush()
        except Exception:
            self.handleError(record)


def _remove_doc_svc_handlers(root: logging.Logger):
    for h in list(root.handlers):
        if getattr(h, "_doc_svc", False):
            root.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def _safe_configure_logging(level_name: str = "INFO", log_file: Optional[str] = None):
    """替换 pipeline._configure_logging。

    原实现 (merge_optimized._configure_logging) 调用 logging.basicConfig(force=True)，
    每次任务会清空 root logger 全部 handler 并重建 —— 在常驻服务中会冲掉其他日志。
    这里只维护一个标记 handler，把 std-logging 导向当前任务的共享文件对象。
    """
    root = logging.getLogger()
    root.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    _remove_doc_svc_handlers(root)

    shared = getattr(_shared_log_file, "file", None)
    if shared is not None:
        fh = _SharedFileHandler(shared)
    elif log_file:
        fh = logging.FileHandler(log_file, encoding="utf-8")
    else:
        return  # 无日志上下文（如测试环境），跳过
    fh.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s"))
    fh._doc_svc = True
    root.addHandler(fh)


# 劫持 run_pipeline 内部的裸名 _configure_logging(...) 调用
pipeline._configure_logging = _safe_configure_logging

# uvicorn 日志不向 root 传播，避免任务期间被写进任务日志文件
for _lg in ("uvicorn", "uvicorn.error", "uvicorn.access"):
    logging.getLogger(_lg).propagate = False


@contextlib.contextmanager
def _task_logging(log_path: Path):
    """合并 print / std-logging / loguru 输出到同一文件对象。

    loguru 默认 sink 在 import 时捕获了原始 stderr，redirect_sys.stderr 抓不到，
    因此必须显式 add/remove 任务 sink。max_workers=1 保证串行，全局替换安全。
    """
    from loguru import logger as _lg  # 局部引用，避免与模块级 logger 混淆

    log_path.parent.mkdir(parents=True, exist_ok=True)
    lf = open(log_path, "w", encoding="utf-8")
    sink_id = _lg.add(
        lf,
        format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <7} | {name}:{line} | {message}",
        colorize=False,
        backtrace=False,
        diagnose=False,
        enqueue=False,
    )
    _shared_log_file.file = lf
    try:
        with contextlib.redirect_stdout(lf), contextlib.redirect_stderr(lf):
            yield
    finally:
        _lg.remove(sink_id)
        _shared_log_file.file = None
        _remove_doc_svc_handlers(logging.getLogger())
        lf.close()


# ────────────────────────────────────────────────────────────────
# 任务记录 / 异常
# ────────────────────────────────────────────────────────────────

@dataclass
class TaskRecord:
    task_id: str
    file_name: str
    params: dict
    task_dir: Path
    input_path: Path
    created_at: datetime
    status: str = "pending"            # pending / running / succeeded / failed / cancelled
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    error: Optional[str] = None
    result: Optional[dict] = None
    future: Optional[Future] = None
    vlm: Optional[dict] = None         # 本次任务 VLM 覆盖配置（含 api_key，不入 params.json）

    def to_brief(self) -> dict:
        return {
            "task_id": self.task_id,
            "status": self.status,
            "file_name": self.file_name,
            "created_at": self.created_at,
        }


class QueueFullError(Exception):
    """排队任务数已达上限。"""


# ────────────────────────────────────────────────────────────────
# 工具函数
# ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


def _sanitize_name(name: str) -> str:
    base = os.path.basename(name or "")
    base = "".join(c for c in base if c.isalnum() or c in "._- ")
    base = base.strip()
    return base or "upload.bin"


def _summarize_result(record: TaskRecord, result: dict) -> dict:
    """从内存中的 layout 巨 dict 计算摘要，并收集产物路径；只返回小 dict。"""
    layout = result.get("layout") or []
    pages = len(layout) if isinstance(layout, list) else 0
    blocks = figures = tables = chars = 0
    if isinstance(layout, list):
        for page in layout:
            if not isinstance(page, dict):
                continue
            for blk in page.get("para_blocks") or []:
                if not isinstance(blk, dict):
                    continue
                blocks += 1
                t = str(blk.get("type", ""))
                if "image" in t:
                    figures += 1
                elif "table" in t:
                    tables += 1
                text = blk.get("text")
                if text:
                    chars += len(text)

    artifacts = {}
    for key, path in (
        ("layout_json", result.get("layout_json")),
        ("markdown", result.get("md_path")),
        ("pdf", result.get("pdf_path")),
        ("middle_json", result.get("middle_json")),
    ):
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            continue
        rel = p.relative_to(record.task_dir) if str(p).startswith(str(record.task_dir)) else Path(p).name
        artifacts[key] = {
            "filename": p.name,
            "path": str(rel),
            "url": f"/files/{record.task_id}/{str(rel).replace(chr(92), '/')}",
            "download_url": f"/api/v1/tasks/{record.task_id}/download/{key}",
            "size_bytes": p.stat().st_size,
        }

    return {
        "file_stem": Path(record.input_path).stem,
        "page_count": pages,
        "block_count": blocks,
        "figure_count": figures,
        "table_count": tables,
        "char_count": chars,
        "artifacts": artifacts,
    }


# ────────────────────────────────────────────────────────────────
# TaskManager
# ────────────────────────────────────────────────────────────────

class TaskManager:
    def __init__(
        self,
        tasks_root: Path,
        vlm: VLMConfig,
        max_concurrency: int = 1,
        max_queue: int = 10,
        ttl_hours: int = 24,
        cleanup_interval_seconds: int = 3600,
    ):
        self._tasks_root = Path(tasks_root)
        self._tasks_root.mkdir(parents=True, exist_ok=True)
        self._vlm = vlm
        self._max_queue = max_queue
        self._ttl_hours = ttl_hours
        self._cleanup_interval = cleanup_interval_seconds
        self._lock = threading.Lock()
        self._tasks: dict[str, TaskRecord] = {}
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, max_concurrency), thread_name_prefix="pipe"
        )
        self._started_at = _now()
        self._stop = threading.Event()

        self._sweep()  # 清理上次进程遗留的过期目录
        if ttl_hours > 0:
            self._cleanup_thread = threading.Thread(
                target=self._cleanup_loop, name="task-cleanup", daemon=True
            )
            self._cleanup_thread.start()

    # ── 生命周期 ──────────────────────────────────────────────

    def reserve(self, file_name: str, params: dict) -> TaskRecord:
        """创建任务记录与目录。排队任务数达上限时抛 QueueFullError。"""
        with self._lock:
            pending = sum(1 for r in self._tasks.values() if r.status == "pending")
            if pending >= self._max_queue:
                raise QueueFullError(f"队列已满（{self._max_queue} 个待处理任务），请稍后重试")

            task_id = uuid.uuid4().hex
            task_dir = self._tasks_root / task_id
            (task_dir / "input").mkdir(parents=True)
            (task_dir / "output").mkdir(parents=True)
            (task_dir / "logs").mkdir(parents=True)

            vlm = params.pop("vlm_override", None)  # 从 params 中抽出，避免 api_key 落盘
            record = TaskRecord(
                task_id=task_id,
                file_name=file_name,
                params=dict(params),
                task_dir=task_dir,
                input_path=task_dir / "input" / _sanitize_name(file_name),
                created_at=_now(),
                vlm=vlm,
            )
            self._tasks[task_id] = record

        try:
            (task_dir / "params.json").write_text(
                json.dumps({"file_name": file_name, **params}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        return record

    def enqueue(self, record: TaskRecord) -> None:
        fut = self._executor.submit(self._run_task, record)
        record.future = fut
        fut.add_done_callback(lambda _f, tid=record.task_id: self._on_done(tid))

    def get(self, task_id: str) -> Optional[TaskRecord]:
        with self._lock:
            return self._tasks.get(task_id)

    def list_tasks(self, limit: int = 50) -> list[dict]:
        with self._lock:
            items = sorted(self._tasks.values(), key=lambda r: r.created_at, reverse=True)
            return [r.to_brief() for r in items[:limit]]

    def stats(self) -> dict:
        with self._lock:
            running = sum(1 for r in self._tasks.values() if r.status == "running")
            pending = sum(1 for r in self._tasks.values() if r.status == "pending")
            total = len(self._tasks)
        return {"active": running, "queued": pending, "total": total, "max_concurrency": self._max_queue}

    def cancel(self, task_id: str) -> str:
        """返回 'cancelled' | 'running' | 'terminal' | 'not_found'。"""
        with self._lock:
            r = self._tasks.get(task_id)
            if not r:
                return "not_found"
            if r.status == "pending":
                if r.future is not None and r.future.cancel():
                    r.status = "cancelled"
                    r.finished_at = _now()
                    return "cancelled"
                return "running"  # 已被 worker 拾取
            if r.status == "running":
                return "running"
            return "terminal"

    def delete(self, task_id: str, force: bool = False) -> bool:
        """删除记录与目录。非终态任务需 force=True（用于创建失败时的清理）。"""
        with self._lock:
            r = self._tasks.get(task_id)
            if not r:
                return False
            if not force and r.status in ("pending", "running"):
                return False
            self._tasks.pop(task_id)
        shutil.rmtree(r.task_dir, ignore_errors=True)
        return True

    def shutdown(self):
        self._stop.set()
        self._executor.shutdown(wait=False, cancel_futures=True)

    # ── worker ────────────────────────────────────────────────

    def _run_task(self, record: TaskRecord):
        with self._lock:
            if record.status == "cancelled":
                return
            record.status = "running"
            record.started_at = _now()

        log_path = record.task_dir / "logs" / "task.log"
        _set_log_path(log_path)
        try:
            with _task_logging(log_path):
                logger.info(f"[{record.task_id}] 开始解析: {record.file_name}")
                ov = record.vlm or {}
                result = pipeline.run_pipeline(
                    input_file=str(record.input_path),
                    output_dir=str(record.task_dir / "output"),
                    enable_vlm=record.params["enable_vlm"],
                    vlm_base_url=ov.get("base_url") or self._vlm.base_url,
                    vlm_model=ov.get("model") or self._vlm.model,
                    vlm_api_key=ov.get("api_key") or self._vlm.api_key,
                    enable_cross_page=record.params["enable_cross_page"],
                    export_pdf=record.params["export_pdf"],
                    export_md=record.params["export_md"],
                    export_mode=record.params["export_mode"],
                    log_level=record.params.get("log_level", "INFO"),
                )
            summary = _summarize_result(record, result)
        except Exception as e:
            logger.error(f"[{record.task_id}] 解析失败: {e}")
            with self._lock:
                record.status = "failed"
                record.error = traceback.format_exc()[:8000]
                record.finished_at = _now()
            return
        finally:
            _set_log_path(None)

        with self._lock:
            record.status = "succeeded"
            record.result = summary
            record.finished_at = _now()
        logger.info(f"[{record.task_id}] 解析完成: 页码={summary['page_count']}")

    def _on_done(self, task_id: str):
        # 释放 future 引用（worker 已写状态）
        with self._lock:
            r = self._tasks.get(task_id)
            if r:
                r.future = None

    # ── 清理 ──────────────────────────────────────────────────

    def _cleanup_loop(self):
        while not self._stop.wait(self._cleanup_interval):
            self._sweep()

    def _sweep(self):
        """删除过期终态任务目录，弹出过期记录。"""
        ttl_s = self._ttl_hours * 3600
        if ttl_s <= 0:
            return
        now_ts = datetime.now(timezone.utc).timestamp()

        with self._lock:
            expired = [
                tid for tid, r in self._tasks.items()
                if r.status in ("succeeded", "failed", "cancelled")
                and r.finished_at is not None
                and (now_ts - r.finished_at.timestamp()) > ttl_s
            ]
            for tid in expired:
                r = self._tasks.pop(tid, None)
                if r:
                    shutil.rmtree(r.task_dir, ignore_errors=True)

        # 清理不在注册表中的遗留目录（上次进程残留 / 已删除记录）
        try:
            for d in self._tasks_root.iterdir():
                if not d.is_dir():
                    continue
                with self._lock:
                    if d.name in self._tasks:
                        continue
                try:
                    mtime = d.stat().st_mtime
                except OSError:
                    continue
                if now_ts - mtime > ttl_s:
                    shutil.rmtree(d, ignore_errors=True)
        except FileNotFoundError:
            pass
