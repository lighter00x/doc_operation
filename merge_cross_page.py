"""
跨页元素合并模块 (通用版)

基于已完成页内合并的 layout JSON，检测并合并跨页分割的元素（表格、图片、及其脚注/图注）。

核心思路:
  1. 接收已经过页内 VLM 合并的 layout_json (来自 merge_optimized.py)
  2. 找出所有包含图表/表格的页面，按连续性分组，每组 +1 下一页
  3. 将这些页面标注 confirmed_index，发送给 VLM 判断跨页合并
  4. 根据 VLM 返回执行合并，信息保留在靠前的元素中

Index 处理策略:
  - 输入的 layout_json 已有 confirmed_index (页内从0开始连续)
  - VLM 看到的是 confirmed_index (标注在画框图上)
  - 跨页合并不新增/删除原始块，所有信息写回最靠前的参与元素
  - 被合并的后续原始块: 保留在原页面，添加 belong_to_cross 标记
  - 合并完成后，保持 detailed_index，不重排页面原始粒度

数据结构:
  跨页合并主块:
    - type: image_body / table_body
    - bbox: 使用第一个页面元素的 bbox
    - img_path: 逗号分隔的来源路径
    - confirmed_img_path: 垂直拼接后的图像路径
    - text: 拼接所有参与块的 text/caption_text/footnote_text
    - merged_from_cross: [{page, index, confirmed_index, detailed_index, type}, ...]
    - page_idx: 序列第一个页面的 page_idx

  被合并的子块 (原始块):
    - belong_to_cross: {page: 第一页page_idx, index: 主块confirmed_index, ...}
    - confirmed_index: None
    - 其余字段保留

用法:
  作为 merge_optimized.py 的下游:
    layout = extract_layout_json(pdf_info)
    layout = vlm_correct_layout(layout, ...)  # 页内合并
    layout = cross_page_merge(layout, pdf_images, ...)  # 跨页合并 (本模块)

  独立 CLI:
    python merge_cross_page.py --layout-json layout.json --pdf input.pdf
"""

import base64
import hashlib
import json
import os
import re
import sys
import logging
import subprocess
from io import BytesIO
from pathlib import Path
from typing import List, Dict, Optional, Tuple, Set

from PIL import Image

try:
    from loguru import logger as loguru_logger
except Exception:
    loguru_logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
try:
    from mineru.utils.enum_class import BlockType
except Exception:
    class BlockType:
        TITLE = 'title'
        TEXT = 'text'
        REF_TEXT = 'ref_text'
        LIST = 'list'
        INDEX = 'index'
        INTERLINE_EQUATION = 'interline_equation'
        IMAGE_BODY = 'image_body'
        IMAGE_CAPTION = 'image_caption'
        IMAGE_FOOTNOTE = 'image_footnote'
        TABLE_BODY = 'table_body'
        TABLE_CAPTION = 'table_caption'
        TABLE_FOOTNOTE = 'table_footnote'
        CODE_BODY = 'code_body'
        CODE_CAPTION = 'code_caption'

try:
    from merge_optimized import _TYPE_COLOR_MAP, _TYPE_COLOR_MAP_STR
except Exception:
    _TYPE_COLOR_MAP = {
        BlockType.TITLE:              (102, 102, 255),
        BlockType.TEXT:               (153,   0,  76),
        BlockType.REF_TEXT:           (153,   0,  76),
        BlockType.LIST:               (40, 169,  92),
        BlockType.INDEX:              (40, 169,  92),
        BlockType.INTERLINE_EQUATION: (0, 255,   0),
        BlockType.IMAGE_BODY:         (153, 255,  51),
        BlockType.IMAGE_CAPTION:      (102, 178, 255),
        BlockType.IMAGE_FOOTNOTE:     (255, 178, 102),
        BlockType.TABLE_BODY:         (204, 204,   0),
        BlockType.TABLE_CAPTION:      (255, 255, 102),
        BlockType.TABLE_FOOTNOTE:     (229, 255, 204),
        BlockType.CODE_BODY:          (102,   0, 204),
        BlockType.CODE_CAPTION:       (204, 153, 255),
    }
    _TYPE_COLOR_MAP_STR = {str(k): v for k, v in _TYPE_COLOR_MAP.items()}


# ══════════════════════════════════════════════════════════════
#  配置
# ══════════════════════════════════════════════════════════════

