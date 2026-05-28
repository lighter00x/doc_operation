# doc_operation

基于 MinerU 解析结果的文档布局合并工具链，通过 VLM 对表格、图片等元素进行页内合并和跨页合并，最终导出 Markdown / PDF。

## 整体流程

```
.doc/.pdf
    │
    ▼
  parse_doc.py              步骤1: MinerU 解析 → middle.json
    │
    ▼
  merge_optimized.py        步骤2: 页内合并 (VLM) → *_layout.json + confirmed_images/
    │
    ▼
  merge_cross_page.py       步骤3: 跨页合并 (VLM) → 更新 layout.json
    │
    ▼
  导出 Markdown / PDF
```

## 文件说明

| 文件 | 功能 |
|------|------|
| `parse_doc.py` | 入口脚本。`.doc` → PDF → MinerU 解析 → 页内合并 → 导出 |
| `merge_optimized.py` | 页内合并模块。提取 layout JSON，调用 VLM 合并同页内的图表/图注/脚注，导出 PDF/Markdown |
| `merge_cross_page.py` | 跨页合并模块。检测跨页分割的表格/图片，调用 VLM 判断并合并 |
| `mineru_service.py` | MinerU 解析服务封装，支持 pipeline/vlm/hybrid 后端 |
| `test_cross_page_merge.py` | 跨页合并测试脚本 |
| `example_usage.py` | MinerU 服务使用示例 |

## 快速开始

### 1. 环境变量

```bash
cp .env.example .env
# 编辑 .env 填入 API Key
```

支持的环境变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `VLM_BASE_URL` | VLM API 地址 | `https://ark.cn-beijing.volces.com/api/v3` |
| `VLM_MODEL` | VLM 模型名 | `doubao-seed-2-0-lite-260428` |
| `VLM_API_KEY` | API Key | (必填) |

### 2. 完整流程 (从 .doc 开始)

```bash
python parse_doc.py /path/to/document.doc --vlm
```

### 3. 仅页内合并 (已有 middle.json)

```bash
python merge_optimized.py middle.json --pdf input.pdf
```

输出：
- `{name}_layout.json` — 布局 JSON (含 confirmed_index)
- `confirmed_images/` — 合并后的图片
- `{name}_confirmed.pdf` / `{name}_detailed.pdf` — 画框 PDF
- `{name}_confirmed.md` / `{name}_detailed.md` — Markdown

### 4. 仅跨页合并 (已有页内合并结果)

```bash
python merge_cross_page.py middle.json
```

自动查找同目录下的 `{name}_layout.json` 和 PDF，输出更新后的 layout JSON。

### 5. 完整流水线 (页内 + 跨页)

```bash
python merge_cross_page.py middle.json --full-pipeline --pdf input.pdf
```

## 索引体系

| 索引 | 含义 |
|------|------|
| `detailed_index` | 页内从 0 连续，原始粒度。synthetic 块 (合并产生) 为 `None` 后重分配 |
| `confirmed_index` | VLM 合并后分配，仅非子块从 0 连续。子块为 `None` |
| `merged_from` | 列表，页内合并的来源 detailed_index |
| `merged_from_cross` | 列表，跨页合并的来源 `{page, index}` |
| `belong_to` | 标量，指向父合并块的 confirmed_index |

## 目录结构 (运行后)

```
output/{doc_name}/
├── auto/
│   ├── {name}_middle.json          # MinerU 解析结果
│   ├── {name}_layout.json          # 合并后的布局 JSON
│   ├── {name}_confirmed.pdf        # 画框 PDF (confirmed 模式)
│   ├── {name}_detailed.pdf         # 画框 PDF (detailed 模式)
│   ├── {name}_confirmed.md         # Markdown (confirmed 模式)
│   ├── {name}_detailed.md          # Markdown (detailed 模式)
│   ├── images/                     # MinerU 原始图片
│   ├── confirmed_images/           # 合并后的图片
│   ├── debug_vlm_annotated/        # 页内合并调试图
│   └── debug_cross_page/           # 跨页合并调试图
└── {name}.pdf
```

## VLM Prompt 策略

### 页内合并

给 VLM 展示单页标注图 (所有块画框 + detailed_index 编号)，返回 `[[1,2,3],[4,5],[6]]` 格式的合并分组。

### 跨页合并

给 VLM 展示多页标注图 (每页底部居中红色页码，只画父块)，多图上传，返回：

```json
{
  "has_cross_page_merge": true,
  "merge_groups": [
    {"blocks": [{"page": 8, "index": 10}, {"page": 9, "index": 0}]}
  ]
}
```

## 依赖

```
mineru
pypdf
reportlab
pdf2image
Pillow
loguru
requests
```
