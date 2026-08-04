#!/usr/bin/env python
"""
文档解析流水线服务客户端

功能:
  1. 上传本地文档（multipart，携带文件名）或传本地路径
  2. 轮询任务直至完成
  3. 按文件结构把全套解析结果保存到指定目录

用法:
  python client.py /path/to/doc.pdf --output /save/dir
  python client.py /path/to/doc.pdf --output /save/dir --name renamed.pdf --no-vlm
  python client.py /path/to/doc.pdf --output /save/dir --by-path   # 服务端可见路径，直接传路径
  python client.py /path/to/doc.pdf --url http://127.0.0.1:8000
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests


def submit(url: str, file_path: str, name: str, params: dict, by_path: bool) -> str:
    """提交任务，返回 task_id。"""
    endpoint = f"{url}/api/v1/tasks"
    if by_path:
        data = {"file_path": file_path, **params}
        resp = requests.post(endpoint, data=data, timeout=30)
    else:
        # multipart 上传，显式携带文件名
        files = {"file": (name, open(file_path, "rb"), "application/octet-stream")}
        resp = requests.post(endpoint, files=files, data=params, timeout=60)
    if resp.status_code not in (200, 202):
        raise RuntimeError(f"提交任务失败 [{resp.status_code}]: {resp.text}")
    return resp.json()["task_id"]


def wait(url: str, task_id: str, timeout: int) -> dict:
    """轮询任务直到成功/失败，返回状态接口的 JSON。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = requests.get(f"{url}/api/v1/tasks/{task_id}", timeout=30)
        resp.raise_for_status()
        data = resp.json()
        status = data["status"]
        if status == "succeeded":
            return data
        if status == "failed":
            raise RuntimeError(f"任务失败: {data.get('error')}")
        time.sleep(5)
    raise TimeoutError(f"任务 {task_id} 在 {timeout}s 内未完成")


def save_result(url: str, task_id: str, save_dir: Path) -> list[Path]:
    """按文件结构把全套解析结果保存到 save_dir，返回保存的文件列表。"""
    resp = requests.get(
        f"{url}/api/v1/tasks/{task_id}/result", params={"include_content": "true"}, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()

    saved = []
    for f in data.get("files", []):
        rel = f["path"]
        # 防目录穿越：清理路径分隔符与 ../
        safe_parts = [p for p in rel.replace("\\", "/").split("/") if p and p not in (".", "..")]
        dest = save_dir.joinpath(*safe_parts)
        dest.parent.mkdir(parents=True, exist_ok=True)

        content = f.get("content")
        if content is not None:
            if isinstance(content, (dict, list)):
                dest.write_text(json.dumps(content, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                dest.write_text(str(content), encoding="utf-8")
        else:
            # 二进制 / 未内嵌大文件 → 经 /files 下载
            file_resp = requests.get(url + f["url"], timeout=120)
            file_resp.raise_for_status()
            dest.write_bytes(file_resp.content)
        saved.append(dest)
    return saved


def main():
    parser = argparse.ArgumentParser(description="文档解析流水线服务客户端")
    parser.add_argument("file", help="本地文档路径 (.doc / .pdf)")
    parser.add_argument("-o", "--output", default="./parsed_output", help="结果保存目录 (默认 ./parsed_output)")
    parser.add_argument("--url", default="http://127.0.0.1:8000", help="服务地址 (默认 http://127.0.0.1:8000)")
    parser.add_argument("--name", default=None, help="上传时使用的文件名 (默认取源文件 basename)")
    parser.add_argument("--by-path", action="store_true", help="传本地路径模式（服务端须可访问该路径），默认 multipart 上传")
    parser.add_argument("--no-vlm", action="store_true", help="禁用 VLM 页内合并")
    parser.add_argument("--no-cross-page", action="store_true", help="禁用跨页合并")
    parser.add_argument("--export-mode", default="confirmed", choices=["confirmed", "detailed"])
    parser.add_argument("--timeout", type=int, default=600, help="轮询超时秒数 (默认 600)")
    args = parser.parse_args()

    file_path = str(Path(args.file).resolve())
    if not Path(file_path).is_file():
        parser.error(f"文件不存在: {file_path}")

    name = args.name or Path(file_path).name
    params = {
        "enable_vlm": "false" if args.no_vlm else "true",
        "enable_cross_page": "false" if args.no_cross_page else "true",
        "export_mode": args.export_mode,
    }

    url = args.url.rstrip("/")
    print(f"提交任务: {name} (vlm={params['enable_vlm']}, cross_page={params['enable_cross_page']})")
    task_id = submit(url, file_path, name, params, by_path=args.by_path)
    print(f"task_id: {task_id}")

    print("轮询中...")
    data = wait(url, task_id, args.timeout)
    summary = data.get("result", {})
    print(f"解析完成: {summary.get('page_count')} 页, {summary.get('block_count')} 块, "
          f"{summary.get('figure_count')} 图, {summary.get('table_count')} 表")

    save_dir = Path(args.output).resolve()
    saved = save_result(url, task_id, save_dir)
    print(f"已保存 {len(saved)} 个文件到: {save_dir}")
    for p in saved:
        print(f"  {p}")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"错误: {e}", file=sys.stderr)
        sys.exit(1)
