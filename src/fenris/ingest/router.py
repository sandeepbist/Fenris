"""MIME routing (the build plan): the day-one policy table as pure functions.

Every Drive file lands in exactly one of:

- ``direct``    - a binary Docling can parse in place (pdf, docx, xlsx, pptx,
                  txt/md, html, epub).
- ``export``    - a Google Workspace native that must go through
                  ``files.export`` to an OOXML target (Docs->docx,
                  Sheets->xlsx, Slides->pptx) before Docling sees it.
- ``shortcut``  - not indexed as content; the *target* file is routed instead.
- ``skipped``   - deliberately out of corpus (audio/video/Vids, Drawings,
                  Forms, Maps, Apps Script, archives, unknown binaries).
                  Recorded as ``status='skipped'`` with a machine-readable
                  reason in ``error``; excluded from retry logic.

Images (PNG/JPEG/TIFF) are OCR candidates and default OFF in v1 (open
decision #2): they route to ``skipped`` with reason ``ocr_deferred_v1``
rather than being silently dropped.

``route()`` is pure: same inputs, same decision, no IO, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass


class Action:
    """What the ingest pipeline must do for this file."""

    PARSE = "parse"          # run Docling over the content bytes
    EXPORT = "export"        # files.export first, then PARSE over the export
    SKIP = "skip"            # no content work; record status='skipped' + reason
    RESOLVE_SHORTCUT = "resolve_shortcut"  # route the target instead


class SkipReason:
    """Machine-readable skip reasons (stored in sources.error)."""

    OCR_DEFERRED = "ocr_deferred_v1"
    UNSUPPORTED_MEDIA = "unsupported_media"
    DRAWING = "unsupported_drawing"
    FORM = "unsupported_form"
    MAP = "unsupported_map"
    APPS_SCRIPT = "unsupported_apps_script"
    ARCHIVE = "unsupported_archive"
    UNKNOWN_MIME = "unknown_mime"
    UNKNOWN_MIME_BINARY = "unknown_mime_binary"


class Route:
    """Stable names for the four pipeline routes."""

    DIRECT = "direct"
    EXPORT = "export"
    SHORTCUT = "shortcut"
    SKIP = "skip"


# --- MIME constants (kept here so this module owns its routing world) --------

MIME_PDF = "application/pdf"
MIME_DOCX = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
)
MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
MIME_PPTX = (
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
)
MIME_TXT = "text/plain"
MIME_MD = "text/markdown"
MIME_HTML = "text/html"
MIME_EPUB = "application/epub+zip"

MIME_GDOC = "application/vnd.google-apps.document"
MIME_GSHEET = "application/vnd.google-apps.spreadsheet"
MIME_GSLIDE = "application/vnd.google-apps.presentation"
MIME_GSHORTCUT = "application/vnd.google-apps.shortcut"
MIME_GDRAWING = "application/vnd.google-apps.drawing"
MIME_GFORM = "application/vnd.google-apps.form"
MIME_GMAP = "application/vnd.google-apps.map"
MIME_GSCRIPT = "application/vnd.google-apps.script"
MIME_GSITE = "application/vnd.google-apps.site"
MIME_GVIDEO = "application/vnd.google-apps.video"
MIME_GVIDS = "application/vnd.google-apps.vids"
MIME_GAUDIO = "application/vnd.google-apps.audio"
MIME_GFOLDER = "application/vnd.google-apps.folder"

# Direct binary -> Docling (the build plan table, row 1). Keyed by exact
# MIME; the extension fallback table below covers Drive's occasional
# octet-stream/generic labels for the same content.
DIRECT_MIME_TO_REPR: dict[str, str] = {
    MIME_PDF: MIME_PDF,
    MIME_DOCX: MIME_DOCX,
    MIME_XLSX: MIME_XLSX,
    MIME_PPTX: MIME_PPTX,
    MIME_TXT: MIME_TXT,
    MIME_MD: MIME_MD,
    MIME_HTML: MIME_HTML,
    MIME_EPUB: MIME_EPUB,
}

# Workspace native -> files.export target (row 2). Sheets never via CSV export
# (first-sheet only, the build plan: that loses every other sheet).
EXPORT_MIME_TO_REPR: dict[str, str] = {
    MIME_GDOC: MIME_DOCX,
    MIME_GSHEET: MIME_XLSX,
    MIME_GSLIDE: MIME_PPTX,
}

# Images: OCR deferred (open decision #2, default off in v1).
OCR_DEFERRED_MIMES = frozenset(
    {
        "image/png",
        "image/jpeg",
        "image/jpg",
        "image/tiff",
        "image/webp",
        "image/bmp",
        "image/gif",
    }
)

# Skip-listed by exact MIME (row 3).
SKIP_MIME_TO_REASON: dict[str, str] = {
    "audio/mpeg": SkipReason.UNSUPPORTED_MEDIA,
    "audio/mp3": SkipReason.UNSUPPORTED_MEDIA,
    "audio/mp4": SkipReason.UNSUPPORTED_MEDIA,
    "audio/ogg": SkipReason.UNSUPPORTED_MEDIA,
    "audio/wav": SkipReason.UNSUPPORTED_MEDIA,
    "audio/x-wav": SkipReason.UNSUPPORTED_MEDIA,
    "audio/webm": SkipReason.UNSUPPORTED_MEDIA,
    "audio/flac": SkipReason.UNSUPPORTED_MEDIA,
    "audio/aac": SkipReason.UNSUPPORTED_MEDIA,
    "audio/x-m4a": SkipReason.UNSUPPORTED_MEDIA,
    "video/mp4": SkipReason.UNSUPPORTED_MEDIA,
    "video/avi": SkipReason.UNSUPPORTED_MEDIA,
    "video/quicktime": SkipReason.UNSUPPORTED_MEDIA,
    "video/x-msvideo": SkipReason.UNSUPPORTED_MEDIA,
    "video/x-matroska": SkipReason.UNSUPPORTED_MEDIA,
    "video/webm": SkipReason.UNSUPPORTED_MEDIA,
    "video/x-flv": SkipReason.UNSUPPORTED_MEDIA,
    MIME_GVIDEO: SkipReason.UNSUPPORTED_MEDIA,
    MIME_GVIDS: SkipReason.UNSUPPORTED_MEDIA,
    MIME_GAUDIO: SkipReason.UNSUPPORTED_MEDIA,
    MIME_GDRAWING: SkipReason.DRAWING,
    MIME_GFORM: SkipReason.FORM,
    MIME_GMAP: SkipReason.MAP,
    MIME_GSCRIPT: SkipReason.APPS_SCRIPT,
    MIME_GSITE: SkipReason.UNSUPPORTED_MEDIA,
    "application/zip": SkipReason.ARCHIVE,
    "application/x-zip-compressed": SkipReason.ARCHIVE,
    "application/x-7z-compressed": SkipReason.ARCHIVE,
    "application/x-tar": SkipReason.ARCHIVE,
    "application/gzip": SkipReason.ARCHIVE,
    "application/x-rar-compressed": SkipReason.ARCHIVE,
    "application/x-bzip2": SkipReason.ARCHIVE,
    "application/vnd.rar": SkipReason.ARCHIVE,
    "application/rtf": SkipReason.UNSUPPORTED_MEDIA,
}

# Skip-listed by prefix (Google Apps emits new media types faster than any
# static table can follow).
SKIP_MIME_PREFIXES: tuple[tuple[str, str], ...] = (
    ("audio/", SkipReason.UNSUPPORTED_MEDIA),
    ("video/", SkipReason.UNSUPPORTED_MEDIA),
    ("image/heic", SkipReason.UNSUPPORTED_MEDIA),
    ("image/heif", SkipReason.UNSUPPORTED_MEDIA),
    ("model/", SkipReason.UNSUPPORTED_MEDIA),
)

# Extension fallback for generic/binary labels (Drive labels raw exports and
# oddball uploads application/octet-stream). Only used when the MIME itself
# is not a known direct format.
EXTENSION_TO_REPR: dict[str, str] = {
    "pdf": (MIME_PDF, "pdf"),
    "docx": (MIME_DOCX, "docx"),
    "xlsx": (MIME_XLSX, "xlsx"),
    "xlsm": (MIME_XLSX, "xlsx"),
    "pptx": (MIME_PPTX, "pptx"),
    "txt": (MIME_TXT, "txt"),
    "text": (MIME_TXT, "txt"),
    "md": (MIME_MD, "md"),
    "markdown": (MIME_MD, "md"),
    "html": (MIME_HTML, "html"),
    "htm": (MIME_HTML, "html"),
    "xhtml": (MIME_HTML, "html"),
    "epub": (MIME_EPUB, "epub"),
}

GENERIC_BINARY_MIMES = frozenset(
    {
        "application/octet-stream",
        "binary/octet-stream",
        "",
    }
)


@dataclass(frozen=True)
class RouteDecision:
    """The routing verdict plus everything the pipeline needs to act on it.

    Attributes:
        route: one of Route.DIRECT / Route.EXPORT / Route.SHORTCUT / Route.SKIP.
        action: one of the Action constants - what the caller must do.
        target_mime: the representation MIME to parse into (repr_mime). For
            Workspace exports this is the OOXML export target; for direct
            files the content MIME itself; empty for skips/shortcuts.
        reason: machine-readable skip reason (SkipReason constant) or None.
        is_workspace: echo of the input flag (native Workspace file).
    """

    route: str
    action: str
    target_mime: str = ""
    reason: str | None = None
    is_workspace: bool = False


def route(mime_type: str, is_workspace: bool, is_shortcut: bool) -> RouteDecision:
    """Decide how one Drive file flows into the ingestion pipeline.

    Pure function: no IO, no clock, no randomness - rebuilds always re-route
    identically.

    Args:
        mime_type: the file's Drive mimeType (empty/None treated as unknown).
        is_workspace: True for Google Workspace native files (vnd.google-apps.*
            except the non-content kinds handled below).
        is_shortcut: True for application/vnd.google-apps.shortcut.

    Returns:
        RouteDecision with the route, the action to take, the representation
        MIME to parse into (when content work is needed) and a skip reason
        (when it is not).
    """
    mime = (mime_type or "").strip().lower()

    # Shortcuts are never indexed as content; the target file is routed on
    # its own merits. (Drive marks them with a dedicated MIME in v3.)
    if is_shortcut or mime == MIME_GSHORTCUT:
        return RouteDecision(
            route=Route.SHORTCUT,
            action=Action.RESOLVE_SHORTCUT,
            is_workspace=is_workspace,
        )

    # Workspace natives first: export to OOXML, hash the export bytes.
    if is_workspace or mime in EXPORT_MIME_TO_REPR:
        if mime in EXPORT_MIME_TO_REPR:
            return RouteDecision(
                route=Route.EXPORT,
                action=Action.EXPORT,
                target_mime=EXPORT_MIME_TO_REPR[mime],
                is_workspace=True,
            )
        # Other vnd.google-apps.* without an export target: skip-listed kinds.
        reason = SKIP_MIME_TO_REASON.get(mime)
        if reason is None:
            reason = (
                SkipReason.UNSUPPORTED_MEDIA
                if mime.startswith("application/vnd.google-apps.")
                else SkipReason.UNKNOWN_MIME
            )
        return RouteDecision(
            route=Route.SKIP, action=Action.SKIP, reason=reason, is_workspace=True
        )

    # Direct binary parseables.
    if mime in DIRECT_MIME_TO_REPR:
        return RouteDecision(
            route=Route.DIRECT,
            action=Action.PARSE,
            target_mime=DIRECT_MIME_TO_REPR[mime],
            is_workspace=False,
        )

    # OCR candidates: deferred out of v1 by decision (never silently dropped).
    if mime in OCR_DEFERRED_MIMES:
        return RouteDecision(
            route=Route.SKIP,
            action=Action.SKIP,
            reason=SkipReason.OCR_DEFERRED,
            is_workspace=False,
        )

    # Exact skip-list.
    if mime in SKIP_MIME_TO_REASON:
        return RouteDecision(
            route=Route.SKIP,
            action=Action.SKIP,
            reason=SKIP_MIME_TO_REASON[mime],
            is_workspace=False,
        )

    # Prefix skip-list (audio/*, video/*, ...).
    for prefix, reason in SKIP_MIME_PREFIXES:
        if mime.startswith(prefix):
            return RouteDecision(
                route=Route.SKIP, action=Action.SKIP, reason=reason, is_workspace=False
            )

    # Unknown: extension sniffing only when the label carries no information.
    if mime in GENERIC_BINARY_MIMES:
        # octet-stream etc.: no title available in route() - caller retries
        # with a filename via route_with_name(); without one it is unknown.
        return RouteDecision(
            route=Route.SKIP,
            action=Action.SKIP,
            reason=SkipReason.UNKNOWN_MIME_BINARY,
            is_workspace=False,
        )

    return RouteDecision(
        route=Route.SKIP,
        action=Action.SKIP,
        reason=SkipReason.UNKNOWN_MIME,
        is_workspace=False,
    )


def route_with_name(
    mime_type: str,
    filename: str,
    is_workspace: bool,
    is_shortcut: bool,
) -> RouteDecision:
    """route() plus an extension fallback for generic binary labels.

    Drive labels many perfectly good files ``application/octet-stream``; the
    extension is the only signal left. A name that still says nothing keeps
    the unknown-binary skip decision.
    """
    decision = route(mime_type, is_workspace, is_shortcut)
    if decision.route != Route.SKIP:
        return decision
    if decision.reason not in (SkipReason.UNKNOWN_MIME_BINARY,):
        return decision
    ext = ""
    name = (filename or "").strip()
    if "." in name:
        ext = name.rsplit(".", 1)[1].lower()
    hit = EXTENSION_TO_REPR.get(ext)
    if hit is None:
        return decision
    return RouteDecision(
        route=Route.DIRECT,
        action=Action.PARSE,
        target_mime=hit[0],
        is_workspace=False,
    )
