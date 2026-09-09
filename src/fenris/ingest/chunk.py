"""Recursive character chunking (the build plan, "Chunk").

- 512-token chunks, recursive character splitting with the hierarchy
 paragraph -> sentence -> word.
- ZERO overlap (the design notes: overlap showed no benefit). The tests
 pin this: adjacent chunks never share text.
- A local heuristic tokenizer (chars-per-token ratio) counts tokens; its
 version constant is recorded in the chunker identity so a tokenizer
 bump forces new representations instead of corrupting old ones.
- Parent (~2048-token) rows are written for parent-expansion retrieval;
 only children are embedded.
- Tables NEVER pass through the splitter: each Docling TableItem
 becomes one tables row (TableRecord) plus row-block chunks
 (~8 data rows, header prepended) with is_table=1, table_id and a
 row_range - so a hit cites an exact region.

char_start/char_end are offsets into the representation's body_text
(the citation frame, the build plan); table row-blocks carry offsets
into their own synthesized block text.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from fenris.ingest.parse import TableData

# --- chunker identity (recorded on every source row) ------------------------
CHUNKER_NAME = "recursive512"
CHUNKER_VERSION = "recursive512-v1"       # matches config.CHUNKER_VERSION
TOKENIZER_NAME = "heuristic-chars"
TOKENIZER_VERSION = "heuristic-chars-v1"
CHUNKER_FULL_VERSION = f"{CHUNKER_VERSION}+{TOKENIZER_VERSION}"

# --- sizing -----------------------------------------------------------------
CHUNK_TOKENS = 512                         # child target (the build plan)
PARENT_TOKENS = 2048                       # parent target (~4 children)
TABLE_ROW_BLOCK = 8                        # data rows per row-block chunk
MAX_TOKENS_SLACK = 1.25                     # splitter may overshoot by <=25%

# --- heuristic tokenizer ----------------------------------------------------
# Measured average for English prose on gemini-class tokenizers: ~4.2 chars
# per token (whitespace runs compress below the naive 4). The version
# constant above is the contract: change the ratio, bump the version.
_CHARS_PER_TOKEN = 4.2

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_WORD_SPLIT = re.compile(r"\s+")
_PARA_SPLIT = re.compile(r"\n\s*\n")


def count_tokens(text: str) -> int:
    """Local heuristic token count: ceil(chars / CHARS_PER_TOKEN), floored at 1.

 Deterministic and fast (no model calls); the tokenizer version constant
 pins the behavior for identity purposes.
 """
    if not text:
        return 0
    n = int(-(-len(text) // _CHARS_PER_TOKEN))  # ceil
    return max(1, n)


# --- chunk records ----------------------------------------------------------


@dataclass(frozen=True)
class ChunkRecord:
    """One chunks row payload (children, parents, or table row-blocks).

 is_table=1 + table_id + row_range are set only for table row-blocks;
 parent rows carry parent_chunk_id=None and children point at them.
 """

    ordinal: int
    body_text: str
    char_start: int
    char_end: int
    token_count: int
    section_path: tuple[str, ...] = ()
    page_nos: tuple[int, ...] = ()
    is_table: bool = False
    table_id: int | None = None
    row_range: str | None = None
    is_parent: bool = False
    children: tuple[int, ...] = ()      # ordinals of child chunks (parents only)
    parent_ordinal: int | None = None   # set by the builder for children


@dataclass(frozen=True)
class TableRowBlock:
    """One ~8-row block of a table, header prepended (its own citation unit)."""

    start_row: int          # 0-based data-row index of the first row in block
    end_row: int            # exclusive end (data-row index)
    text: str               # rendered markdown: header + these rows
    header_row: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class TableRecord:
    """The typed tables row for one Docling TableItem (the build plan)."""

    table_id: int | None    # assigned by the pipeline at insert time
    sheet_or_page: str
    range: str
    columns_json: str
    row_blocks_json: str
    num_rows: int
    num_cols: int
    formulas_json: str | None = None
    merged_cells_json: str | None = None
    header_hierarchy_json: str | None = None
    page_nos: tuple[int, ...] = ()
    row_blocks: tuple[TableRowBlock, ...] = field(default_factory=tuple)

    # serializable view for logging/tests
    def to_json(self) -> str:
        return json.dumps(
            {
                "table_id": self.table_id,
                "sheet_or_page": self.sheet_or_page,
                "range": self.range,
                "num_rows": self.num_rows,
                "num_cols": self.num_cols,
                "row_blocks": [
                    {"start_row": b.start_row, "end_row": b.end_row}
                    for b in self.row_blocks
                ],
            }
        )


# --- normalization ----------------------------------------------------------


def _normalize_cell(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def _normalize_token(text: str) -> str:
    """Lowercase alphanumeric fold: used for term_df, not for storage."""
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


# --- recursive split --------------------------------------------------------


def _split_paragraphs(text: str) -> list[str]:
    parts = [p for p in _PARA_SPLIT.split(text)]
    return [p for p in parts if p.strip()]


def _split_sentences(text: str) -> list[str]:
    parts = _SENTENCE_SPLIT.split(text)
    return [p for p in parts if p.strip()]


def _split_words(text: str) -> list[str]:
    return _WORD_SPLIT.split(text)


def _join_clean(parts: list[str]) -> str:
    """Join split parts without duplicating separator whitespace."""
    return " ".join(part.strip() for part in parts if part.strip())


def _recursive_split(text: str, max_tokens: int) -> list[str]:
    """Recursive character split: paragraph -> sentence -> word.

 Zero overlap by construction: every piece is emitted exactly once and
 the pieces partition the input. A single unit (word) whose token count
 exceeds the limit is hard-cut at the character boundary - the last
 resort after the hierarchy failed.

 Every piece respects the limit exactly: when a level's packing cannot
 fit (or its joiner would push a piece over), the next level splits
 finer; the hard cut is the exact character-width guarantee.
 """
    if not text.strip():
        return []
    if count_tokens(text) <= max_tokens:
        return [text.strip()]

    # level 1: paragraphs
    paragraphs = _split_paragraphs(text)
    pieces: list[str] | None = None
    if len(paragraphs) > 1:
        pieces = _fit_or_none(_recursive_pack(paragraphs, max_tokens, "\n\n"), max_tokens)

    # level 2: sentences
    if not pieces:
        sentences = _split_sentences(text)
        if len(sentences) > 1:
            pieces = _fit_or_none(_recursive_pack(sentences, max_tokens, " "), max_tokens)
        else:
            pieces = None

    # level 3: words
    if not pieces:
        words = _split_words(text)
        if len(words) > 1:
            pieces = _fit_or_none(_recursive_pack(words, max_tokens, " "), max_tokens)
        else:
            pieces = None

    # level 4: hard character cut (a single unbreakable unit)
    if not pieces:
        pieces = _hard_cut(text, max_tokens)

    return pieces


def _fit_or_none(pieces: list[str], max_tokens: int) -> list[str] | None:
    """None when any piece still exceeds the limit (drop to next level);
 otherwise the pieces. An empty result is None too so the caller keeps
 splitting rather than silently emitting nothing."""
    if not pieces:
        return None
    if any(count_tokens(p) > max_tokens for p in pieces):
        return None
    return pieces


def _recursive_pack(units: list[str], max_tokens: int, sep: str) -> list[str]:
    """Greedily pack split units into chunks <= max_tokens.

 Keeps the original unit order; separators are re-inserted with the
 level's joiner. A unit that alone exceeds the limit is recursively
 split at the next hierarchy level before packing.
 """
    pieces: list[str] = []
    current: list[str] = []
    current_tokens = 0

    def flush():
        nonlocal current, current_tokens
        if current:
            pieces.append(_join_with_sep(current, sep))
            current = []
            current_tokens = 0

    for unit in units:
        unit_tokens = count_tokens(unit)
        if unit_tokens > max_tokens:
            flush()
            pieces.extend(_recursive_split(unit, max_tokens))
            continue
        if current and current_tokens + unit_tokens > max_tokens:
            flush()
        current.append(unit)
        current_tokens += unit_tokens
    flush()
    return pieces


def _join_with_sep(parts: list[str], sep: str) -> str:
    if sep == "\n\n":
        return "\n\n".join(p.strip() for p in parts if p.strip())
    return " ".join(p.strip() for p in parts if p.strip())


def _hard_cut(text: str, max_tokens: int) -> list[str]:
    """Character cut for unbreakable units: exact, zero overlap."""
    limit_chars = int(max_tokens * _CHARS_PER_TOKEN)
    pieces: list[str] = []
    idx = 0
    while idx < len(text):
        piece = text[idx : idx + limit_chars].strip()
        if piece:
            pieces.append(piece)
        idx += limit_chars
    return pieces


# --- region -> spans --------------------------------------------------------


def _covered_spans(spans, char_start: int, char_end: int):
    """Body spans overlapping a chunk's char range."""
    return [
        s
        for s in spans
        if s.char_start < char_end and s.char_end > char_start
    ]


