"""Fenris ingestion (the build plan / milestone M2).

Public surface:

- ``router.route(mime_type, is_workspace, is_shortcut) -> RouteDecision``
- ``parse.parse_bytes(content, target_mime, filename) -> ParsedFile``
- ``chunk.chunk_body(body_text, body_spans) -> list[ChunkRecord]``
- ``chunk.build_table_record(index, table) -> TableRecord``
- ``headers.build_header(title, path, section_path) -> str``
- ``embed.Embedder`` protocol; ``embed.GeminiEmbedder`` / ``embed.FakeEmbedder``
- ``scanner.scan_chunk(header_text, body_text) -> (flagged, hits)``
- ``pipeline.ingest_file(conn, client, file_meta, content_bytes, cfg)``
"""

from fenris.ingest.chunk import (
    CHUNKER_FULL_VERSION,
    CHUNKER_VERSION,
    ChunkRecord,
    TableRecord,
    build_table_record,
    chunk_body,
    count_tokens,
    split_text,
)
from fenris.ingest.embed import EmbedError, FakeEmbedder, GeminiEmbedder, serialize_vector
from fenris.ingest.headers import build_header
from fenris.ingest.parse import (
    PARSER_VERSION,
    BodySpan,
    CellData,
    ParsedFile,
    TableData,
    parse_bytes,
)
from fenris.ingest.pipeline import FileMeta, IngestResult, ingest_file
from fenris.ingest.router import Action, Route, RouteDecision, SkipReason, route
from fenris.ingest.scanner import scan_chunk, scan_text

__all__ = [
    "CHUNKER_FULL_VERSION",
    "CHUNKER_VERSION",
    "PARSER_VERSION",
    "Action",
    "BodySpan",
    "CellData",
    "ChunkRecord",
    "EmbedError",
    "FakeEmbedder",
    "FileMeta",
    "GeminiEmbedder",
    "IngestResult",
    "ParsedFile",
    "Route",
    "RouteDecision",
    "SkipReason",
    "TableData",
    "TableRecord",
    "build_header",
    "build_table_record",
    "chunk_body",
    "count_tokens",
    "ingest_file",
    "parse_bytes",
    "route",
    "scan_chunk",
    "scan_text",
    "serialize_vector",
    "split_text",
]
