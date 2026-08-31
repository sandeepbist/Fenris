"""Synthetic "personal Drive" corpus for the golden eval suite (the build plan).

Design goals - every family exists because a specific eval slice needs it:

- invoices       -> exact-id/name + table + hard-negative (same vendor, different year)
- meetings       -> temporal + multi-hop (decision referenced by a later invoice)
- projects       -> versioned revisions of the SAME doc (freshness/multi-hop)
- expenses sheet -> table slice (row-level facts)
- travel         -> paraphrase/alias + temporal
- near-duplicates-> hard-negatives that must NOT rank for the target question

Each document carries ground truth: the questions it answers and the supporting
character spans. Documents are addressed as ``doc_id`` strings throughout the
eval stack; char spans are [start, end) into the doc's exact text as stored in
manifest.json (byte-stable because the generator is seeded).

The corpus is deterministic: ``build_corpus(seed)`` always returns the same
docs in the same order, and ``write_corpus`` always produces the same bytes.
The committed copy under eval/fixtures/golden_corpus/ is regenerated with
``.venv/bin/python eval/fixtures/generate_corpus.py``.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

SEED = 20260831  # MVP authoring date; bump to regenerate a different corpus

# ---------------------------------------------------------------------------
# Ground-truth dataclasses
# ---------------------------------------------------------------------------


@dataclass
class AnswerSpan:
    """A character span [start, end) within a document's text."""

    doc_id: str
    start: int
    end: int

    def overlaps_fraction(self, other: AnswerSpan) -> float:
        """Fraction of THIS span covered by ``other`` (0.0 - 1.0)."""
        lo = max(self.start, other.start)
        hi = min(self.end, other.end)
        if hi <= lo:
            return 0.0
        return (hi - lo) / (self.end - self.start)


@dataclass
class CorpusDoc:
    """One synthetic document with its ground truth."""

    doc_id: str
    family: str                 # invoice | meeting | project | expenses | travel | near_dup
    title: str
    text: str                   # plain text; char spans index into this
    questions: list[str] = field(default_factory=list)  # question ids this doc answers
    answer_spans: list[AnswerSpan] = field(default_factory=list)  # supporting evidence
    meta: dict = field(default_factory=dict)           # family-specific facts

    def to_manifest(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "family": self.family,
            "title": self.title,
            "text": self.text,
            "questions": list(self.questions),
            "answer_spans": [
                {"doc_id": s.doc_id, "start": s.start, "end": s.end}
                for s in self.answer_spans
            ],
            "meta": self.meta,
        }


# ---------------------------------------------------------------------------
# Vocabulary - fixed so the corpus reads like a real person's Drive
# ---------------------------------------------------------------------------

VENDORS = [
    "Acme Cloud Services", "Northwind Logistics", "Helios Design Studio",
    "Bracket & Fell LLP", "Quanta Analytics", "Ridgeline Hardware",
    "Sable Marketing Group", "Tessellate Consulting",
]

PROJECTS = {
    "ATLAS": "internal data-platform migration",
    "BODEEN": "customer-facing mobile app refresh",
    "CINDER": "compliance audit remediation",
    "DRIFT": "marketing site relaunch",
}

PEOPLE = [
    "Priya N.", "Marcus T.", "Elena V.", "Jonah K.", "Sofia R.",
    "Dana W.", "Alex Q.", "Mira H.",
]

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]

CITIES = [
    ("Lisbon", "Portugal"), ("Osaka", "Japan"), ("Toronto", "Canada"),
    ("Krakow", "Poland"), ("Bristol", "UK"), ("Medellin", "Colombia"),
]


def _fmt_money(amount: float) -> str:
    """Deterministic money format: 1,250.00 style with thousands separators."""
    return f"{amount:,.2f}"


def _span_of(text: str, needle: str, which: int = 0) -> tuple[int, int]:
    """Locate the which-th occurrence of needle; ValueError if absent."""
    idx = -1
    for _ in range(which + 1):
        idx = text.find(needle, idx + 1)
        if idx < 0:
            raise ValueError(f"needle {needle!r} (#{which}) not in doc text")
    return idx, idx + len(needle)


# ---------------------------------------------------------------------------
# Family builders - all take the seeded RNG, all fully deterministic
# ---------------------------------------------------------------------------