def _page_nos_for(spans, char_start: int, char_end: int) -> tuple[int, ...]:
    """Union of page numbers over the spans a chunk touches (item-level
 provenance; Docling has no char offsets - the build plan)."""
    pages: list[int] = []
    for s in _covered_spans(spans, char_start, char_end):
        pages.extend(s.page_nos)
    return tuple(dict.fromkeys(sorted(pages)))


def _deepest_section_path(spans, char_start: int, char_end: int) -> tuple[str, ...]:
    """The deepest (most specific) section_path among overlapping spans."""
    best: tuple[str, ...] = ()
    for s in _covered_spans(spans, char_start, char_end):
        if len(s.section_path) > len(best):
            best = s.section_path
    return best


# --- public API -------------------------------------------------------------


def split_text(body_text: str, max_tokens: int = CHUNK_TOKENS) -> list[str]:
    """Split text into <=max_token pieces. Zero overlap; the concatenation
 of pieces (with normalized separators) covers the input."""
    return _recursive_split(body_text, max_tokens)


def chunk_body(
    body_text: str,
    body_spans,
    *,
    chunk_tokens: int = CHUNK_TOKENS,
    parent_tokens: int = PARENT_TOKENS,
) -> list[ChunkRecord]:
    """Chunk one representation's body text into child chunks + parents.

 Children are the <=512-token pieces the retriever embeds; parents
 (~2048 tokens) group consecutive children for parent expansion.
 Tables must never arrive here - they go through build_table_records.

 Args:
 body_text: the parse output's linear text (table-free).
 body_spans: parse.BodySpan list with section/page provenance.
 chunk_tokens: child target size.
 parent_tokens: parent grouping target.

 Returns:
 ChunkRecords in ordinal order: parent rows interleave with their
 children (parent first), children carry parent_ordinal.
 """
    if not body_text.strip():
        return []

    pieces = split_text(body_text, chunk_tokens)
    # map each piece to its char range in body_text via forward scan
    located: list[tuple[str, int, int]] = []
    cursor = 0
    for piece in pieces:
        idx = body_text.find(piece, cursor)
        if idx < 0:
            # whitespace-normalized piece: fall back to a whole-body frame
            idx = cursor
        located.append((piece, idx, idx + len(piece)))
        cursor = idx + len(piece)

    # group children into parents by token count
    children: list[tuple[str, int, int]] = []
    groups: list[list[tuple[str, int, int]]] = []
    running = 0
    for item in located:
        running += count_tokens(item[0])
        children.append(item)
        if running >= parent_tokens // 2 or len(children) >= 4:
            groups.append(children)
            children = []
            running = 0
    if children:
        groups.append(children)

    records: list[ChunkRecord] = []
    for group_no, group in enumerate(groups):
        # ordinals: parent first, then its children, contiguous per group
        parent_ordinal = sum(len(g) + 1 for g in groups[:group_no])
        child_ordinals: tuple[int, ...] = tuple(
            range(parent_ordinal + 1, parent_ordinal + 1 + len(group))
        )
        # parent row: the joined text of its children, full char range
        parent_start = group[0][1]
        parent_end = group[-1][2]
        parent_text = body_text[parent_start:parent_end]
        records.append(
            ChunkRecord(
                ordinal=parent_ordinal,
                body_text=parent_text,
                char_start=parent_start,
                char_end=parent_end,
                token_count=count_tokens(parent_text),
                section_path=_deepest_section_path(
                    body_spans, parent_start, parent_end
                ),
                page_nos=_page_nos_for(body_spans, parent_start, parent_end),
                is_parent=True,
                children=child_ordinals,
            )
        )
        for offset, (piece, start, end) in enumerate(group):
            records.append(
                ChunkRecord(
                    ordinal=parent_ordinal + 1 + offset,
                    body_text=body_text[start:end],
                    char_start=start,
                    char_end=end,
                    token_count=count_tokens(piece),
                    section_path=_deepest_section_path(body_spans, start, end),
                    page_nos=_page_nos_for(body_spans, start, end),
                    parent_ordinal=parent_ordinal,
                )
            )
    return records


