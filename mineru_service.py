"""
MinerU 文档解析服务封装
支持 pipeline / vlm / hybrid 三种后端，提供同步与异步接口
"""

import asyncio
import json
import os
import tempfile

os.environ.setdefault("MINERU_MODEL_SOURCE", "local")
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

from loguru import logger
from mineru.cli.common import (
    do_parse,
    aio_do_parse,
    read_fn,
    prepare_env,
    _prepare_pdf_bytes,
    _process_output,
    _process_pipeline,
    _process_vlm,
    _process_hybrid,
    _process_office_doc,
)
from mineru.data.data_reader_writer import FileBasedDataWriter, DummyDataWriter
from mineru.utils.enum_class import MakeMode


class Backend(str, Enum):
    """解析后端类型"""
    PIPELINE = "pipeline"
    VLM_VLLM = "vlm-vllm-engine"
    VLM_VLLM_ASYNC = "vlm-vllm-async-engine"
    VLM_TRANSFORMERS = "vlm-transformers"
    VLM_AUTO = "vlm-auto-engine"
    VLM_HTTP_CLIENT = "vlm-http-client"
    HYBRID_VLLM = "hybrid-vllm-engine"
    HYBRID_VLLM_ASYNC = "hybrid-vllm-async-engine"
    HYBRID_AUTO = "hybrid-auto-engine"
    HYBRID_HTTP_CLIENT = "hybrid-http-client"


class ParseMethod(str, Enum):
    """解析方法（仅 pipeline / hybrid 有效）"""
    AUTO = "auto"
    TXT = "txt"
    OCR = "ocr"


class MarkdownMode(str, Enum):
    """Markdown 输出模式"""
    MM_MD = "mm_markdown"
    NLP_MD = "nlp_markdown"
    CONTENT_LIST = "content_list"
    CONTENT_LIST_V2 = "content_list_v2"


@dataclass
class ParseResult:
    """单个文档的解析结果"""
    file_name: str
    markdown: Optional[str] = None
    content_list: Optional[list] = None
    content_list_v2: Optional[list] = None
    middle_json: Optional[dict] = None
    model_output: Optional[list] = None
    output_dir: Optional[str] = None


@dataclass
class MinerUConfig:
    """
    MinerU 解析配置

    Attributes:
        backend:            解析后端，支持 pipeline / vlm-vllm-engine / hybrid-vllm-engine 等
        parse_method:       解析方法 auto/txt/ocr（仅 pipeline 和 hybrid 后端有效）
        formula_enable:     是否解析数学公式
        table_enable:       是否解析表格
        image_analysis:     是否解析图表/图片（VLM 和 hybrid 后端有效）
        language:           文档语言 ch/en/korean/japan 等（仅 pipeline 和 hybrid 后端有效）
        start_page:         起始页码（0-indexed）
        end_page:           结束页码（0-indexed，None 表示最后一页）
        markdown_mode:      Markdown 输出模式
        draw_layout_bbox:   是否输出布局检测可视化 PDF
        draw_span_bbox:     是否输出 span 检测可视化 PDF
        dump_middle_json:   是否输出中间 JSON
        dump_model_output:  是否输出模型原始输出
        dump_content_list:  是否输出 content_list JSON
        dump_orig_pdf:      是否保留原始 PDF 副本
        server_url:         远程服务地址（*-http-client 后端使用）
        output_dir:         输出目录（None 则不写文件，仅返回结果）
        kwargs:             传递给 VLM 引擎的额外参数（如 gpu_memory_utilization 等）
    """
    backend: Backend = Backend.VLM_VLLM
    parse_method: ParseMethod = ParseMethod.AUTO
    formula_enable: bool = True
    table_enable: bool = True
    image_analysis: bool = True
    language: str = "ch"
    start_page: int = 0
    end_page: Optional[int] = None
    markdown_mode: MarkdownMode = MarkdownMode.MM_MD
    draw_layout_bbox: bool = False
    draw_span_bbox: bool = False
    dump_middle_json: bool = False
    dump_model_output: bool = False
    dump_content_list: bool = False
    dump_orig_pdf: bool = False
    server_url: Optional[str] = None
    output_dir: Optional[str] = None
    kwargs: dict = field(default_factory=dict)