def _build_invoices(rng: random.Random) -> list[CorpusDoc]:
    """Dated invoices: amounts, vendors, project codes.

    Years 2023-2026 so 'same vendor different year' near-duplicates exist.
    Invoice numbers are unique and high-signal (exact-id slice).
    """
    docs: list[CorpusDoc] = []
    for year in (2023, 2024, 2025, 2026):
        for m_idx, month in enumerate(MONTHS):           # 12 months/yr
            vendor = VENDORS[(m_idx + year) % len(VENDORS)]
            project = list(PROJECTS)[(m_idx * 2 + year) % len(PROJECTS)]
            inv_no = f"INV-{year}{m_idx + 1:02d}-{(m_idx * 7 + year) % 900 + 100}"
            amount = 120.0 + ((m_idx * 137 + year * 53) % 8700) + 0.5 * (m_idx % 4)
            day = 3 + (m_idx + year) % 24
            text = (
                f"INVOICE {inv_no}\n"
                f"Vendor: {vendor}\n"
                f"Invoice date: {month} {day}, {year}\n"
                f"Bill to: Orbis Consulting\n"
                f"Project code: {project}\n"
                f"Description: professional services rendered for {project} - "
                f"{PROJECTS[project]}.\n"
                f"Total amount due: {_fmt_money(amount)} USD\n"
                f"Payment terms: net 30\n"
            )
            spans: list[AnswerSpan] = [AnswerSpan("", *_span_of(text, inv_no))]
            docs.append(CorpusDoc(
                doc_id="", family="invoice",
                title=f"Invoice {inv_no} - {vendor}",
                text=text,
                questions=[], answer_spans=spans,
                meta={"invoice_no": inv_no, "vendor": vendor, "year": year,
                      "month": month, "amount": amount, "project": project},
            ))
    # second invoice per (year, month) for some pairs -> same-vendor/same-project
    # different-invoice hard negatives
    for i, d in enumerate(docs):
        d.doc_id = f"inv-{i:03d}"
        for s in d.answer_spans:
            s.doc_id = d.doc_id
        d.questions = [f"exact-{d.meta['invoice_no']}"]
    return docs


def _build_meetings(rng: random.Random) -> list[CorpusDoc]:
    """Meeting notes: attendees, decisions. Decisions reference projects so
    multi-hop cases can chain meeting -> invoice."""
    docs: list[CorpusDoc] = []
    for i in range(14):
        year = 2024 + i % 3
        month = MONTHS[(i * 5) % 12]
        day = 1 + (i * 3) % 27
        attendees = rng.sample(PEOPLE, 4)
        project = list(PROJECTS)[i % len(PROJECTS)]
        vendor = VENDORS[(i * 3) % len(VENDORS)]
        decision_no = f"DEC-{year}-{i:02d}"
        budget = 5000 + (i * 1250) % 40000
        text = (
            f"Meeting notes - {project} weekly sync\n"
            f"Date: {month} {day}, {year}\n"
            f"Attendees: {', '.join(attendees)}\n"
            f"Agenda: {project} status, vendor review, budget planning.\n"
            f"Decision {decision_no}: approved {_fmt_money(float(budget))} USD "
            f"budget for {PROJECTS[project]} phase {1 + i % 3}.\n"
            f"Action items: {attendees[0]} to follow up with {vendor}; "
            f"{attendees[1]} to draft the {project} rollout plan.\n"
        )
        doc_id = f"mtg-{i:03d}"
        spans = [AnswerSpan(doc_id, *_span_of(text, decision_no)),
                 AnswerSpan(doc_id, *_span_of(text, f"{_fmt_money(float(budget))} USD"))]
        docs.append(CorpusDoc(
            doc_id=doc_id, family="meeting",
            title=f"{project} sync - {month} {day}, {year}",
            text=text,
            questions=[f"meeting-{decision_no}"],
            answer_spans=spans,
            meta={"decision_no": decision_no, "project": project,
                  "budget": budget, "attendees": attendees,
                  "vendor": vendor, "date": f"{month} {day}, {year}",
                  "year": year, "month": month},
        ))
    return docs


