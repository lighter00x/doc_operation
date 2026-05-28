"""
测试跨页合并模块 (merge_cross_page.py)

使用与 merge_optimized.py 页内合并相同的 VLM provider 和目录逻辑。
"""

import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from merge_cross_page import (
    cross_page_merge,
    find_figure_page_groups,
    reassign_detailed_indices,
    reassign_confirmed_indices,
)

# 与 merge_optimized.py 一致的 VLM 配置 (从环境变量读取)
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VLM_MODEL = os.environ.get("VLM_MODEL", "doubao-seed-2-0-lite-260428")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")

# 测试数据 (复用 merge_optimized.py 的输出目录)
INPUT_DIR = Path("/home/xq/rag/output/304设计（咨询）成品校审管理细则/auto")
BASE_NAME = "304设计（咨询）成品校审管理细则"
PDF_PATH = Path(f"/home/xq/rag/output/304设计（咨询）成品校审管理细则/{BASE_NAME}.pdf")

LAYOUT_JSON_PATH = INPUT_DIR / f"{BASE_NAME}_layout.json"
IMAGES_DIR = INPUT_DIR / "images"
CONFIRMED_IMAGES_DIR = INPUT_DIR / "confirmed_images"
DEBUG_DIR = INPUT_DIR / "debug_cross_page"
OUTPUT_JSON = INPUT_DIR / f"{BASE_NAME}_layout.json"  # 覆盖原文件


def test_detect_sequences():
    """测试: 检测图表页面分组。"""
    print("=" * 60)
    print("[测试 1] 检测图表页面分组")
    print("=" * 60)

    with open(LAYOUT_JSON_PATH, 'r', encoding='utf-8') as f:
        layout = json.load(f)

    reassign_detailed_indices(layout)
    sequences = find_figure_page_groups(layout)

    if sequences:
        print(f"\n发现 {len(sequences)} 个图表页面分组:")
        for seq in sequences:
            print(f"\n  分组: {seq} (共 {len(seq)} 页)")
            for page_idx in seq:
                for page in layout:
                    if page['page_idx'] == page_idx:
                        blocks = page.get('para_blocks', [])
                        parent_fig = [b for b in blocks
                                      if ('table' in str(b.get('type', '')).lower()
                                          or 'image' in str(b.get('type', '')).lower())
                                      and 'belong_to' not in b]
                        print(f"    页面 {page_idx}: {len(parent_fig)} 个图表块")
                        for fb in parent_fig:
                            btype = fb.get('type', '')
                            if hasattr(btype, 'value'):
                                btype = btype.value
                            merged = 'merged' if fb.get('merged_from') else 'original'
                            print(f"      - type={btype}, didx={fb.get('detailed_index')}, {merged}")
                        break
    else:
        print("\n未发现图表页面")

    return layout, sequences


def test_full_cross_page_merge():
    """测试: 完整的跨页合并流程。"""
    print("\n" + "=" * 60)
    print("[测试 2] 完整跨页合并流程")
    print("=" * 60)

    with open(LAYOUT_JSON_PATH, 'r', encoding='utf-8') as f:
        layout = json.load(f)
    print(f"加载 layout JSON: {LAYOUT_JSON_PATH} ({len(layout)} 页)")

    from pdf2image import convert_from_path
    pdf_images = convert_from_path(str(PDF_PATH), dpi=300)
    print(f"加载 PDF 图像: {PDF_PATH} ({len(pdf_images)} 页)")

    os.makedirs(CONFIRMED_IMAGES_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)

    print("\n开始跨页合并...")
    result = cross_page_merge(
        layout_json=layout,
        pdf_images=pdf_images,
        confirmed_images_dir=str(CONFIRMED_IMAGES_DIR),
        confirmed_img_prefix='confirmed_images',
        base_url=VLM_BASE_URL,
        model_name=VLM_MODEL,
        api_key=VLM_API_KEY,
        debug_dir=str(DEBUG_DIR),
    )

    with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    # 统计
    total = sum(1 for p in result for b in p.get('para_blocks', []))
    cross = sum(1 for p in result for b in p.get('para_blocks', [])
                if b.get('merged_from_cross'))

    print(f"\n{'='*60}")
    print("跨页合并完成:")
    print(f"  结果 JSON  → {OUTPUT_JSON}")
    print(f"  调试图像   → {DEBUG_DIR}")
    print(f"  确认图像   → {CONFIRMED_IMAGES_DIR}")
    print(f"\n  总块数: {total}, 跨页合并块: {cross}")


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='测试跨页合并')
    parser.add_argument('--detect-only', action='store_true',
                        help='仅检测连续图表序列')
    parser.add_argument('--full', action='store_true',
                        help='执行完整跨页合并')
    args = parser.parse_args()

    if args.detect_only:
        test_detect_sequences()
    elif args.full:
        test_full_cross_page_merge()
    else:
        layout, sequences = test_detect_sequences()
        if sequences:
            print(f"\nVLM: {VLM_MODEL} @ {VLM_BASE_URL}")
            answer = input("\n执行合并? (y/N): ").strip().lower()
            if answer == 'y':
                test_full_cross_page_merge()