# --- tables -----------------------------------------------------------------


def build_row_blocks(table: TableData, block_rows: int = TABLE_ROW_BLOCK) -> list[TableRowBlock]:
    """Cut a table's data rows into ~8-row blocks, header prepended.

 The header row (grid row 0) is prepended to every block so each block
 is self-describing; only data rows consume block slots.
 """
    grid = table.grid
    if not grid:
        return []
    header = tuple(_normalize_cell(c.text) for c in grid[0])
    data_rows = [tuple(_normalize_cell(c.text) for c in row) for row in grid[1:]]
    if not data_rows:
        return []
    blocks: list[TableRowBlock] = []
    for start in range(0, len(data_rows), block_rows):
        rows = data_rows[start : start + block_rows]
        blocks.append(
            TableRowBlock(
                start_row=start,
                end_row=start + len(rows),
                text=_render_block(header, rows),
                header_row=header,
                rows=tuple(rows),
            )
        )
    return blocks


def _render_block(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> str:
    """Render one row-block as a small markdown table (header + rows)."""
    cols = max(len(header), *(len(r) for r in rows)) if (header or rows) else 0
    lines = ["|" + "".join(f" {header[i] if i < len(header) else ''} |" for i in range(cols))]
    lines.append("|" + "".join("-" * 3 + "|" for _ in range(cols)))
    for row in rows:
        lines.append("|" + "".join(f" {row[i] if i < len(row) else ''} |" for i in range(cols)))
    return "\n".join(lines)


def build_table_record(
    index: int,
    table: TableData,
    sheet_or_page: str = "",
    region: str = "",
    block_rows: int = TABLE_ROW_BLOCK,
) -> TableRecord:
    """One TableItem -> one TableRecord (tables row payload + row blocks).

 The TableRecord carries the serialized JSON the schema expects:
 columns_json ([{name, normalized_name, type, unit}]) and row_blocks_json
 (~8-row blocks with the header prepended).
 """
    grid = table.grid
    num_rows = table.num_rows or len(grid)
    num_cols = table.num_cols or (len(grid[0]) if grid else 0)
    columns = []
    if grid:
        for col_idx, cell in enumerate(grid[0]):
            name = _normalize_cell(cell.text) or f"col{col_idx + 1}"
            columns.append(
                {
                    "name": name,
                    "normalized_name": _normalize_token(name),
                    "type": "number" if _is_number_type_column(grid, col_idx) else "text",
                    "unit": "",
                }
            )
    blocks = build_row_blocks(table, block_rows)
    row_blocks_json = json.dumps(
        [
            {
                "start_row": b.start_row,
                "end_row": b.end_row,
                "header": list(b.header_row),
                "rows": [list(r) for r in b.rows],
            }
            for b in blocks
        ]
    )
    return TableRecord(
        table_id=index,
        sheet_or_page=sheet_or_page,
        range=region,
        columns_json=json.dumps(columns),
        row_blocks_json=row_blocks_json,
        num_rows=num_rows,
        num_cols=num_cols,
        merged_cells_json=_merged_cells_json(grid),
        page_nos=table.page_nos,
        row_blocks=tuple(blocks),
    )


def _is_number(text: str) -> bool:
    try:
        float(text)
        return True
    except (TypeError, ValueError):
        return False


def _is_number_type_column(grid, col_idx: int) -> bool:
    """A column is 'number' when a majority of its data cells parse as
 numeric - header text alone says nothing about the cell types."""
    if len(grid) < 2:
        return False
    data = [row[col_idx].text for row in grid[1:] if col_idx < len(row)]
    if not data:
        return False
    numeric = sum(1 for cell in data if _is_number(cell))
    return numeric / len(data) >= 0.5


def _merged_cells_json(grid) -> str | None:
    """Serialize spanned cells as the merged_cells payload."""
    merged = []
    for r_idx, row in enumerate(grid):
        for c_idx, cell in enumerate(row):
            if cell.col_span > 1 or cell.row_span > 1:
                merged.append(
                    {
                        "row": r_idx,
                        "col": c_idx,
                        "row_span": cell.row_span,
                        "col_span": cell.col_span,
                    }
                )
    return json.dumps(merged) if merged else None