# VLM 配置 (优先读取环境变量)
VLM_BASE_URL = os.environ.get("VLM_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
VLM_MODEL = os.environ.get("VLM_MODEL", "doubao-seed-2-0-lite-260428")
VLM_API_KEY = os.environ.get("VLM_API_KEY", "")


# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════

def _to_type_str(block_type) -> str:
    """将 BlockType 枚举或字符串统一为小写字符串。"""
    if hasattr(block_type, 'value'):
        return block_type.value
    return str(block_type).lower()


def _compute_merged_bbox(bbox_list: list) -> list:
    """计算多个 bbox 的并集 [x0_min, y0_min, x1_max, y1_max]。"""
    if not bbox_list:
        return [0, 0, 0, 0]
    return [
        min(b[0] for b in bbox_list),
        min(b[1] for b in bbox_list),
        max(b[2] for b in bbox_list),
        max(b[3] for b in bbox_list),
    ]


def _hash_crop_name(page_idx: int, bbox: list, ext: str = '.jpg') -> str:
    """根据 sha256 生成裁剪图文件名。"""
    raw = f"{page_idx}_{bbox[0]}_{bbox[1]}_{bbox[2]}_{bbox[3]}"
    h = hashlib.sha256(raw.encode('utf-8')).hexdigest()
    return f"{h}{ext}"


def _is_figure_table_type(blk_type: str) -> bool:
    """判断是否为图表/表格相关类型。"""
    t = _to_type_str(blk_type)
    return 'table' in t or 'image' in t


def _is_body_type(blk_type: str) -> bool:
    """判断是否为 body 类型。"""
    t = _to_type_str(blk_type)
    return t.endswith('_body')


def _has_cross_page_role(blk: dict) -> bool:
    """判断 block 是否已经参与跨页合并。"""
    return bool(
        blk.get('cross_page_merged')
        or blk.get('merged_from_cross')
        or blk.get('belong_to_cross')
    )


# ══════════════════════════════════════════════════════════════
#  页面序列检测
# ══════════════════════════════════════════════════════════════

def _page_has_figure_or_table(page_blocks: list) -> bool:
    """检测页面是否包含图表/表格块 (body 类型)。"""
    for blk in page_blocks:
        if _has_cross_page_role(blk):
            continue
        blk_type = _to_type_str(blk.get('type', ''))
        if _is_figure_table_type(blk_type) and _is_body_type(blk_type):
            return True
    return False


def _get_figure_table_body_indices(page_blocks: list) -> list:
    """获取页面中所有图表/表格 body 块的 detailed_index。"""
    indices = []
    for blk in page_blocks:
        if _has_cross_page_role(blk):
            continue
        blk_type = _to_type_str(blk.get('type', ''))
        if _is_figure_table_type(blk_type) and _is_body_type(blk_type):
            didx = blk.get('detailed_index')
            if didx is not None:
                indices.append(didx)
    return indices


def find_figure_page_groups(layout_json: list) -> List[List[int]]:
    """找出所有包含图表/表格的页面，按连续性分组，每组扩展 +1 下一页。

    逻辑：
      1. 找出所有包含 figure/table body 的页面
      2. 按连续性分组（连续的页面归为一组）
      3. 每组扩展 +1 页（下一页），防止图表+文本割裂
      4. 单页图表也处理（该页 + 下一页）

    Returns:
        [[page_idx, ...], ...]  每组至少包含 1 个图表页 + 可能的下一页
    """
    all_pages = [p['page_idx'] for p in layout_json]
    max_page = max(all_pages) if all_pages else -1

    # 找出所有有图表的页面
    figure_pages = set()
    for page in layout_json:
        page_idx = page['page_idx']
        if _page_has_figure_or_table(page.get('para_blocks', [])):
            figure_pages.add(page_idx)

    if not figure_pages:
        return []

    # 按连续性分组
    sorted_fig = sorted(figure_pages)
    groups = []
    current_group = [sorted_fig[0]]

    for i in range(1, len(sorted_fig)):
        if sorted_fig[i] == sorted_fig[i - 1] + 1:
            current_group.append(sorted_fig[i])
        else:
            groups.append(current_group)
            current_group = [sorted_fig[i]]
    groups.append(current_group)

    # 每组扩展 +1 下一页
    result = []
    for group in groups:
        last_page = group[-1]
        next_page = last_page + 1
        if next_page <= max_page and next_page not in figure_pages:
            group.append(next_page)
        result.append(group)

    return result


# ══════════════════════════════════════════════════════════════
#  标注图绘制 (多页拼接)
# ══════════════════════════════════════════════════════════════

def _get_block_color(blk_type) -> tuple:
    """获取块类型对应的颜色，与 merge_optimized.py 的配色方案一致。"""
    color = _TYPE_COLOR_MAP.get(blk_type)
    if color is None:
        color = _TYPE_COLOR_MAP_STR.get(_to_type_str(blk_type), (255, 0, 0))
    return color


def _convert_bbox_to_pixel(bbox: list, page_size: list, img_size: tuple) -> list:
    """将 PDF 坐标系的 bbox 转换为像素坐标系。"""
    pdf_w, pdf_h = page_size
    img_w, img_h = img_size
    if pdf_w <= 0 or pdf_h <= 0:
        return bbox
    scale_x = img_w / pdf_w
    scale_y = img_h / pdf_h
    x0, y0, x1, y1 = bbox
    return [x0 * scale_x, y0 * scale_y, x1 * scale_x, y1 * scale_y]


def draw_cross_page_annotation(page_image, page_blocks: list,
                               page_size: list,
                               page_label: str = '', line_width: int = 3,
                               font_size: int = 28,
                               mode: str = 'confirmed',
                               skip_cross_merged: bool = False) -> Image.Image:
    """在页面图像上绘制标注框，绘图逻辑与 export_layout_pdf 一致。

    Args:
        mode:
          'confirmed' → 只画合并后 bbox + 独立块，跳过 belong_to 子块，标注 confirmed_index
          'detailed'  → 画所有原始子块 (跳过 merged_from 块)，标注 detailed_index

    底部居中用红色字体标注页码。
    """
    from PIL import Image, ImageDraw, ImageFont

    img = page_image.copy().convert('RGBA')
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    font = None
    for fp in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
               '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf']:
        try:
            font = ImageFont.truetype(fp, font_size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    draw = ImageDraw.Draw(img)

    # 页面标签 (底部居中，红色，白底衬托)
    if page_label:
        if hasattr(draw, 'textbbox'):
            tw = draw.textbbox((0, 0), page_label, font=font)
            text_w = tw[2] - tw[0]
        else:
            text_w = len(page_label) * font_size * 0.6
        lx = (img.width - text_w) // 2
        ly = img.height - font_size - 10
        draw.rectangle([lx - 4, ly - 2, lx + text_w + 4, ly + font_size + 2],
                       fill=(255, 255, 255))
        draw.text((lx, ly), page_label, fill=(255, 0, 0), font=font)

    # 与 export_layout_pdf 的 confirmed/detailed 模式对齐
    for blk in page_blocks:
        is_child = 'belong_to' in blk
        is_cross_child = 'belong_to_cross' in blk
        is_merged = 'merged_from' in blk

        # 模式过滤
        if mode == 'confirmed' and (is_child or is_cross_child):
            continue
        if mode == 'confirmed' and skip_cross_merged and _has_cross_page_role(blk):
            continue
        if mode == 'detailed' and is_merged:
            continue

        bbox = blk.get('bbox', [])
        if not bbox or len(bbox) < 4:
            continue

        color = _get_block_color(blk.get('type', ''))
        pixel_bbox = _convert_bbox_to_pixel(bbox, page_size, img.size)
        x0, y0, x1, y1 = [int(v) for v in pixel_bbox]

        # 半透明填充 (alpha=0.3，与 export_layout_pdf 一致)
        overlay_draw.rectangle([x0, y0, x1, y1], fill=(*color, 76))
        # 边框
        for i in range(line_width):
            draw.rectangle([x0 - i, y0 - i, x1 + i, y1 + i], outline=color)

        # 编号 (右上角)
        if mode == 'confirmed':
            idx_val = blk.get('confirmed_index')
            label = str(idx_val) if idx_val is not None else ''
        else:
            idx_val = blk.get('detailed_index')
            label = str(idx_val) if idx_val is not None else ''

        if label:
            draw.text((x1 + 4, y0 - 2), label, fill=(255, 0, 0), font=font)

    img = Image.alpha_composite(img, overlay).convert('RGB')
    return img


def annotate_page_images(page_images: list, page_layouts: list,
                         page_labels: list = None,
                         mode: str = 'confirmed',
                         skip_cross_merged: bool = False) -> list:
    """为每页图像生成标注图，返回标注图列表 (不做拼接)。

    绘图逻辑与 export_layout_pdf 对齐。

    Args:
        page_images: 页面图像列表
        page_layouts: 页面布局数据列表
        page_labels: 页面标签列表 (显示在底部居中，红色)
        mode: 'confirmed' / 'detailed'

    Returns:
        标注后的页面图像列表
    """
    annotated_images = []
    for i, (img, layout) in enumerate(zip(page_images, page_layouts)):
        page_size = layout.get('page_size', [0, 0])
        label = page_labels[i] if page_labels else f"P{layout.get('page_idx', i)}"

        annotated = draw_cross_page_annotation(
            img, layout.get('para_blocks', []), page_size, label,
            mode=mode, skip_cross_merged=skip_cross_merged)
        annotated_images.append(annotated)

    return annotated_images


# ══════════════════════════════════════════════════════════════
#  VLM 调用
# ══════════════════════════════════════════════════════════════

def _image_to_base64(img, fmt: str = 'JPEG') -> str:
    buf = BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def build_cross_page_vlm_prompt(block_infos_by_page: Dict[int, list],
                                page_sequence: List[int]) -> str:
    """构建跨页合并的 VLM prompt。

    Args:
        block_infos_by_page: {page_idx: [block_info, ...]}
        page_sequence: 页面序列
    """
    block_desc_lines = []
    for page_idx in page_sequence:
        infos = block_infos_by_page.get(page_idx, [])
        if not infos:
            continue
        block_desc_lines.append(f"  --- Page {page_idx} ---")
        for info in infos:
            desc = f"    index={info['index']}, type={info['type']}"
            if info.get('text'):
                desc += f', text="{info["text"][:80].replace(chr(10), " ")}"'
            if info.get('is_merged'):
                desc += ' [already merged]'
            block_desc_lines.append(desc)

    block_desc = '\n'.join(block_desc_lines)
    pages_str = ', '.join(f'Page {p}' for p in page_sequence)

    return f"""你是一名文档布局分析专家。我向你展示了 {len(page_sequence)} 个连续文档页面 ({pages_str})，每张图片对应一页。每页底部居中用红色字体标注了页码（如 "Page 30"），页面上用检测框标注了所有布局区块，每个区块的右上角标注了编号。

**阅读顺序**：请严格按页码从小到大的顺序逐页阅读图片。重点关注相邻两页交界处（上一页底部 ↔ 下一页顶部）的元素关系。

重要: "page" 字段必须使用图片底部红色标注的实际页码（如 30, 31），而不是序号（0, 1）。
"index" 字段必须使用区块右上角标注的编号。

所有检测到的区块（按页码顺序列出）:
{block_desc}

任务:
1. 逐页检查，判断是否有图表/表格被分页截断，需要跨页合并。
2. **需要合并的典型情况**（逐页对照图片仔细检查，不要遗漏）：
   a. 表格跨页：上一页表格末尾与下一页表格开头列结构一致，属于同一张表
   b. 图片/图表跨页：同一张图被分页切割
   c. 图注/脚注分离：图表在上一页，其图注（如"图1: XXX"）或脚注（如"注："）在下一页开头
   d. 标题与内容分离：表格标题在上一页末尾，表格体在下一页开头
   e. 表格续行：上一页表格最后一行在下一页顶部继续
   f. 下一页顶部的 text 类型区块是上一页表格/图像的注释或续行内容
3. **只合并图表/表格类区块**（image_body, table_body 及其 caption/footnote）。**例外**：如果下一页顶部的 text/list 类型区块在内容和语义上是上一页表格或图像的跨页延续（例如表格续行、"注："开头的注释文本紧跟跨页表格），也应将其纳入合并组。
4. **宁多勿漏**：如果不确定某个区块是否属于跨页分割，倾向于合并。漏检比误合并更严重。
5. 返回 JSON 对象:
   {{
     "has_cross_page_merge": true/false,
     "merge_groups": [
       {{
         "blocks": [
           {{"page": 实际页码1, "index": 编号1}},
           {{"page": 实际页码2, "index": 编号2}},
           ...
         ]
       }},
       ...
     ]
   }}
6. 若无需合并: {{"has_cross_page_merge": false, "merge_groups": []}}
7. 每个 merge_group 中的块将被合并为一个元素。"page" 必须是图片底部红色标注的实际页码，"index" 必须是区块右上角标注的编号。
8. 已标记 [already merged] 的块是页内已合并的块，如需跨页合并也可以包含它们。
9. 只返回 JSON，不要添加解释文字。

输出:"""


def call_vlm_for_cross_page(images: list, block_infos_by_page: Dict[int, list],
                            page_sequence: List[int],
                            base_url: str = VLM_BASE_URL,
                            model_name: str = VLM_MODEL,
                            api_key: str = VLM_API_KEY,
                            temperature: float = 0.1,
                            max_tokens: int = 2048,
                            timeout: int = 120) -> Optional[dict]:
    """调用 VLM 检测跨页合并 (多图上传)。

    Args:
        images: 标注后的页面图像列表 (与 page_sequence 一一对应)
        block_infos_by_page: {page_idx: [block_info, ...]}
        page_sequence: 页面序列
    """
    import requests

    prompt = build_cross_page_vlm_prompt(block_infos_by_page, page_sequence)

    # 构建多图 content: 每页一张图，最后附上文本 prompt
    content = []
    for img in images:
        img_b64 = _image_to_base64(img)
        content.append({
            'type': 'image_url',
            'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'},
        })
    content.append({
        'type': 'text',
        'text': prompt,
    })

    headers = {
        'Authorization': f'Bearer {api_key}',
        'Content-Type': 'application/json',
    }
    payload = {
        'model': model_name,
        'messages': [
            {
                'role': 'user',
                'content': content,
            }
        ],
        'temperature': temperature,
        'max_tokens': max_tokens,
    }

    try:
        url = f'{base_url}/chat/completions'
        loguru_logger.debug(f'调用 VLM (跨页): {url}, model={model_name}')
        resp = requests.post(url, json=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()

        result = resp.json()
        content = result['choices'][0]['message']['content']

        json_match = re.search(r'\{[\s\S]*\}', content)
        if json_match:
            parsed = json.loads(json_match.group())
            loguru_logger.info(
                f'VLM 跨页返回: has_cross_page_merge={parsed.get("has_cross_page_merge")}, '
                f'merge_groups数量={len(parsed.get("merge_groups", []))}')
            print(f'VLM 跨页返回 JSON: {json.dumps(parsed, indent=2)}')
            return parsed
        else:
            loguru_logger.warning(f'VLM 返回无法解析为 JSON: {content[:200]}')
            return None

    except Exception as e:
        loguru_logger.error(f'VLM 跨页调用失败: {e}')
        return None


# ══════════════════════════════════════════════════════════════
#  跨页合并执行
# ══════════════════════════════════════════════════════════════

def _crop_from_pdf_page(page_image, bbox_pixel: list, save_path: str):
    """从页面图像中按像素 bbox 裁剪并保存。"""
    x0, y0, x1, y1 = [int(v) for v in bbox_pixel]
    w, h = page_image.size
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(w, x1), min(h, y1)
    if x1 <= x0 or y1 <= y0:
        return
    cropped = page_image.crop((x0, y0, x1, y1))
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    cropped.save(save_path, quality=95)


def _split_image_paths(value) -> List[str]:
    """拆分逗号分隔的图像路径，保持顺序并去重。"""
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        raw_paths = []
        for item in value:
            raw_paths.extend(_split_image_paths(item))
    else:
        raw_paths = [p.strip() for p in str(value).split(',') if p.strip()]

    seen = set()
    paths = []
    for p in raw_paths:
        if p in seen:
            continue
        seen.add(p)
        paths.append(p)
    return paths


def _extend_unique(target: list, values: list):
    """按顺序追加未出现过的值。"""
    seen = set(target)
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        target.append(value)


def _join_unique_text(parts: list) -> str:
    """拼接文本并去掉完全重复的片段。"""
    seen = set()
    result = []
    for part in parts:
        text = str(part or '').strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return '\n'.join(result)


def _resolve_image_path(img_ref: str, confirmed_images_dir: str) -> Optional[str]:
    """将 layout 中的相对图像路径解析为本地文件路径。"""
    if not img_ref:
        return None
    if os.path.isabs(img_ref) and os.path.isfile(img_ref):
        return img_ref

    layout_dir = os.path.dirname(os.path.abspath(confirmed_images_dir))
    candidates = [
        os.path.join(layout_dir, img_ref),
        os.path.join(confirmed_images_dir, os.path.basename(img_ref)),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _crop_block_to_confirmed(
    p_idx: int,
    blk: dict,
    page_idx_to_layout: Dict[int, dict],
    pdf_images: list,
    confirmed_images_dir: str,
    confirmed_img_prefix: str,
) -> Optional[str]:
    """裁剪一个 block 的 bbox 到 confirmed_images，返回相对路径。"""
    bbox = blk.get('bbox', [])
    if not bbox or len(bbox) < 4:
        return None
    if p_idx < 0 or p_idx >= len(pdf_images):
        return None

    page = page_idx_to_layout.get(p_idx)
    page_size = page.get('page_size', [0, 0]) if page else [0, 0]
    if not page_size or page_size[0] <= 0 or page_size[1] <= 0:
        return None

    page_image = pdf_images[p_idx]
    scale_x = page_image.width / page_size[0]
    scale_y = page_image.height / page_size[1]
    pixel_bbox = [
        bbox[0] * scale_x, bbox[1] * scale_y,
        bbox[2] * scale_x, bbox[3] * scale_y,
    ]
    crop_name = _hash_crop_name(p_idx, bbox)
    crop_path = os.path.join(confirmed_images_dir, crop_name)
    _crop_from_pdf_page(page_image, pixel_bbox, crop_path)
    return f'{confirmed_img_prefix}/{crop_name}'


def _concat_images_vertical(
    img_refs: List[str],
    confirmed_images_dir: str,
    confirmed_img_prefix: str,
    merge_key: str,
) -> Optional[str]:
    """将跨页元素图像垂直拼接后保存，返回 confirmed_images 下的相对路径。"""
    resolved = []
    resolved_refs = []
    for ref in img_refs:
        full_path = _resolve_image_path(ref, confirmed_images_dir)
        if not full_path:
            loguru_logger.warning(f'跨页拼接图像不存在，已跳过: {ref}')
            continue
        if full_path in resolved:
            continue
        resolved.append(full_path)
        resolved_refs.append(ref)

    if not resolved:
        return None

    if len(resolved) == 1:
        # 只有一个可用图像时不重复生成文件，直接沿用原路径。
        return resolved_refs[0]

    images = []
    for full_path in resolved:
        try:
            with Image.open(full_path) as img:
                images.append(img.convert('RGB'))
        except Exception as exc:
            loguru_logger.warning(f'跨页拼接图像读取失败，已跳过: {full_path}, error={exc}')

    if not images:
        return None

    max_w = max(img.width for img in images)
    total_h = sum(img.height for img in images)
    merged = Image.new('RGB', (max_w, total_h), (255, 255, 255))

    y_offset = 0
    for img in images:
        merged.paste(img, (0, y_offset))
        y_offset += img.height

    os.makedirs(confirmed_images_dir, exist_ok=True)
    digest = hashlib.sha256(
        (merge_key + '|' + '|'.join(img_refs)).encode('utf-8')
    ).hexdigest()[:16]
    filename = f'cross_page_concat_{digest}.jpg'
    out_path = os.path.join(confirmed_images_dir, filename)
    merged.save(out_path, quality=95)
    return f'{confirmed_img_prefix}/{filename}'


_TEXT_TYPE_STRS = {'text', 'list', 'index', 'ref_text'}


def _img_path_to_md(img_path: str, out_path: str = '') -> str:
    if not img_path:
        return ''
    paths = _split_image_paths(img_path)
    if not paths:
        return ''
    if len(paths) == 1:
        return f'![]({paths[0]})'
    if out_path:
        concat_ref = _concat_images_vertical(
            paths,
            os.path.join(out_path, 'confirmed_images'),
            'confirmed_images',
            'md_export',
        )
        if concat_ref:
            return f'![]({concat_ref})'
    return f'![]({paths[0]})'


def export_cross_page_markdown(layout_json: list, out_path: str,
                               filename: str = 'layout_cross_confirmed.md') -> str:
    """导出 confirmed Markdown，跳过页内/跨页归属子块。"""
    all_lines = []

    for page_data in layout_json:
        for blk in page_data.get('para_blocks', []):
            if 'belong_to' in blk or 'belong_to_cross' in blk:
                continue

            blk_type = _to_type_str(blk.get('type', ''))
            text = str(blk.get('text', '') or '').strip()
            img_path = str(blk.get('confirmed_img_path', '') or blk.get('img_path', '') or '').strip()
            md_line = ''

            if blk_type == 'title':
                level = blk.get('title_level', 1) or 1
                md_line = f'{"#" * int(level)} {text}' if text else ''
            elif blk_type in _TEXT_TYPE_STRS:
                md_line = text
            elif blk_type == 'interline_equation':
                md_line = text or _img_path_to_md(img_path, out_path)
            elif blk_type in ('image_body', 'table_body'):
                if img_path:
                    md_line = _img_path_to_md(img_path, out_path)
                if blk.get('cross_page_merged') and text:
                    md_line = f'{md_line}\n\n{text}' if md_line else text
                else:
                    html = str(blk.get('html', '') or '').strip()
                    if blk_type == 'table_body' and html and not md_line:
                        md_line = f'\n{html}\n'
                    caption = str(blk.get('caption_text', '') or '').strip()
                    footnote = str(blk.get('footnote_text', '') or '').strip()
                    if caption:
                        md_line = f'{md_line}\n\n{caption}' if md_line else caption
                    if footnote:
                        md_line = f'{md_line}\n\n{footnote}' if md_line else footnote
            elif blk_type == 'code_body':
                md_line = f'```\n{text}\n```' if text else ''
                caption = str(blk.get('caption_text', '') or '').strip()
                if caption:
                    md_line = f'{md_line}\n\n{caption}' if md_line else caption

            if md_line.strip():
                all_lines.append(md_line.strip())

    markdown_text = '\n\n'.join(all_lines)
    os.makedirs(out_path, exist_ok=True)
    md_path = os.path.join(out_path, filename)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(markdown_text)
    loguru_logger.info(f'跨页 Markdown saved to {md_path}')
    return markdown_text


def _page_num_from_image_path(path: Path) -> int:
    match = re.search(r'page[_-]?(\d+)', path.stem)
    if not match:
        return 10 ** 9
    return int(match.group(1))


def load_pdf_page_images(pdf_path: str, auto_dir: str, dpi: int = 300) -> List[Image.Image]:
    """加载 PDF 页面图像。优先使用 auto 同级 pages/page_*.jpg，缺失时尝试渲染 PDF。"""
    auto_path = Path(auto_dir)
    candidate_dirs = [
        auto_path.parent / 'pages',
        auto_path / 'pages',
    ]

    for pages_dir in candidate_dirs:
        if not pages_dir.is_dir():
            continue
        page_files = sorted(
            list(pages_dir.glob('page_*.jpg')) + list(pages_dir.glob('page_*.png')),
            key=_page_num_from_image_path,
        )
        if page_files:
            images = [Image.open(p).convert('RGB') for p in page_files]
            loguru_logger.info(f'使用已有页面图像: {pages_dir}, pages={len(images)}')
            return images

    try:
        import pdf2image
        images = pdf2image.convert_from_path(pdf_path, dpi=dpi)
        loguru_logger.info(f'使用 pdf2image 渲染 PDF: pages={len(images)}')
        return [img.convert('RGB') for img in images]
    except Exception as exc:
        loguru_logger.warning(f'pdf2image 不可用，尝试 pdftoppm: {exc}')

    render_dir = auto_path / 'pages'
    try:
        render_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        digest = hashlib.sha256(os.path.abspath(pdf_path).encode('utf-8')).hexdigest()[:12]
        render_dir = Path('/tmp') / f'cross_page_pdf_pages_{digest}'
        render_dir.mkdir(parents=True, exist_ok=True)
    prefix = render_dir / 'page'
    cmd = ['pdftoppm', '-jpeg', '-r', str(dpi), pdf_path, str(prefix)]
    try:
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except Exception as exc:
        raise RuntimeError(f'无法渲染 PDF 页面图像: {exc}') from exc

    page_files = sorted(render_dir.glob('page-*.jpg'), key=_page_num_from_image_path)
    images = [Image.open(p).convert('RGB') for p in page_files]
    loguru_logger.info(f'使用 pdftoppm 渲染 PDF: pages={len(images)}')
    return images


def export_cross_page_pdf(layout_json: list, pdf_images: list, out_path: str,
                          filename: str = 'layout_cross_confirmed.pdf'):
    """导出 confirmed 标注 PDF。使用页面图像栅格化输出，避免依赖 pypdf/reportlab。"""
    page_idx_to_layout = {p.get('page_idx'): p for p in layout_json}
    annotated_images = []

    for page_idx, page_image in enumerate(pdf_images):
        page_layout = page_idx_to_layout.get(page_idx, {})
        annotated = draw_cross_page_annotation(
            page_image,
            page_layout.get('para_blocks', []),
            page_layout.get('page_size', [0, 0]),
            page_label=f'Page {page_idx}',
            mode='confirmed',
            skip_cross_merged=False,
        ).convert('RGB')
        annotated_images.append(annotated)

    if not annotated_images:
        raise ValueError('没有可导出的页面图像')

    os.makedirs(out_path, exist_ok=True)
    out_file = os.path.join(out_path, filename)
    first, rest = annotated_images[0], annotated_images[1:]
    first.save(out_file, 'PDF', save_all=True, append_images=rest, resolution=100.0)
    loguru_logger.info(f'跨页标注 PDF saved to {out_file}')


def execute_cross_page_merge(
    layout_json: list,
    merge_groups: List[dict],
    page_sequence: List[int],
    pdf_images: list,
    confirmed_images_dir: str,
    confirmed_img_prefix: str = 'confirmed_images',
) -> list:
    """执行跨页元素合并。

    策略:
      1. 不新增/删除原始块，将跨页合并信息写入最靠前的参与块
      2. 参与块的图像在 confirmed_images 中垂直拼接，作为主块 confirmed_img_path
      3. 文本统一拼接到主块 text 字段，同时保留 caption_text/footnote_text
      4. 后续参与块保留在原页面，添加 belong_to_cross 指向主块

    Args:
        layout_json: 完整的布局 JSON (已过页内合并)
        merge_groups: VLM 返回的合并分组 (index 为 confirmed_index)
        page_sequence: 页面序列
        pdf_images: PDF 页面图像列表
        confirmed_images_dir: 确认图像输出目录
        confirmed_img_prefix: 确认图像路径前缀

    Returns:
        更新后的布局 JSON
    """
    if not merge_groups:
        return layout_json

    os.makedirs(confirmed_images_dir, exist_ok=True)

    # 快速查找结构
    page_idx_to_layout = {p['page_idx']: p for p in layout_json}

    for group in merge_groups:
        blocks_spec = group.get('blocks', [])
        if len(blocks_spec) < 2:
            continue

        # 兜底: 如果 VLM 返回的是序列内相对索引 (0, 1, ...) 而非实际页码，
        # 自动映射回实际页码
        for spec in blocks_spec:
            p = spec['page']
            if p not in page_idx_to_layout and 0 <= p < len(page_sequence):
                spec['page'] = page_sequence[p]
                loguru_logger.debug(f'VLM 返回相对索引 {p} → 映射为实际页码 {page_sequence[p]}')

        loguru_logger.info(f'跨页合并组: {blocks_spec}')

        # 每次循环重建 confirmed_index → block 映射
        cidx_to_block: Dict[Tuple[int, int], dict] = {}
        for page_idx in page_sequence:
            page = page_idx_to_layout.get(page_idx)
            if not page:
                continue
            for blk in page.get('para_blocks', []):
                cidx = blk.get('confirmed_index')
                if cidx is not None:
                    cidx_to_block[(page_idx, cidx)] = blk

        # 获取参与合并的块 (用 confirmed_index 查找)
        group_blocks = []
        for spec in blocks_spec:
            p_idx = spec['page']
            c_idx = spec['index']
            blk = cidx_to_block.get((p_idx, c_idx))
            if blk:
                group_blocks.append((p_idx, blk))
            else:
                loguru_logger.warning(
                    f'未找到块: page={p_idx}, confirmed_index={c_idx}')

        if not group_blocks:
            continue

        if any(_has_cross_page_role(blk) for _, blk in group_blocks):
            loguru_logger.info(f'合并组包含已跨页处理元素，已跳过: {blocks_spec}')
            continue

        if len({p for p, _ in group_blocks}) < 2:
            loguru_logger.info(f'合并组不跨页，已跳过: {blocks_spec}')
            continue

        # 按页面与页内 confirmed_index 排序，第一个块作为主块。
        page_order = {page_idx: order for order, page_idx in enumerate(page_sequence)}

        def _block_sort_key(item):
            p_idx, blk = item
            cidx = blk.get('confirmed_index')
            didx = blk.get('detailed_index')
            bbox = blk.get('bbox') or [0, 0, 0, 0]
            return (
                page_order.get(p_idx, p_idx),
                cidx if cidx is not None else 10 ** 9,
                didx if didx is not None else 10 ** 9,
                bbox[1] if len(bbox) >= 2 else 0,
            )

        group_blocks.sort(key=_block_sort_key)
        first_page_idx, main_blk = group_blocks[0]
        main_didx = main_blk.get('detailed_index')

        # 确定合并类型
        all_types = {_to_type_str(b.get('type', '')) for _, b in group_blocks}
        has_table = any('table' in t for t in all_types)
        has_image = any('image' in t for t in all_types)
        merged_type = 'table_body' if has_table else 'image_body'
        parent_type = 'table' if has_table else 'image'

        # 收集合并信息
        merged_htmls = []
        merged_img_paths = []
        source_confirmed_img_paths = []
        caption_texts = []
        footnote_texts = []
        all_texts = []
        all_sub_blocks = []
        merged_from_list = []

        for p_idx, blk in group_blocks:
            blk_type = _to_type_str(blk.get('type', ''))
            source_cidx = blk.get('confirmed_index')
            source_didx = blk.get('detailed_index')

            if blk.get('text'):
                all_texts.append(blk['text'])

            # 收集 body 信息
            if 'body' in blk_type:
                if blk.get('html'):
                    merged_htmls.append(blk['html'])

            # 收集 caption/footnote 文本
            ct = blk.get('caption_text', '').strip()
            ft = blk.get('footnote_text', '').strip()
            if ct:
                caption_texts.append(ct)
                all_texts.append(ct)
            if ft:
                footnote_texts.append(ft)
                all_texts.append(ft)

            # 收集 sub_blocks
            all_sub_blocks.extend(blk.get('sub_blocks', []))

            # 优先使用页内合并后的 confirmed 图像；没有则回退原图，再没有则裁剪 bbox。
            original_img_refs = _split_image_paths(blk.get('img_path', ''))
            confirmed_refs = _split_image_paths(blk.get('confirmed_img_path', ''))
            _extend_unique(merged_img_paths, original_img_refs)

            usable_refs = [
                ref for ref in confirmed_refs
                if _resolve_image_path(ref, confirmed_images_dir)
            ]
            if not usable_refs:
                usable_refs = [
                    ref for ref in original_img_refs
                    if _resolve_image_path(ref, confirmed_images_dir)
                ]
            if not usable_refs:
                crop_ref = _crop_block_to_confirmed(
                    p_idx, blk, page_idx_to_layout, pdf_images,
                    confirmed_images_dir, confirmed_img_prefix,
                )
                if crop_ref:
                    usable_refs = [crop_ref]

            _extend_unique(source_confirmed_img_paths, usable_refs)

            # 记录来源
            merged_from_list.append({
                'page': p_idx,
                'index': source_cidx,
                'confirmed_index': source_cidx,
                'detailed_index': source_didx,
                'type': blk_type,
            })

        merge_key = ';'.join(
            f'{item["page"]}:{item.get("confirmed_index")}:{item.get("detailed_index")}'
            for item in merged_from_list
        )
        concat_img_path = _concat_images_vertical(
            source_confirmed_img_paths,
            confirmed_images_dir,
            confirmed_img_prefix,
            merge_key,
        )

        # 信息保留在最靠前的元素中。
        if (has_table or has_image) and 'body' not in _to_type_str(main_blk.get('type', '')):
            main_blk['type'] = merged_type
        main_blk['page_idx'] = first_page_idx
        main_blk['parent_type'] = parent_type
        main_blk['cross_page_merged'] = True
        main_blk['cross_page_body_image_paths'] = source_confirmed_img_paths
        main_blk['merged_from_cross'] = merged_from_list
        main_blk['caption_text'] = _join_unique_text(caption_texts)
        main_blk['footnote_text'] = _join_unique_text(footnote_texts)
        main_blk['text'] = _join_unique_text(all_texts)
        main_blk['sub_blocks'] = all_sub_blocks
        if concat_img_path:
            main_blk['confirmed_img_path'] = concat_img_path
            main_blk['img_path'] = concat_img_path
        elif merged_img_paths:
            main_blk['img_path'] = ','.join(merged_img_paths)
        if merged_htmls:
            main_blk['html'] = _join_unique_text(merged_htmls)

        # 为后续页面的原始块添加 belong_to_cross 标记
        for i, (p_idx, blk) in enumerate(group_blocks):
            if i == 0:
                # 第一个块是主块，不需要 belong_to_cross
                continue
            blk['belong_to_cross'] = {
                'page': first_page_idx,
                'index': None,  # reassign_confirmed_indices 后填充
                'confirmed_index': None,
                'detailed_index': main_didx,
            }
            blk['cross_merge_source'] = {
                'page': p_idx,
                'index': blk.get('confirmed_index'),
                'confirmed_index': blk.get('confirmed_index'),
                'detailed_index': blk.get('detailed_index'),
            }
            blk['confirmed_index'] = None

    return layout_json


# ══════════════════════════════════════════════════════════════
#  Index 管理
# ══════════════════════════════════════════════════════════════

def reassign_detailed_indices(layout_json: list):
    """为所有页面重新分配 detailed_index (页内从0连续)。

    跨页合并默认不新增块，因此该函数主要用于兼容旧 synthetic 数据。
    """
    for page in layout_json:
        for idx, blk in enumerate(page.get('para_blocks', [])):
            blk['detailed_index'] = idx


def reassign_confirmed_indices(layout_json: list):
    """为所有页面重新分配 confirmed_index，并填充 belong_to_cross.index。

    规则:
      - confirmed_index 保持页内编号语义，每页从 0 开始
      - 有 belong_to / belong_to_cross 的块: confirmed_index = None
      - belong_to_cross.index 指向主块所在页的 confirmed_index
    """
    source_to_main_cidx: Dict[Tuple[int, int], int] = {}
    source_to_main_didx: Dict[Tuple[int, int], int] = {}

    # 第一遍: 清理跨页子块，并记录每个来源块应指向的主块 index。
    for page_data in layout_json:
        page_idx = page_data.get('page_idx')
        for blk in page_data.get('para_blocks', []):
            if 'belong_to_cross' in blk:
                blk['confirmed_index'] = None
            if 'merged_from_cross' not in blk:
                continue

            main_cidx = blk.get('confirmed_index')
            main_didx = blk.get('detailed_index')
            if main_cidx is None:
                continue
            for src in blk.get('merged_from_cross', []):
                src_page = src.get('page')
                src_cidx = src.get('confirmed_index', src.get('index'))
                if src_page is None or src_cidx is None:
                    continue
                source_to_main_cidx[(src_page, src_cidx)] = main_cidx
                if main_didx is not None:
                    source_to_main_didx[(src_page, src_cidx)] = main_didx

    # 填充 belong_to_cross.index
    for page_data in layout_json:
        page_idx = page_data.get('page_idx')
        for blk in page_data.get('para_blocks', []):
            if 'belong_to_cross' not in blk:
                continue
            source = blk.get('cross_merge_source') or {}
            source_page = source.get('page', page_idx)
            source_cidx = source.get('confirmed_index', source.get('index'))
            merged_cidx = source_to_main_cidx.get((source_page, source_cidx))
            if merged_cidx is not None:
                blk['belong_to_cross']['index'] = merged_cidx
                blk['belong_to_cross']['confirmed_index'] = merged_cidx
            merged_didx = source_to_main_didx.get((source_page, source_cidx))
            if merged_didx is not None:
                blk['belong_to_cross']['detailed_index'] = merged_didx

    # 清理残留的 _cross_source_blocks (如果有的话)
    for page_data in layout_json:
        for blk in page_data.get('para_blocks', []):
            if '_cross_source_blocks' in blk:
                del blk['_cross_source_blocks']


def _find_block_by_index(page: dict, index_value, prefer_confirmed: bool = True) -> Optional[dict]:
    """按 confirmed_index/detailed_index 查找块，兼容旧 merged_from_cross 字段。"""
    if index_value is None:
        return None
    blocks = page.get('para_blocks', [])
    if prefer_confirmed:
        for blk in blocks:
            if blk.get('confirmed_index') == index_value:
                return blk
    for blk in blocks:
        if blk.get('detailed_index') == index_value:
            return blk
    if not prefer_confirmed:
        for blk in blocks:
            if blk.get('confirmed_index') == index_value:
                return blk
    return None


def normalize_existing_cross_page_merges(
    layout_json: list,
    confirmed_images_dir: str,
    confirmed_img_prefix: str = 'confirmed_images',
) -> list:
    """兼容旧跨页结果，补齐主块/子块字段并生成单个垂直拼接图。"""
    page_idx_to_layout = {p.get('page_idx'): p for p in layout_json}

    for page_data in layout_json:
        page_idx = page_data.get('page_idx')
        for blk in page_data.get('para_blocks', []):
            merged_sources = blk.get('merged_from_cross')
            if not merged_sources:
                continue

            blk['cross_page_merged'] = True

            if _split_image_paths(blk.get('confirmed_img_path', '')):
                merge_key = ';'.join(
                    f'{src.get("page")}:{src.get("confirmed_index", src.get("index"))}:{src.get("detailed_index")}'
                    for src in merged_sources
                )
                concat_ref = _concat_images_vertical(
                    _split_image_paths(blk.get('confirmed_img_path', '')),
                    confirmed_images_dir,
                    confirmed_img_prefix,
                    merge_key,
                )
                if concat_ref:
                    blk['cross_page_body_image_paths'] = _split_image_paths(
                        blk.get('confirmed_img_path', '')
                    )
                    blk['confirmed_img_path'] = concat_ref
                    blk['img_path'] = concat_ref

            main_page = page_idx
            main_confirmed_index = blk.get('confirmed_index')
            main_detailed_index = blk.get('detailed_index')
            if main_confirmed_index is None:
                continue

            normalized_sources = []
            for src in merged_sources:
                src_page = src.get('page')
                src_index = src.get('confirmed_index', src.get('index'))
                src_didx = src.get('detailed_index')
                normalized_sources.append({
                    'page': src_page,
                    'index': src_index,
                    'confirmed_index': src_index,
                    'detailed_index': src_didx,
                    'type': src.get('type', ''),
                })

                if src_page is None or src_index is None:
                    continue
                source_page = page_idx_to_layout.get(src_page)
                if not source_page:
                    continue
                source_blk = _find_block_by_index(source_page, src_index, prefer_confirmed=True)
                if source_blk is None or source_blk is blk:
                    continue
                if source_blk.get('merged_from_cross') and source_blk.get('confirmed_index') is not None:
                    # 该块本身也是另一个跨页主块时，不降级为子块。
                    continue
                source_blk['belong_to_cross'] = {
                    'page': main_page,
                    'index': main_confirmed_index,
                    'confirmed_index': main_confirmed_index,
                    'detailed_index': main_detailed_index,
                }
                source_blk['cross_merge_source'] = {
                    'page': src_page,
                    'index': source_blk.get('confirmed_index'),
                    'confirmed_index': source_blk.get('confirmed_index'),
                    'detailed_index': source_blk.get('detailed_index'),
                }
                source_blk['confirmed_index'] = None

            blk['merged_from_cross'] = normalized_sources

    reassign_confirmed_indices(layout_json)
    return layout_json


# ══════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════

def cross_page_merge(
    layout_json: list,
    pdf_images: list,
    confirmed_images_dir: str,
    confirmed_img_prefix: str = 'confirmed_images',
    base_url: str = VLM_BASE_URL,
    model_name: str = VLM_MODEL,
    api_key: str = VLM_API_KEY,
    debug_dir: str = None,
) -> list:
    """跨页元素合并的主入口。

    调试图像使用 confirmed 模式绘图 (与 export_layout_pdf 一致)。

    Args:
        layout_json: 已过页内合并的布局 JSON
        pdf_images: PDF 页面图像列表
        confirmed_images_dir: 确认图像输出目录
        confirmed_img_prefix: 确认图像路径前缀
        base_url: VLM API 地址
        model_name: VLM 模型名
        api_key: VLM API Key
        debug_dir: 调试图输出目录

    Returns:
        更新后的布局 JSON
    """
    loguru_logger.info('开始跨页元素合并检测...')

    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)

    normalize_existing_cross_page_merges(
        layout_json,
        confirmed_images_dir=confirmed_images_dir,
        confirmed_img_prefix=confirmed_img_prefix,
    )

    # 1. 找出包含图表的页面，按连续性分组，每组 +1 下一页
    sequences = find_figure_page_groups(layout_json)

    if not sequences:
        loguru_logger.info('未发现包含图表的页面，跳过跨页合并')
        normalize_existing_cross_page_merges(
            layout_json,
            confirmed_images_dir=confirmed_images_dir,
            confirmed_img_prefix=confirmed_img_prefix,
        )
        return layout_json

    loguru_logger.info(f'发现 {len(sequences)} 个连续图表页面序列')
    for seq in sequences:
        loguru_logger.info(f'  序列: {seq}')

    # 2. 快速查找结构
    page_idx_to_layout = {p['page_idx']: p for p in layout_json}

    # 3. 对每个序列进行 VLM 检测
    for page_sequence in sequences:
        loguru_logger.info(f'分析页面序列 {page_sequence} 的跨页元素...')

        # 收集每页的块信息 (使用 confirmed_index，与标注图一致)
        block_infos_by_page: Dict[int, list] = {}
        for page_idx in page_sequence:
            page = page_idx_to_layout.get(page_idx)
            if not page:
                continue

            blocks = page.get('para_blocks', [])
            infos = []
            for blk in blocks:
                cidx = blk.get('confirmed_index')
                if cidx is None:
                    continue
                # 跳过子块 (已被页内合并的父块吸收)
                if 'belong_to' in blk:
                    continue
                # 跳过已参与跨页合并的块，避免重复合并。
                if _has_cross_page_role(blk):
                    continue
                info = {
                    'index': cidx,
                    'type': _to_type_str(blk.get('type', '')),
                    'is_merged': 'merged_from' in blk,
                }
                # 优先使用 caption_text / footnote_text，其次是 text
                text = blk.get('caption_text', '') or blk.get('text', '')
                if text:
                    info['text'] = text
                infos.append(info)

            block_infos_by_page[page_idx] = infos

        # 获取页面图像
        page_images = []
        page_layouts = []
        for page_idx in page_sequence:
            if page_idx < len(pdf_images):
                page_images.append(pdf_images[page_idx])
                page_layouts.append(page_idx_to_layout.get(page_idx, {}))

        if not page_images:
            loguru_logger.warning(f'页面序列 {page_sequence} 无对应图像，跳过')
            continue

        # 生成每页标注图 (confirmed 模式，与 export_layout_pdf 对齐)
        page_labels = [f"Page {idx}" for idx in page_sequence]
        annotated_images = annotate_page_images(
            page_images, page_layouts, page_labels,
            mode='confirmed',
            skip_cross_merged=True,
        )

        # 保存调试图像
        if debug_dir:
            seq_str = '_'.join(str(p) for p in page_sequence)
            for ann_img, p_idx in zip(annotated_images, page_sequence):
                debug_path = os.path.join(debug_dir, f'cross_page_seq_{seq_str}_p{p_idx}.jpg')
                ann_img.save(debug_path)
            loguru_logger.debug(f'已保存跨页调试图: {debug_dir}/cross_page_seq_{seq_str}_p*.jpg')

        # 调用 VLM (多图上传)
        vlm_result = call_vlm_for_cross_page(
            annotated_images,
            block_infos_by_page,
            page_sequence,
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
        )

        if vlm_result is None:
            loguru_logger.warning(f'页面序列 {page_sequence}: VLM 返回为空，跳过')
            continue

        has_cross_page = vlm_result.get('has_cross_page_merge', False)
        merge_groups = vlm_result.get('merge_groups', [])

        if not has_cross_page or not merge_groups:
            loguru_logger.info(f'页面序列 {page_sequence}: 无需跨页合并')
            continue

        loguru_logger.info(f'页面序列 {page_sequence}: 发现 {len(merge_groups)} 组跨页合并')

        # 执行合并
        layout_json = execute_cross_page_merge(
            layout_json, merge_groups,
            page_sequence,
            pdf_images, confirmed_images_dir, confirmed_img_prefix,
        )

    # 填充跨页归属关系中的 confirmed_index。
    normalize_existing_cross_page_merges(
        layout_json,
        confirmed_images_dir=confirmed_images_dir,
        confirmed_img_prefix=confirmed_img_prefix,
    )

    loguru_logger.info('跨页元素合并完成')
    return layout_json


# ══════════════════════════════════════════════════════════════
#  与 merge_optimized.py 集成的便捷函数
# ══════════════════════════════════════════════════════════════

def full_merge_pipeline(
    middle_json_path: str,
    pdf_path: str,
    output_dir: str = None,
    # 页内合并参数
    vllm_base_url: str = VLM_BASE_URL,
    vllm_model: str = VLM_MODEL,
    api_key: str = VLM_API_KEY,
    img_prefix: str = 'images',
    confirmed_prefix: str = 'confirmed_images',
    # 跨页合并参数
    cross_page_base_url: str = None,
    cross_page_model: str = None,
    cross_page_api_key: str = None,
    # 调试
    debug_dir: str = None,
    log_level: str = 'INFO',
) -> list:
    """完整流水线: middle.json → 页内合并 → 跨页合并 → 输出。

    Args:
        middle_json_path: middle.json 文件路径
        pdf_path: PDF 文件路径
        output_dir: 输出目录 (默认为 middle.json 同级目录)
        其余参数见各阶段函数

    Returns:
        最终的布局 JSON
    """
    from merge_optimized import extract_layout_json, vlm_correct_layout

    # 配置日志
    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        force=True,
    )

    middle_path = Path(middle_json_path)
    pdf_file = Path(pdf_path)

    if not middle_path.exists():
        raise FileNotFoundError(f'middle.json 不存在: {middle_path}')
    if not pdf_file.exists():
        raise FileNotFoundError(f'PDF 文件不存在: {pdf_file}')

    # 输出目录
    if output_dir:
        out_dir = Path(output_dir)
    else:
        out_dir = middle_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)

    images_dir = str(middle_path.parent / img_prefix)
    confirmed_images_dir = str(out_dir / confirmed_prefix)
    cross_debug_dir = str(out_dir / 'debug_cross_page') if debug_dir is None else debug_dir

    # 加载数据
    with open(middle_path, 'r', encoding='utf-8') as f:
        data = json.load(f)
    pdf_info = data.get('pdf_info', data)
    if not isinstance(pdf_info, list):
        pdf_info = [pdf_info]

    # PDF → 图像
    import pdf2image
    loguru_logger.info(f'PDF 转图像: {pdf_path}')
    pdf_images = pdf2image.convert_from_path(pdf_path, dpi=300)
    loguru_logger.info(f'PDF 图像加载完成: {len(pdf_images)} 页')

    # ── 阶段 1: 页内合并 ──
    loguru_logger.info('=== 阶段 1: 页内合并 ===')
    layout = extract_layout_json(pdf_info, img_buket_path=img_prefix)

    layout = vlm_correct_layout(
        layout_json=layout,
        pdf_images=pdf_images,
        images_dir=images_dir,
        confirmed_images_dir=confirmed_images_dir,
        confirmed_img_prefix=confirmed_prefix,
        vllm_base_url=vllm_base_url,
        vllm_model=vllm_model,
        api_key=api_key,
        debug_dir=str(out_dir / 'debug_vlm_annotated'),
    )

    # 保存页内合并结果
    intra_json_path = out_dir / 'layout_intra_merged.json'
    with open(intra_json_path, 'w', encoding='utf-8') as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    loguru_logger.info(f'页内合并结果已保存: {intra_json_path}')

    # ── 阶段 2: 跨页合并 ──
    loguru_logger.info('=== 阶段 2: 跨页合并 ===')
    cp_base_url = cross_page_base_url or vllm_base_url
    cp_model = cross_page_model or vllm_model
    cp_api_key = cross_page_api_key or api_key

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

    # 保存最终结果
    final_json_path = out_dir / 'layout_final.json'
    with open(final_json_path, 'w', encoding='utf-8') as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    loguru_logger.info(f'最终结果已保存: {final_json_path}')

    return layout


