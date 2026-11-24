"""文档解析的格式覆盖测试 — 每种格式都必须真的读出正文。

守的是一条线：**用户发来的文件，模型要能读到里面的字**。历史实现只覆盖
xlsx/docx/pdf，xls 会被 openpyxl 抛 `BadZipFile`、pptx/odt 直接落到「类型不支持」，
用户看到的是「附件解析失败」而不知道该转成什么格式。

这里每条都用**真文件**（现场生成或手工构造最小合法容器），不打桩解析库——
打桩只能证明「我们调了某个函数」，证明不了「这个函数吃得下这种文件」。
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from app.multimodal import _MAX_DOC_CHARS, _cap_doc_text, _parse_document


def _xlsx_bytes(rows: list[list[object]], sheet: str = "Sheet1") -> bytes:
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def _docx_bytes() -> bytes:
    import docx

    d = docx.Document()
    d.add_paragraph("第一段正文")
    table = d.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "列A"
    table.cell(0, 1).text = "列B"
    table.cell(1, 0).text = "值1"
    table.cell(1, 1).text = "值2"
    out = io.BytesIO()
    d.save(out)
    return out.getvalue()


def _pptx_bytes() -> bytes:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "季度复盘"
    slide.placeholders[1].text = "结论：增长 12%"
    slide.notes_slide.notes_text_frame.text = "讲稿备注"
    out = io.BytesIO()
    prs.save(out)
    return out.getvalue()


def _xls_bytes() -> bytes | None:
    """真 .xls（BIFF8）没法用纯 Python 写，改用 xlwt；没装就跳过。"""
    try:
        import xlwt
    except ImportError:
        return None
    book = xlwt.Workbook()
    sheet = book.add_sheet("旧表")
    sheet.write(0, 0, "编号")
    sheet.write(1, 0, "A-1")
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _odt_bytes() -> bytes:
    from odf.opendocument import OpenDocumentText
    from odf.table import Table, TableCell, TableRow
    from odf.text import P

    doc = OpenDocumentText()
    doc.text.addElement(P(text="ODF 正文一行"))
    table = Table(name="清单")
    row = TableRow()
    for value in ("名称", "状态"):
        cell = TableCell()
        cell.addElement(P(text=value))
        row.addElement(cell)
    table.addElement(row)
    doc.text.addElement(table)
    out = io.BytesIO()
    doc.save(out)
    return out.getvalue()


# ── 表格类 ──────────────────────────────────────────────────────────────


def test_xlsx_reads_all_sheets() -> None:
    raw = _xlsx_bytes([["编号", "金额"], ["1", "100"]], sheet="明细")
    text = _parse_document(raw, "a.xlsx")
    assert "【工作表 明细】" in text
    assert "编号 | 金额" in text
    assert "1 | 100" in text


def test_xls_legacy_biff_is_parsed() -> None:
    raw = _xls_bytes()
    if raw is None:
        pytest.skip("未安装 xlwt，无法生成 .xls 样本")
    text = _parse_document(raw, "old.xls")
    # 曾经的实现把 .xls 也交给 openpyxl → BadZipFile；这里守住「能读到内容」
    assert "【工作表 旧表】" in text
    assert "A-1" in text


def test_xls_container_error_is_explicit() -> None:
    """给 xlrd 一个非 xls 的字节流时，报错要能指回格式，而不是 zip 错误。"""
    with pytest.raises(Exception) as exc:
        _parse_document(b"not an xls at all", "bad.xls")
    assert "xls" in str(exc.value).lower() or "格式" in str(exc.value)


# ── 文字类 ──────────────────────────────────────────────────────────────


def test_docx_keeps_tables() -> None:
    text = _parse_document(_docx_bytes(), "报告.docx")
    assert "第一段正文" in text
    # 表格是清单/规格类文档的关键信息，纯段落抽取会整块丢掉
    assert "列A | 列B" in text
    assert "值1 | 值2" in text


def test_pptx_extracts_slides_in_order() -> None:
    text = _parse_document(_pptx_bytes(), "复盘.pptx")
    assert "【第 1 页】" in text
    assert "季度复盘" in text
    assert "增长 12%" in text
    assert "讲稿备注" in text


def test_odt_reads_paragraph_and_table() -> None:
    text = _parse_document(_odt_bytes(), "说明.odt")
    assert "ODF 正文一行" in text
    assert "名称 | 状态" in text


def test_rtf_strips_control_words() -> None:
    # RTF 的中文走 \\uN? 转义（RTF 本身就是 ASCII）：19990=世, 30028=界
    rtf = ("{\\rtf1\\ansi{\\fonttbl{\\f0 Arial;}}Hello \\u19990?\\u30028?"
           "\\par Second line}")
    text = _parse_document(rtf.encode("ascii"), "note.rtf")
    assert "Hello" in text and "世界" in text
    assert "Second" in text
    assert "fonttbl" not in text


def test_html_keeps_cell_separators() -> None:
    html = ("<html><body><table><tr><td>A</td><td>B</td></tr></table>"
            "<script>var x=1</script><p>正文</p></body></html>").encode()
    text = _parse_document(html, "page.html")
    assert "A | B" in text
    assert "正文" in text
    assert "var x" not in text


def test_gbk_csv_falls_back_to_gbk() -> None:
    raw = "编号,名称\n1,测试".encode("gbk")
    text = _parse_document(raw, "data.csv")
    assert "测试" in text


# ── 边界 ────────────────────────────────────────────────────────────────


def test_unknown_extension_raises_not_silent() -> None:
    """不认识的格式必须报错：静默返回空串会让模型以为文件是空的。"""
    with pytest.raises(RuntimeError, match="暂不支持"):
        _parse_document(b"\x00\x01binary", "thing.xyz")


def test_long_document_is_capped_and_says_so() -> None:
    text = _cap_doc_text("x" * (_MAX_DOC_CHARS + 500), "big.pdf", _MAX_DOC_CHARS)
    assert len(text) > _MAX_DOC_CHARS
    assert "已截取前" in text and "big.pdf" in text


def test_budget_exhausted_is_explained() -> None:
    text = _cap_doc_text("anything", "later.xlsx", 0)
    assert "未注入" in text and "单独重发" in text


def test_pptx_is_zip_container_not_text() -> None:
    """pptx 走容器分支，不能被当成纯文本解码（否则是满屏乱码）。"""
    raw = _pptx_bytes()
    assert zipfile.is_zipfile(io.BytesIO(raw))
    text = _parse_document(raw, "deck.pptx")
    assert "PK\x03\x04" not in text


def test_doc_without_antiword_reports_actionable_error() -> None:
    """没有 antiword 时给的是「另存为 docx」，不是一行 traceback。"""
    import app.multimodal as mm

    original = mm.shutil.which
    mm.shutil.which = lambda name: None  # type: ignore[assignment]
    try:
        with pytest.raises(Exception) as exc:
            _parse_document(b"\xd0\xcf\x11\xe0junk", "legacy.doc")
        assert "docx" in str(exc.value)
    finally:
        mm.shutil.which = original  # type: ignore[assignment]


def test_temp_file_is_cleaned_up(tmp_path: Path, monkeypatch) -> None:
    """pdf 分支会落临时文件；失败路径也要删掉，否则 logs 里堆垃圾。"""
    import tempfile

    created: list[str] = []
    real_named = tempfile.NamedTemporaryFile

    def _spy(*a, **kw):
        fh = real_named(*a, **kw)
        created.append(fh.name)
        return fh

    monkeypatch.setattr(tempfile, "NamedTemporaryFile", _spy)
    # 坏 PDF 的异常由 pypdf 抛（PdfStreamError），不是本模块的 RuntimeError——
    # 这里验的是「失败路径也清理临时文件」，异常类型不重要
    with pytest.raises(Exception):  # noqa: B017
        _parse_document(b"not a pdf", "broken.pdf")
    assert created
    assert not any(Path(name).exists() for name in created)
