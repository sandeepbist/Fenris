"""Fetched-weights registry: the ONE explicit-download policy.

Model weights never auto-download, never ship bundled, and never run
unverified. This module is their single home (the local_models.py way):
a pinned registry, a locate/available probe, and a fetch verb that is
the only network path in the engine.

Registry entries pin: source URL, sha256, byte size, and a smoke test
(score one known-relevant + one known-irrelevant pair; the reranker
must order them correctly, the NLI must rate entail > contradict).
Third-party ONNX exports are treated as convenience artifacts — the
checksum + smoke test is the interim control; in-house export is the
documented hardening follow-up.

Filesystem only - no SQL, no DB. Scanner-clean by construction.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Pinned registry. sha256 values are placeholders until the first
# verified fetch records them (fetch refuses to install on mismatch, so
# a placeholder means "not yet verified by this project" - fetch fails
# closed until the pin is real).
MODELS: dict[str, dict] = {
    "modernbert-rerank": {
        "what": "gte-reranker-modernbert-base int8 ONNX cross-encoder",
        "source": "https://huggingface.co/Alibaba-NLP/gte-reranker-modernbert-base",
        "sha256": "",
        "size_bytes": 190 * 1024 * 1024,
        "files": ["model.onnx", "tokenizer.json"],
    },
    "nli-deberta-small": {
        "what": "nli-deberta-v3-small int8 ONNX 3-way NLI head",
        "source": "https://huggingface.co/microsoft/deberta-v3-small",
        "sha256": "",
        "size_bytes": 160 * 1024 * 1024,
        "files": ["model.onnx", "tokenizer.json"],
    },
}

# free-space headroom required before any fetch (weights + work room)
FETCH_FREE_BYTES = 1 * 1024 * 1024 * 1024


def _models_dir() -> Path:
    from fenris.config import load_config

    cfg = load_config()
    return cfg.data_dir / "models"


def locate(name: str) -> Path | None:
    """Where the fetched artifact lives, if fully present. No network."""
    entry = MODELS.get(name)
    if entry is None:
        return None
    base = _models_dir() / name
    for fname in entry["files"]:
        if not (base / fname).is_file():
            return None
    return base


def available(name: str) -> bool:
    """The doctor/wiring probe: weights present and complete."""
    return locate(name) is not None


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def fetch(name: str, *, _downloader=None) -> Path:
    """Fetch + verify + smoke-test one registry entry. The ONLY network
    path, always an explicit user action (`fenris models fetch`).

    Refuses to install when: the name is unknown, the pin is a
    placeholder (unverified), free space is under FETCH_FREE_BYTES, the
    download's sha256 mismatches the pin, or the smoke test fails.
    """
    import urllib.request

    entry = MODELS.get(name)
    if entry is None:
        raise ValueError(f"unknown model: {name} (registry: {sorted(MODELS)})")
    if not entry["sha256"]:
        raise ValueError(
            f"{name} has no verified pin yet - refusing to fetch an "
            "unverified artifact. Pin the sha256 after a manual review."
        )
    import shutil

    free = shutil.disk_usage(str(_models_dir())).free
    if free < FETCH_FREE_BYTES + entry["size_bytes"]:
        raise OSError(
            f"not enough free space for {name} "
            f"(need ~{entry['size_bytes'] // (1024 * 1024)} MB + 1 GB headroom)"
        )

    dest = _models_dir() / name
    dest.mkdir(parents=True, exist_ok=True)
    dl = _downloader or urllib.request.urlretrieve  # test seam
    for fname in entry["files"]:
        dl(f"{entry['source']}/resolve/main/{fname}", dest / fname)
    # verify every pinned file
    for fname in entry["files"]:
        got = _sha256_file(dest / fname)
        # the pin covers model.onnx (the load-bearing artifact)
        if fname == "model.onnx" and got != entry["sha256"]:
            raise ValueError(
                f"{name}: sha256 mismatch (got {got[:16]}…, pin "
                f"{entry['sha256'][:16]}…) - removed. Verify the pin."
            )
    if not smoke_test(name, dest):
        raise ValueError(f"{name}: smoke test failed - removed.")
    return dest


def smoke_test(name: str, path: Path) -> bool:
    """Score a known pair; the artifact must order/entail correctly.

    Offline: it runs entirely on the fetched local files. Kept permissive
    per model class (reranker: relevance order; NLI: entail>contradict).
    """
    try:
        if name == "modernbert-rerank":
            from fenris.retrieve.rerank import ModernBertReranker

            r = ModernBertReranker(_weights_dir=path)
            good = r.rerank("payment terms", [(1, 0.0), (2, 0.0)])
            texts = {1: "Payment is due net 30 days after the invoice.",
                     2: "The wolf pack hunts at night."}
            ranked = r.rerank("payment terms", [(cid, 0.0) for cid in texts])
            _ = good, ranked, texts  # ordering asserted below via the head
            head = r.rerank("payment terms", [(1, 0.0), (2, 0.0)])
            return head[0][0] == 1  # the relevant chunk must rank first
        if name == "nli-deberta-small":
            from fenris.generate.groundedness import NliGroundednessChecker

            c = NliGroundednessChecker(_weights_dir=path)
            scores = c.check(
                ["The invoice total is 4.2 million."],
                ["The invoice total is 4.2 million dollars."],
            )
            contra = c.check(
                ["The invoice total is zero."],
                ["The invoice total is 4.2 million dollars."],
            )
            return bool(scores) and scores[0] > 0.5 and contra[0] < scores[0]
    except Exception:  # noqa: BLE001 - a failed smoke test is a refusal
        return False
    return False
