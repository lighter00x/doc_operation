"""
服务配置

⚠️ 顺序敏感: 本模块必须在 import pipeline 之前加载 .env，
否则 pipeline.py / merge_cross_page.py 在 import 时会把
VLM_* 环境变量冻结进模块级默认值。app.py 的 import 顺序固定为:
    from config import settings
    from service import TaskManager
"""
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _env_int(name: str, default: int) -> int:
    v = os.getenv(name)
    return int(v) if v and v.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class VLMConfig:
    """VLM 合并服务配置，worker 显式传给 run_pipeline（不依赖模块 import 期默认值）。"""
    base_url: str
    model: str
    api_key: str


@dataclass
class Settings:
    host: str = "0.0.0.0"
    port: int = 8000
    version: str = "0.1.0"

    # 任务
    tasks_root: Path = BASE_DIR / "tasks_root"
    max_concurrency: int = 1          # MinerU 模型进程级共享，必须串行
    max_queue: int = 10               # 队列中 pending 任务上限
    max_upload_bytes: int = 200 * 1024 * 1024
    task_ttl_hours: int = 24          # 0 表示禁用清理
    cleanup_interval_seconds: int = 3600

    enable_metrics: bool = True

    vlm: VLMConfig = field(default_factory=lambda: VLMConfig(
        os.getenv("VLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
        os.getenv("VLM_MODEL", "doubao-seed-2-0-lite-260428"),
        os.getenv("VLM_API_KEY", ""),
    ))


def _load_settings() -> Settings:
    return Settings(
        host=os.getenv("HOST", "0.0.0.0"),
        port=_env_int("PORT", 8000),
        tasks_root=Path(os.getenv("TASKS_ROOT", str(BASE_DIR / "tasks_root"))),
        max_concurrency=_env_int("MAX_CONCURRENCY", 1),
        max_queue=_env_int("MAX_QUEUE", 10),
        max_upload_bytes=_env_int("MAX_UPLOAD_MB", 200) * 1024 * 1024,
        task_ttl_hours=_env_int("TASK_TTL_HOURS", 24),
        cleanup_interval_seconds=_env_int("CLEANUP_INTERVAL", 3600),
        enable_metrics=_env_bool("ENABLE_METRICS", True),
        vlm=VLMConfig(
            os.getenv("VLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            os.getenv("VLM_MODEL", "doubao-seed-2-0-lite-260428"),
            os.getenv("VLM_API_KEY", ""),
        ),
    )


settings = _load_settings()
