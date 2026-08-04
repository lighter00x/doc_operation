#!/usr/bin/env bash
# 启动文档解析流水线 HTTP 服务
# 用法: bash start.sh        (默认 0.0.0.0:8000，默认使用 GPU 4)
#       HOST=127.0.0.1 PORT=9000 GPU=0 bash start.sh
#
# GPU: MinerU 默认使用 device 0。若该卡被其他任务占用会 OOM，
#      通过 CUDA_VISIBLE_DEVICES 指定空闲卡（默认 4，可覆盖）。
set -e
cd "$(dirname "$0")"
export PYTHONPATH="$(pwd):${PYTHONPATH}"
export CUDA_VISIBLE_DEVICES="${GPU:-4}"
exec /home/xq/.conda/envs/newmineru/bin/python -m uvicorn app:app \
    --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" \
    --workers 1 --timeout-keep-alive 30
