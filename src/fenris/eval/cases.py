"""Golden-suite eval cases: schema, authoring, I/O, validation (the build plan).

Case schema (the build plan, golden suite):

- id                  unique, stable across runs (regression diffs key on it)
- suite               always "golden" for this suite
- slice               exact | temporal | multi_hop | paraphrase | hard_negative
                      | unanswerable | table
- question            the query text
- expected_docs       {doc_id: grade} with grade 2 = must retrieve, 1 = helpful,
                      0 = hard negative (must NOT rank); empty for unanswerable
- answerable          bool; false only for the unanswerable slice
- expected_state      ANSWERED | INSUFFICIENT (system ladder state for THIS suite;
                      other states are exercised by later suites)
- expected_answer_facts  strings that must appear in a correct answer
- expected_spans      [{doc_id, start, end}] supporting evidence, char offsets
                      into the manifest doc text
- created_by          author tag, "synth-v1" for generator-authored cases
- locked              false here; true is reserved for human-labeled cases
                      (judge calibration, M6) and those are never used for tuning

Files live in eval/cases/golden/ as YAML (pyyaml is installed); ``load_cases``
also accepts .json so the suite works without pyyaml.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover - pyyaml is present in this env
    yaml = None

SUITE = "golden"

SLICES = (
    "exact",
    "temporal",
    "multi_hop",
    "paraphrase",
    "hard_negative",
    "unanswerable",
    "table",
)

# the build plan exact composition of the golden suite
SLICE_COUNTS = {
    "exact": 20,
    "temporal": 15,
    "multi_hop": 15,
    "paraphrase": 15,
    "hard_negative": 25,
    "unanswerable": 15,
    "table": 15,
}

ANSWERED, INSUFFICIENT = "ANSWERED", "INSUFFICIENT"

# Grades for expected_docs
GRADE_MUST, GRADE_HELPFUL, GRADE_HARD_NEG = 2, 1, 0


@dataclass
class ExpectedSpan:
    doc_id: str
    start: int
    end: int


@dataclass
class EvalCase:
    """One golden-suite question with ground truth."""

    id: str
    suite: str = SUITE
    slice: str = ""                     # schema field name per the build plan (shadows builtin)
    question: str = ""
    expected_docs: dict[str, int] = field(default_factory=dict)
    answerable: bool = True
    expected_state: str = ANSWERED
    expected_answer_facts: list[str] = field(default_factory=list)
    expected_spans: list[ExpectedSpan] = field(default_factory=list)
    created_by: str = "synth-v1"
    locked: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d["expected_spans"] = [asdict(s) for s in self.expected_spans]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> EvalCase:
        return cls(
            id=d["id"],
            suite=d.get("suite", SUITE),
            slice=d["slice"],
            question=d["question"],
            expected_docs={str(k): int(v) for k, v in d.get("expected_docs", {}).items()},
            answerable=bool(d.get("answerable", True)),
            expected_state=d.get("expected_state", ANSWERED),
            expected_answer_facts=list(d.get("expected_answer_facts", [])),
            expected_spans=[ExpectedSpan(**s) for s in d.get("expected_spans", [])],
            created_by=d.get("created_by", "synth-v1"),
            locked=bool(d.get("locked", False)),
        )


# ---------------------------------------------------------------------------
# Validation - programmatic guarantees the suite relies on
# ---------------------------------------------------------------------------


def validate_case(case: EvalCase, manifest_docs: dict[str, dict]) -> None:
    """Raise ValueError unless the case is well-formed AND answerable by the corpus.

    For every case (including unanswerable ones) every referenced doc must exist
    and every expected span must resolve inside that doc's text. Unanswerable
    cases must reference no docs and no spans, and their expected_state is
    INSUFFICIENT.
    """
    if case.suite != SUITE:
        raise ValueError(f"{case.id}: suite must be {SUITE!r}, got {case.suite!r}")
    if case.slice not in SLICES:
        raise ValueError(f"{case.id}: unknown slice {case.slice!r}")
    if not case.question or not case.question.strip():
        raise ValueError(f"{case.id}: empty question")

    if case.slice == "unanswerable":
        if case.expected_docs:
            raise ValueError(f"{case.id}: unanswerable case must have no expected_docs")
        if case.expected_spans:
            raise ValueError(f"{case.id}: unanswerable case must have no expected_spans")
        if case.answerable:
            raise ValueError(f"{case.id}: unanswerable case must set answerable: false")
        if case.expected_state != INSUFFICIENT:
            raise ValueError(
                f"{case.id}: unanswerable case must expect {INSUFFICIENT}, "
                f"got {case.expected_state!r}")
        return

    # answerable slices
    if not case.answerable:
        raise ValueError(f"{case.id}: slice {case.slice} cases must be answerable")
    if case.expected_state != ANSWERED:
        raise ValueError(
            f"{case.id}: slice {case.slice} must expect {ANSWERED}, "
            f"got {case.expected_state!r}")
    if not case.expected_docs:
        raise ValueError(f"{case.id}: answerable case needs >=1 expected_doc")
    if not any(g >= GRADE_MUST for g in case.expected_docs.values()):
        raise ValueError(f"{case.id}: answerable case needs >=1 grade-2 must-doc")

    for doc_id, grade in case.expected_docs.items():
        if grade not in (GRADE_MUST, GRADE_HELPFUL, GRADE_HARD_NEG):
            raise ValueError(f"{case.id}: doc {doc_id} has invalid grade {grade}")
        if doc_id not in manifest_docs:
            raise ValueError(f"{case.id}: expected doc {doc_id} not in corpus manifest")

    for sp in case.expected_spans:
        if sp.doc_id not in manifest_docs:
            raise ValueError(f"{case.id}: span doc {sp.doc_id} not in manifest")
        text = manifest_docs[sp.doc_id]["text"]
        if not (0 <= sp.start < sp.end <= len(text)):
            raise ValueError(
                f"{case.id}: span {sp.doc_id}[{sp.start}:{sp.end}] out of bounds "
                f"(len={len(text)})")
    # answerable cases need at least one supporting span
    if not case.expected_spans:
        raise ValueError(f"{case.id}: answerable case needs >=1 expected span")


def validate_all(cases: list[EvalCase], manifest_docs: dict[str, dict]) -> None:
    ids: set[str] = set()
    for c in cases:
        if c.id in ids:
            raise ValueError(f"duplicate case id {c.id}")
        ids.add(c.id)
        validate_case(c, manifest_docs)
    # slice composition matches the the build-plan table exactly
    got: dict[str, int] = {}
    for c in cases:
        got[c.slice] = got.get(c.slice, 0) + 1
    if got != SLICE_COUNTS:
        raise ValueError(
            f"slice composition mismatch: got {sorted(got.items())}, "
            f"want {sorted(SLICE_COUNTS.items())}")


def slice_counts(cases: list[EvalCase]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for c in cases:
        counts[c.slice] = counts.get(c.slice, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# I/O
# ---------------------------------------------------------------------------


def _dump_yaml(obj) -> str:
    if yaml is None:  # pragma: no cover
        raise RuntimeError("pyyaml not installed; use JSON case files")
    return yaml.safe_dump(obj, sort_keys=False, allow_unicode=True, width=100)


def save_cases(cases: list[EvalCase], out_dir: Path, fmt: str = "yaml") -> list[Path]:
    """Write one file per case (eval/cases/golden/). Returns written paths.

    fmt: "yaml" (default; falls back to json when pyyaml is missing) or "json".
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    use_yaml = fmt == "yaml" and yaml is not None
    ext = "yaml" if use_yaml else "json"
    paths = []
    for c in cases:
        payload = c.to_dict()
        p = out_dir / f"{c.id}.{ext}"
        if use_yaml:
            p.write_text(_dump_yaml(payload), encoding="utf-8")
        else:
            p.write_text(
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8")
        paths.append(p)
    return paths


def _load_one(path: Path) -> EvalCase:
    raw = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        if yaml is None:  # pragma: no cover
            raise RuntimeError(f"pyyaml not installed; cannot read {path}")
        data = yaml.safe_load(raw)
    else:
        data = json.loads(raw)
    return EvalCase.from_dict(data)


def load_cases(case_dir: Path) -> list[EvalCase]:
    """Load all cases from a directory (yaml or json), sorted by id."""
    cases = []
    for p in sorted(case_dir.iterdir()):
        if p.suffix in (".yaml", ".yml", ".json"):
            cases.append(_load_one(p))
    return cases


def load_manifest_docs(manifest_path: Path) -> dict[str, dict]:
    """manifest.json -> {doc_id: doc dict}. Used by validation and metrics."""
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return {d["doc_id"]: d for d in manifest["docs"]}
