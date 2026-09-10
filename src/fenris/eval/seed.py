"""Seed the golden corpus into a fresh projection DB.

The eval command runs the REAL retrieval stack, so the corpus must be
ingested through the REAL pipeline first: FileMeta + bytes through
IngestQueue, which is exactly how sync feeds it. No Drive client is
involved - the corpus is local fixture data.

Offline by construction: IngestQueue.apply routes the parse to the
doc-side embedder via a None config, which the pipeline resolves to the
deterministic FakeEmbedder (1536-dim, L2-normalized) - the same embedder
the query side uses when FENRIS_EMBEDDER=fake. Retrieval stays coherent
with zero network and zero API keys; BM25 carries the lexical signal.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fenris.ingest.pipeline import FileMeta
from fenris.sync.ingest_queue import IngestQueue

# The corpus is repo fixture data (eval/fixtures/), not package data:
# seeding runs from a checkout. CWD is the documented invocation root
# (`fenris eval` is repo-rooted, like `make eval`).
MANIFEST = Path("eval") / "fixtures" / "golden_corpus" / "manifest.json"


def seed_golden_corpus(conn) -> tuple[int, set[str]]:
    """Ingest every manifest doc through the real pipeline into `conn`.

    Returns (n_docs, families). Idempotent per revision: the md5 of the
    text is the source_revision, so re-running on the same DB hits the
    unchanged path and writes nothing new.
    """
    if not MANIFEST.is_file():
        raise FileNotFoundError(
            f"golden corpus not found at {MANIFEST} - `fenris eval` runs from "
            "a repository checkout (the corpus is fixture data, not package "
            "data). cd to the repo root or run via `make eval`."
        )
    manifest = json.loads(MANIFEST.read_text())
    queue = IngestQueue(pace_seconds=0)   # fixture data, nothing to pace
    families: set[str] = set()
    for doc in manifest["docs"]:
        data = doc["text"].encode("utf-8")
        fm = FileMeta(
            drive_id="mydrive",
            file_id=doc["doc_id"],
            title=doc["title"],
            mime_type="text/plain",
            source_revision=hashlib.md5(data).hexdigest(),
        )
        queue.apply(conn, queue.stage(fm, data, cfg=None))
        families.add(doc["family"])
    conn.commit()
    return manifest["doc_count"], families