# ══════════════════════════════════════════════════════════════
#  CLI
# ══════════════════════════════════════════════════════════════

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description='跨页元素合并 — 基于页内合并结果，合并跨页分割的图表/表格'
    )

    # ── 输入 (与 merge_optimized.py 一致) ──
    parser.add_argument('input', nargs='?',
                        default='/home/xq/rag/output/304设计（咨询）成品校审管理细则/auto/304设计（咨询）成品校审管理细则_layout.json',
                        help='middle.json 或 *_layout.json 文件路径 (用于推断目录和 PDF)')
    parser.add_argument('--layout-json', default=None,
                        help='页内合并后的 layout JSON；不填则按 input 自动推断')
    parser.add_argument('-o', '--output', default='', help='输出 JSON 文件名')
    parser.add_argument('--out_dir', default=None, help='输出目录 (默认与输入同目录)')
    parser.add_argument('--pdf', default=None, help='PDF 文件路径 (默认自动查找)')
    parser.add_argument('--img-prefix', default='images',
                        help='原始图片路径前缀 (默认 images)')
    parser.add_argument('--confirmed-prefix', default='confirmed_images',
                        help='合并后图片路径前缀 (默认 confirmed_images)')
    parser.add_argument('--no-pdf', action='store_true', help='禁用跨页 confirmed PDF 导出')
    parser.add_argument('--no-md', action='store_true', help='禁用跨页 confirmed Markdown 导出')

    # ── VLM ──
    parser.add_argument('--vlm-base-url', type=str, default=VLM_BASE_URL)
    parser.add_argument('--vlm-model', type=str, default=VLM_MODEL)
    parser.add_argument('--vlm-api-key', type=str, default=VLM_API_KEY)

    # ── 调试 ──
    parser.add_argument('--debug-dir', default=None, help='跨页合并调试图目录')
    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'])

    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        force=True,
    )

    # ══════════════════════════════════════════════════════════
    #  目录解析 (与 merge_optimized.py 相同逻辑)
    # ══════════════════════════════════════════════════════════

    input_path = Path(args.input)
    input_dir = input_path.parent
    input_stem = input_path.stem
    base_name = input_stem.replace('_middle', '').replace('_layout', '')

    out_dir = Path(args.out_dir) if args.out_dir else input_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    confirmed_images_dir = str(out_dir / args.confirmed_prefix)

    # ══════════════════════════════════════════════════════════
    #  查找 layout JSON (页内合并的输出)
    # ══════════════════════════════════════════════════════════

    if args.layout_json:
        layout_json_path = Path(args.layout_json)
    elif input_stem.endswith('_layout'):
        layout_json_path = input_path
    else:
        layout_json_path = out_dir / f'{base_name}_layout.json'
    if not layout_json_path.exists():
        print(f'layout JSON 不存在: {layout_json_path}')
        print('请先运行 merge_optimized.py 完成页内合并')
        sys.exit(1)

    with open(layout_json_path, 'r', encoding='utf-8') as f:
        layout = json.load(f)
    print(f'[1/2] 加载 layout JSON: {layout_json_path} ({len(layout)} 页)')

    # ══════════════════════════════════════════════════════════
    #  查找 PDF (与 merge_optimized.py 相同逻辑)
    # ══════════════════════════════════════════════════════════

    input_pdf_path = args.pdf
    if input_pdf_path is None:
        pdf_candidates = [
            input_dir / f'{base_name}.pdf',
            input_dir / f'{base_name}_origin.pdf',
            input_dir.parent / f'{base_name}.pdf',
            input_dir.parent / f'{base_name}_origin.pdf',
        ]
        for p in [str(path) for path in pdf_candidates]:
            if os.path.exists(p):
                input_pdf_path = p
                break

    if not input_pdf_path or not os.path.isfile(input_pdf_path):
        print('未找到 PDF 文件，请通过 --pdf 指定')
        sys.exit(1)

    print(f'[1/2] PDF 路径: {input_pdf_path}')

    # ══════════════════════════════════════════════════════════
    #  PDF → 图像
    # ══════════════════════════════════════════════════════════

    pdf_images = load_pdf_page_images(input_pdf_path, str(out_dir), dpi=300)
    print(f'      PDF 图像加载完成: {len(pdf_images)} 页')

    # ══════════════════════════════════════════════════════════
    #  跨页合并
    # ══════════════════════════════════════════════════════════

    debug_dir = args.debug_dir or str(out_dir / 'debug_cross_page')

    print(f'[2/2] 开始跨页合并...')
    layout = cross_page_merge(
        layout_json=layout, # 来自layout.json
        pdf_images=pdf_images,  # 来自干净pdf页面图像
        confirmed_images_dir=confirmed_images_dir,
        confirmed_img_prefix=args.confirmed_prefix,
        base_url=args.vlm_base_url,
        model_name=args.vlm_model,
        api_key=args.vlm_api_key,
        debug_dir=debug_dir,
    )

    # ══════════════════════════════════════════════════════════
    #  保存结果
    # ══════════════════════════════════════════════════════════

    json_name = args.output.strip() or f'{base_name}_layout_cross.json'
    json_path = out_dir / json_name
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)

    pdf_out_path = None
    md_out_path = None

    if not args.no_pdf:
        pdf_name = f'{base_name}_layout_cross_confirmed.pdf'
        export_cross_page_pdf(layout, pdf_images, str(out_dir), pdf_name)
        pdf_out_path = out_dir / pdf_name

    if not args.no_md:
        md_name = f'{base_name}_layout_cross_confirmed.md'
        export_cross_page_markdown(layout, str(out_dir), md_name)
        md_out_path = out_dir / md_name

    print(f'\n{"="*60}')
    print('跨页合并完成:')
    print(f'  结果 JSON  → {json_path}')
    if pdf_out_path:
        print(f'  结果 PDF   → {pdf_out_path}')
    if md_out_path:
        print(f'  结果 MD    → {md_out_path}')
    print(f'  调试图像   → {debug_dir}')
    print(f'  确认图像   → {confirmed_images_dir}')


if __name__ == '__main__':
    main()