class MinerUService:
    """
    MinerU 文档解析服务

    使用示例:
        config = MinerUConfig(
            backend=Backend.VLM_VLLM,
            formula_enable=True,
            table_enable=True,
            image_analysis=True,
            output_dir="./output",
        )
        service = MinerUService(config)

        # 解析单个文件
        result = service.parse("/path/to/document.pdf")
        print(result.markdown)

        # 批量解析
        results = service.batch_parse(["/path/to/doc1.pdf", "/path/to/doc2.pdf"])

        # 异步解析（仅 vlm-vllm-async-engine / hybrid-vllm-async-engine 支持）
        result = await service.async_parse("/path/to/document.pdf")
    """

    def __init__(self, config: Optional[MinerUConfig] = None):
        self.config = config or MinerUConfig()

    def parse(
        self,
        file_path: str | Path,
        output_dir: Optional[str] = None,
        backend: Optional[Backend] = None,
        **kwargs,
    ) -> ParseResult:
        """
        解析单个文档

        Args:
            file_path:   文档路径（PDF / 图片 / DOCX / PPTX / XLSX）
            output_dir:  覆盖配置中的输出目录
            backend:     覆盖配置中的后端
            **kwargs:    覆盖配置中的其他参数

        Returns:
            ParseResult 包含 markdown、content_list 等解析结果
        """
        results = self.batch_parse([file_path], output_dir=output_dir, backend=backend, **kwargs)
        return results[0]

    def batch_parse(
        self,
        file_paths: list[str | Path],
        output_dir: Optional[str] = None,
        backend: Optional[Backend] = None,
        **kwargs,
    ) -> list[ParseResult]:
        """
        批量解析文档

        Args:
            file_paths:  文档路径列表
            output_dir:  覆盖配置中的输出目录
            backend:     覆盖配置中的后端
            **kwargs:    覆盖配置中的其他参数

        Returns:
            ParseResult 列表
        """
        cfg = self._merge_config(output_dir=output_dir, backend=backend, **kwargs)
        file_paths = [Path(p) if not isinstance(p, Path) else p for p in file_paths]

        pdf_file_names = [p.stem for p in file_paths]
        pdf_bytes_list = [read_fn(p) for p in file_paths]
        p_lang_list = [cfg.language] * len(file_paths)

        if cfg.output_dir:
            return self._parse_with_output(
                cfg, pdf_file_names, pdf_bytes_list, p_lang_list,
            )
        else:
            return self._parse_in_memory(
                cfg, pdf_file_names, pdf_bytes_list, p_lang_list,
            )

    async def async_parse(
        self,
        file_path: str | Path,
        output_dir: Optional[str] = None,
        backend: Optional[Backend] = None,
        **kwargs,
    ) -> ParseResult:
        """异步解析单个文档（仅 vlm-vllm-async-engine / hybrid-vllm-async-engine 支持）"""
        results = await self.async_batch_parse([file_path], output_dir=output_dir, backend=backend, **kwargs)
        return results[0]

    async def async_batch_parse(
        self,
        file_paths: list[str | Path],
        output_dir: Optional[str] = None,
        backend: Optional[Backend] = None,
        **kwargs,
    ) -> list[ParseResult]:
        """异步批量解析文档"""
        cfg = self._merge_config(output_dir=output_dir, backend=backend, **kwargs)
        file_paths = [Path(p) if not isinstance(p, Path) else p for p in file_paths]

        pdf_file_names = [p.stem for p in file_paths]
        pdf_bytes_list = [read_fn(p) for p in file_paths]
        p_lang_list = [cfg.language] * len(file_paths)

        if cfg.output_dir:
            return await self._async_parse_with_output(
                cfg, pdf_file_names, pdf_bytes_list, p_lang_list,
            )
        else:
            return await self._async_parse_in_memory(
                cfg, pdf_file_names, pdf_bytes_list, p_lang_list,
            )

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    def _merge_config(self, output_dir=None, backend=None, **kwargs) -> MinerUConfig:
        """合并运行时参数与默认配置"""
        cfg = MinerUConfig(
            backend=backend or self.config.backend,
            parse_method=self.config.parse_method,
            formula_enable=self.config.formula_enable,
            table_enable=self.config.table_enable,
            image_analysis=self.config.image_analysis,
            language=self.config.language,
            start_page=self.config.start_page,
            end_page=self.config.end_page,
            markdown_mode=self.config.markdown_mode,
            draw_layout_bbox=self.config.draw_layout_bbox,
            draw_span_bbox=self.config.draw_span_bbox,
            dump_middle_json=self.config.dump_middle_json,
            dump_model_output=self.config.dump_model_output,
            dump_content_list=self.config.dump_content_list,
            dump_orig_pdf=self.config.dump_orig_pdf,
            server_url=self.config.server_url,
            output_dir=output_dir if output_dir is not None else self.config.output_dir,
            kwargs={**self.config.kwargs, **kwargs},
        )
        return cfg

    def _parse_with_output(
        self,
        cfg: MinerUConfig,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
    ) -> list[ParseResult]:
        """写文件模式：直接调用 do_parse，结果写入磁盘"""
        do_parse(
            output_dir=cfg.output_dir,
            pdf_file_names=pdf_file_names,
            pdf_bytes_list=pdf_bytes_list,
            p_lang_list=p_lang_list,
            backend=cfg.backend.value,
            parse_method=cfg.parse_method.value,
            formula_enable=cfg.formula_enable,
            table_enable=cfg.table_enable,
            server_url=cfg.server_url,
            f_draw_layout_bbox=cfg.draw_layout_bbox,
            f_draw_span_bbox=cfg.draw_span_bbox,
            f_dump_md=True,
            f_dump_middle_json=cfg.dump_middle_json,
            f_dump_model_output=cfg.dump_model_output,
            f_dump_orig_pdf=cfg.dump_orig_pdf,
            f_dump_content_list=cfg.dump_content_list,
            f_make_md_mode=cfg.markdown_mode.value,
            start_page_id=cfg.start_page,
            end_page_id=cfg.end_page,
            image_analysis=cfg.image_analysis,
            **cfg.kwargs,
        )

        results = []
        for name in pdf_file_names:
            result = ParseResult(file_name=name, output_dir=cfg.output_dir)
            md_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}.md"
            if md_path.exists():
                result.markdown = md_path.read_text(encoding="utf-8")
            cl_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}_content_list.json"
            if cl_path.exists():
                result.content_list = json.loads(cl_path.read_text(encoding="utf-8"))
            cl2_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}_content_list_v2.json"
            if cl2_path.exists():
                result.content_list_v2 = json.loads(cl2_path.read_text(encoding="utf-8"))
            mj_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}_middle.json"
            if mj_path.exists():
                result.middle_json = json.loads(mj_path.read_text(encoding="utf-8"))
            mo_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}_model.json"
            if mo_path.exists():
                result.model_output = json.loads(mo_path.read_text(encoding="utf-8"))
            results.append(result)
        return results

    def _parse_in_memory(
        self,
        cfg: MinerUConfig,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
    ) -> list[ParseResult]:
        """
        内存模式：不写文件，直接返回解析结果
        通过 DummyDataWriter 抑制文件输出，手动提取 markdown 和 content_list
        """
        from mineru.cli.common import _prepare_pdf_bytes, prepare_env
        from mineru.utils.guess_suffix_or_lang import guess_suffix_by_bytes

        backend = cfg.backend.value
        office_suffixes = ["docx", "pptx", "xlsx"]

        # 分离 office 文件和 PDF/图片文件
        office_indices = set()
        office_results: dict[int, ParseResult] = {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            # 处理 office 文档（office 解析器需要文件写入）
            for i, (name, bts) in enumerate(zip(pdf_file_names, pdf_bytes_list)):
                suffix = guess_suffix_by_bytes(bts)
                if suffix in office_suffixes:
                    office_indices.add(i)
                    local_image_dir, local_md_dir = prepare_env(tmp_dir, name, "office")
                    image_writer = FileBasedDataWriter(local_image_dir)
                    md_writer = FileBasedDataWriter(local_md_dir)

                    if suffix == "docx":
                        from mineru.backend.office.docx_analyze import office_docx_analyze as analyze_fn
                    elif suffix == "pptx":
                        from mineru.backend.office.pptx_analyze import office_pptx_analyze as analyze_fn
                    else:
                        from mineru.backend.office.xlsx_analyze import office_xlsx_analyze as analyze_fn

                    middle_json, infer_result = analyze_fn(bts, image_writer=image_writer)
                    _process_output(
                        middle_json["pdf_info"], bts, name, local_md_dir, local_image_dir,
                        md_writer, False, False, False, True, True, False, False,
                        cfg.markdown_mode.value, middle_json, infer_result, process_mode=suffix,
                    )
                    md_path = Path(local_md_dir) / f"{name}.md"
                    office_results[i] = ParseResult(
                        file_name=name,
                        markdown=md_path.read_text(encoding="utf-8") if md_path.exists() else None,
                        middle_json=middle_json,
                        model_output=infer_result,
                    )

        # PDF/图片文件
        remaining_names = []
        remaining_bytes = []
        remaining_langs = []
        for i, (name, bts, lang) in enumerate(zip(pdf_file_names, pdf_bytes_list, p_lang_list)):
            if i not in office_indices:
                remaining_names.append(name)
                remaining_bytes.append(bts)
                remaining_langs.append(lang)

        # 预处理 PDF
        if remaining_bytes:
            remaining_bytes = _prepare_pdf_bytes(remaining_bytes, cfg.start_page, cfg.end_page)

        # 根据后端分发 PDF/图片解析
        pdf_results: list[ParseResult] = []
        if remaining_bytes:
            if backend == "pipeline":
                pdf_results = self._run_pipeline_in_memory(cfg, remaining_names, remaining_bytes, remaining_langs)
            elif backend.startswith("vlm-"):
                vlm_backend = backend[4:]
                if vlm_backend == "auto-engine":
                    from mineru.utils.engine_utils import get_vlm_engine
                    vlm_backend = get_vlm_engine(inference_engine='auto', is_async=False)
                os.environ['MINERU_VLM_FORMULA_ENABLE'] = str(cfg.formula_enable)
                os.environ['MINERU_VLM_TABLE_ENABLE'] = str(cfg.table_enable)
                pdf_results = self._run_vlm_in_memory(cfg, remaining_names, remaining_bytes, vlm_backend)
            elif backend.startswith("hybrid-"):
                hybrid_backend = backend[7:]
                if hybrid_backend == "auto-engine":
                    from mineru.utils.engine_utils import get_vlm_engine
                    hybrid_backend = get_vlm_engine(inference_engine='auto', is_async=False)
                os.environ['MINERU_VLM_TABLE_ENABLE'] = str(cfg.table_enable)
                os.environ['MINERU_VLM_FORMULA_ENABLE'] = "true"
                pdf_results = self._run_hybrid_in_memory(cfg, remaining_names, remaining_bytes, remaining_langs, hybrid_backend)

        # 按原始顺序合并 office 结果和 PDF 结果
        final_results: list[ParseResult] = [None] * len(pdf_file_names)
        pdf_iter = iter(pdf_results)
        for i in range(len(pdf_file_names)):
            if i in office_results:
                final_results[i] = office_results[i]
            else:
                final_results[i] = next(pdf_iter)

        return final_results

    def _run_pipeline_in_memory(self, cfg, names, bytes_list, langs) -> list[ParseResult]:
        """Pipeline 后端内存模式"""
        from mineru.backend.pipeline.pipeline_analyze import doc_analyze_streaming as pipeline_doc_analyze_streaming

        results_map: dict[int, ParseResult] = {}
        image_writer_list = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            for name in names:
                local_image_dir, _ = prepare_env(tmp_dir, name, cfg.parse_method.value)
                image_writer_list.append(FileBasedDataWriter(local_image_dir))

            def on_doc_ready(doc_index, model_list, middle_json, ocr_enable):
                name = names[doc_index]
                make_func = self._get_make_func("pipeline")
                image_dir = "images"
                md_content = make_func(middle_json["pdf_info"], cfg.markdown_mode.value, image_dir)
                content_list = make_func(middle_json["pdf_info"], MakeMode.CONTENT_LIST, image_dir)
                content_list_v2 = make_func(middle_json["pdf_info"], MakeMode.CONTENT_LIST_V2, image_dir)
                results_map[doc_index] = ParseResult(
                    file_name=name,
                    markdown=md_content,
                    content_list=content_list,
                    content_list_v2=content_list_v2,
                    middle_json=middle_json,
                    model_output=model_list,
                )

            pipeline_doc_analyze_streaming(
                bytes_list,
                image_writer_list,
                langs,
                on_doc_ready,
                parse_method=cfg.parse_method.value,
                formula_enable=cfg.formula_enable,
                table_enable=cfg.table_enable,
            )

        return [results_map[i] for i in range(len(names))]

    def _run_vlm_in_memory(self, cfg, names, bytes_list, backend) -> list[ParseResult]:
        """VLM 后端内存模式"""
        from mineru.backend.vlm.vlm_analyze import doc_analyze as vlm_doc_analyze

        results = []
        make_func = self._get_make_func("vlm")

        for i, (name, pdf_bytes) in enumerate(zip(names, bytes_list)):
            image_writer = DummyDataWriter()
            middle_json, infer_result = vlm_doc_analyze(
                pdf_bytes,
                image_writer=image_writer,
                backend=backend,
                server_url=cfg.server_url,
                image_analysis=cfg.image_analysis,
                **cfg.kwargs,
            )
            pdf_info = middle_json["pdf_info"]
            image_dir = "images"
            md_content = make_func(pdf_info, cfg.markdown_mode.value, image_dir)
            content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
            content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
            results.append(ParseResult(
                file_name=name,
                markdown=md_content,
                content_list=content_list,
                content_list_v2=content_list_v2,
                middle_json=middle_json,
                model_output=infer_result,
            ))
        return results

    def _run_hybrid_in_memory(self, cfg, names, bytes_list, langs, backend) -> list[ParseResult]:
        """Hybrid 后端内存模式"""
        from mineru.backend.hybrid.hybrid_analyze import doc_analyze as hybrid_doc_analyze

        results = []
        make_func = self._get_make_func("vlm")

        for i, (name, pdf_bytes, lang) in enumerate(zip(names, bytes_list, langs)):
            image_writer = DummyDataWriter()
            middle_json, infer_result, _vlm_ocr_enable = hybrid_doc_analyze(
                pdf_bytes,
                image_writer=image_writer,
                backend=backend,
                parse_method=cfg.parse_method.value,
                language=lang,
                inline_formula_enable=cfg.formula_enable,
                server_url=cfg.server_url,
                image_analysis=cfg.image_analysis,
                **cfg.kwargs,
            )
            pdf_info = middle_json["pdf_info"]
            image_dir = "images"
            md_content = make_func(pdf_info, cfg.markdown_mode.value, image_dir)
            content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
            content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
            results.append(ParseResult(
                file_name=name,
                markdown=md_content,
                content_list=content_list,
                content_list_v2=content_list_v2,
                middle_json=middle_json,
                model_output=infer_result,
            ))
        return results

    async def _async_parse_with_output(
        self,
        cfg: MinerUConfig,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
    ) -> list[ParseResult]:
        """异步写文件模式"""
        await aio_do_parse(
            output_dir=cfg.output_dir,
            pdf_file_names=pdf_file_names,
            pdf_bytes_list=pdf_bytes_list,
            p_lang_list=p_lang_list,
            backend=cfg.backend.value,
            parse_method=cfg.parse_method.value,
            formula_enable=cfg.formula_enable,
            table_enable=cfg.table_enable,
            server_url=cfg.server_url,
            f_draw_layout_bbox=cfg.draw_layout_bbox,
            f_draw_span_bbox=cfg.draw_span_bbox,
            f_dump_md=True,
            f_dump_middle_json=cfg.dump_middle_json,
            f_dump_model_output=cfg.dump_model_output,
            f_dump_orig_pdf=cfg.dump_orig_pdf,
            f_dump_content_list=cfg.dump_content_list,
            f_make_md_mode=cfg.markdown_mode.value,
            start_page_id=cfg.start_page,
            end_page_id=cfg.end_page,
            image_analysis=cfg.image_analysis,
            **cfg.kwargs,
        )
        results = []
        for name in pdf_file_names:
            result = ParseResult(file_name=name, output_dir=cfg.output_dir)
            md_path = Path(cfg.output_dir) / name / cfg.parse_method.value / f"{name}.md"
            if md_path.exists():
                result.markdown = md_path.read_text(encoding="utf-8")
            results.append(result)
        return results

    async def _async_parse_in_memory(
        self,
        cfg: MinerUConfig,
        pdf_file_names: list[str],
        pdf_bytes_list: list[bytes],
        p_lang_list: list[str],
    ) -> list[ParseResult]:
        """异步内存模式"""
        from mineru.backend.vlm.vlm_analyze import aio_doc_analyze as aio_vlm_doc_analyze
        from mineru.backend.hybrid.hybrid_analyze import aio_doc_analyze as aio_hybrid_doc_analyze
        from mineru.backend.hybrid.hybrid_analyze import doc_analyze as hybrid_doc_analyze

        backend = cfg.backend.value
        make_func = self._get_make_func("vlm")
        results = []

        pdf_bytes_list = _prepare_pdf_bytes(pdf_bytes_list, cfg.start_page, cfg.end_page)

        if backend.startswith("vlm-"):
            vlm_backend = backend[4:]
            for name, pdf_bytes in zip(pdf_file_names, pdf_bytes_list):
                image_writer = DummyDataWriter()
                middle_json, infer_result = await aio_vlm_doc_analyze(
                    pdf_bytes,
                    image_writer=image_writer,
                    backend=vlm_backend,
                    server_url=cfg.server_url,
                    image_analysis=cfg.image_analysis,
                    **cfg.kwargs,
                )
                pdf_info = middle_json["pdf_info"]
                image_dir = "images"
                md_content = make_func(pdf_info, cfg.markdown_mode.value, image_dir)
                content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
                content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
                results.append(ParseResult(
                    file_name=name,
                    markdown=md_content,
                    content_list=content_list,
                    content_list_v2=content_list_v2,
                    middle_json=middle_json,
                    model_output=infer_result,
                ))
        elif backend.startswith("hybrid-"):
            hybrid_backend = backend[7:]
            for name, pdf_bytes, lang in zip(pdf_file_names, pdf_bytes_list, p_lang_list):
                image_writer = DummyDataWriter()
                middle_json, infer_result, _ = await aio_hybrid_doc_analyze(
                    pdf_bytes,
                    image_writer=image_writer,
                    backend=hybrid_backend,
                    parse_method=cfg.parse_method.value,
                    language=lang,
                    inline_formula_enable=cfg.formula_enable,
                    server_url=cfg.server_url,
                    image_analysis=cfg.image_analysis,
                    **cfg.kwargs,
                )
                pdf_info = middle_json["pdf_info"]
                image_dir = "images"
                md_content = make_func(pdf_info, cfg.markdown_mode.value, image_dir)
                content_list = make_func(pdf_info, MakeMode.CONTENT_LIST, image_dir)
                content_list_v2 = make_func(pdf_info, MakeMode.CONTENT_LIST_V2, image_dir)
                results.append(ParseResult(
                    file_name=name,
                    markdown=md_content,
                    content_list=content_list,
                    content_list_v2=content_list_v2,
                    middle_json=middle_json,
                    model_output=infer_result,
                ))

        return results

    @staticmethod
    def _get_make_func(process_mode: str):
        """获取内容生成函数"""
        if process_mode == "pipeline":
            from mineru.backend.pipeline.pipeline_middle_json_mkcontent import union_make
        else:
            from mineru.backend.vlm.vlm_middle_json_mkcontent import union_make
        return union_make


# ------------------------------------------------------------------
# 快捷函数
# ------------------------------------------------------------------

def parse_document(
    file_path: str | Path,
    backend: str = "vlm-vllm-engine",
    output_dir: Optional[str] = None,
    formula_enable: bool = True,
    table_enable: bool = True,
    image_analysis: bool = True,
    language: str = "ch",
    start_page: int = 0,
    end_page: Optional[int] = None,
    **kwargs,
) -> ParseResult:
    """
    快捷解析函数 - 一行代码完成文档解析

    使用示例:
        result = parse_document("/path/to/doc.pdf", backend="vlm-vllm-engine")
        print(result.markdown)
    """
    config = MinerUConfig(
        backend=Backend(backend),
        formula_enable=formula_enable,
        table_enable=table_enable,
        image_analysis=image_analysis,
        language=language,
        start_page=start_page,
        end_page=end_page,
        output_dir=output_dir,
        kwargs=kwargs,
    )
    service = MinerUService(config)
    return service.parse(file_path)
