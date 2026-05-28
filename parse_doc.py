"""
MinerU 文档解析 (步骤1)

.doc → .pdf (LibreOffice) → MinerU pipeline 解析 → middle.json + images/ + markdown
不涉及任何 VLM 合并逻辑。

用法:
  python parse_doc.py /path/to/document.doc
  python parse_doc.py /path/to/document.doc -o /path/to/output
"""

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

os.environ["MINERU_MODEL_SOURCE"] = "local"

sys.path.insert(0, str(Path(__file__).parent))
from mineru_service import MinerUService, MinerUConfig, Backend


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


def parse(src_file: Path, output_dir: Path) -> Path:
    """MinerU 解析: .doc/.pdf → middle.json + images/ + markdown

    Args:
        src_file: 源文件路径 (.doc 或 .pdf)
        output_dir: 输出根目录

    Returns:
        输出子目录路径 (output_dir / file_stem)
    """
    if not src_file.exists():
        raise FileNotFoundError(f"文件不存在: {src_file}")

    output_dir.mkdir(parents=True, exist_ok=True)
    file_stem = src_file.stem
    out_dir = output_dir / file_stem
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        if src_file.suffix.lower() == '.doc':
            pdf_path = convert_doc_to_pdf(src_file, tmp_dir)
        else:
            pdf_path = src_file

        pdf_out = out_dir / f"{file_stem}.pdf"
        if not pdf_out.exists():
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
        result = service.parse(pdf_out)
        print(f"      解析完成!")

    auto_dir = out_dir / "auto"
    print(f"\n{'='*60}")
    print(f"输出目录: {out_dir}")
    if result.markdown:
        print(f"Markdown 长度: {len(result.markdown)} 字符")

    middle_json_path = auto_dir / f"{file_stem}_middle.json"
    if middle_json_path.exists():
        print(f"middle.json: {middle_json_path}")

    return out_dir


def main():
    parser = argparse.ArgumentParser(description='MinerU 文档解析')
    parser.add_argument('input', help='源文件路径 (.doc 或 .pdf)')
    parser.add_argument('-o', '--output', default=None,
                        help='输出目录 (默认 /home/xq/rag/output)')
    args = parser.parse_args()

    src_file = Path(args.input)
    output_dir = Path(args.output) if args.output else Path("/home/xq/rag/output")
    parse(src_file, output_dir)


if __name__ == "__main__":
    main()
