#!/usr/bin/env python
"""Regenerate the committed golden corpus: eval/fixtures/golden_corpus/.

Run:  .venv/bin/python eval/fixtures/generate_corpus.py

Writes docs/<doc_id>.txt (one plain file per doc) and manifest.json
(doc_id, family, text, questions, answer_spans, meta). Seeded and fully
deterministic: two runs produce byte-identical output.
"""

from __future__ import annotations

import sys
from pathlib import Path

# make src/ importable when run as a plain script from the repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fenris.eval.corpus import SEED, build_corpus, validate_corpus, write_corpus

OUT_DIR = Path(__file__).resolve().parent / "golden_corpus"


def main() -> int:
    docs = build_corpus(SEED)
    validate_corpus(docs)
    manifest_path = write_corpus(OUT_DIR, docs)
    by_family: dict[str, int] = {}
    for d in docs:
        by_family[d.family] = by_family.get(d.family, 0) + 1
    print(f"wrote {len(docs)} docs to {OUT_DIR}")
    for fam, n in sorted(by_family.items()):
        print(f"  {fam}: {n}")
    print(f"manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
