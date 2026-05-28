"""
从 MinerU 的 pdf_info (middle.json) 中提取按页分组的布局信息 JSON，
可选地通过 VLM 对 figure/table 分块进行合并纠正，
最终导出为画框 PDF 和/或 Markdown。

完整流水线:
  middle.json
       │
       ▼
  extract_layout_json          → layout JSON (img_path 指向 images/)
       │                          每个 block 拥有 detailed_index (页内从0开始连续)
       │                          caption/footnote 已预合并到 body 块的 sub_blocks 中
       ▼
  vlm_correct_layout (可选)    → 合并后 layout JSON
       │                          - 合并产生的 body 块: 新 bbox + confirmed_img_path + merged_from
       │                          - 被合并的子块: belong_to 指向父块的 confirmed_index
       │                          - 未参与合并的 image/table body: img_path 复制到 confirmed_images/
       │                          - 所有非子块获得连续的 confirmed_index
       ▼
  export_layout_pdf            → 两种模式:
       │                          detailed  = 画所有原始子块 (sub_blocks)，标注 detailed_index
       │                          confirmed = 只画合并后的 bbox (跳过被合并的子块)，标注 confirmed_index
       ▼
  export_layout_markdown       → 两种模式:
                                  detailed  = 原始粒度，图像来自 images/
                                  confirmed = 合并粒度，图像来自 confirmed_images/

索引体系:
  detailed_index   — extract 阶段分配，页内从0开始连续，代表原始粒度的排列顺序。
                     合并产生的 synthetic 块 detailed_index = None。
  confirmed_index  — VLM 合并后分配，仅对非子块（无 belong_to）从0开始连续编号，
                     子块 confirmed_index = None。
                     未经过 VLM 的页面: confirmed_index = detailed_index。
  merged_from      — 列表，存储被合并子块的 detailed_index。
  belong_to        — 标量，存储父合并块的 confirmed_index。

数据结构变化 (相比旧版):
  - caption/footnote 不再作为独立块出现在 para_blocks 中
  - 而是预合并到 body 块的 sub_blocks 列表中
  - body 块的 bbox 已扩展覆盖 caption/footnote 区域
  - _apply_merge_groups 中子块用 belong_to_group (group key) 暂存关联
  - _assign_confirmed_indices 统一解析 belong_to_group → belong_to

用法:
  python merge_optimized.py middle.json --vlm --pdf input.pdf
  python merge_optimized.py middle.json --vlm --pdf input.pdf --export-mode confirmed
"""

import base64
import copy
import hashlib
import json
import os
import re
import shutil
import logging
from io import BytesIO
from typing import List, Dict, Optional, Tuple

try:
    from loguru import logger as loguru_logger
except Exception:
    loguru_logger = logging.getLogger(__name__)

try:
    from mineru.utils.enum_class import BlockType, ContentType
    from mineru.backend.pipeline.pipeline_middle_json_mkcontent import (
        merge_para_with_text,
        get_title_level,
    )
except Exception:
    class BlockType:
        TITLE = 'title'
        TEXT = 'text'
        REF_TEXT = 'ref_text'
        LIST = 'list'
        INDEX = 'index'
        INTERLINE_EQUATION = 'interline_equation'
        IMAGE = 'image'
        IMAGE_BODY = 'image_body'
        IMAGE_CAPTION = 'image_caption'
        IMAGE_FOOTNOTE = 'image_footnote'
        TABLE = 'table'
        TABLE_BODY = 'table_body'
        TABLE_CAPTION = 'table_caption'
        TABLE_FOOTNOTE = 'table_footnote'
        CODE = 'code'
        CODE_BODY = 'code_body'
        CODE_CAPTION = 'code_caption'

    class ContentType:
        IMAGE = 'image'
        TABLE = 'table'

    def merge_para_with_text(block):
        texts = []
        for line in block.get('lines', []):
            for span in line.get('spans', []):
                text = span.get('content', '') or span.get('text', '')
                if text:
                    texts.append(text)
        return ''.join(texts).strip()

    def get_title_level(block):
        level = block.get('level', 1)
        try:
            return int(level)
        except (TypeError, ValueError):
            return 1

std_logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  日志 / 工具
# ══════════════════════════════════════════════════════════════

def _configure_logging(level_name: str = 'INFO', log_file: Optional[str] = None):
    """配置标准 logging，支持控制台和可选文件输出。"""
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    handlers = [logging.StreamHandler()]
    if log_file:
        log_file_abs = os.path.abspath(log_file)
        log_dir = os.path.dirname(log_file_abs)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(log_file_abs, encoding='utf-8'))
    logging.basicConfig(
        level=level,
        format='%(asctime)s | %(levelname)s | %(name)s | %(message)s',
        handlers=handlers,
        force=True,
    )


def _resolve_default_debug_dir(input_json_path: str,
                               default_name: str = 'debug_vlm_annotated') -> str:
    """根据输入 JSON 路径推断默认调试目录。"""
    input_dir = os.path.dirname(os.path.abspath(input_json_path))
    if os.path.basename(input_dir).lower() == 'auto':
        return os.path.join(os.path.dirname(input_dir), default_name)
    return os.path.join(input_dir, default_name)


