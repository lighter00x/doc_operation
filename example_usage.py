"""
MinerU 文档解析服务 - 使用示例
"""

from mineru_service import MinerUService, MinerUConfig, Backend, ParseMethod, MarkdownMode, parse_document


# ============================================================
# 示例 1: 快捷函数 - 一行代码解析文档
# ============================================================

def example_quick_parse():
    result = parse_document(
        file_path="/path/to/document.pdf",
        backend="vlm-vllm-engine",
        formula_enable=True,
        table_enable=True,
        image_analysis=True,
    )
    print(result.markdown)
    print(result.content_list)


# ============================================================
# 示例 2: Pipeline 后端解析（传统多模型管线）
# ============================================================

def example_pipeline():
    config = MinerUConfig(
        backend=Backend.PIPELINE,
        parse_method=ParseMethod.AUTO,     # auto / txt / ocr
        formula_enable=True,               # 解析数学公式
        table_enable=True,                 # 解析表格
        language="ch",                     # ch / en / korean / japan
        start_page=0,
        end_page=10,                       # 只解析前 10 页
        output_dir="./output_pipeline",    # 输出目录（None 则仅返回内存结果）
    )
    service = MinerUService(config)

    result = service.parse("/path/to/document.pdf")
    print("=== Markdown ===")
    print(result.markdown)
    print("=== Content List ===")
    print(result.content_list)


# ============================================================
# 示例 3: VLM + vLLM 后端解析（推荐，速度最快）
# ============================================================

def example_vlm_vllm():
    config = MinerUConfig(
        backend=Backend.VLM_VLLM,
        formula_enable=True,
        table_enable=True,
        image_analysis=True,               # 是否解析图表/图片
        markdown_mode=MarkdownMode.MM_MD,  # mm_markdown / nlp_markdown
        # output_dir="./output_vlm",       # 设为 None 则不写文件
        kwargs={
            # "gpu_memory_utilization": 0.8,  # vLLM GPU 显存利用率
            # "max_model_len": 4096,
        },
    )
    service = MinerUService(config)

    # 单文件解析（内存模式，不写文件）
    result = service.parse("/path/to/document.pdf")
    print(result.markdown)

    # 批量解析
    results = service.batch_parse([
        "/path/to/doc1.pdf",
        "/path/to/doc2.pdf",
    ])
    for r in results:
        print(f"--- {r.file_name} ---")
        print(r.markdown[:200])


# ============================================================
# 示例 4: Hybrid 后端解析（VLM 布局 + Pipeline OCR，高精度）
# ============================================================

def example_hybrid():
    config = MinerUConfig(
        backend=Backend.HYBRID_VLLM,
        parse_method=ParseMethod.AUTO,
        formula_enable=True,
        table_enable=True,
        image_analysis=True,
        language="ch",
    )
    service = MinerUService(config)

    result = service.parse("/path/to/document.pdf")
    print(result.markdown)


# ============================================================
# 示例 5: 远程 HTTP 服务调用（轻量客户端，无需本地 GPU）
# ============================================================

def example_http_client():
    config = MinerUConfig(
        backend=Backend.VLM_HTTP_CLIENT,
        server_url="http://127.0.0.1:8000",  # mineru-api 服务地址
        image_analysis=True,
    )
    service = MinerUService(config)

    result = service.parse("/path/to/document.pdf")
    print(result.markdown)


# ============================================================
# 示例 6: 异步解析（仅 vlm-vllm-async-engine 支持）
# ============================================================

async def example_async():
    config = MinerUConfig(
        backend=Backend.VLM_VLLM_ASYNC,
        formula_enable=True,
        table_enable=True,
    )
    service = MinerUService(config)

    result = await service.async_parse("/path/to/document.pdf")
    print(result.markdown)

    # 异步批量
    results = await service.async_batch_parse([
        "/path/to/doc1.pdf",
        "/path/to/doc2.pdf",
    ])


# ============================================================
# 示例 7: 仅解析图表，不解析公式和表格
# ============================================================

def example_chart_only():
    config = MinerUConfig(
        backend=Backend.VLM_VLLM,
        formula_enable=False,
        table_enable=False,
        image_analysis=True,
    )
    service = MinerUService(config)

    result = service.parse("/path/to/report.pdf")
    print(result.markdown)


# ============================================================
# 示例 8: 解析后写文件 + 返回结构化结果
# ============================================================

def example_with_output():
    config = MinerUConfig(
        backend=Backend.VLM_VLLM,
        output_dir="./parsed_output",
        dump_middle_json=True,
        dump_content_list=True,
    )
    service = MinerUService(config)

    result = service.parse("/path/to/document.pdf")
    print(f"输出目录: {result.output_dir}")
    print(f"Markdown: {result.markdown[:200]}")
    print(f"Content List 条目数: {len(result.content_list)}")


# ============================================================
# 示例 9: 运行时覆盖参数
# ============================================================

def example_runtime_override():
    config = MinerUConfig(
        backend=Backend.VLM_VLLM,
        formula_enable=True,
    )
    service = MinerUService(config)

    # 运行时临时切换后端或参数
    result = service.parse(
        "/path/to/document.pdf",
        backend=Backend.PIPELINE,
        formula_enable=False,
    )
    print(result.markdown)


# ============================================================
# 示例 10: 解析 Office 文档（DOCX / PPTX / XLSX）
# ============================================================

def example_office():
    config = MinerUConfig(output_dir="./output_office")
    service = MinerUService(config)

    result = service.parse("/path/to/document.docx")
    print(result.markdown)


if __name__ == "__main__":
    import sys
    # 根据实际文件路径运行对应示例
    if len(sys.argv) > 1:
        result = parse_document(sys.argv[1])
        print(result.markdown)
    else:
        print("用法: python example_usage.py <pdf_path>")
