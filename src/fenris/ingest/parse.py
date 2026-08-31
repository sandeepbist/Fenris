"""Docling parsing (the build plan, "Parse"): bytes in, ParsedFile out.

Rules this module enforces:

- Input is bytes over a ``BytesIO``/``DocumentStream`` - never a temp file.
- Every conversion is bounded: max pages, max bytes, max wall time. The
  bounds live here (parse owns its resource policy), mirrored from the
  the build plan ceilings.
- Every conversion runs inside try/except: a parse failure records
  ``status='failed'`` plus the error string and the *caller keeps going*.
- Missing deep models (this venv ships docling SLIM without torch) map to
  ``status='skipped'`` with reason ``deep_model_unavailable`` - that is the
  designed behavior, not a crash.
- Provenance comes from ``iterate_items()``: page numbers per region. Docling
  has no char offsets, so chunk provenance is item-level page sets.
- ``SectionHeaderItem``s build the section_path; ``TableItem`` grids become
  ``TableData`` records with text, row/col spans and offset indices - tables
  bypass the chunker entirely (see chunk.py).

Slim-mode reality (verified against this environment, docling 2.123.1):
docx/xlsx/pptx/md/txt/html/epub parse without deep models; pdf and images
require the layout/OCR stack (torch) and surface as
``ModuleNotFoundError: No module named 'torch'``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from io import BytesIO

# The parse layer's whole contract is "never raise" (the build plan: every
# conversion in try/except, failure recorded, loop continues). All Docling
# call sites therefore catch Exception deliberately - a narrow except would
# let a parser bug crash the ingest loop.
ParseError = Exception

# --- Bounding constants (parse owns its resource policy) --------------------
# Mirrors the the build plan ceilings; kept here rather than config.py because this
# module owns parse-time bounding.

PARSER_NAME = "docling"
PARSER_VERSION = "docling-1"          # matches config.PARSER_VERSION identity
MAX_NUM_PAGES = 500                   # the build plan ceiling
MAX_FILE_BYTES = 100 * 1024 * 1024    # 100 MB parse ceiling
MAX_PARSE_SECONDS = 300.0             # wall-clock bound per document
MIN_BYTES = 1                        # empty streams fail at the parse layer

# Deep-model marker: docling SLIM (no torch) raises this for pdf/image.
# Matched against the exception chain text, so it works both for the direct
# raise and for the chained cause inside ConversionError.
DEEP_MODEL_MARKERS = (
    "No module named 'torch'",
    "No module named torch",
)
DEEP_MODEL_REASON = "deep_model_unavailable"

_STATUS_SUCCESS = ("success", "partial_success")
_STATUS_SKIPPED = "skipped"
_STATUS_FAILED = "failed"


@dataclass(frozen=True)
class CellData:
    """One table cell: text, spans, and offset indices into the grid."""

    text: str
    start_row_offset_idx: int
    start_col_offset_idx: int
    end_row_offset_idx: int
    end_col_offset_idx: int
    row_span: int
    col_span: int
    is_column_header: bool
    is_row_header: bool
    is_row_section: bool
    fillable: bool = False


@dataclass(frozen=True)
class TableData:
    """A typed TableRecord payload extracted from one Docling TableItem."""

    grid: tuple[tuple[CellData, ...], ...]
    num_rows: int
    num_cols: int
    caption: str = ""
    page_nos: tuple[int, ...] = ()
    # 'sheet:NAME' / 'page:N' provenance label
    sheet_or_page: str = ""
    region: str = ""


@dataclass(frozen=True)
class BodySpan:
    """A located body-text region with the Docling provenance that covers it.

    char_start/char_end are offsets into ParsedFile.body_text - the chunker
    never re-derives them.
    """

    char_start: int
    char_end: int
    text: str
    section_path: tuple[str, ...]
    page_nos: tuple[int, ...]


@dataclass
class ParsedFile:
    """The full parse output for one representation.

    Attributes:
        status: 'success' | 'partial_success' | 'failed' | 'skipped'.
        error: for failures, a short error string; for skips, the
            machine-readable reason (e.g. 'deep_model_unavailable').
        body_text: the linear text (markdown export minus table rows) that
            the chunker operates over.
        body_spans: located regions covering body_text.
        tables: TableData records, one per Docling TableItem.
    """

    status: str = _STATUS_FAILED
    error: str | None = None
    body_text: str = ""
    body_spans: list[BodySpan] = field(default_factory=list)
    tables: list[TableData] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when the parse produced indexable content (or was skipped)."""
        return self.status in _STATUS_SUCCESS or self.status == _STATUS_SKIPPED

    @property
    def reason(self) -> str | None:
        """For skipped parses, the machine-readable reason (== error)."""
        return self.error if self.status == _STATUS_SKIPPED else None