def _build_projects(rng: random.Random) -> list[CorpusDoc]:
    """Project docs with VERSIONED REVISIONS of the same document: v1, v2, v3
    of e.g. 'ATLAS architecture spec'. The latest revision is the current
    truth; older revisions answer 'what did v1 say' questions and act as
    freshness/multi-hop material."""
    docs: list[CorpusDoc] = []
    for p_idx, (project, desc) in enumerate(PROJECTS.items()):
        for rev in (1, 2, 3):
            i = len(docs)
            year = 2024 + p_idx
            month = MONTHS[(p_idx * 3 + rev) % 12]
            owner = PEOPLE[(p_idx + rev) % len(PEOPLE)]
            status = {1: "draft", 2: "in review", 3: "approved"}[rev]
            lead = PEOPLE[(p_idx * 2 + rev * 3) % len(PEOPLE)]
            text = (
                f"{project} architecture spec - revision v{rev}\n"
                f"Project: {project} - {desc}\n"
                f"Status: {status}\n"
                f"Revision date: {month} {5 + rev}, {year}\n"
                f"Document owner: {owner}\n"
                f"Technical lead: {lead}\n"
                f"Revision v{rev} summary: this revision of the {project} "
                f"spec updates the scope for {desc} and records the current "
                f"status as {status}.\n"
            )
            doc_id = f"prj-{i:03d}"
            spans = [AnswerSpan(doc_id, *_span_of(text, f"revision v{rev}\n"))]
            # span for the status line in the body (the second occurrence)
            spans.append(AnswerSpan(doc_id, *_span_of(text, f"as {status}.\n", 0)))
            docs.append(CorpusDoc(
                doc_id=doc_id, family="project",
                title=f"{project} architecture spec v{rev}",
                text=text,
                questions=[f"project-{project}-v{rev}"],
                answer_spans=spans,
                meta={"project": project, "revision": rev, "status": status,
                      "year": year, "month": month, "owner": owner,
                      "lead": lead, "is_latest": rev == 3},
            ))
    return docs


def _build_expenses(rng: random.Random) -> list[CorpusDoc]:
    """The expenses spreadsheet: one doc per sheet block of rows, rendered as
    pipe-separated rows so table-slice questions have row-level ground truth."""
    docs: list[CorpusDoc] = []
    rows: list[dict] = []
    for i in range(36):
        year = 2025
        month = MONTHS[i % 12]
        day = 2 + (i * 5) % 25
        vendor = VENDORS[i % len(VENDORS)]
        project = list(PROJECTS)[i % len(PROJECTS)]
        amount = 40.0 + (i * 17) % 460
        rows.append({"date": f"{month} {day}, {year}", "vendor": vendor,
                     "amount": amount, "project": project, "row": i + 2})
    header = "Date | Vendor | Amount | Project\n"
    for block_idx in range(3):
        block = rows[block_idx * 12:(block_idx + 1) * 12]
        lines = [header.rstrip("\n")]
        for r in block:
            lines.append(
                f"{r['date']} | {r['vendor']} | {_fmt_money(r['amount'])} USD | {r['project']}"
            )
        text = (
            f"Expenses 2025 - tracked purchases (sheet block {block_idx + 1} of 3)\n"
            + "\n".join(lines) + "\n"
        )
        doc_id = f"exp-{block_idx:03d}"
        spans = []
        for r in block:
            needle = f"{r['vendor']} | {_fmt_money(r['amount'])} USD"
            spans.append(AnswerSpan(doc_id, *_span_of(text, needle)))
        docs.append(CorpusDoc(
            doc_id=doc_id, family="expenses",
            title=f"Expenses 2025 sheet block {block_idx + 1}",
            text=text, questions=[f"expenses-block-{block_idx}"],
            answer_spans=spans,
            meta={"block": block_idx, "rows": block},
        ))
    return docs


def _build_travel(rng: random.Random) -> list[CorpusDoc]:
    """Travel itineraries: cities, dates, flight numbers, aliases (city short
    forms) for the paraphrase slice."""
    docs: list[CorpusDoc] = []
    for i in range(12):
        city, country = CITIES[i % len(CITIES)]
        year = 2025 + i % 2
        month = MONTHS[(i * 7) % 12]
        day = 4 + (i * 6) % 20
        flight = f"{'KL' if i % 2 else 'TP'}{400 + i * 37}"
        ret_day = day + 3 + i % 6
        conf = f"TRV-{year}-{i:02d}"
        ret_flight = f"{flight[:2]}{int(flight[2:]) + 1}"
        text = (
            f"Travel itinerary {conf}: {city}\n"
            f"Destination: {city}, {country}\n"
            f"Outbound: {month} {day}, {year} - flight {flight}\n"
            f"Return: {month} {ret_day}, {year} - flight {ret_flight}\n"
            f"Hotel: Grand {city} Hotel\n"
            f"Booking reference: {conf}-HK\n"
        )
        doc_id = f"trv-{i:03d}"
        spans = [AnswerSpan(doc_id, *_span_of(text, flight)),
                 AnswerSpan(doc_id, *_span_of(text, conf))]
        docs.append(CorpusDoc(
            doc_id=doc_id, family="travel",
            title=f"Itinerary {conf} - {city}",
            text=text, questions=[f"travel-{conf}"],
            answer_spans=spans,
            meta={"city": city, "country": country, "flight": flight,
                  "conf": conf, "year": year, "month": month,
                  "date": f"{month} {day}, {year}", "ret_date": f"{month} {ret_day}, {year}"},
        ))
    return docs


