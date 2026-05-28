"""
解析指定 .doc 文档，输出到 /home/xq/rag/output
流程分两步，可独立运行:
  步骤1: .doc -> .pdf (LibreOffice) -> MinerU pipeline 解析
  步骤2: middle.json -> VLM 合并纠正 -> 导出 confirmed/detailed PDF + Markdown

用法:
  python parse_doc.py              # 仅步骤1 (MinerU 解析)
  python parse_doc.py --vlm        # 步骤1 + 步骤2 (VLM 纠正)
  python parse_doc.py --vlm-only   # 仅步骤2 (跳过 MinerU，直接对已有 middle.json 纠正)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["MINERU_MODEL_SOURCE"] = "local"

sys.path.insert(0, str(Path(__file__).parent))
from mineru_service import MinerUService, MinerUConfig, Backend
from merge import (
    extract_layout_json,
    vlm_correct_layout,
    export_layout_pdf,
    export_layout_markdown,
    export_pdf_pages_with_page_index,
)

SRC_FILE = Path("/home/xq/rag/广州院制度文件/质量、环境、职业健康安全、信息安全管理体系文件（2024版）/2 作业文件一/304设计（咨询）成品校审管理细则.doc")
OUTPUT_DIR = Path("/home/xq/rag/output")

# VLM 配置 (优先读取环境变量)
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "https://token-plan-cn.xiaomimimo.com/v1")
VLM_MODEL = os.environ.get("VLM_MODEL", "mimo-v2.5")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")


def convert_doc_to_pdf(doc_path: Path, tmp_dir: str) -> Path:
    """用 LibreOffice 将 .doc 转换为 .pdf"""
    cmd = [
        "libreoffice", "--headless", "--convert-to", "pdf",
        "--outdir", tmp_dir, str(doc_path),
    ]
    print(f"[1/2] 转换 .doc -> .pdf ...")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(f"LibreOffice 转换失败:\n{result.stderr}")

    pdf_path = Path(tmp_dir) / f"{doc_path.stem}.pdf"
    if not pdf_path.exists():
        raise FileNotFoundError(f"转换后文件不存在: {pdf_path}")
    print(f"      转换完成: {pdf_path.name} ({pdf_path.stat().st_size / 1024:.1f} KB)")
    return pdf_path


# ══════════════════════════════════════════════════════════════
#  步骤1: MinerU 解析
# ══════════════════════════════════════════════════════════════

def step1_mineru_parse(src_file: Path, output_dir: Path):
    """MinerU 解析: .doc -> .pdf -> middle.json + content_list + markdown"""
    if not src_file.exists():
        print(f"文件不存在: {src_file}")
        sys.exit(1)

    output_dir.mkdir(parents=True, exist_ok=True)
    file_stem = src_file.stem
    out_dir = output_dir / file_stem
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        pdf_path = convert_doc_to_pdf(src_file, tmp_dir)

        pdf_out = out_dir / f"{file_stem}.pdf"
        shutil.copy2(str(pdf_path), str(pdf_out))
        print(f"      PDF 已复制到: {pdf_out}")

        config = MinerUConfig(
            backend=Backend.PIPELINE,
            formula_enable=True,
            table_enable=False,
            image_analysis=False,
            language="ch",
            output_dir=str(output_dir),
            dump_content_list=True,
            draw_layout_bbox=True,
            dump_middle_json=True,
        )
        service = MinerUService(config)

        print(f"[2/2] 开始 MinerU 解析 (backend=pipeline) ...")
        result = service.parse(pdf_path)
        print(f"      解析完成!")

    auto_dir = out_dir / "auto"
    print(f"\n{'='*60}")
    print(f"输出目录: {out_dir}")
    if result.markdown:
        print(f"Markdown 长度: {len(result.markdown)} 字符")
    if result.content_list:
        print(f"Content List 条目数: {len(result.content_list)}")

    middle_json_path = auto_dir / f"{file_stem}_middle.json"
    if middle_json_path.exists():
        print(f"middle.json: {middle_json_path}")

    for f in sorted(out_dir.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(out_dir)}  ({f.stat().st_size / 1024:.1f} KB)")

    return out_dir


# ══════════════════════════════════════════════════════════════
#  步骤2: VLM 合并纠正
# ══════════════════════════════════════════════════════════════

def step2_vlm_correct(out_dir: Path, file_stem: str,
                      vlm_base_url: str = VLM_BASE_URL,
                      vlm_model: str = VLM_MODEL,
                      api_key: str = VLM_API_KEY):
    """对已有 middle.json 执行 VLM 合并纠正，导出 confirmed/detailed 结果。"""
    auto_dir = out_dir / "auto"
    images_dir = auto_dir / "images"
    pdf_out = out_dir / f"{file_stem}.pdf"

    # 加载 middle.json
    middle_json_path = auto_dir / f"{file_stem}_middle.json"
    if not middle_json_path.exists():
        print(f"middle.json 不存在: {middle_json_path}")
        return

    print(f"[1/4] 加载 middle.json ...")
    with open(middle_json_path, 'r', encoding='utf-8') as f:
        middle_data = json.load(f)

    pdf_info = middle_data.get('pdf_info', middle_data)
    if not isinstance(pdf_info, list):
        pdf_info = [pdf_info]

    # 提取布局 JSON
    layout = extract_layout_json(pdf_info, img_buket_path='images')
    print(f"      布局提取完成: {len(layout)} 页")

    # PDF 页面 -> 图像
    print(f"[2/4] PDF -> 页面图像 ...")
    page_image_paths = export_pdf_pages_with_page_index(
        pdf_path=str(pdf_out),
        auto_dir=str(auto_dir),
        pages_dir_name='pages',
        dpi=300,
    )

    from PIL import Image
    pdf_images = [Image.open(p) for p in page_image_paths]
    print(f"      已加载 {len(pdf_images)} 页图像")

    # VLM 纠正
    print(f"[3/4] VLM 合并纠正 (model={vlm_model}) ...")
    confirmed_images_dir = auto_dir / "confirmed_images"
    debug_dir = out_dir / "debug_vlm_annotated"

    corrected_layout = vlm_correct_layout(
        layout_json=layout,
        pdf_images=pdf_images,
        images_dir=str(images_dir),
        confirmed_images_dir=str(confirmed_images_dir),
        confirmed_img_prefix='confirmed_images',
        vllm_base_url=vlm_base_url,
        vllm_model=vlm_model,
        api_key=api_key,
        debug_dir=str(debug_dir),
    )

    # 保存布局 JSON
    layout_json_path = auto_dir / f"{file_stem}_layout.json"
    with open(layout_json_path, 'w', encoding='utf-8') as f:
        json.dump(corrected_layout, f, ensure_ascii=False, indent=2)
    print(f"      布局 JSON → {layout_json_path}")

    # 导出
    print(f"[4/4] 导出 PDF + Markdown ...")
    with open(str(pdf_out), 'rb') as f:
        pdf_bytes = f.read()

    for mode in ['confirmed', 'detailed']:
        pdf_name = f'{file_stem}_{mode}.pdf'
        export_layout_pdf(corrected_layout, pdf_bytes, str(auto_dir),
                          filename=pdf_name, mode=mode)
        print(f"      {mode} PDF → {auto_dir / pdf_name}")

        md_name = f'{file_stem}_{mode}.md'
        export_layout_markdown(corrected_layout, str(auto_dir),
                               filename=md_name, mode=mode)
        print(f"      {mode} MD  → {auto_dir / md_name}")

    print(f"\n{'='*60}")
    print(f"VLM 纠正完成:")
    print(f"  标注图     → {debug_dir}")
    print(f"  confirmed  → {confirmed_images_dir}")
    for f in sorted(auto_dir.rglob("*")):
        if f.is_file():
            print(f"  {f.relative_to(auto_dir)}  ({f.stat().st_size / 1024:.1f} KB)")


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='.doc 文档解析 + VLM 合并纠正')
    parser.add_argument('--vlm', action='store_true',
                        help='MinerU 解析后执行 VLM 合并纠正')
    parser.add_argument('--vlm-only', action='store_true',
                        help='跳过 MinerU，直接对已有 middle.json 执行 VLM 纠正')
    parser.add_argument('--src', type=str, default=None,
                        help='源文件路径 (默认 SRC_FILE)')
    parser.add_argument('--vlm-model', type=str, default=VLM_MODEL,
                        help=f'VLM 模型名 (默认 {VLM_MODEL})')
    args = parser.parse_args()

    src_file = Path(args.src) if args.src else SRC_FILE
    file_stem = src_file.stem
    out_dir = OUTPUT_DIR / file_stem

    if not args.vlm_only:
        out_dir = step1_mineru_parse(src_file, OUTPUT_DIR)
        print()

    if args.vlm or args.vlm_only:
        step2_vlm_correct(out_dir, file_stem, vlm_model=args.vlm_model)


if __name__ == "__main__":
    main()