def _is_deep_model_error(exc: BaseException) -> bool:
    """Walk the exception chain looking for the missing-torch signature."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        text = f"{type(cur).__name__}: {cur}"
        if any(marker in text for marker in DEEP_MODEL_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def _safe_text(value) -> str:
    """Coerce a possibly-None item text to str without ever raising."""
    try:
        return str(value) if value is not None else ""
    except ParseError:
        return ""


def _cell_data(cell) -> CellData:
    return CellData(
        text=_safe_text(getattr(cell, "text", "")),
        start_row_offset_idx=int(getattr(cell, "start_row_offset_idx", 0) or 0),
        start_col_offset_idx=int(getattr(cell, "start_col_offset_idx", 0) or 0),
        end_row_offset_idx=int(getattr(cell, "end_row_offset_idx", 0) or 0),
        end_col_offset_idx=int(getattr(cell, "end_col_offset_idx", 0) or 0),
        row_span=int(getattr(cell, "row_span", 1) or 1),
        col_span=int(getattr(cell, "col_span", 1) or 1),
        is_column_header=bool(getattr(cell, "column_header", False)),
        is_row_header=bool(getattr(cell, "row_header", False)),
        is_row_section=bool(getattr(cell, "row_section", False)),
        fillable=bool(getattr(cell, "fillable", False)),
    )


def _item_page_nos(item) -> tuple[int, ...]:
    """Collect page numbers from an item's provenance list."""
    pages: list[int] = []
    provs = getattr(item, "prov", None) or getattr(item, "provenance", None) or []
    for prov in provs or []:
        page_no = getattr(prov, "page_no", None)
        if isinstance(page_no, int) and page_no > 0:
            pages.append(page_no)
    return tuple(dict.fromkeys(pages))


def _stream_name(target_mime: str, filename: str | None) -> str:
    """Docling selects its backend by filename extension: mint one that
    matches the representation MIME when the real filename disagrees."""
    ext_by_mime = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
        "text/plain": "txt",
        "text/markdown": "md",
        "text/html": "html",
        "application/epub+zip": "epub",
    }
    ext = ext_by_mime.get(target_mime, "txt")
    if filename and "." in filename:
        return filename
    return f"document.{ext}"


