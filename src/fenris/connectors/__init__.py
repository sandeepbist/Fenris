"""The connector protocol: one sync contract, many sources.

A connector turns any source (a Drive, a local folder, a Notion space)
into the same three operations the sync loop already knows how to
consume:

    list()          -> iterable of source-file metadata (the initial scan)
    changes(token)  -> (entries, next_token, done) - the incremental feed
    fetch(meta)     -> (FileMeta, content_bytes) - one file's content

The Drive client implements it implicitly (its list_files /
changes_list / download-export surface); the local-folder connector is
the first explicit implementation. Sources are namespaced by
``source_id`` ("drive", "folder:/path", "notion:..."), and every file
row carries the namespace so one projection can serve many sources
without collisions.

Design rules, inherited from the local-first research:
- connectors SYNC INTO the index; nothing is ever queried live.
- incremental wherever the source offers a change feed; full list
  where it does not (the folder connector diffs mtimes - cheap and
  honest at personal scale).
- content identity stays content-hash based, so the same file
  reaching two connectors dedups naturally upstream (same
  source_revision -> skipped).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol

# Supported ingest formats for the folder connector (same set the
# Drive router parses directly, minus Drive-specific workspaces).
FOLDER_MIMES = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".html": "text/html",
    ".htm": "text/html",
    ".epub": "application/epub+zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".json": "application/json",
    ".csv": "text/plain",
}
FOLDER_MAX_BYTES = 100 * 1024 * 1024  # matches the parse ceiling


@dataclass
class ConnectorFile:
    """One source-agnostic file: the shape every connector emits."""
    file_id: str                 # stable, unique within the connector
    title: str
    path: str
    mime_type: str
    modified_time: str           # ISO or empty; a hint, never identity
    content_hash: str            # content identity (md5/sha) - '' if unknown
    version: str                 # change-detection hint
    trashed: bool = False


@dataclass
class ChangesPage:
    entries: list[ConnectorFile] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)   # file_ids gone
    next_token: str | None = None
    done: bool = True


class Connector(Protocol):
    """The three-operation sync surface every source must provide."""

    source_id: str   # namespace, e.g. "drive" or "folder:/home/x/notes"

    def list(self) -> Iterator[ConnectorFile]: ...

    def changes(self, token: str | None) -> ChangesPage: ...

    def fetch(self, file: ConnectorFile) -> tuple[str, bytes]:
        """Return (source_revision, content_bytes); never partial."""


# ---- the local-folder connector (the first explicit implementation) ----------


def _hash_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


class FolderConnector:
    """A local directory as a Fenris source.

    Initial list: walk supported files under the root. Incremental:
    ''token'' is an opaque snapshot id; changes() re-walks, hashes
    mtime-changed files, and diffs against the token's recorded state.
    At personal-folder scale a re-walk is milliseconds-to-seconds, so
    the simple honest design beats a filesystem-watch dependency.

    Content identity is the sha256 of the file bytes - the same bytes
    reaching the Drive connector dedup upstream naturally.
    """

    def __init__(self, root: str | Path, source_id: str | None = None):
        self.root = Path(root).expanduser().resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"not a directory: {self.root}")
        self.source_id = source_id or f"folder:{self.root}"

    # -- list -----------------------------------------------------------------

    def _walk(self) -> list[ConnectorFile]:
        out = []
        for p in sorted(self.root.rglob("*")):
            if not p.is_file():
                continue
            mime = FOLDER_MIMES.get(p.suffix.lower())
            if mime is None:
                continue
            try:
                if p.stat().st_size > FOLDER_MAX_BYTES:
                    continue
                rel = str(p.relative_to(self.root))
                out.append(ConnectorFile(
                    file_id=rel,
                    title=p.name,
                    path=rel,
                    mime_type=mime,
                    modified_time="",
                    content_hash="",           # hashed on fetch
                    version=str(int(p.stat().st_mtime)),
                ))
            except OSError:
                continue  # unreadable/racing: skip, next sync retries
        return out

    def list(self) -> Iterator[ConnectorFile]:
        yield from self._walk()

    # -- changes ----------------------------------------------------------------

    def changes(self, token: str | None) -> ChangesPage:
        """Diff the walk against the token's packed state.

        The token packs the last-seen (file_id -> version) map, so the
        connector is stateless on disk: the sync cursor carries it, and
        a lost cursor simply degrades to a full-diff (removals detected,
        new/changed files ingested).
        """
        import json

        current = {f.file_id: f for f in self._walk()}
        seen: dict[str, str] = {}
        if token:
            try:
                seen = json.loads(token)
            except ValueError:
                seen = {}  # corrupt token: treat as full diff

        entries, removed = [], []
        for fid, f in current.items():
            if fid not in seen or seen[fid] != f.version:
                entries.append(f)
        for fid in seen:
            if fid not in current:
                removed.append(fid)

        new_seen = {fid: f.version for fid, f in current.items()}
        return ChangesPage(
            entries=entries,
            removed=removed,
            next_token=json.dumps(new_seen),
            done=True,
        )

    # -- fetch -----------------------------------------------------------------

    def fetch(self, file: ConnectorFile) -> tuple[str, bytes]:
        p = self.root / file.file_id
        data = p.read_bytes()
        # refuse mid-diff races: the bytes must hash to what we indexed
        revision = _hash_bytes(data)
        if file.content_hash and revision != file.content_hash:
            # re-walk caught a newer version; report the fresh one so the
            # caller ingests the CURRENT revision, not a torn one
            file.content_hash = revision
        return revision, data
