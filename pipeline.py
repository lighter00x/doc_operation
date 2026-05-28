"""
文档解析完整流水线

串联三个步骤:
  1. MinerU 解析 (.doc/.pdf → middle.json + images/)
  2. 页内合并 (middle.json → layout JSON，默认启用 VLM 增强)
  3. 跨页合并 (layout JSON → 最终结果，默认启用 VLM 增强)

各步骤对应的独立模块:
  - parse_doc.py      : 步骤1 - MinerU 解析
  - merge_optimized.py: 步骤2 - 页内合并
  - merge_cross_page.py: 步骤3 - 跨页合并

用法:
  # 完整流程 (默认启用 VLM 页内合并 + 跨页合并)
  python pipeline.py input.doc

  # 禁用 VLM，纯 MinerU 解析
  python pipeline.py input.doc --no-vlm

  # 只启用页内合并，禁用跨页合并
  python pipeline.py input.doc --no-cross-page

  # 指定输出目录
  python pipeline.py input.doc -o /path/to/output
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

# 添加当前目录到 path
sys.path.insert(0, str(Path(__file__).parent))

from parse_doc import parse as mineru_parse
from merge_optimized import (
    extract_layout_json,
    vlm_correct_layout,
    export_layout_pdf,
    export_layout_markdown,
    _assign_confirmed_indices,
    _configure_logging,
)
from merge_cross_page import (
    cross_page_merge,
    export_cross_page_pdf,
    export_cross_page_markdown,
    load_pdf_page_images,
)

# VLM 默认配置 (从环境变量读取)
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VLM_MODEL = os.environ.get("VLM_MODEL", "doubao-seed-2-0-lite-260428")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")

std_logger = logging.getLogger(__name__)


def run_pipeline(
    input_file: str,
    output_dir: str = None,
    # VLM 页内合并参数
    enable_vlm: bool = False,
    vlm_base_url: str = VLM_BASE_URL,
    vlm_model: str = VLM_MODEL,
    vlm_api_key: str = VLM_API_KEY,
    # 跨页合并参数
    enable_cross_page: bool = False,
    cross_page_base_url: str = None,
    cross_page_model: str = None,
    cross_page_api_key: str = None,
    # 导出选项
    export_pdf: bool = True,
    export_md: bool = True,
    export_mode: str = "confirmed",
    # 其他
    img_prefix: str = "images",
    confirmed_prefix: str = "confirmed_images",
    debug_dir: str = None,
    log_level: str = "INFO",
) -> dict:
    """执行完整流水线。

    Args:
        input_file: 输入文件路径 (.doc 或 .pdf)
        output_dir: 输出目录 (默认 /home/xq/rag/output)
        enable_vlm: 是否启用 VLM 页内合并
        enable_cross_page: 是否启用跨页合并 (需要先启用 VLM)
        其余参数见各模块文档

    Returns:
        {
            "layout": 最终的 layout JSON,
            "output_dir": 输出目录路径,
            "middle_json": middle.json 路径,
            "layout_json": layout JSON 路径,
            "pdf_path": PDF 导出路径 (如果启用),
            "md_path": Markdown 导出路径 (如果启用),
        }
    """
    # 配置日志
    _configure_logging(log_level)

    input_path = Path(input_file)
    if not input_path.exists():
        raise FileNotFoundError(f"输入文件不存在: {input_path}")

    # 输出目录
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = Path("/home/xq/rag/output")
    out_dir.mkdir(parents=True, exist_ok=True)

    file_stem = input_path.stem

    # ══════════════════════════════════════════════════════════
    #  步骤 1: MinerU 解析
    # ══════════════════════════════════════════════════════════
    print("=" * 60)
    print("步骤 1: MinerU 解析")
    print("=" * 60)

    parse_out_dir = mineru_parse(input_path, out_dir)
    print(f"MinerU 解析完成: {parse_out_dir}")

    # 查找生成的 middle.json 和 PDF
    auto_dir = parse_out_dir / "auto"
    middle_json_path = auto_dir / f"{file_stem}_middle.json"

    if not middle_json_path.exists():
        raise FileNotFoundError(f"middle.json 未生成: {middle_json_path}")

    # 查找 PDF 文件
    pdf_candidates = [
        parse_out_dir / f"{file_stem}.pdf",
        input_path if input_path.suffix.lower() == ".pdf" else None,
    ]
    pdf_path = None
    for candidate in pdf_candidates:
        if candidate and candidate.exists():
            pdf_path = candidate
            break

    # ══════════════════════════════════════════════════════════
    #  步骤 2: 页内合并
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("步骤 2: 页内合并")
    print("=" * 60)

    # 读取 middle.json
    with open(middle_json_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    pdf_info = data.get("pdf_info", data)
    if not isinstance(pdf_info, list):
        pdf_info = [pdf_info]

    # 提取布局 JSON
    images_dir = str(auto_dir / img_prefix)
    confirmed_images_dir = str(out_dir / confirmed_prefix)

    layout = extract_layout_json(pdf_info, img_buket_path=img_prefix)
    print(f"布局提取完成: {len(layout)} 页")

    # 加载 PDF 图像 (VLM 和跨页合并共用)
    pdf_images = None
    if (enable_vlm or enable_cross_page) and pdf_path:
        print(f"加载 PDF 图像: {pdf_path}")
        try:
            pdf_images = load_pdf_page_images(str(pdf_path), str(out_dir), dpi=300)
            print(f"PDF 图像加载完成: {len(pdf_images)} 页")
        except Exception as e:
            print(f"PDF 图像加载失败: {e}")

    # VLM 页内合并 (可选)
    if enable_vlm:
        if pdf_images is not None:
            vlm_debug_dir = debug_dir or str(out_dir / "debug_vlm_annotated")
            print(f"VLM 页内合并...")

            layout = vlm_correct_layout(
                layout_json=layout,
                pdf_images=pdf_images,
                images_dir=images_dir,
                confirmed_images_dir=confirmed_images_dir,
                confirmed_img_prefix=confirmed_prefix,
                vllm_base_url=vlm_base_url,
                vllm_model=vlm_model,
                api_key=vlm_api_key,
                debug_dir=vlm_debug_dir,
            )
            print(f"VLM 页内合并完成")
            print(f"  标注图    → {vlm_debug_dir}")
            print(f"  confirmed → {confirmed_images_dir}")
        else:
            print("VLM 已启用但未找到 PDF 图像，跳过")
    else:
        print("VLM 未启用，跳过页内合并")
        # 未启用 VLM 时，也需要分配 confirmed_index
        _assign_confirmed_indices(layout)

    # ══════════════════════════════════════════════════════════
    #  步骤 3: 跨页合并
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("步骤 3: 跨页合并")
    print("=" * 60)

    if enable_cross_page:
        if pdf_images is not None:
            cp_base_url = cross_page_base_url or vlm_base_url
            cp_model = cross_page_model or vlm_model
            cp_api_key = cross_page_api_key or vlm_api_key
            cross_debug_dir = str(out_dir / "debug_cross_page")

            print("跨页合并...")
            layout = cross_page_merge(
                layout_json=layout,
                pdf_images=pdf_images,
                confirmed_images_dir=confirmed_images_dir,
                confirmed_img_prefix=confirmed_prefix,
                base_url=cp_base_url,
                model_name=cp_model,
                api_key=cp_api_key,
                debug_dir=cross_debug_dir,
            )
            print("跨页合并完成")
            print(f"  调试图 → {cross_debug_dir}")
        else:
            print("跨页合并已启用但未找到 PDF 图像，跳过")
    else:
        print("跨页合并未启用，跳过")

    # ══════════════════════════════════════════════════════════
    #  保存结果
    # ══════════════════════════════════════════════════════════
    print("\n" + "=" * 60)
    print("保存结果")
    print("=" * 60)

    # 保存 layout JSON
    layout_json_path = out_dir / f"{file_stem}_layout.json"
    with open(layout_json_path, "w", encoding="utf-8") as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    print(f"Layout JSON → {layout_json_path}")

    # 导出 PDF
    pdf_out_path = None
    if export_pdf and pdf_path:
        pdf_name = f"{file_stem}_{export_mode}.pdf"
        if enable_cross_page and pdf_images:
            export_cross_page_pdf(layout, pdf_images, str(out_dir), pdf_name)
        else:
            with open(pdf_path, "rb") as f:
                pdf_bytes = f.read()
            export_layout_pdf(layout, pdf_bytes, str(out_dir), pdf_name, mode=export_mode)
        pdf_out_path = out_dir / pdf_name
        print(f"PDF → {pdf_out_path}")

    # 导出 Markdown
    md_out_path = None
    if export_md:
        md_name = f"{file_stem}_{export_mode}.md"
        if enable_cross_page and pdf_images:
            export_cross_page_markdown(layout, str(out_dir), md_name)
        else:
            export_layout_markdown(layout, str(out_dir), md_name, mode=export_mode)
        md_out_path = out_dir / md_name
        print(f"Markdown → {md_out_path}")

    print("\n" + "=" * 60)
    print("流水线完成!")
    print("=" * 60)

    return {
        "layout": layout,
        "output_dir": str(out_dir),
        "middle_json": str(middle_json_path),
        "layout_json": str(layout_json_path),
        "pdf_path": str(pdf_out_path) if pdf_out_path else None,
        "md_path": str(md_out_path) if md_out_path else None,
    }


def main():
    parser = argparse.ArgumentParser(
        description="文档解析完整流水线: MinerU 解析 → 页内合并 → 跨页合并"
    )

    # 输入输出
    parser.add_argument("input", help="输入文件路径 (.doc 或 .pdf)")
    parser.add_argument("-o", "--output", default=None, help="输出目录 (默认 /home/xq/rag/output)")

    # VLM 页内合并
    parser.add_argument("--no-vlm", action="store_true", help="禁用 VLM 页内合并 (默认启用)")
    parser.add_argument("--vlm-url", default=VLM_BASE_URL, help="VLM API URL")
    parser.add_argument("--vlm-model", default=VLM_MODEL, help="VLM 模型名")
    parser.add_argument("--vlm-api-key", default=VLM_API_KEY, help="VLM API Key")

    # 跨页合并
    parser.add_argument("--no-cross-page", action="store_true", help="禁用跨页合并 (默认启用)")
    parser.add_argument("--cross-url", default=None, help="跨页 VLM URL (默认同 --vlm-url)")
    parser.add_argument("--cross-model", default=None, help="跨页 VLM 模型 (默认同 --vlm-model)")
    parser.add_argument("--cross-api-key", default=None, help="跨页 VLM API Key (默认同 --vlm-api-key)")

    # 导出选项
    parser.add_argument("--no-pdf", action="store_true", help="禁用 PDF 导出")
    parser.add_argument("--no-md", action="store_true", help="禁用 Markdown 导出")
    parser.add_argument("--export-mode", default="confirmed",
                        choices=["detailed", "confirmed"],
                        help="导出模式 (默认 confirmed)")

    # 其他
    parser.add_argument("--img-prefix", default="images", help="图片路径前缀")
    parser.add_argument("--confirmed-prefix", default="confirmed_images", help="合并后图片前缀")
    parser.add_argument("--debug-dir", default=None, help="调试输出目录")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="日志级别")

    args = parser.parse_args()

    result = run_pipeline(
        input_file=args.input,
        output_dir=args.output,
        enable_vlm=not args.no_vlm,
        vlm_base_url=args.vlm_url,
        vlm_model=args.vlm_model,
        vlm_api_key=args.vlm_api_key,
        enable_cross_page=not args.no_cross_page,
        cross_page_base_url=args.cross_url,
        cross_page_model=args.cross_model,
        cross_page_api_key=args.cross_api_key,
        export_pdf=not args.no_pdf,
        export_md=not args.no_md,
        export_mode=args.export_mode,
        img_prefix=args.img_prefix,
        confirmed_prefix=args.confirmed_prefix,
        debug_dir=args.debug_dir,
        log_level=args.log_level,
    )


if __name__ == "__main__":
    main()
