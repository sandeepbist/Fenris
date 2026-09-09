"""Deterministic contextual headers (the build plan).

``header = title / path breadcrumbs / section_path`` joined - zero LLM
calls, zero cost, and (decisive for the MVP) reproducible: a rebuild
yields identical headers, so identical embeddings, so a stable identity
key. LLM-generated headers ship behind a flag later; their
non-determinism is why they lose the MVP slot.

Contract: same input -> same header, always. No clock, no randomness, no
locale-dependent casing, no IO.
"""

from __future__ import annotations

HEADER_VERSION = "deterministic-v1"
HEADER_SEPARATOR = " / "


def normalize_title(title: str | None) -> str:
    """Strip the noise that would otherwise make headers unstable.

 Whitespace is collapsed; None/empty becomes ''. Casing is left alone
 (titles are user data, not tokens to be normalized away).
 """
    if not title:
        return ""
    collapsed = " ".join((title or "").split())
    return collapsed


def path_breadcrumbs(path: str | None, max_depth: int = 4) -> str:
    """Render a Drive path as ``A / B / C`` breadcrumbs, newest last.

 Leading/trailing slashes are ignored; empty segments dropped. Depth is
 clamped from the *root* side (the leaf is always kept - it is the most
 specific context), so long folder chains cannot blow up header length.
 """
    if not path:
        return ""
    parts = [seg.strip() for seg in path.split("/")]
    parts = [seg for seg in parts if seg]
    if not parts:
        return ""
    if len(parts) > max_depth:
        parts = parts[-max_depth:]
    return HEADER_SEPARATOR.join(parts)


def build_header(
    title: str | None,
    path: str | None,
    section_path: tuple[str, ...] | list[str] | None,
) -> str:
    """Build the deterministic contextual header for one chunk.

 The non-empty components are joined with ``" / "`` in the fixed order
 title, path breadcrumbs, section path. Empty components are dropped
 without leaving dangling separators.

 Args:
 title: the file title (sources.title).
 path: the Drive folder path (sources.path).
 section_path: the Docling-derived section breadcrumbs covering the
 chunk (chunks.section_path).

 Returns:
 The header string; '' only when every component is empty.
 """
    parts: list[str] = []
    t = normalize_title(title)
    if t:
        parts.append(t)
    crumbs = path_breadcrumbs(path)
    if crumbs:
        parts.append(crumbs)
    if section_path:
        clean = [normalize_title(s) for s in section_path]
        clean = [s for s in clean if s]
        if clean:
            parts.append(HEADER_SEPARATOR.join(clean))
    return HEADER_SEPARATOR.join(parts)


def is_deterministic(left: str, right: str) -> bool:
    """Sanity check used by tests: identical inputs must yield identical
 headers, and different inputs must not collide."""
    return left == right
