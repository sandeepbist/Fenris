"""Input sanitation and bounds - the security boundary of ingestion.

Attacks these guard against (found by the security review, reproduced in
tests/test_security.py):

- Hostile Drive metadata: a 10 MB ``title`` field inflates headers, the
  FTS index, and the DB with no functional value. Capped.
- Control characters: ANSI escape sequences (OSC 52 clipboard exfiltration,
  terminal resizing), null bytes, and bidi-override characters smuggled in
  content or metadata. Stripped to safe subsets.
- Storage-bomb content: a "text" file of N bytes becomes ~6x its size in
  the projection once chunk rows, FTS index entries, and vector rows are
  written. A text cap well below the binary parse ceiling keeps the
  worst-case growth bounded while staying above any real document.
"""

from __future__ import annotations

# Metadata bounds. Titles/paths appear in citations, prompts, and the FTS
# index; anything past this is truncation-safe (the file_id stays exact).
MAX_TITLE_CHARS = 512
MAX_PATH_CHARS = 1024

# Text-content bound. Binary formats (docx/pdf/...) carry their layout in
# bytes and parse to less text than they weigh, so the 100 MB parse ceiling
# governs them. Plain-text formats ingest ~1:1 with a ~6x projection cost -
# 20 MB of text is already a pathological document (~5M chars); the 586 MB
# projection growth from the attack run came from a single 100 MB text file
# the old cap let through.
MAX_TEXT_CONTENT_BYTES = 20 * 1024 * 1024

# Characters that never legitimately appear in titles/paths/citations:
# C0 controls except tab (stripped entirely), plus the bidi-override range
# used to disguise text direction (CVE-2021-42574 class).
_FORBIDDEN_C0 = set(range(0x09)) | set(range(0x0A, 0x20)) - {0x09}
_BIDI_OVERRIDES = set(range(0x202A, 0x202F)) | set(range(0x2066, 0x2070))


def sanitize_text(value: str, *, max_chars: int) -> str:
    """Strip control/ANSI/bidi characters, then truncate to max_chars.

    Tabs are preserved (legitimate in paths); newlines/other C0 controls,
    the C1/ANSI range, DEL, and bidi overrides are removed. Applied to
    metadata and display strings - never to body content, which the
    chunker handles and the scanner inspects before storage.
    """
    if not value:
        return value
    out = []
    for ch in value:
        cp = ord(ch)
        if cp in _FORBIDDEN_C0 or cp == 0x7F or 0x80 <= cp <= 0x9F:
            continue
        if cp in _BIDI_OVERRIDES:
            continue
        out.append(ch)
    return "".join(out)[:max_chars]


def sanitize_file_meta(meta):
    """Return a FileMeta with hostile title/path fields made safe.

    The original object is untouched (frozen dataclass); a sanitized copy
    is returned. ``file_id`` and ``source_revision`` are identity fields -
    never modified, only the display/burden fields are bounded.
    """
    from dataclasses import replace

    return replace(
        meta,
        title=sanitize_text(meta.title or "", max_chars=MAX_TITLE_CHARS),
        path=sanitize_text(meta.path or "", max_chars=MAX_PATH_CHARS),
    )


def content_within_text_cap(mime_type: str, content: bytes) -> bool:
    """Plain-text formats are bounded to MAX_TEXT_CONTENT_BYTES.

    Text mimes ingest at ~1:1 with a ~6x projection multiplier; binary
    formats (docx, pdf, xlsx, ...) parse to far less text than bytes, so
    they stay under the parse-stage ceiling instead.
    """
    if mime_type in ("text/plain", "text/markdown", "text/html", "application/json"):
        return len(content) <= MAX_TEXT_CONTENT_BYTES
    return True
