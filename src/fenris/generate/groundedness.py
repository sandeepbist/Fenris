"""NLI groundedness: the last hole in citation enforcement (R4).

Today's check is ID-validity: a model can cite a real evidence_id and
still say something the chunk doesn't support. This module closes it
with per-sentence entailment scoring against the cited evidence UNION
(never per-chunk — per-chunk scoring collapses on multi-hop claims,
AUC 0.951 -> 0.52-0.64; the union premise is the corrected design).

The ladder interaction is downgrade-only by construction:
- unsupported sentences are DROPPED from the answer (drop-and-log,
  the invented-ID shape);
- citations not referenced by surviving text are dropped;
- all-dropped downgrades ANSWERABLE -> INSUFFICIENT;
- the gate can NEVER add text, add a citation, or raise a state.

The shipped checker (nli-deberta-v3-small int8 ONNX) arrives only via
`fenris models fetch nli-deberta-small` (pinned sha256 + smoke test).
No SQL — it operates on the Evidence objects it is handed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class GroundednessChecker(Protocol):
    """Per-sentence support scores in [0,1] against premise texts."""

    def check(self, sentences: list[str], premises: list[str]) -> list[float]: ...


@dataclass
class GroundednessResult:
    surviving_text: str
    dropped_sentences: list[str] = field(default_factory=list)
    per_sentence: list[dict] = field(default_factory=list)
    all_dropped: bool = False


def _sentences(answer_text: str) -> list[str]:
    """One sentence definition, shared with the renderer's claim logic."""
    from fenris.generate.render import _CLAIM_RE

    return [m.group(0).strip() for m in _CLAIM_RE.finditer(answer_text or "")
            if m.group(0).strip()]


def _cited_ids(sentence: str) -> list[str]:
    import re

    return re.findall(r"\[E:([0-9a-zA-Z]+)\]", sentence)


def apply_groundedness(answer_text: str, kept_evidence, checker,
                       threshold: float, surviving_floor: int) -> GroundednessResult:
    """Score each sentence against its cited evidence union; drop the
    unsupported ones. Pure: no state changes, no DB, never raises.

    ``kept_evidence`` is the verified evidence list (Evidence objects);
    a sentence's premise is the concatenated bodies of ITS cited chunks
    (top 3), falling back to the kept-set head for uncited sentences.
    """
    ev_by_id = {ev.evidence_id: ev for ev in kept_evidence}
    fallback = " ".join((ev.body_excerpt or "") for ev in list(kept_evidence)[:3])

    sentences = _sentences(answer_text)
    if not sentences:
        return GroundednessResult(surviving_text=answer_text or "")

    to_score: list[str] = []
    premises: list[str] = []
    for s in sentences:
        ids = _cited_ids(s)[:3]
        parts = [ev_by_id[i].body_excerpt or "" for i in ids if i in ev_by_id]
        premise = " ".join(p for p in parts if p) or fallback
        if not premise:
            continue  # no evidence at all: the renderer's own rules govern
        to_score.append(s)
        premises.append(premise)

    if not to_score:
        return GroundednessResult(surviving_text=answer_text)

    try:
        scores = checker.check(to_score, premises)
    except Exception:  # noqa: BLE001 - a checker failure never blocks the answer
        return GroundednessResult(surviving_text=answer_text)

    surviving: list[str] = []
    dropped: list[str] = []
    per_sentence: list[dict] = []
    by_text = dict(zip(to_score, scores))
    for s in sentences:
        if s in by_text:
            score = float(by_text[s])
            kept = score >= threshold
            per_sentence.append({"text": s[:120], "score": round(score, 3), "kept": kept})
            (surviving if kept else dropped).append(s)
        else:
            surviving.append(s)  # unscored (no premise): the existing rules govern

    result = GroundednessResult(
        surviving_text=" ".join(surviving),
        dropped_sentences=dropped,
        per_sentence=per_sentence,
        all_dropped=bool(sentences) and len(surviving) < surviving_floor,
    )
    return result


class FakeGroundednessChecker:
    """Deterministic offline double: scripted scores, round-robin."""

    name = "fake-groundedness"

    def __init__(self, scores: list[float] | None = None, default: float = 1.0):
        self._scores = list(scores or [])
        self._default = default
        self.calls: list[tuple[list[str], list[str]]] = []

    def check(self, sentences: list[str], premises: list[str]) -> list[float]:
        self.calls.append((list(sentences), list(premises)))
        out = []
        for i in range(len(sentences)):
            if i < len(self._scores):
                out.append(self._scores[i])
            else:
                out.append(self._default)
        return out


class NliGroundednessChecker:
    """nli-deberta-v3-small int8 ONNX, 3-way head -> P(entail).

    Lazy session; weights only via `fenris models fetch` — never a
    download from here. Premises are the caller's union strings.
    """

    name = "nli-deberta-v3-small"

    def __init__(self, *, _weights_dir=None):
        self._session = None
        self._tokenizer = None
        self._weights_dir = _weights_dir

    def _ensure_session(self):
        if self._session is None:
            import onnxruntime as ort
            from tokenizers import Tokenizer

            from fenris import models as models_mod

            base = self._weights_dir or models_mod.locate("nli-deberta-small")
            if base is None:
                raise FileNotFoundError(
                    "NLI weights not fetched - run "
                    "`fenris models fetch nli-deberta-small`"
                )
            self._session = ort.InferenceSession(
                str(base / "model.onnx"),
                providers=["CPUExecutionProvider"],
            )
            self._tokenizer = Tokenizer.from_file(str(base / "tokenizer.json"))

    def check(self, sentences: list[str], premises: list[str]) -> list[float]:
        import numpy as np

        self._ensure_session()
        out: list[float] = []
        for sent, premise in zip(sentences, premises):
            enc = self._tokenizer.encode(premise, sent)
            ids = np.array([enc.ids], dtype=np.int64)
            mask = np.array([enc.attention_mask], dtype=np.int64)
            logits = self._session.run(None, {
                "input_ids": ids, "attention_mask": mask,
            })[0]
            arr = np.asarray(logits).reshape(-1)
            if arr.size < 3:
                out.append(0.0)
                continue
            # 3-way NLI head: entail / neutral / contradict (softmax)
            e = float(arr[0]); n = float(arr[1]); c = float(arr[2])
            m = max(e, n, c)
            exps = [pow(2.718281828, v - m) for v in (e, n, c)]
            p_entail = exps[0] / sum(exps)
            out.append(p_entail)
        return out
