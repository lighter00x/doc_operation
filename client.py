#!/usr/bin/env python
"""
文档解析流水线服务客户端

功能:
  1. 上传本地文档（multipart，携带文件名）或传本地路径
  2. 轮询任务直至完成
  3. 按文件结构把全套解析结果保存到指定目录

保存结构（{stem} = 上传文件名，不含扩展名）:
  <--output>/<stem>/auto/<stem>_layout.json   最终布局 JSON
                      <stem>_confirmed.md     最终 Markdown
                      <stem>_confirmed.pdf    最终 PDF
                      <stem>_middle.json      MinerU 中间结果
                      <stem>.md               MinerU 原始 Markdown
                      <stem>.pdf              原始 PDF 副本
                      images/xxx.jpg          切分图片
                      ...
  即: 所有产物（含最终成品）统一收进 <stem>/auto/ 下逐层存放。

用法:
  # 默认连接服务 http://10.154.24.43:8000（外部调用方直接使用本 IP）
  python client.py /path/to/doc.pdf --output /save/dir
  python client.py /path/to/doc.pdf --output /save/dir --name renamed.pdf --no-vlm
  python client.py /path/to/doc.pdf --by-path   # 服务端可见路径，直接传路径
  # 其他地址/端口用 --url 覆盖
  python client.py /path/to/doc.pdf --url http://127.0.0.1:8000
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

# 服务默认地址：本机内网 IP（服务绑定 0.0.0.0:8000，外部机器经此访问）
DEFAULT_URL = "http://10.154.24.43:8000"


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


def _rebase_rel(rel: str, stem: str) -> str:
    """把服务端相对路径重排为 {stem}/auto/xxx 结构。

    服务端 path 形如:
      output/{stem}_layout.json         → {stem}/auto/{stem}_layout.json
      output/{stem}/{stem}.pdf          → {stem}/auto/{stem}.pdf
      output/{stem}/auto/{stem}.md      → {stem}/auto/{stem}.md   (保持)
      output/{stem}/auto/images/x.jpg   → {stem}/auto/images/x.jpg (保持)
    即: 去掉 output/ 前缀，最终产物也统一收进 {stem}/auto/ 下。
    """
    rel = rel.replace("\\", "/")
    if rel.startswith("output/"):
        rel = rel[len("output/"):]
    prefix = f"{stem}/auto/"
    if rel.startswith(prefix):
        return rel
    if rel.startswith(f"{stem}/"):
        return prefix + rel[len(f"{stem}/"):]
    return prefix + rel


def save_result(url: str, task_id: str, save_dir: Path) -> list[Path]:
    """按文件结构把全套解析结果保存到 save_dir，返回保存的文件列表。

    保存结构: {stem}/auto/ 下逐层存放（含最终产物 layout.json / md / pdf）。
    """
    resp = requests.get(
        f"{url}/api/v1/tasks/{task_id}/result", params={"include_content": "true"}, timeout=60
    )
    resp.raise_for_status()
    data = resp.json()

    files = data.get("files", [])
    stem = (data.get("summary") or {}).get("file_stem") or (
        files[0]["path"].split("/")[-1].rsplit(".", 1)[0] if files else "result"
    )

    saved = []
    for f in files:
        rel = _rebase_rel(f["path"], stem)
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
    parser.add_argument("--url", default=DEFAULT_URL, help=f"服务地址 (默认 {DEFAULT_URL})")
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