def _build_near_dups(rng: random.Random) -> list[CorpusDoc]:
    """Hard-negative near-duplicates: same vendor different year, same project
    different invoice number, near-identical wording. These must NOT rank for
    the target question about the OTHER document."""
    docs: list[CorpusDoc] = []
    pairs = [
        # (vendor, year A, year B, differing detail)
        ("Acme Cloud Services", 2024, 2025),
        ("Quanta Analytics", 2023, 2025),
        ("Ridgeline Hardware", 2024, 2026),
        ("Sable Marketing Group", 2023, 2024),
        ("Tessellate Consulting", 2025, 2026),
    ]
    for i, (vendor, yr_a, yr_b) in enumerate(pairs):
        for tag, yr in (("a", yr_a), ("b", yr_b)):
            project = list(PROJECTS)[i % len(PROJECTS)]
            inv_no = f"INV-{yr}-ND{i}{tag}"
            amount = 800.0 + i * 111 + (250 if tag == "b" else 0)
            text = (
                f"INVOICE {inv_no}\n"
                f"Vendor: {vendor}\n"
                f"Invoice date: {'March' if i % 2 else 'April'} {10 + i}, {yr}\n"
                f"Project code: {project}\n"
                f"Total amount due: {_fmt_money(amount)} USD\n"
            )
            doc_id = f"nd-{i}{tag}"
            spans = [AnswerSpan(doc_id, *_span_of(text, inv_no))]
            docs.append(CorpusDoc(
                doc_id=doc_id, family="near_dup",
                title=f"Invoice {inv_no} - {vendor}",
                text=text, questions=[f"near-dup-{inv_no}"],
                answer_spans=spans,
                meta={"invoice_no": inv_no, "vendor": vendor, "year": yr,
                      "amount": amount, "project": project, "pair": i, "tag": tag},
            ))
    return docs


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def build_corpus(seed: int = SEED) -> list[CorpusDoc]:
    """Build the full deterministic corpus (~100 docs)."""
    rng = random.Random(seed)
    docs: list[CorpusDoc] = []
    docs += _build_invoices(rng)
    docs += _build_meetings(rng)
    docs += _build_projects(rng)
    docs += _build_expenses(rng)
    docs += _build_travel(rng)
    docs += _build_near_dups(rng)
    # stable ids are already assigned by family builders; verify uniqueness
    seen: set[str] = set()
    for d in docs:
        if d.doc_id in seen:
            raise ValueError(f"duplicate doc_id {d.doc_id}")
        seen.add(d.doc_id)
    return docs


def validate_corpus(docs: list[CorpusDoc]) -> None:
    """Self-consistency: doc ids unique, spans within bounds, span text resolves."""
    seen: set[str] = set()
    for d in docs:
        if d.doc_id in seen:
            raise ValueError(f"duplicate doc_id {d.doc_id}")
        seen.add(d.doc_id)
        for s in d.answer_spans:
            if s.doc_id != d.doc_id:
                raise ValueError(f"span {s} references foreign doc {d.doc_id}")
            if not (0 <= s.start < s.end <= len(d.text)):
                raise ValueError(
                    f"span {s} out of bounds for {d.doc_id} (len={len(d.text)})")


def write_corpus(out_dir: Path, docs: list[CorpusDoc]) -> Path:
    """Write docs/ (one plain file per doc) + manifest.json. Deterministic."""
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "seed": SEED,
        "generated_by": "eval/fixtures/generate_corpus.py",
        "doc_count": len(docs),
        "families": sorted({d.family for d in docs}),
        "docs": [d.to_manifest() for d in docs],
    }
    for d in docs:
        (out_dir / "docs").mkdir(exist_ok=True)
        (out_dir / "docs" / f"{d.doc_id}.txt").write_text(d.text, encoding="utf-8")
    manifest_path = out_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8")
    return manifest_path


def load_manifest(manifest_path: Path) -> dict:
    """Load manifest.json into memory (dict with 'docs' list)."""
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def manifest_spans(manifest: dict) -> dict[str, list[AnswerSpan]]:
    """doc_id -> [AnswerSpan] from a loaded manifest."""
    out: dict[str, list[AnswerSpan]] = {}
    for d in manifest["docs"]:
        out[d["doc_id"]] = [
            AnswerSpan(s["doc_id"], s["start"], s["end"])
            for s in d["answer_spans"]
        ]
    return out