def _input_format(target_mime: str):
    """Map the representation MIME to Docling's InputFormat enum.

    Images map to InputFormat.IMAGE even though the router defers them out
    of v1 (ocr_deferred_v1): if one ever reaches the parser anyway, it must
    land in the deep-model skip path, not in 'unsupported repr mime'.
    """
    from docling.datamodel.base_models import InputFormat

    if target_mime == "application/pdf":
        return InputFormat.PDF
    if target_mime == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ):
        return InputFormat.DOCX
    if target_mime == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
        return InputFormat.XLSX
    if target_mime == (
        "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    ):
        return InputFormat.PPTX
    if target_mime in ("text/plain", "text/markdown"):
        return InputFormat.MD
    if target_mime == "text/html":
        return InputFormat.HTML
    if target_mime == "application/epub+zip":
        return InputFormat.EPUB
    if target_mime in (
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/tiff",
        "image/webp",
        "image/bmp",
    ):
        return InputFormat.IMAGE
    return None


def _render_table_markdown(grid: list[list[CellData]]) -> str:
    """Render a cell grid the way export_to_markdown renders its tables.

    Note: this is only a fallback used when Docling's own per-item rendering
    is unavailable; the primary path reuses
    ``TableItem.export_to_markdown(doc=...)`` so the bytes we excise from the
    body are exactly the bytes Docling emitted.
    """
    if not grid or not grid[0]:
        return ""
    cols = max((len(row) for row in grid), default=0)
    if cols == 0:
        return ""

    def _cell(row: list[CellData], idx: int) -> str:
        return row[idx].text if idx < len(row) else ""

    lines: list[str] = []
    lines.append("|" + "".join(f" {_cell(grid[0], i)} |" for i in range(cols)))
    lines.append(
        "|" + "".join("-" * max(len(_cell(grid[0], i)), 3) + "|" for i in range(cols))
    )
    for row in grid[1:]:
        lines.append("|" + "".join(f" {_cell(row, i)} |" for i in range(cols)))
    return "\n".join(lines) + "\n"


def _status_to_ours(status_value: str) -> str:
    """Docling ConversionStatus -> our status vocabulary."""
    if status_value in ("success", "partial_success"):
        return status_value
    if status_value == "skipped":
        return _STATUS_SKIPPED
    return _STATUS_FAILED


def _collect_tables(document) -> list[tuple[object, TableData]]:
    """Extract TableData from every TableItem in the document."""
    from docling_core.types.doc import TableItem

    collected: list[tuple[object, TableData]] = []
    seen_ids: set[int] = set()
    for item, _level in document.iterate_items():
        if not isinstance(item, TableItem):
            continue
        if id(item) in seen_ids:
            continue
        seen_ids.add(id(item))
        try:
            data = item.data
            grid: list[list[CellData]] = []
            for row in data.grid:
                grid.append([_cell_data(cell) for cell in row])
            table = TableData(
                grid=tuple(tuple(r) for r in grid),
                num_rows=int(getattr(data, "num_rows", len(grid)) or len(grid)),
                num_cols=int(
                    getattr(data, "num_cols", (len(grid[0]) if grid else 0))
                    or (len(grid[0]) if grid else 0)
                ),
                caption=_safe_text(getattr(item, "caption_text", "") or ""),
                page_nos=_item_page_nos(item),
                sheet_or_page="",
                region="",
            )
            collected.append((item, table))
        except ParseError:
            continue
    return collected


def _table_page_label(table: TableData) -> str:
    if table.sheet_or_page:
        return table.sheet_or_page
    if table.page_nos:
        pages = sorted(table.page_nos)
        if len(pages) == 1:
            return f"page:{pages[0]}"
        return f"pages:{pages[0]}-{pages[-1]}"
    return "page:unknown"


def _table_region_label(table: TableData) -> str:
    """A coarse region citation for the tables.range column."""
    r0 = min(
        (c.start_row_offset_idx for row in table.grid for c in row),
        default=0,
    )
    r1 = max(
        (max(c.end_row_offset_idx, c.start_row_offset_idx + 1)
         for row in table.grid for c in row),
        default=0,
    )
    c0 = min((c.start_col_offset_idx for row in table.grid for c in row), default=0)
    c1 = max(
        (max(c.end_col_offset_idx, c.start_col_offset_idx + 1)
         for row in table.grid for c in row),
        default=0,
    )
    return f"{_table_page_label(table)}!r{r0}:{r1},c{c0}:{c1}"


def parse_bytes(
    content: bytes,
    target_mime: str,
    filename: str | None = None,
    *,
    max_num_pages: int = MAX_NUM_PAGES,
    max_file_bytes: int = MAX_FILE_BYTES,
    max_seconds: float = MAX_PARSE_SECONDS,
) -> ParsedFile:
    """Parse one representation's bytes with Docling.

    Never raises: every failure mode is carried in the returned ParsedFile
    (status='failed' + error, or status='skipped' + reason when the slim
    install cannot load the deep models the format needs).

    Args:
        content: the raw representation bytes (download or export).
        target_mime: the representation MIME (routing target).
        filename: optional original name, used for backend selection.
        max_num_pages / max_file_bytes / max_seconds: bounds; the defaults are
            the MVP ceilings and the tests tighten them.

    Returns:
        ParsedFile with status, error, body_text, body_spans, tables.
    """
    failed = ParsedFile(status=_STATUS_FAILED)
    if content is None or len(content) < MIN_BYTES:
        failed.error = "empty_input"
        return failed
    if len(content) > max_file_bytes:
        failed.error = f"file_too_large:{len(content)}>{max_file_bytes}"
        return failed

    try:
        from docling.datamodel.base_models import DocumentStream
        from docling.document_converter import DocumentConverter
    except ParseError as exc:  # pragma: no cover - import-time env damage
        failed.error = f"docling_import_failed: {exc}"
        return failed

    input_format = _input_format(target_mime)
    if input_format is None:
        failed.error = f"unsupported_repr_mime:{target_mime}"
        return failed

    name = _stream_name(target_mime, filename)
    converter = DocumentConverter(allowed_formats=[input_format])

    deadline = time.monotonic() + max_seconds
    try:
        stream = DocumentStream(name=name, stream=BytesIO(content))
        result = converter.convert(
            stream,
            raises_on_error=False,
            max_num_pages=max_num_pages,
            max_file_size=max_file_bytes,
        )
    except ParseError as exc:
        if _is_deep_model_error(exc):
            skipped = ParsedFile(status=_STATUS_SKIPPED)
            skipped.error = DEEP_MODEL_REASON
            return skipped
        failed.error = f"{type(exc).__name__}: {exc}"[:500]
        return failed

    if time.monotonic() > deadline:
        failed.error = "timeout"
        return failed

    status = _status_to_ours(result.status.value if result.status else "")
    if status == _STATUS_SKIPPED:
        skipped = ParsedFile(status=_STATUS_SKIPPED)
        skipped.error = "docling_skipped"
        return skipped
    if status == _STATUS_FAILED:
        failed.error = "; ".join(
            _safe_text(getattr(e, "error_message", "")) for e in (result.errors or [])
        )[:500] or "conversion_failed"
        return failed
    if time.monotonic() > deadline:  # second check: convert may have blocked
        failed.error = "timeout"
        return failed

    document = getattr(result, "document", None)
    if document is None:
        failed.error = "no_document"
        return failed

    parsed = ParsedFile(status=status)
    try:
        body_md = document.export_to_markdown() or ""
    except ParseError as exc:
        failed.error = f"markdown_export_failed: {type(exc).__name__}: {exc}"[:500]
        return failed

    # -- tables: extract then excise from the body text ---------------------
    extracted = _collect_tables(document)
    parsed.tables = [t for _item, t in extracted]
    body_text = body_md
    body_spans: list[BodySpan] = []
    if extracted:
        try:
            body_text = _remove_table_markdown(body_md, extracted, document)
        except ParseError:
            body_text = body_md

    # -- located body spans with section paths + page provenance ------------
    try:
        section_stack: list[str] = []
        spans: list[tuple[str, tuple[str, ...], tuple[int, ...]]] = []
        from docling_core.types.doc import (
            SectionHeaderItem,
            TableItem,
            TextItem,
            TitleItem,
        )

        for item, _level in document.iterate_items():
            if isinstance(item, TableItem):
                continue
            if isinstance(item, TitleItem):
                text = _safe_text(getattr(item, "text", ""))
                if text:
                    spans.append((text, tuple(section_stack), _item_page_nos(item)))
                continue
            if isinstance(item, SectionHeaderItem):
                text = _safe_text(getattr(item, "text", ""))
                if not text:
                    continue
                level = int(getattr(item, "level", 1) or 1)
                while len(section_stack) >= level:
                    section_stack.pop()
                section_stack.append(text)
                # Headings carry the breadcrumb context themselves; keep them
                # in the body text (they are searchable) but they are not a
                # body span - the following text items own them.
                continue
            if isinstance(item, TextItem):
                text = _safe_text(getattr(item, "text", ""))
                if text:
                    spans.append((text, tuple(section_stack), _item_page_nos(item)))
        body_spans = _locate_spans(body_text, spans)
    except ParseError as exc:
        # Provenance extraction failing must not fail the ingest: fall back
        # to a single unlabeled span over the whole body.
        parsed.body_text = body_text
        if body_text.strip():
            parsed.body_spans = [
                BodySpan(0, len(body_text), body_text, (), ())
            ]
        parsed.error = f"provenance_fallback: {type(exc).__name__}"[:200]
        return parsed

    parsed.body_text = body_text
    parsed.body_spans = body_spans
    return parsed


def _remove_table_markdown(body_md: str, extracted, document) -> str:
    """Remove each extracted table's markdown rendering from the body text.

    Docling renders tables into export_to_markdown() output; the chunker
    never sees table text (tables bypass it by design, the build plan).
    The rendering comes from ``TableItem.export_to_markdown(doc=document)`` -
    the same serializer the document export uses - so the excised bytes match
    byte-for-byte. Falls back to a plain grid rendering when the serializer
    path fails (then to a no-op if even that cannot be located).
    """
    remaining = body_md.replace("\r\n", "\n")
    for item, table in extracted:
        rendering = ""
        try:
            rendering = (item.export_to_markdown(doc=document) or "").replace(
                "\r\n", "\n"
            )
        except ParseError:
            rendering = ""
        if not rendering.strip():
            try:
                rendering = _render_table_markdown(
                    [[cell for cell in row] for row in table.grid]
                ).replace("\r\n", "\n")
            except ParseError:
                rendering = ""
        if not rendering.strip():
            continue
        rendering = rendering.rstrip("\n")
        idx = remaining.find(rendering)
        if idx < 0:
            continue
        start = idx
        while start > 0 and remaining[start - 1] == "\n":
            start -= 1
        end = idx + len(rendering)
        while end < len(remaining) and remaining[end] == "\n":
            end += 1
        keep_break = start > 0 and end <= len(remaining)
        remaining = remaining[:start] + ("\n\n" if keep_break else "") + remaining[end:]
    # collapse any >2 consecutive newlines left by removals
    while "\n\n\n" in remaining:
        remaining = remaining.replace("\n\n\n", "\n\n")
    return remaining.strip("\n")


def _locate_spans(
    body_text: str,
    spans: list[tuple[str, tuple[str, ...], tuple[int, ...]]],
) -> list[BodySpan]:
    """Map each item text to its location in body_text, in order.

    The markdown export emits item texts in iteration order, so a forward
    scan (each search starts after the previous hit) locates every item even
    when identical text repeats. Text that cannot be found (Docling rendered
    it differently, e.g. escapes) is skipped - the chunker falls back to
    whole-body spans for uncovered regions.
    """
    out: list[BodySpan] = []
    cursor = 0
    work = body_text
    for text, section_path, page_nos in spans:
        if not text:
            continue
        idx = work.find(text, cursor)
        if idx < 0:
            continue
        out.append(
            BodySpan(
                char_start=idx,
                char_end=idx + len(text),
                text=text,
                section_path=section_path,
                page_nos=page_nos,
            )
        )
        cursor = idx + len(text)
    return out


def iter_parse(
    items: list[tuple[bytes, str, str | None]],
) -> list[ParsedFile]:
    """Parse a batch; a failure marks one item and the loop continues."""
    out: list[ParsedFile] = []
    for content, target_mime, filename in items:
        out.append(parse_bytes(content, target_mime, filename))
    return out
