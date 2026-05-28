# doc_operation

基于 MinerU 解析结果的文档布局合并工具链，通过 VLM 对表格、图片等元素进行页内合并和跨页合并，最终导出 Markdown / PDF。

## 整体流程

```
.doc/.pdf
    │
    ▼
  pipeline.py               一键执行完整流水线
    │
    ├─ 步骤1: parse_doc.py       MinerU 解析 → middle.json + images/
    ├─ 步骤2: merge_optimized.py 页内合并 (VLM) → layout.json + confirmed_images/
    └─ 步骤3: merge_cross_page.py 跨页合并 (VLM) → 更新 layout.json
    │
    ▼
  导出 Markdown / PDF
```

## 文件说明

| 文件 | 功能 |
|------|------|
| `pipeline.py` | **一键流水线**。串联三个步骤，默认启用 VLM 页内合并 + 跨页合并 |
| `parse_doc.py` | 步骤1: MinerU 解析。`.doc/.pdf` → middle.json + images/ |
| `merge_optimized.py` | 步骤2: 页内合并。提取 layout JSON，VLM 合并同页图表/图注/脚注 |
| `merge_cross_page.py` | 步骤3: 跨页合并。检测并合并跨页分割的表格/图片 |
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

### 2. 一键流水线 (推荐)

```bash
# 完整流程: MinerU 解析 → 页内合并(VLM) → 跨页合并(VLM) → 导出
python pipeline.py /path/to/document.doc

# 指定输出目录
python pipeline.py /path/to/document.doc -o /path/to/output

# 禁用 VLM，纯 MinerU 解析
python pipeline.py /path/to/document.doc --no-vlm

# 只启用页内合并，禁用跨页合并
python pipeline.py /path/to/document.doc --no-cross-page
```

### 3. 单独运行各步骤

```bash
# 步骤1: MinerU 解析 (.doc → middle.json)
python parse_doc.py /path/to/document.doc

# 步骤2: 页内合并 (middle.json → layout.json)
python merge_optimized.py middle.json --pdf input.pdf

# 步骤3: 跨页合并 (layout.json → 最终结果)
python merge_cross_page.py layout.json --pdf input.pdf
```

## 索引体系

| 索引 | 含义 |
|------|------|
| `detailed_index` | 页内从 0 连续，原始粒度。synthetic 块 (合并产生) 为 `None` 后重分配 |
| `confirmed_index` | VLM 合并后分配，仅非子块从 0 连续。子块为 `None` |
| `merged_from` | 列表，页内合并的来源 detailed_index |
| `merged_from_cross` | 列表，跨页合并的来源 `{page, index}` |
| `belong_to` | 标量，指向父合并块的 confirmed_index |

## 目录结构 (pipeline 运行后)

```
output/{doc_name}/
├── auto/
│   ├── {name}_middle.json          # MinerU 解析结果
│   └── images/                     # MinerU 原始图片
├── {name}_layout.json              # 合并后的布局 JSON
├── {name}_confirmed.pdf            # 画框 PDF (confirmed 模式)
├── {name}_confirmed.md             # Markdown (confirmed 模式)
├── confirmed_images/               # 合并后的图片
├── debug_vlm_annotated/            # 页内合并调试图 (启用 VLM 时)
├── debug_cross_page/               # 跨页合并调试图 (启用跨页时)
└── {name}.pdf                      # 原始 PDF 副本
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

```bash
conda activate newmineru
pip install -r requirements.txt
```

| 包 | 版本 | 用途 |
|---|---|---|
| mineru | 3.1.15 | 文档解析引擎 |
| pdf2image | 1.17.0 | PDF 转图像 |
| pillow | 12.2.0 | 图像处理/标注 |
| pypdf | 6.12.1 | PDF 读写 |
| reportlab | 4.5.1 | PDF 画框叠加 |
| loguru | 0.7.3 | 日志 |
| requests | 2.34.2 | VLM API 调用 |