def export_pdf_pages_with_page_index(
    pdf_path: str,
    auto_dir: Optional[str] = None,
    pages_dir_name: str = 'pages',
    dpi: int = 300,
    font_size: int = 28,
) -> List[str]:
    """导出 PDF 各页到 auto 同级目录 pages/，并在图像左上角写入页码。"""
    import pdf2image
    from PIL import ImageDraw, ImageFont

    pdf_abs = os.path.abspath(pdf_path)
    if not os.path.isfile(pdf_abs):
        raise FileNotFoundError(f'PDF 文件不存在: {pdf_abs}')

    if auto_dir is None:
        pdf_dir = os.path.dirname(pdf_abs)
        if os.path.basename(pdf_dir).lower() == 'auto':
            auto_abs = pdf_dir
        else:
            candidate_auto = os.path.join(pdf_dir, 'auto')
            if os.path.isdir(candidate_auto):
                auto_abs = os.path.abspath(candidate_auto)
            else:
                raise ValueError(
                    f'无法推断 auto 目录: pdf_path={pdf_abs}。请显式传入 auto_dir。'
                )
    else:
        auto_abs = os.path.abspath(auto_dir)
        if not os.path.isdir(auto_abs):
            raise FileNotFoundError(f'auto 目录不存在: {auto_abs}')

    pages_dir = os.path.join(os.path.dirname(auto_abs), pages_dir_name)
    os.makedirs(pages_dir, exist_ok=True)
    std_logger.info('开始导出 PDF 页面图像: pdf=%s, pages_dir=%s, dpi=%d', pdf_abs, pages_dir, dpi)

    images = pdf2image.convert_from_path(
        pdf_abs, dpi=dpi, thread_count=4, poppler_path=None,
        grayscale=False, size=None, paths_only=False,
    )

    font = None
    for fp in [
        '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
        '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf',
    ]:
        try:
            font = ImageFont.truetype(fp, font_size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    saved_paths: List[str] = []
    for page_index, image in enumerate(images):
        img = image.convert('RGB')
        draw = ImageDraw.Draw(img)
        label = str(page_index)
        x, y = 12, 12
        if hasattr(draw, 'textbbox'):
            tx0, ty0, tx1, ty1 = draw.textbbox((x, y), label, font=font)
            pad = 6
            draw.rectangle([tx0 - pad, ty0 - pad, tx1 + pad, ty1 + pad], fill=(255, 255, 255))
        draw.text((x, y), label, fill=(255, 0, 0), font=font)
        out_path = os.path.join(pages_dir, f'page_{page_index}.jpg')
        img.save(out_path, format='JPEG', quality=95)
        saved_paths.append(os.path.abspath(out_path))

    saved_paths.sort(key=lambda p: int(os.path.splitext(os.path.basename(p))[0].split('_')[-1]))
    std_logger.info('PDF 页面导出完成: pages=%d, pages_dir=%s', len(saved_paths), pages_dir)
    return saved_paths


# ══════════════════════════════════════════════════════════════
#  工具函数
# ══════════════════════════════════════════════════════════════

def _to_type_str(block_type) -> str:
    """将 BlockType 枚举或字符串统一转为字符串。"""
    return str(block_type.value) if hasattr(block_type, 'value') else str(block_type)


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


# ══════════════════════════════════════════════════════════════
#  第一阶段: 布局 JSON 提取 (含 caption/footnote 预合并)
# ══════════════════════════════════════════════════════════════

def _build_figure_table_body(
    para_block: dict,
    body_type: str,
    parent_type: str,
    page_idx: int,
    img_buket_path: str,
    extract_body_fn,
) -> dict:
    """从 IMAGE/TABLE/CODE 块中提取 body 块，并将 caption/footnote 预合并到 sub_blocks。

    Args:
        para_block: 原始 para_block (来自 middle.json)
        body_type: body 块类型 (BlockType.IMAGE_BODY / TABLE_BODY / CODE_BODY)
        parent_type: 父类型 (BlockType.IMAGE / TABLE / CODE)
        page_idx: 页码
        img_buket_path: 图片路径前缀
        extract_body_fn: 从 block 中提取 body 特有字段的回调
                         签名: fn(block, body_info) -> None (原地修改 body_info)

    Returns:
        合并后的 body 块 dict，包含 sub_blocks 字段
    """
    caption_type = parent_type.lower() + '_caption'
    footnote_type = parent_type.lower() + '_footnote'

    caption_blocks = []
    footnote_blocks = []
    body_block_info = None

    for block in para_block.get('blocks', []):
        block_type = _to_type_str(block.get('type', ''))

        if block_type == body_type:
            body_info = {
                'type': body_type,
                'bbox': block.get('bbox', para_block.get('bbox', [])),
                'page_idx': page_idx,
                'parent_type': parent_type,
            }
            extract_body_fn(block, body_info)
            body_block_info = body_info

        elif block_type == caption_type:
            caption_blocks.append({
                'type': caption_type,
                'bbox': block.get('bbox', []),
                'text': merge_para_with_text(block),
                'page_idx': page_idx,
                'parent_type': parent_type,
            })

        elif block_type == footnote_type:
            footnote_blocks.append({
                'type': footnote_type,
                'bbox': block.get('bbox', []),
                'text': merge_para_with_text(block),
                'page_idx': page_idx,
                'parent_type': parent_type,
            })

    # 若未找到 body 块，创建一个基础 body
    if body_block_info is None:
        body_block_info = {
            'type': body_type,
            'bbox': para_block.get('bbox', []),
            'page_idx': page_idx,
            'parent_type': parent_type,
        }

    # 合并 caption/footnote 文本到 body
    body_block_info['caption_text'] = '\n'.join(b['text'] for b in caption_blocks)
    body_block_info['footnote_text'] = '\n'.join(b['text'] for b in footnote_blocks)

    # 扩展 body bbox 以覆盖 caption/footnote 区域
    all_bboxes = [body_block_info['bbox']]
    for b in caption_blocks + footnote_blocks:
        if b.get('bbox') and len(b['bbox']) >= 4:
            all_bboxes.append(b['bbox'])
    body_block_info['bbox'] = _compute_merged_bbox(all_bboxes)

    # 存储 sub_blocks 供 detailed 模式使用
    body_block_info['sub_blocks'] = caption_blocks + footnote_blocks

    return body_block_info


def extract_layout_json(pdf_info_dict: list, img_buket_path: str = '') -> list:
    """从 pdf_info 列表中提取按页分组的布局信息。

    每个 block 分配 detailed_index (页内从0开始连续)。
    caption/footnote 已预合并到 body 块的 sub_blocks 中，不再作为独立块出现。
    """
    std_logger.info(
        '开始提取布局 JSON: pages=%d, img_prefix=%s',
        len(pdf_info_dict), img_buket_path,
    )

    result = []

    for page_info in pdf_info_dict:
        paras_of_layout = page_info.get('para_blocks')
        page_idx = page_info.get('page_idx', 0)
        page_size = page_info.get('page_size', [0, 0])

        if not paras_of_layout:
            result.append({
                'page_idx': page_idx,
                'page_size': page_size,
                'para_blocks': [],
            })
            std_logger.debug('页面 %s: 无 para_blocks，输出空页面块', page_idx)
            continue

        page_blocks = []
        # 页面内元素
        for para_block in paras_of_layout:
            para_type = para_block['type']
            bbox = para_block.get('bbox', [])

            # ── 文本 / 列表 / 索引 ──
            if para_type in [BlockType.TEXT, BlockType.LIST, BlockType.INDEX, BlockType.REF_TEXT]:
                page_blocks.append({
                    'type': para_type,
                    'bbox': bbox,
                    'text': merge_para_with_text(para_block),
                    'page_idx': page_idx,
                })

            # ── 标题 ──
            elif para_type == BlockType.TITLE:
                page_blocks.append({
                    'type': para_type,
                    'bbox': bbox,
                    'text': merge_para_with_text(para_block),
                    'title_level': get_title_level(para_block),
                    'page_idx': page_idx,
                })

            # ── 行间公式 ──
            elif para_type == BlockType.INTERLINE_EQUATION:
                if (len(para_block.get('lines', [])) == 0
                        or len(para_block['lines'][0].get('spans', [])) == 0):
                    continue
                span0 = para_block['lines'][0]['spans'][0]
                block_info = {
                    'type': para_type,
                    'bbox': bbox,
                    'text': merge_para_with_text(para_block) if span0.get('content', '') else '',
                    'page_idx': page_idx,
                }
                if span0.get('image_path', ''):
                    block_info['img_path'] = f"{img_buket_path}/{span0['image_path']}"
                page_blocks.append(block_info)

            # ── 图片 ──
            elif para_type == BlockType.IMAGE:
                def _extract_image_body(block, info):
                    img_path = ''
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            if span['type'] == ContentType.IMAGE and span.get('image_path', ''):
                                img_path = f"{img_buket_path}/{span['image_path']}"
                    info['img_path'] = img_path

                body = _build_figure_table_body(
                    para_block, BlockType.IMAGE_BODY, BlockType.IMAGE,
                    page_idx, img_buket_path, _extract_image_body,
                )
                page_blocks.append(body)

            # ── 表格 ──
            elif para_type == BlockType.TABLE:
                def _extract_table_body(block, info):
                    table_html = ''
                    table_img_path = ''
                    for line in block.get('lines', []):
                        for span in line.get('spans', []):
                            if span['type'] == ContentType.TABLE:
                                if span.get('html', ''):
                                    table_html = span['html']
                                if span.get('image_path', ''):
                                    table_img_path = f"{img_buket_path}/{span['image_path']}"
                    if table_html:
                        info['html'] = table_html
                    if table_img_path:
                        info['img_path'] = table_img_path

                body = _build_figure_table_body(
                    para_block, BlockType.TABLE_BODY, BlockType.TABLE,
                    page_idx, img_buket_path, _extract_table_body,
                )
                page_blocks.append(body)

            # ── 代码块 ──
            elif para_type == BlockType.CODE:
                def _extract_code_body(block, info):
                    info['text'] = merge_para_with_text(block)

                body = _build_figure_table_body(
                    para_block, BlockType.CODE_BODY, BlockType.CODE,
                    page_idx, img_buket_path, _extract_code_body,
                )
                page_blocks.append(body)

            else:
                loguru_logger.debug(f"Unknown block type '{para_type}' on page {page_idx}, skipped.")

        # ── 分配 detailed_index (页内从0连续) ──
        for idx, blk in enumerate(page_blocks):
            blk['detailed_index'] = idx

        result.append({
            'page_idx': page_idx,
            'page_size': page_size,
            'para_blocks': page_blocks,
        })
        std_logger.debug('页面 %s: 提取块数=%d', page_idx, len(page_blocks))

    std_logger.info('布局 JSON 提取完成: pages=%d', len(result))
    return result


# ══════════════════════════════════════════════════════════════
#  confirmed_index 分配 (优化版: group key 代替 list position)
# ══════════════════════════════════════════════════════════════

def _assign_confirmed_indices(layout_json: list):
    """为所有页面的 block 分配 confirmed_index。

    规则:
      - 无 belong_to_group / belong_to 的块: 按顺序从 0 开始分配 confirmed_index
      - 有 belong_to 的块 (子块): confirmed_index = None
      - belong_to_group 在此函数中解析为 confirmed_index 后写入 belong_to 并删除

    对于未经过 VLM 合并的页面 (所有块都无 belong_to / merged_from),
    confirmed_index 自然等于 detailed_index。
    """
    for page_data in layout_json:
        blocks = page_data.get('para_blocks', [])

        # 第一遍: 为非子块分配 confirmed_index
        confirmed_counter = 0
        for blk in blocks:
            if 'belong_to_group' in blk:
                blk['confirmed_index'] = None
            elif 'belong_to' in blk:
                # 已有 belong_to (旧格式兼容)
                blk['confirmed_index'] = None
            else:
                blk['confirmed_index'] = confirmed_counter
                confirmed_counter += 1

        # 构建 group_key → confirmed_index 映射
        group_to_cidx: Dict[tuple, int] = {}
        for blk in blocks:
            if 'merged_from' in blk:
                key = tuple(sorted(blk['merged_from']))
                group_to_cidx[key] = blk['confirmed_index']

        # 第二遍: 解析 belong_to_group → belong_to
        for blk in blocks:
            if 'belong_to_group' not in blk:
                continue
            key = blk['belong_to_group']
            blk['belong_to'] = group_to_cidx.get(key)
            del blk['belong_to_group']


# ══════════════════════════════════════════════════════════════
#  第二阶段: VLM 辅助合并纠正
# ══════════════════════════════════════════════════════════════
#
#  索引约定:
#    - VLM 看到的是 detailed_index（标注在画框图上）
#    - VLM 返回的 merge_groups 中的数字是 detailed_index
#    - merged_from 存储 detailed_index 列表
#    - belong_to_group 暂存 group key (sorted tuple)，最后由 _assign_confirmed_indices 替换为 confirmed_index
# ──────────────────────────────────────────────────────────────

_FIGURE_TABLE_TYPES = {
    'image_body', 'image_caption', 'image_footnote',
    'table_body', 'table_caption', 'table_footnote',
}

_VLM_TYPE_COLORS = {
    'image_body':     (153, 255,  51),
    'image_caption':  (102, 178, 255),
    'image_footnote': (255, 178, 102),
    'table_body':     (204, 204,   0),
    'table_caption':  (255, 255, 102),
    'table_footnote': (229, 255, 204),
}
_VLM_DEFAULT_COLOR = (255, 0, 0)


def _get_figure_table_indices(blocks: list) -> list:
    indices = []
    for blk in blocks:
        if _to_type_str(blk.get('type', '')) in _FIGURE_TABLE_TYPES:
            indices.append(blk['detailed_index'])
    return indices


def _get_indices(blocks: list) -> list:
    """获取所有块的 detailed_index（VLM 需要看到全部块以判断合并）。"""
    return [blk['detailed_index'] for blk in blocks if blk.get('detailed_index') is not None]


def _page_has_figure_or_table(blocks: list) -> bool:
    return len(_get_figure_table_indices(blocks)) > 0


# ── 坐标转换 ──

def _convert_bboxes_to_pixel(blocks, page_size, image_size):
    pdf_w, pdf_h = page_size[0], page_size[1]
    img_w, img_h = image_size
    if pdf_w <= 0 or pdf_h <= 0:
        return copy.deepcopy(blocks)
    scale_x, scale_y = img_w / pdf_w, img_h / pdf_h
    new_blocks = copy.deepcopy(blocks)
    for blk in new_blocks:
        bbox = blk.get('bbox', [])
        if bbox and len(bbox) >= 4:
            blk['bbox'] = [bbox[0]*scale_x, bbox[1]*scale_y,
                           bbox[2]*scale_x, bbox[3]*scale_y]
    return new_blocks


# ── 标注图绘制 ──

def _draw_bboxes_on_page(page_image, blocks, target_indices,
                         line_width=3, font_size=28, font_path=None):
    """在页面图像上画框并标注 detailed_index。"""
    from PIL import Image, ImageDraw, ImageFont

    img = page_image.copy().convert('RGBA')
    overlay = Image.new('RGBA', img.size, (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)

    font = None
    if font_path:
        try:
            font = ImageFont.truetype(font_path, font_size)
        except Exception:
            pass
    if font is None:
        for fp in ['/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
                   '/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf']:
            try:
                font = ImageFont.truetype(fp, font_size)
                break
            except Exception:
                continue
    if font is None:
        font = ImageFont.load_default()

    didx_to_blk = {blk['detailed_index']: blk for blk in blocks}

    draw = ImageDraw.Draw(img)
    for didx in target_indices:
        blk = didx_to_blk.get(didx)
        if blk is None:
            continue
        bbox = blk.get('bbox', [])
        if not bbox or len(bbox) < 4:
            continue
        color = _VLM_DEFAULT_COLOR
        x0, y0, x1, y1 = [int(v) for v in bbox]
        overlay_draw.rectangle([x0, y0, x1, y1], fill=(*color, 50))
        for i in range(line_width):
            draw.rectangle([x0-i, y0-i, x1+i, y1+i], outline=color)
        label = str(blk['detailed_index'])
        draw.text((x1 + 4, y0 - 2), label, fill=(255, 0, 0), font=font)

    img = Image.alpha_composite(img, overlay).convert('RGB')
    return img


# ── VLM 调用 ──

def _image_to_base64(img, fmt='JPEG'):
    buf = BytesIO()
    img.save(buf, format=fmt)
    return base64.b64encode(buf.getvalue()).decode('utf-8')


def _build_vlm_prompt(block_infos):
    """构建 VLM prompt。block_infos 中的 index 字段是 detailed_index。"""
    lines = []
    for info in block_infos:
        desc = f"  index={info['index']}, type={info['type']}"
        if info.get('text'):
            desc += f', text="{info["text"][:80].replace(chr(10), " ")}"'
        lines.append(desc)
    block_desc = '\n'.join(lines)
    return f"""你是一名文档布局分析专家。当前页面已用检测框划分出不同布局区块，每个区块的右上角标注了对应编号。

任务要求：
1. 观察图片，判断哪些区块需要合并为**同一个图片/表格**（例如：图表主体搭配图注、脚注，或是被错误拆分的同一张图表碎片）。
2. **仅合并以下情况**：
   - 图表主体与其直接图注/标题（如”表1”、”图1”等紧挨图表的短标题行）
   - 被检测框错误拆分的同一张图表/表格碎片（列结构相同、内容连续）
   - 图表下方以”注：”、”备注”等明确标注开头的脚注文本
3. **禁止合并**：不要将图表/表格附近的独立段落文本、正文内容、列表项等合并进来。即使文本紧挨着图表，只要不是直接的图注标题或明确的脚注，就不应合并。
4. 最终仅输出**二维JSON数组**，内层数组存放需要合并的区块编号。
5. 无需合并的独立区块，单独作为只含一个编号的数组。
6. 禁止添加任何解释文字，只返回JSON结果。

示例输出：
- [[1,2,3],[4,5],[6]] → 1、2、3号区块合并；4、5号区块合并；6号区块独立
- [[1],[2],[3]] → 所有区块均独立
"""


def _call_vllm(image, block_infos,
               base_url='http://localhost:8000/v1',
               model_name='Qwen/Qwen2.5-VL-72B-Instruct',
               api_key='EMPTY', temperature=0.1, max_tokens=512, timeout=60,
               retries=3):
    import requests
    import time
    img_b64 = _image_to_base64(image)
    prompt = _build_vlm_prompt(block_infos)
    payload = {
        'model': model_name,
        'messages': [{'role': 'user', 'content': [
            {'type': 'image_url', 'image_url': {'url': f'data:image/jpeg;base64,{img_b64}'}},
            {'type': 'text', 'text': prompt},
        ]}],
        'temperature': temperature, 'max_tokens': max_tokens,
    }
    headers = {'Content-Type': 'application/json', 'Authorization': f'Bearer {api_key}'}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(f'{base_url}/chat/completions',
                                 headers=headers, json=payload, timeout=timeout)
            resp.raise_for_status()
            content = resp.json()['choices'][0]['message']['content'].strip()
            std_logger.info(f'VLM raw response: {content}')
            result = _parse_merge_groups(content)
            if result is not None:
                return result
            std_logger.warning(f'VLM 返回解析为空 (attempt {attempt}/{retries})')
        except Exception as e:
            std_logger.error(f'VLM 请求失败 (attempt {attempt}/{retries}): {e}')
        if attempt < retries:
            time.sleep(2)
    return None


def _parse_merge_groups(text):
    text = text.strip()
    if not text:
        std_logger.warning('VLM 返回空内容')
        return None
    text = re.sub(r'```(?:json)?\s*', '', text).strip('`').strip()
    match = re.search(r'\[\s*\[.*?\]\s*\]', text, re.DOTALL)
    if match:
        try:
            groups = json.loads(match.group())
            if isinstance(groups, list) and all(
                isinstance(g, list) and all(isinstance(i, int) for i in g) for g in groups
            ):
                return groups
        except json.JSONDecodeError:
            pass
    try:
        groups = json.loads(text)
        if isinstance(groups, list):
            normalized = []
            for g in groups:
                if isinstance(g, int):
                    normalized.append([g])
                elif isinstance(g, list):
                    normalized.append(g)
                else:
                    continue
            if normalized:
                return normalized
    except json.JSONDecodeError:
        pass
    std_logger.warning(f'无法解析 VLM 输出: {text}')
    return None


# ── 从 PDF 页面图像裁剪区域并保存 ──

def _crop_from_pdf_page(page_image, bbox_pixel, save_path):
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


# ── 合并核心 (优化版: group key 代替 list position) ──

def _determine_merged_type(blocks):
    types = {_to_type_str(b.get('type', '')) for b in blocks}
    if 'image_body' in types:
        return 'image_body'
    if 'table_body' in types:
        return 'table_body'
    for t in types:
        if 'image' in t:
            return 'image_body'
        if 'table' in t:
            return 'table_body'
    return _to_type_str(blocks[0].get('type', ''))


def _apply_merge_groups(
    page_blocks: list,
    merge_groups: list,
    page_image,                    # PIL.Image
    page_size: list,               # [pdf_w, pdf_h]
    confirmed_images_dir: str,
    confirmed_img_prefix: str,
    page_idx: int,
) -> list:
    """根据 VLM 返回的分组执行合并。

    merge_groups 中的 index 均为 detailed_index。

    合并策略:
      多元素分组 [i,j,k]:
        1. 在首次遇到分组成员的位置插入合并 body 块
           (bbox = 并集, caption_text/footnote_text = 各成员拼接, confirmed_img_path = 裁剪图)
           合并块: detailed_index = None (synthetic 块无原始 index)
        2. 原始子块全部保留在列表中，紧跟合并块之后
           每个子块保留原始 detailed_index
           belong_to_group 暂存 group key (sorted tuple)，后续由 _assign_confirmed_indices 替换
      单元素分组 [i]:
        不做合并，原样保留
      不在任何分组中的块:
        原样保留

    Returns:
        合并后的新 para_blocks 列表。
    """
    # detailed_index → block 映射
    didx_to_block = {blk['detailed_index']: blk for blk in page_blocks}

    # 每个 detailed_index → 所属分组 key
    didx_to_group = {}
    for group in merge_groups:
        key = tuple(sorted(group))
        for didx in group:
            didx_to_group[didx] = key

    grouped_didxs = set(didx_to_group.keys())
    seen_keys = set()

    # 缩放因子
    pdf_w, pdf_h = page_size[0], page_size[1]
    img_w, img_h = page_image.size
    scale_x = img_w / pdf_w if pdf_w > 0 else 1
    scale_y = img_h / pdf_h if pdf_h > 0 else 1

    new_blocks = []
    group_to_merged: Dict[tuple, dict] = {}  # group key → merged block

    for blk in page_blocks:
        blk_didx = blk['detailed_index']

        # ── 不在任何分组中 → 原样保留 ──
        if blk_didx not in grouped_didxs:
            new_blocks.append(copy.deepcopy(blk))
            continue

        key = didx_to_group[blk_didx]

        if key not in seen_keys:
            seen_keys.add(key)

            # ── 单元素分组 → 不合并 ──
            if len(key) == 1:
                new_blocks.append(copy.deepcopy(blk))
                continue

            # ── 多元素分组 → 创建合并块 + 保留子块 ──
            group_blocks = [didx_to_block[gi] for gi in sorted(key) if gi in didx_to_block]

            # 合并 bbox (PDF 坐标系)
            bboxes = [b['bbox'] for b in group_blocks if b.get('bbox')]
            merged_bbox = _compute_merged_bbox(bboxes)
            merged_type = _determine_merged_type(group_blocks)

            parent_types = {_to_type_str(b.get('parent_type', '')) for b in group_blocks}
            parent_type = 'image' if 'image' in parent_types else (
                'table' if 'table' in parent_types else '')

            # 收集 caption_text / footnote_text / body 信息
            body_html, body_img_path, body_text = '', '', ''
            caption_texts, footnote_texts = [], []
            all_sub_blocks = []

            for gb in group_blocks:
                ct = gb.get('caption_text', '').strip()
                ft = gb.get('footnote_text', '').strip()
                if ct:
                    caption_texts.append(ct)
                if ft:
                    footnote_texts.append(ft)

                body_html = gb.get('html', body_html)
                body_img_path = gb.get('img_path', body_img_path)
                body_text = gb.get('text', body_text)

                # 收集子块的 sub_blocks
                all_sub_blocks.extend(gb.get('sub_blocks', []))

            # 裁剪合并后的区域 → confirmed_images/
            pixel_bbox = [
                merged_bbox[0] * scale_x, merged_bbox[1] * scale_y,
                merged_bbox[2] * scale_x, merged_bbox[3] * scale_y,
            ]
            crop_name = _hash_crop_name(page_idx, merged_bbox)
            crop_path = os.path.join(confirmed_images_dir, crop_name)
            _crop_from_pdf_page(page_image, pixel_bbox, crop_path)

            # 构建合并 body 块
            confirmed_img_rel = f"{confirmed_img_prefix}/{crop_name}"
            merged_body = {
                'type': merged_type,
                'bbox': merged_bbox,
                'detailed_index': None,      # synthetic 块无原始 detailed_index
                'page_idx': page_idx,
                'parent_type': parent_type,
                'caption_text': '\n'.join(caption_texts),
                'footnote_text': '\n'.join(footnote_texts),
                'img_path': confirmed_img_rel,
                'confirmed_img_path': confirmed_img_rel,
                'merged_from': list(sorted(key)),
                'sub_blocks': all_sub_blocks,
            }
            if 'table' in merged_type and body_html:
                merged_body['html'] = body_html
            if body_text:
                merged_body['text'] = body_text

            new_blocks.append(merged_body)
            group_to_merged[key] = merged_body

            # 原始子块紧跟在合并块后面
            for gb in group_blocks:
                child = copy.deepcopy(gb)
                child['belong_to_group'] = key   # 暂存 group key，后续由 _assign_confirmed_indices 解析
                new_blocks.append(child)

        # else: 该分组已处理，后续成员已在上面统一添加 → 跳过

    return new_blocks


# ── 将未合并的 images 复制到 confirmed_images ──

def _sync_unmerged_images(layout_json, images_dir, confirmed_images_dir,
                          confirmed_img_prefix, pdf_images=None):
    """对没有 belong_to 且没有 confirmed_img_path 的 image_body/table_body 块，
    根据扩展 bbox 从 PDF 页面图像重新裁剪并保存到 confirmed_images_dir。
    无 pdf_images 时回退为从 images_dir 复制原文件。"""
    os.makedirs(confirmed_images_dir, exist_ok=True)
    cropped_count = 0

    for page_data in layout_json:
        page_idx = page_data.get('page_idx', -1)
        page_size = page_data.get('page_size', [0, 0])

        for blk in page_data.get('para_blocks', []):
            if 'belong_to' in blk:
                continue
            if blk.get('confirmed_img_path'):
                continue

            blk_type = _to_type_str(blk.get('type', ''))
            if blk_type not in ('image_body', 'table_body'):
                continue

            bbox = blk.get('bbox', [])
            if not bbox or len(bbox) < 4:
                continue

            # 从 PDF 页面按扩展 bbox 裁剪
            if (pdf_images is not None
                    and 0 <= page_idx < len(pdf_images)
                    and page_size and page_size[0] > 0 and page_size[1] > 0):
                page_image = pdf_images[page_idx]
                scale_x = page_image.width / page_size[0]
                scale_y = page_image.height / page_size[1]
                pixel_bbox = [
                    bbox[0] * scale_x, bbox[1] * scale_y,
                    bbox[2] * scale_x, bbox[3] * scale_y,
                ]
                crop_name = _hash_crop_name(page_idx, bbox)
                crop_path = os.path.join(confirmed_images_dir, crop_name)
                _crop_from_pdf_page(page_image, pixel_bbox, crop_path)
                blk['confirmed_img_path'] = f"{confirmed_img_prefix}/{crop_name}"
                blk['img_path'] = blk['confirmed_img_path']
                cropped_count += 1
                std_logger.debug('页面 %s: 重新裁剪未合并图像 bbox=%s -> %s',
                                 page_idx, bbox, crop_path)
            else:
                # 回退：从 images_dir 复制原文件
                img_path = blk.get('img_path', '')
                if not img_path:
                    continue
                src_filename = os.path.basename(img_path)
                src_full = os.path.join(images_dir, src_filename)
                dst_full = os.path.join(confirmed_images_dir, src_filename)
                if os.path.isfile(src_full):
                    shutil.copy2(src_full, dst_full)
                    std_logger.debug('页面 %s: 复制未合并图像 %s -> %s', page_idx, src_full, dst_full)
                else:
                    std_logger.warning(f'源图像不存在，跳过: {src_full}')
                blk['confirmed_img_path'] = f"{confirmed_img_prefix}/{src_filename}"

    std_logger.info('未合并图像处理完成: cropped=%d, confirmed_dir=%s', cropped_count, confirmed_images_dir)


# ── 对外接口: vlm_correct_layout ──

def vlm_correct_layout(
    layout_json: list,
    pdf_images: list,
    images_dir: str,
    confirmed_images_dir: str,
    confirmed_img_prefix: str = 'confirmed_images',
    vllm_base_url: str = 'http://localhost:8000/v1',
    vllm_model: str = 'Qwen/Qwen2.5-VL-72B-Instruct',
    api_key: str = 'EMPTY',
    temperature: float = 0.1,
    max_tokens: int = 512,
    timeout: int = 60,
    font_path: str = None,
    font_size: int = 28,
    line_width: int = 3,
    debug_dir: str = None,
) -> list:
    """对 layout_json 中含 figure/table 的页面执行 VLM 辅助合并纠正。

    完成后统一调用 _assign_confirmed_indices 分配 confirmed_index，
    并将 belong_to_group 解析为 confirmed_index 写入 belong_to。
    """
    std_logger.info(
        '开始 VLM 纠正: pages=%d, confirmed_dir=%s, debug_dir=%s',
        len(layout_json), confirmed_images_dir, debug_dir or '',
    )

    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
    os.makedirs(confirmed_images_dir, exist_ok=True)
    # 来自初步处理过后的json
    corrected = copy.deepcopy(layout_json)

    for page_data in corrected:
        page_idx = page_data.get('page_idx', 0)
        page_blocks = page_data.get('para_blocks', [])
        page_size = page_data.get('page_size', [0, 0])

        if not _page_has_figure_or_table(page_blocks):
            std_logger.debug('页面 %s: 无 figure/table，跳过 VLM', page_idx)
            continue
        if page_idx >= len(pdf_images):
            std_logger.warning(f'页面 {page_idx} 无对应 PDF 图像，跳过')
            continue

        page_image = pdf_images[page_idx]   # pdf页面
        ft_indices = _get_indices(page_blocks)
        if not ft_indices:
            std_logger.debug('页面 %s: 未找到可处理块 index，跳过 VLM', page_idx)
            continue

        std_logger.info('页面 %s: VLM 候选块数量=%d', page_idx, len(ft_indices))

        pixel_blocks = _convert_bboxes_to_pixel(page_blocks, page_size, page_image.size)

        annotated = _draw_bboxes_on_page(
            page_image, pixel_blocks, ft_indices,
            line_width=line_width, font_size=font_size, font_path=font_path,
        )
        if debug_dir:
            debug_img_path = os.path.join(debug_dir, f'page_{page_idx}_annotated.jpg')
            annotated.save(debug_img_path)
            std_logger.debug('页面 %s: 已保存标注调试图 %s', page_idx, debug_img_path)

        # 构建 VLM 输入: index 字段使用 detailed_index
        didx_to_blk = {blk['detailed_index']: blk for blk in page_blocks}
        block_infos = []
        for didx in ft_indices:
            blk = didx_to_blk[didx]
            info = {'index': blk['detailed_index'], 'type': _to_type_str(blk.get('type', ''))}
            if blk.get('text'):
                info['text'] = blk['text']
            elif blk.get('caption_text'):
                info['text'] = blk['caption_text']
            block_infos.append(info)
        # block_info没有用实际上
        merge_groups = _call_vllm(
            image=annotated, block_infos=block_infos,
            base_url=vllm_base_url, model_name=vllm_model,
            api_key=api_key, temperature=temperature,
            max_tokens=max_tokens, timeout=timeout,
        )

        if merge_groups is None:
            std_logger.warning(f'页面 {page_idx}: VLM 返回为空，跳过')
            continue

        needs_merge = any(len(g) > 1 for g in merge_groups)
        if not needs_merge:
            std_logger.info(f'页面 {page_idx}: 无需合并')
            continue

        std_logger.info(f'页面 {page_idx}: 合并分组 = {merge_groups}')

        page_data['para_blocks'] = _apply_merge_groups(
            page_blocks=page_blocks,
            merge_groups=merge_groups,
            page_image=page_image,
            page_size=page_size,
            confirmed_images_dir=confirmed_images_dir,
            confirmed_img_prefix=confirmed_img_prefix,
            page_idx=page_idx,
        )
        std_logger.info('页面 %s: 合并完成，新块数=%d', page_idx, len(page_data['para_blocks']))

    # 未合并的 image/table body → 从 PDF 重新裁剪到 confirmed_images/
    _sync_unmerged_images(corrected, images_dir, confirmed_images_dir,
                          confirmed_img_prefix, pdf_images=pdf_images)

    # ── 统一分配 confirmed_index 并解析 belong_to_group → belong_to ──
    _assign_confirmed_indices(corrected)

    std_logger.info('VLM 纠正流程结束')
    return corrected


# ══════════════════════════════════════════════════════════════
#  第三阶段 A: layout JSON → 画框 PDF
# ══════════════════════════════════════════════════════════════

_TYPE_COLOR_MAP = {
    BlockType.TITLE:              (102, 102, 255),
    BlockType.TEXT:                (153,   0,  76),
    BlockType.REF_TEXT:            (153,   0,  76),
    BlockType.LIST:               ( 40, 169,  92),
    BlockType.INDEX:              ( 40, 169,  92),
    BlockType.INTERLINE_EQUATION: (  0, 255,   0),
    BlockType.IMAGE_BODY:         (153, 255,  51),
    BlockType.IMAGE_CAPTION:      (102, 178, 255),
    BlockType.IMAGE_FOOTNOTE:     (255, 178, 102),
    BlockType.TABLE_BODY:         (204, 204,   0),
    BlockType.TABLE_CAPTION:      (255, 255, 102),
    BlockType.TABLE_FOOTNOTE:     (229, 255, 204),
    BlockType.CODE_BODY:          (102,   0, 204),
    BlockType.CODE_CAPTION:       (204, 153, 255),
}
_TYPE_COLOR_MAP_STR = {_to_type_str(k): v for k, v in _TYPE_COLOR_MAP.items()}
_DEFAULT_COLOR = (255, 0, 0)


def _get_block_color(blk_type):
    color = _TYPE_COLOR_MAP.get(blk_type)
    if color is None:
        color = _TYPE_COLOR_MAP_STR.get(_to_type_str(blk_type), _DEFAULT_COLOR)
    return color


def _cal_canvas_rect_simple(page_width, page_height, bbox):
    x0, y0, x1, y1 = bbox
    return [x0, page_height - y1, abs(x1 - x0), abs(y1 - y0)]


def export_layout_pdf(layout_json: list,
                      pdf_bytes: bytes,
                      out_path: str,
                      filename: str = 'layout_output.pdf',
                      draw_index: bool = True,
                      fill: bool = True,
                      mode: str = 'confirmed',
                      show_belong_arrow: bool = False):
    """将 layout JSON 以彩色画框 + 编号叠加到原始 PDF 上。

    Args:
        mode:
          'detailed'  → 画所有原始子块 (sub_blocks) + body 块本身，标注 detailed_index
          'confirmed' → 只画合并后的 bbox + 独立块，跳过 belong_to 子块
                         标注 confirmed_index
        show_belong_arrow:
          若为 True，detailed 模式下在子块编号后显示 "→belong_to"。
    """
    from io import BytesIO as _BytesIO
    from pypdf import PdfReader, PdfWriter, PageObject
    from reportlab.pdfgen import canvas as rl_canvas

    pdf_reader = PdfReader(_BytesIO(pdf_bytes))
    output_pdf = PdfWriter()

    for i, page in enumerate(pdf_reader.pages):
        page_width = float(page.cropbox[2])
        page_height = float(page.cropbox[3])

        page_layout = None
        for pl in layout_json:
            if pl['page_idx'] == i:
                page_layout = pl
                break
        if page_layout is None or not page_layout.get('para_blocks'):
            output_pdf.add_page(page)
            continue

        packet = _BytesIO()
        c = rl_canvas.Canvas(packet, pagesize=(page_width, page_height))

        for blk in page_layout['para_blocks']:
            is_child = 'belong_to' in blk
            is_cross_child = 'belong_to_cross' in blk
            is_merged = 'merged_from' in blk

            # ── 模式过滤 ──
            if mode == 'confirmed' and (is_child or is_cross_child):
                continue
            if mode == 'detailed' and is_merged:
                continue

            bbox = blk.get('bbox', [])
            if not bbox or len(bbox) < 4:
                continue

            blk_type = blk['type']

            if mode == 'detailed':
                # detailed 模式: 先画 sub_blocks 的独立区域，再画 body 的扩展 bbox
                sub_blocks = blk.get('sub_blocks', [])
                for sub in sub_blocks:
                    sub_bbox = sub.get('bbox', [])
                    if not sub_bbox or len(sub_bbox) < 4:
                        continue
                    sub_type = sub.get('type', blk_type)
                    sr, sg, sb = _get_block_color(sub_type)
                    snr, sng, snb = sr / 255.0, sg / 255.0, sb / 255.0
                    sub_rect = _cal_canvas_rect_simple(page_width, page_height, sub_bbox)
                    if fill:
                        c.setFillColorRGB(snr, sng, snb, 0.3)
                        c.rect(sub_rect[0], sub_rect[1], sub_rect[2], sub_rect[3], stroke=0, fill=1)
                    else:
                        c.setStrokeColorRGB(snr, sng, snb)
                        c.rect(sub_rect[0], sub_rect[1], sub_rect[2], sub_rect[3], stroke=1, fill=0)
                    if draw_index:
                        idx_val = blk.get('detailed_index')
                        if idx_val is not None:
                            c.setFillColorRGB(snr, sng, snb, 1.0)
                            c.setFontSize(10)
                            c.drawString(sub_rect[0] + sub_rect[2] + 2,
                                         sub_rect[1] + sub_rect[3] - 10, str(idx_val))

                # 画 body 扩展 bbox (虚线边框，不填充)
                r, g, b = _get_block_color(blk_type)
                nr, ng, nb = r / 255.0, g / 255.0, b / 255.0
                rect = _cal_canvas_rect_simple(page_width, page_height, bbox)
                c.setStrokeColorRGB(nr, ng, nb)
                c.setDash(6, 3)
                c.rect(rect[0], rect[1], rect[2], rect[3], stroke=1, fill=0)
                c.setDash()
            else:
                # confirmed 模式: 画合并后的 bbox
                r, g, b = _get_block_color(blk_type)
                nr, ng, nb = r / 255.0, g / 255.0, b / 255.0
                rect = _cal_canvas_rect_simple(page_width, page_height, bbox)

                if fill:
                    c.setFillColorRGB(nr, ng, nb, 0.3)
                    c.rect(rect[0], rect[1], rect[2], rect[3], stroke=0, fill=1)
                else:
                    c.setStrokeColorRGB(nr, ng, nb)
                    c.rect(rect[0], rect[1], rect[2], rect[3], stroke=1, fill=0)

                if draw_index:
                    idx_val = blk.get('confirmed_index')
                    if idx_val is not None:
                        c.setFillColorRGB(nr, ng, nb, 1.0)
                        c.setFontSize(10)
                        label = str(idx_val)
                        if show_belong_arrow and is_child and 'belong_to' in blk:
                            label += f" →{blk['belong_to']}"
                        c.drawString(rect[0] + rect[2] + 2, rect[1] + rect[3] - 10, label)

        c.save()
        packet.seek(0)
        overlay = PdfReader(packet)

        if len(overlay.pages) > 0:
            new_page = PageObject(pdf=None)
            new_page.update(page)
            new_page.merge_page(overlay.pages[0])
            output_pdf.add_page(new_page)
        else:
            output_pdf.add_page(page)

    os.makedirs(out_path, exist_ok=True)
    out_file = os.path.join(out_path, filename)
    with open(out_file, 'wb') as f:
        output_pdf.write(f)
    loguru_logger.info(f'Layout PDF saved to {out_file}')


# ══════════════════════════════════════════════════════════════
#  第三阶段 B: layout JSON → Markdown
# ══════════════════════════════════════════════════════════════

_TEXT_TYPE_STRS = {_to_type_str(t) for t in
                   [BlockType.TEXT, BlockType.LIST, BlockType.INDEX, BlockType.REF_TEXT]}


def _concat_images_vertical(img_paths: list, out_dir: str = '') -> str:
    """垂直拼接多张图像，保存并返回 Markdown 引用。"""
    from PIL import Image

    images = []
    for p in img_paths:
        # 尝试直接打开，或相对于 out_dir 打开
        full_p = p
        if not os.path.isabs(p) and os.path.isfile(p):
            full_p = p
        elif not os.path.isabs(p) and out_dir:
            candidate = os.path.join(out_dir, p)
            if os.path.isfile(candidate):
                full_p = candidate
        try:
            img = Image.open(full_p)
            images.append(img.convert('RGB'))
        except Exception as e:
            std_logger.warning(f'无法读取图像 {full_p}: {e}')

    if not images:
        return ''
    if len(images) == 1:
        return f"![]({img_paths[0]})"

    # 计算拼接尺寸
    max_w = max(img.width for img in images)
    total_h = sum(img.height for img in images)

    merged = Image.new('RGB', (max_w, total_h), (255, 255, 255))
    y_offset = 0
    for img in images:
        # 居中放置
        x_offset = (max_w - img.width) // 2
        merged.paste(img, (x_offset, y_offset))
        y_offset += img.height

    # 保存到 out_dir/confirmed_images/ (与其他 confirmed 图像一致)
    if out_dir:
        save_dir = os.path.join(out_dir, 'confirmed_images')
        os.makedirs(save_dir, exist_ok=True)
        hash_name = 'concat_' + hashlib.sha256(','.join(img_paths).encode()).hexdigest()[:12] + '.jpg'
        save_path = os.path.join(save_dir, hash_name)
        merged.save(save_path, quality=95)
        return f"![](confirmed_images/{hash_name})"

    # 无 out_dir 时返回第一张图的引用
    return f"![]({img_paths[0]})"


def _img_path_to_md(img_path: str, out_dir: str = '') -> str:
    """将图像路径转为 Markdown 引用。

    多路径 (逗号分隔) 时垂直拼接为一张图，保存到 out_dir 后引用。
    """
    if not img_path:
        return ''
    paths = [p.strip() for p in img_path.split(',') if p.strip()]
    if not paths:
        return ''
    if len(paths) == 1:
        return f"![]({paths[0]})"
    # 多路径：垂直拼接
    return _concat_images_vertical(paths, out_dir)


def export_layout_markdown(layout_json: list,
                           out_path: str,
                           filename: str = 'layout_output.md',
                           mode: str = 'confirmed'):
    """将 layout JSON 按阅读顺序转为 Markdown 文件。

    Args:
        mode:
          'detailed'  → 输出全部原始块 (含 sub_blocks 展开)，图像路径来自 img_path (images/)
          'confirmed' → 跳过 belong_to 子块，图像路径来自 confirmed_img_path (confirmed_images/)
                        caption_text/footnote_text 直接从合并块读取
    """
    all_lines = []

    for page_data in layout_json:
        for blk in page_data.get('para_blocks', []):
            is_child = 'belong_to' in blk
            is_cross_child = 'belong_to_cross' in blk

            if mode == 'confirmed' and (is_child or is_cross_child):
                continue

            blk_type = _to_type_str(blk.get('type', ''))
            text = blk.get('text', '').strip()

            # 选择图像路径 (支持逗号分隔的多路径)
            if mode == 'confirmed':
                img_path = blk.get('confirmed_img_path', '') or blk.get('img_path', '')
            else:
                img_path = blk.get('img_path', '')

            md_line = ''

            # ── 标题 ──
            if blk_type == _to_type_str(BlockType.TITLE):
                level = blk.get('title_level', 1)
                md_line = f'{"#" * level} {text}'

            # ── 普通文本 ──
            elif blk_type in _TEXT_TYPE_STRS:
                md_line = text

            # ── 行间公式 ──
            elif blk_type == _to_type_str(BlockType.INTERLINE_EQUATION):
                md_line = text if text else _img_path_to_md(img_path, out_path)

            # ── image_body ──
            elif blk_type == 'image_body':
                if img_path:
                    md_line = _img_path_to_md(img_path, out_path)
                if mode == 'confirmed' and blk.get('cross_page_merged') and text:
                    md_line = f"{md_line}\n\n{text}" if md_line else text
                    caption = ''
                    footnote = ''
                else:
                    caption = blk.get('caption_text', '').strip()
                    footnote = blk.get('footnote_text', '').strip()
                if caption:
                    md_line = f"{md_line}\n\n{caption}" if md_line else caption
                if footnote:
                    md_line = f"{md_line}\n\n{footnote}" if md_line else footnote
                # detailed 模式: 展开 sub_blocks
                if mode == 'detailed':
                    for sub in blk.get('sub_blocks', []):
                        sub_text = sub.get('text', '').strip()
                        if sub_text and sub_text != caption and sub_text != footnote:
                            md_line = f"{md_line}\n\n{sub_text}" if md_line else sub_text

            # ── table_body ──
            elif blk_type == 'table_body':
                html = blk.get('html', '')
                if mode == 'confirmed' and blk.get('cross_page_merged') and img_path:
                    md_line = _img_path_to_md(img_path, out_path)
                elif html:
                    md_line = f"\n{html}\n"
                elif img_path:
                    md_line = _img_path_to_md(img_path, out_path)
                if mode == 'confirmed' and blk.get('cross_page_merged') and text:
                    md_line = f"{md_line}\n\n{text}" if md_line else text
                    caption = ''
                    footnote = ''
                else:
                    caption = blk.get('caption_text', '').strip()
                    footnote = blk.get('footnote_text', '').strip()
                if caption:
                    md_line = f"{md_line}\n\n{caption}" if md_line else caption
                if footnote:
                    md_line = f"{md_line}\n\n{footnote}" if md_line else footnote
                if mode == 'detailed':
                    for sub in blk.get('sub_blocks', []):
                        sub_text = sub.get('text', '').strip()
                        if sub_text and sub_text != caption and sub_text != footnote:
                            md_line = f"{md_line}\n\n{sub_text}" if md_line else sub_text

            # ── code ──
            elif blk_type == 'code_body':
                if text:
                    md_line = f"```\n{text}\n```"
                caption = blk.get('caption_text', '').strip()
                if caption:
                    md_line = f"{md_line}\n\n{caption}" if md_line else caption
                if mode == 'detailed':
                    for sub in blk.get('sub_blocks', []):
                        sub_text = sub.get('text', '').strip()
                        if sub_text and sub_text != caption:
                            md_line = f"{md_line}\n\n{sub_text}" if md_line else sub_text

            if md_line.strip():
                all_lines.append(md_line.strip())

    markdown_text = '\n\n'.join(all_lines)

    os.makedirs(out_path, exist_ok=True)
    md_path = os.path.join(out_path, filename)
    with open(md_path, 'w', encoding='utf-8') as f:
        f.write(markdown_text)
    loguru_logger.info(f'Markdown saved to {md_path}')
    return markdown_text


# ══════════════════════════════════════════════════════════════
#  辅助: 图像页码映射
# ══════════════════════════════════════════════════════════════

def _extract_layout_pages_from_merged_json(data) -> list:
    """从 merged_confimed.json 内容中提取页面列表。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('layout', 'pages', 'pdf_info'):
            value = data.get(key)
            if isinstance(value, list):
                return value
        if isinstance(data.get('para_blocks'), list):
            return [data]
    raise ValueError('不支持的 merged_confimed.json 结构，未找到页面列表')


def build_confirmed_image_page_map(layout_json: list) -> Dict[str, int]:
    """构建 confirmed 模式下图像文件名到页码的映射。"""
    image_page_map: Dict[str, int] = {}

    for page_data in layout_json:
        page_idx = page_data.get('page_idx', None)
        if page_idx is None:
            continue

        for blk in page_data.get('para_blocks', []):
            if 'belong_to' in blk:
                continue

            blk_type = _to_type_str(blk.get('type', ''))
            if blk_type not in ('image_body', 'table_body'):
                continue

            img_path = (blk.get('confirmed_img_path', '') or blk.get('img_path', '')).strip()
            if not img_path:
                continue

            # 支持逗号分隔的多路径
            img_names = [os.path.basename(p.strip()) for p in img_path.split(',') if p.strip()]
            if not img_names:
                continue

            blk_page_idx = blk.get('page_idx', page_idx)
            try:
                blk_page_idx = int(blk_page_idx)
            except (TypeError, ValueError):
                std_logger.warning('图像 %s 的 page_idx 无效，已跳过: %s', img_names, blk_page_idx)
                continue

            for img_name in img_names:
                if not img_name:
                    continue
                if img_name in image_page_map and image_page_map[img_name] != blk_page_idx:
                    std_logger.warning(
                        '图像名重复且页码冲突，保留首次页码: %s -> %s (新值: %s)',
                        img_name, image_page_map[img_name], blk_page_idx,
                    )
                    continue
                image_page_map[img_name] = blk_page_idx

    return image_page_map


def export_confirmed_image_page_map_from_json(
    merged_confimed_json_path: str,
    out_json_path: Optional[str] = None,
) -> Dict[str, int]:
    """根据最终 merged_confimed.json 导出 confirmed 图像页码映射。"""
    with open(merged_confimed_json_path, 'r', encoding='utf-8') as f:
        data = json.load(f)

    layout_pages = _extract_layout_pages_from_merged_json(data)
    image_page_map = build_confirmed_image_page_map(layout_pages)

    if out_json_path:
        out_dir = os.path.dirname(os.path.abspath(out_json_path))
        if out_dir:
            os.makedirs(out_dir, exist_ok=True)
        with open(out_json_path, 'w', encoding='utf-8') as f:
            json.dump(image_page_map, f, ensure_ascii=False, indent=2)
        std_logger.info('confirmed 图像页码映射已导出: %s (count=%d)', out_json_path, len(image_page_map))

    return image_page_map


# ══════════════════════════════════════════════════════════════
#  CLI 入口
# ══════════════════════════════════════════════════════════════

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='MinerU middle.json → 布局JSON → (VLM纠正) → PDF/Markdown (优化版)'
    )

    # ── 输入输出 ──
    parser.add_argument('input', nargs='?',
                        default='/home/xq/rag/output/304设计（咨询）成品校审管理细则/auto/304设计（咨询）成品校审管理细则_middle.json',
                        help='middle.json 文件路径')
    parser.add_argument('-o', '--output', default='', help='输出 JSON 文件名')
    parser.add_argument('--img-prefix', default='images',
                        help='原始图片路径前缀 (默认 images)')
    parser.add_argument('--confirmed-prefix', default='confirmed_images',
                        help='合并后图片路径前缀 (默认 confirmed_images)')
    parser.add_argument('--no-pdf', action='store_true', help='禁用 PDF 导出')
    parser.add_argument('--no-md', action='store_true', help='禁用 Markdown 导出')
    parser.add_argument('--out_dir', default=None, help='输出目录')
    parser.add_argument('--pdf', default=None, help='原始 PDF 路径')

    # ── 导出模式 ──
    parser.add_argument('--export-mode', default='both',
                        choices=['detailed', 'confirmed', 'both'],
                        help='导出模式: detailed / confirmed / both(默认)')

    # ── VLM (默认值从环境变量读取) ──
    parser.add_argument('--vlm', action='store_false', help='启用 VLM 合并纠正（默认开启）')
    parser.add_argument('--vllm-url',
                        default=os.environ.get('VLM_BASE_URL', 'https://ark.cn-beijing.volces.com/api/v3'))
    parser.add_argument('--vllm-model',
                        default=os.environ.get('VLM_MODEL', 'doubao-seed-2-0-lite-260428'))
    parser.add_argument('--vllm-api-key',
                        default=os.environ.get('VLM_API_KEY', ''))
    parser.add_argument('--debug-dir', default=None, help='VLM 标注图调试目录')

    # ── 跨页合并 ──
    parser.add_argument('--cross-page', action='store_true', help='启用跨页合并（默认开启）')
    parser.add_argument('--cross-vllm-url', default=None, help='跨页 VLM URL (默认同 --vllm-url)')
    parser.add_argument('--cross-vllm-model', default=None, help='跨页 VLM 模型 (默认同 --vllm-model)')
    parser.add_argument('--cross-vllm-api-key', default=None, help='跨页 VLM API Key (默认同 --vllm-api-key)')

    parser.add_argument('--log-level', default='INFO',
                        choices=['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'],
                        help='日志级别 (默认 INFO)')
    parser.add_argument('--log-file', default=None,
                        help='日志文件路径 (可选，不填则仅输出到控制台)')

    args = parser.parse_args()
    _configure_logging(args.log_level, args.log_file)
    std_logger.info('启动流程: input=%s, vlm=%s, export_mode=%s', args.input, args.vlm, args.export_mode)

    # ===================== 读取 =====================
    # middle.json
    with open(args.input, 'r', encoding='utf-8') as f:
        data = json.load(f)
    pdf_info = data.get('pdf_info', data)
    std_logger.info('输入读取完成: pdf_info_pages=%d', len(pdf_info) if isinstance(pdf_info, list) else 1)

    # ===================== 目录 =====================
    input_dir = os.path.dirname(os.path.abspath(args.input))
    base_name = os.path.splitext(os.path.basename(args.input))[0].replace('_middle', '')

    out_dir = args.out_dir or input_dir
    os.makedirs(out_dir, exist_ok=True)

    images_dir = os.path.join(input_dir, args.img_prefix)
    confirmed_images_dir = os.path.join(out_dir, args.confirmed_prefix)
    std_logger.info('目录信息: input_dir=%s, out_dir=%s, images_dir=%s, confirmed_images_dir=%s',
                    input_dir, out_dir, images_dir, confirmed_images_dir)

    # ===================== 1. 提取 layout JSON =====================
    layout = extract_layout_json(pdf_info, img_buket_path=args.img_prefix)
    print(f'[1/4] 布局提取完成: {len(layout)} 页')
    std_logger.info('阶段 1 完成: 提取页面数=%d', len(layout))

    # ===================== 查找 PDF =====================
    input_pdf_path = args.pdf
    if input_pdf_path is None:
        for p in [os.path.join(input_dir, base_name + '.pdf'),
                  os.path.join(input_dir, base_name + '_origin.pdf')]:
            if os.path.exists(p):
                input_pdf_path = p
                break
    std_logger.info('PDF 路径: %s', input_pdf_path or '未找到')

    # ===================== 加载 PDF 图像 (VLM 和跨页合并共用) =====================
    pdf_images = None
    if (args.vlm or args.cross_page) and input_pdf_path and os.path.isfile(input_pdf_path):
        print(f'PDF → 图像: {input_pdf_path}')
        std_logger.info('开始 PDF 转图像: %s', input_pdf_path)
        try:
            import pdf2image
            pdf_images = pdf2image.convert_from_path(
                input_pdf_path, dpi=300, thread_count=4, poppler_path=None,
                grayscale=False, size=None, paths_only=False,
            )
        except Exception as exc:
            std_logger.warning('pdf2image 不可用，使用 merge_cross_page.load_pdf_page_images: %s', exc)
            from merge_cross_page import load_pdf_page_images
            pdf_images = load_pdf_page_images(input_pdf_path, out_dir, dpi=300)
        print(f'      共 {len(pdf_images)} 页')
        std_logger.info('PDF 转图像完成: pages=%d', len(pdf_images))

    # ===================== 2. VLM 页内合并 =====================
    if args.vlm:
        if pdf_images is not None:
            debug_dir = args.debug_dir or _resolve_default_debug_dir(args.input)
            std_logger.info('VLM 调试图目录: %s', debug_dir)

            print(f'[2/4] VLM 页内合并...')
            layout = vlm_correct_layout(
                layout_json=layout,
                pdf_images=pdf_images,
                images_dir=images_dir,
                confirmed_images_dir=confirmed_images_dir,
                confirmed_img_prefix=args.confirmed_prefix,
                vllm_base_url=args.vllm_url,
                vllm_model=args.vllm_model,
                api_key=args.vllm_api_key,
                debug_dir=debug_dir,
            )
            print(f'[2/4] VLM 页内合并完成')
            print(f'      标注图       → {debug_dir}')
            print(f'      confirmed    → {confirmed_images_dir}')
            std_logger.info('阶段 2 完成: 标注图目录=%s, confirmed_images_dir=%s',
                            debug_dir, confirmed_images_dir)
        else:
            print('[2/4] --vlm 已启用但未找到原始 PDF，跳过')
            std_logger.warning('--vlm 已启用但未找到原始 PDF，已跳过 VLM 纠正')
    else:
        print('[2/4] VLM 未启用 (添加 --vlm 开启)')
        std_logger.info('阶段 2 跳过: VLM 未启用')

    # ===================== 3. 跨页合并 =====================
    if args.cross_page:
        if pdf_images is not None:
            from merge_cross_page import cross_page_merge
            cp_url = args.cross_vllm_url or args.vllm_url
            cp_model = args.cross_vllm_model or args.vllm_model
            cp_key = args.cross_vllm_api_key or args.vllm_api_key
            cross_debug_dir = os.path.join(out_dir, 'debug_cross_page')

            print(f'[3/4] 跨页合并...')
            std_logger.info('开始跨页合并: url=%s, model=%s', cp_url, cp_model)
            layout = cross_page_merge(
                layout_json=layout,
                pdf_images=pdf_images,
                confirmed_images_dir=confirmed_images_dir,
                confirmed_img_prefix=args.confirmed_prefix,
                base_url=cp_url,
                model_name=cp_model,
                api_key=cp_key,
                debug_dir=cross_debug_dir,
            )
            print(f'[3/4] 跨页合并完成')
            print(f'      调试图       → {cross_debug_dir}')
            std_logger.info('阶段 3 完成: 跨页合并调试图目录=%s', cross_debug_dir)
        else:
            print('[3/4] --cross-page 已启用但未找到原始 PDF，跳过')
            std_logger.warning('--cross-page 已启用但未找到原始 PDF，已跳过跨页合并')
    else:
        print('[3/4] 跨页合并未启用 (添加 --cross-page 开启)')
        std_logger.info('阶段 3 跳过: 跨页合并未启用')

    # ===================== 确保 confirmed_index 存在 =====================
    if not args.vlm and not args.cross_page:
        _assign_confirmed_indices(layout)

    # ===================== 保存 JSON =====================
    json_name = args.output.strip() or f'{base_name}_layout.json'
    json_path = os.path.join(out_dir, json_name)
    with open(json_path, 'w', encoding='utf-8') as f:
        json.dump(layout, f, ensure_ascii=False, indent=2)
    print(f'[JSON] {len(layout)} pages → {json_path}')
    std_logger.info('JSON 已保存: %s', json_path)

    # ===================== 4. 导出 =====================
    export_modes = ['detailed', 'confirmed'] if args.export_mode == 'both' else [args.export_mode]

    used_cross_page_export = False
    if args.cross_page and pdf_images is not None:
        from merge_cross_page import export_cross_page_pdf, export_cross_page_markdown
        if not args.no_pdf and 'confirmed' in export_modes:
            name = f'{base_name}_confirmed.pdf'
            export_cross_page_pdf(layout, pdf_images, out_dir, name)
            print(f'[PDF:confirmed] → {out_dir}/{name}')
            std_logger.info('跨页 PDF 导出完成: path=%s', os.path.join(out_dir, name))
            used_cross_page_export = True
        if not args.no_md and 'confirmed' in export_modes:
            name = f'{base_name}_confirmed.md'
            export_cross_page_markdown(layout, out_dir, name)
            print(f'[MD:confirmed]  → {out_dir}/{name}')
            std_logger.info('跨页 Markdown 导出完成: path=%s', os.path.join(out_dir, name))
            used_cross_page_export = True

    if not args.no_pdf and input_pdf_path and os.path.isfile(input_pdf_path):
        with open(input_pdf_path, 'rb') as f:
            pdf_bytes = f.read()
        for em in export_modes:
            if used_cross_page_export and em == 'confirmed':
                continue
            name = f'{base_name}_{em}.pdf'
            export_layout_pdf(layout, pdf_bytes, out_dir, name, mode=em)
            print(f'[PDF:{em}] → {out_dir}/{name}')
            std_logger.info('PDF 导出完成: mode=%s, path=%s', em, os.path.join(out_dir, name))
    elif not args.no_pdf:
        print('[PDF] 未找到原始 PDF，跳过')
        std_logger.warning('未找到原始 PDF，跳过 PDF 导出')

    if not args.no_md:
        for em in export_modes:
            if used_cross_page_export and em == 'confirmed':
                continue
            name = f'{base_name}_{em}.md'
            export_layout_markdown(layout, out_dir, name, mode=em)
            print(f'[MD:{em}]  → {out_dir}/{name}')
            std_logger.info('Markdown 导出完成: mode=%s, path=%s', em, os.path.join(out_dir, name))

    print('[4/4] 全部完成')
    std_logger.info('流程全部完成')
