#!/usr/bin/env python
"""Author the 120 golden-suite cases into eval/cases/golden/ (the build plan).

Run:  .venv/bin/python eval/fixtures/generate_cases.py

Composition (exact, from the build plan):
  exact 20, temporal 15, multi_hop 15, paraphrase 15,
  hard_negative 25, unanswerable 15, table 15  ->  120 cases.

Every case is generated from the COMMITTED manifest (eval/fixtures/
golden_corpus/manifest.json) and its expected_spans are located in the actual
doc text via the same _span_of helper the corpus uses - so "case answerable by
the corpus" is enforced by construction and re-verified by validate_all()
before anything is written. Unanswerable cases reference vendors/projects/
invoice numbers that provably do NOT exist in the corpus (checked against the
manifest text) and expect INSUFFICIENT.

Deterministic: fixed selection, no RNG. Output is YAML (one file per case).
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "src"))

from fenris.eval.cases import (
    ANSWERED,
    GRADE_HARD_NEG,
    GRADE_HELPFUL,
    GRADE_MUST,
    INSUFFICIENT,
    EvalCase,
    ExpectedSpan,
    save_cases,
    validate_all,
)
from fenris.eval.corpus import load_manifest

MANIFEST_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "golden_corpus" / "manifest.json"
OUT_DIR = Path(__file__).resolve().parents[2] / "eval" / "cases" / "golden"

# Unanswerable-case vocabulary: values verified ABSENT from the corpus below.
MISSING_VENDORS = ["Zenith Parallax Labs", "Orbital Freight Systems",
                   "Kestrel Legal Group", "Bluewater Marine Supplies",
                   "Vantage Pixel Studio"]
MISSING_PROJECTS = ["EPOCH", "FATHOM", "GLACIER", "HALLWAY"]
MISSING_INV_NOS = ["INV-99001", "INV-99002", "INV-99003", "INV-99004",
                   "INV-99005", "INV-99006"]
MISSING_DECISIONS = ["DEC-2099-99", "DEC-2098-98", "DEC-2097-97"]
MISSING_CITIES = ["Reykjavik", "Nairobi", "Hanoi"]
MISSING_FLIGHTS = ["ZZ999", "QQ888", "XX777"]


def _span_of(text: str, needle: str, which: int = 0) -> tuple[int, int]:
    idx = -1
    for _ in range(which + 1):
        idx = text.find(needle, idx + 1)
        if idx < 0:
            raise ValueError(f"needle {needle!r} (#{which}) not found - case not answerable")
    return idx, idx + len(needle)


def _docs(docs: list[dict], **filters) -> list[dict]:
    out = []
    for d in docs:
        if all(d.get("meta", {}).get(k) == v for k, v in filters.items()):
            out.append(d)
    return out


def _spans_for(doc: dict, needles: list[str]) -> list[ExpectedSpan]:
    """Expected spans located in the doc's actual text."""
    return [
        ExpectedSpan(doc_id=doc["doc_id"], start=s, end=e)
        for needle in needles
        for s, e in [_span_of(doc["text"], needle)]
    ]


# ---------------------------------------------------------------------------
# Slice builders - each returns a list of EvalCase
# ---------------------------------------------------------------------------


def build_exact(docs: list[dict]) -> list[EvalCase]:
    """20 exact-id/name cases: unique invoice numbers, decision numbers,
    booking references - the EXACT route (df <= 3)."""
    cases = []
    invoices = [d for d in docs if d["family"] == "invoice"][:12]
    meetings = [d for d in docs if d["family"] == "meeting"][:4]
    projects = [d for d in docs if d["family"] == "project"
                and d["meta"]["revision"] == 3][:4]
    for d in invoices + meetings + projects:
        if d["family"] == "invoice":
            inv = d["meta"]["invoice_no"]
            q = f"Find invoice {inv}"
            spans = _spans_for(d, [inv])
            facts = [inv, d["meta"]["vendor"]]
            expected = {d["doc_id"]: GRADE_MUST}
        elif d["family"] == "meeting":
            dec = d["meta"]["decision_no"]
            q = f"What was decided in decision {dec}?"
            spans = _spans_for(d, [dec, f"{d['meta']['budget']:,.2f} USD"])
            facts = [dec, f"{d['meta']['budget']:,.2f}"]
            expected = {d["doc_id"]: GRADE_MUST}
        else:
            proj = d["meta"]["project"]
            q = f"Show me the approved {proj} architecture spec"
            spans = _spans_for(d, [f"revision v{d['meta']['revision']}"])
            facts = [proj, "approved"]
            expected = {d["doc_id"]: GRADE_MUST}
        cases.append(EvalCase(
            id=f"golden-exact-{len(cases) + 1:03d}",
            slice="exact", question=q,
            expected_docs=expected, answerable=True,
            expected_state=ANSWERED,
            expected_answer_facts=facts,
            expected_spans=spans,
        ))
    return cases[:20]


def build_temporal(docs: list[dict]) -> list[EvalCase]:
    """15 temporal cases: questions where the DATE discriminates the target
    from same-topic docs (other years/months)."""
    cases = []
    invoices = [d for d in docs if d["family"] == "invoice"
                and d["meta"]["year"] in (2024, 2025)]
    for i, d in enumerate(invoices[:8]):
        m = d["meta"]
        q = (f"What did {m['vendor']} invoice in {m['month']} {m['year']} "
             f"for project {m['project']}?")
        spans = _spans_for(d, [f"{m['amount']:,.2f}"])
        # same vendor in another year = grade-0 hard negative for this question
        same_vendor = [o for o in docs if o["family"] == "invoice"
                       and o["meta"]["vendor"] == m["vendor"]
                       and o["doc_id"] != d["doc_id"]]
        expected = {d["doc_id"]: GRADE_MUST}
        for o in same_vendor[:2]:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-temporal-{len(cases) + 1:03d}",
            slice="temporal", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[m["vendor"], f"{m['amount']:,.2f}"],
            expected_spans=spans,
        ))
    travel = [d for d in docs if d["family"] == "travel"][:5]
    for d in travel:
        m = d["meta"]
        q = f"When did I travel to {m['city']} in {m['month']} {m['year']}?"
        spans = _spans_for(d, [m["date"]])
        other_city = [o for o in docs if o["family"] == "travel"
                      and o["doc_id"] != d["doc_id"]]
        expected = {d["doc_id"]: GRADE_MUST}
        for o in other_city[:2]:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-temporal-{len(cases) + 1:03d}",
            slice="temporal", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[m["city"], m["month"], str(m["year"])],
            expected_spans=spans,
        ))
    meetings = [d for d in docs if d["family"] == "meeting"][:2]
    for d in meetings:
        m = d["meta"]
        q = f"What budget was approved for {m['project']} in {m['month']} {m['year']}?"
        spans = _spans_for(d, [f"{m['budget']:,.2f}"])
        same_project = [o for o in docs if o["family"] == "meeting"
                        and o["meta"]["project"] == m["project"]
                        and o["doc_id"] != d["doc_id"]]
        expected = {d["doc_id"]: GRADE_MUST}
        for o in same_project[:2]:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-temporal-{len(cases) + 1:03d}",
            slice="temporal", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[m["project"], f"{m['budget']:,.2f}"],
            expected_spans=spans,
        ))
    return cases[:15]


def build_multi_hop(docs: list[dict]) -> list[EvalCase]:
    """15 multi-hop cases: meeting decision -> invoice for the same project
    (the answer needs both docs), and project spec revision chains (v1 -> v3).
    Grade 2 on both hops' docs when both are required evidence."""
    cases = []
    meetings = [d for d in docs if d["family"] == "meeting"]
    invoices = [d for d in docs if d["family"] == "invoice"]
    n = 0
    for mtg in meetings:
        if n >= 10:
            break
        project = mtg["meta"]["project"]
        vendor = mtg["meta"]["vendor"]
        linked = [d for d in invoices if d["meta"]["project"] == project
                  and d["meta"]["vendor"] == vendor]
        if not linked:
            continue
        inv = linked[0]
        q = (f"Which invoice from {vendor} relates to the {project} budget "
             f"approved in decision {mtg['meta']['decision_no']}, and how much was it for?")
        spans = _spans_for(mtg, [mtg["meta"]["decision_no"]]) + \
            _spans_for(inv, [f"{inv['meta']['amount']:,.2f}"])
        cases.append(EvalCase(
            id=f"golden-multi-hop-{len(cases) + 1:03d}",
            slice="multi_hop", question=q,
            expected_docs={mtg["doc_id"]: GRADE_MUST, inv["doc_id"]: GRADE_MUST},
            answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[vendor, f"{inv['meta']['amount']:,.2f}"],
            expected_spans=spans,
        ))
        n += 1
    projects = {}
    for d in docs:
        if d["family"] == "project":
            projects.setdefault(d["meta"]["project"], []).append(d)
    for project, revs in projects.items():
        if n >= 15 or len(revs) < 3:
            continue
        revs_sorted = sorted(revs, key=lambda d: d["meta"]["revision"])
        v1, v3 = revs_sorted[0], revs_sorted[-1]
        q = (f"How did the {project} architecture spec change between "
             f"revision v1 and the approved revision?")
        spans = _spans_for(v1, ["draft"]) + _spans_for(v3, ["approved"])
        cases.append(EvalCase(
            id=f"golden-multi-hop-{len(cases) + 1:03d}",
            slice="multi_hop", question=q,
            expected_docs={v1["doc_id"]: GRADE_MUST, v3["doc_id"]: GRADE_MUST},
            answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[project, "draft", "approved"],
            expected_spans=spans,
        ))
        n += 1
    # a 3-hop chain: project spec v1 (draft) -> spec v3 (approved) plus the
    # meeting that covers the same project - evidence spans live in 3 docs
    for project, revs in projects.items():
        if n >= 15 or len(revs) < 3:
            continue
        revs_sorted = sorted(revs, key=lambda d: d["meta"]["revision"])
        v1, v3 = revs_sorted[0], revs_sorted[-1]
        mtg_for_project = [d for d in docs if d["family"] == "meeting"
                           and d["meta"]["project"] == project]
        if not mtg_for_project:
            continue
        mtg = mtg_for_project[0]
        q = (f"Who owned the {project} spec while it was drafted, and what "
             f"budget did the team later approve for it?")
        spans = (_spans_for(v1, [v1["meta"]["owner"]])
                 + _spans_for(mtg, [f"{mtg['meta']['budget']:,.2f}"]))
        cases.append(EvalCase(
            id=f"golden-multi-hop-{len(cases) + 1:03d}",
            slice="multi_hop", question=q,
            expected_docs={
                v1["doc_id"]: GRADE_MUST,
                mtg["doc_id"]: GRADE_MUST,
                v3["doc_id"]: GRADE_HELPFUL,   # confirms the approved state
            },
            answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[
                v1["meta"]["owner"], f"{mtg['meta']['budget']:,.2f}"],
            expected_spans=spans,
        ))
        n += 1
    return cases[:15]


def build_paraphrase(docs: list[dict]) -> list[EvalCase]:
    """15 paraphrase/alias cases: same doc, different surface form - the doc
    never contains the question's words (cost -> 'amount due', 'trip' vs
    'itinerary', full country name vs the doc's usage, 'latest' vs v3)."""
    cases = []
    invoices = [d for d in docs if d["family"] == "invoice"][:5]
    for d in invoices:
        m = d["meta"]
        # paraphrase: "charged" / "cost" never appear in the doc; it says
        # "Total amount due"
        q = (f"How much did {m['vendor']} charge us for work on {m['project']} "
             f"back in {m['month']} {m['year']}?")
        spans = _spans_for(d, [f"{m['amount']:,.2f}"])
        same_project = [o for o in docs if o["family"] == "invoice"
                        and o["meta"]["project"] == m["project"]
                        and o["doc_id"] != d["doc_id"]]
        expected = {d["doc_id"]: GRADE_MUST}
        for o in same_project[:2]:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-paraphrase-{len(cases) + 1:03d}",
            slice="paraphrase", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[f"{m['amount']:,.2f}"],
            expected_spans=spans,
        ))
    travel = [d for d in docs if d["family"] == "travel"][:5]
    for d in travel:
        m = d["meta"]
        # alias: 'holiday'/'getaway' vs 'itinerary'; country instead of city
        q = f"On which flight did I fly to {m['city']} for the {m['month']} trip?"
        spans = _spans_for(d, [m["flight"]])
        expected = {d["doc_id"]: GRADE_MUST}
        cases.append(EvalCase(
            id=f"golden-paraphrase-{len(cases) + 1:03d}",
            slice="paraphrase", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[m["flight"]],
            expected_spans=spans,
        ))
    meetings = [d for d in docs if d["family"] == "meeting"]
    seen_projects: set[str] = set()
    for d in meetings:
        if len(cases) >= 15:
            break
        m = d["meta"]
        if m["project"] in seen_projects:
            continue  # one spending-cap question per project (no dup questions)
        seen_projects.add(m["project"])
        # paraphrase: 'spending cap' vs 'approved budget'
        q = f"What spending cap did the team set for {m['project']} phase work?"
        spans = _spans_for(d, [f"{m['budget']:,.2f}"])
        expected = {d["doc_id"]: GRADE_MUST}
        cases.append(EvalCase(
            id=f"golden-paraphrase-{len(cases) + 1:03d}",
            slice="paraphrase", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[f"{m['budget']:,.2f}"],
            expected_spans=spans,
        ))
    # 'current/latest' alias: the question never names a revision number but
    # the answer lives in the LATEST revision (older revisions are negatives)
    latest_by_project: dict[str, dict] = {}
    for d in docs:
        if d["family"] == "project" and d["meta"]["is_latest"]:
            latest_by_project[d["meta"]["project"]] = d
    for project, latest in sorted(latest_by_project.items()):
        if len(cases) >= 15:
            break
        q = f"Who is the current technical lead on the {project} architecture spec?"
        spans = _spans_for(latest, [latest["meta"]["lead"]])
        older = [d for d in docs if d["family"] == "project"
                 and d["meta"]["project"] == project
                 and not d["meta"]["is_latest"]]
        expected = {latest["doc_id"]: GRADE_MUST}
        for o in older[:2]:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-paraphrase-{len(cases) + 1:03d}",
            slice="paraphrase", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[project, latest["meta"]["lead"]],
            expected_spans=spans,
        ))
    return cases[:15]


def build_hard_negative(docs: list[dict]) -> list[EvalCase]:
    """25 hard-negative cases: a near-duplicate exists that must NOT rank.

    Sources of near-duplicates: same vendor different year (near_dup family),
    same project different invoice, same vendor different year in the invoice
    family, same city different trip.
    """
    cases = []
    near_dups = [d for d in docs if d["family"] == "near_dup"]
    # pair structure: same pair index, tags a/b
    pairs: dict[int, dict[str, dict]] = {}
    for d in near_dups:
        pairs.setdefault(d["meta"]["pair"], {})[d["meta"]["tag"]] = d
    n = 0
    for pair_idx in sorted(pairs):
        if n >= 10 or len(pairs[pair_idx]) < 2:
            continue
        a, b = pairs[pair_idx]["a"], pairs[pair_idx]["b"]
        for target, distractor in ((a, b), (b, a)):
            tm = target["meta"]
            q = (f"What is the total amount due on invoice {tm['invoice_no']} "
                 f"from {tm['vendor']} dated {tm['year']}?")
            spans = _spans_for(target, [tm["invoice_no"]])
            cases.append(EvalCase(
                id=f"golden-hard-negative-{len(cases) + 1:03d}",
                slice="hard_negative", question=q,
                expected_docs={
                    target["doc_id"]: GRADE_MUST,
                    distractor["doc_id"]: GRADE_HARD_NEG,
                },
                answerable=True, expected_state=ANSWERED,
                expected_answer_facts=[tm["invoice_no"], f"{tm['amount']:,.2f}"],
                expected_spans=spans,
            ))
            n += 1
    # same project, different invoice: grade-2 target + 2 same-project grade-0
    invoices = [d for d in docs if d["family"] == "invoice"]
    by_project: dict[str, list[dict]] = {}
    for d in invoices:
        by_project.setdefault(d["meta"]["project"], []).append(d)
    for project, lst in sorted(by_project.items()):
        if n >= 25 or len(lst) < 3:
            continue
        target = lst[0]
        distractors = [o for o in lst if o["doc_id"] != target["doc_id"]][:2]
        tm = target["meta"]
        q = f"How much was invoice {tm['invoice_no']} for {tm['project']}?"
        spans = _spans_for(target, [f"{tm['amount']:,.2f}"])
        expected = {target["doc_id"]: GRADE_MUST}
        for o in distractors:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-hard-negative-{len(cases) + 1:03d}",
            slice="hard_negative", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[f"{tm['amount']:,.2f}"],
            expected_spans=spans,
        ))
        n += 1
    # same vendor, different year (invoice family): the target names the year;
    # same-vendor invoices from other years must not rank
    by_vendor: dict[str, list[dict]] = {}
    for d in invoices:
        by_vendor.setdefault(d["meta"]["vendor"], []).append(d)
    for vendor, lst in sorted(by_vendor.items()):
        if n >= 25 or len(lst) < 4:
            continue
        target = lst[0]
        same_vendor_other_year = [o for o in lst
                                  if o["meta"]["year"] != target["meta"]["year"]][:2]
        if not same_vendor_other_year:
            continue
        tm = target["meta"]
        q = (f"What was the total amount due on the {tm['month']} {tm['year']} "
             f"invoice from {vendor} for project {tm['project']}?")
        spans = _spans_for(target, [f"{tm['amount']:,.2f}"])
        expected = {target["doc_id"]: GRADE_MUST}
        for o in same_vendor_other_year:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-hard-negative-{len(cases) + 1:03d}",
            slice="hard_negative", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[vendor, f"{tm['amount']:,.2f}"],
            expected_spans=spans,
        ))
        n += 1
    # same city, different trip (travel family): wrong-trip docs are negatives
    travel_by_city: dict[str, list[dict]] = {}
    for d in docs:
        if d["family"] == "travel":
            travel_by_city.setdefault(d["meta"]["city"], []).append(d)
    for city, lst in sorted(travel_by_city.items()):
        if n >= 25:
            continue
        if len(lst) < 2:
            # single-visit city: a same-country OTHER city is the negative
            same_country = [o for o in docs if o["family"] == "travel"
                             and o["meta"]["country"] == lst[0]["meta"]["country"]
                             and o["doc_id"] != lst[0]["doc_id"]]
            if not same_country:
                continue
            target, distractors = lst[0], same_country[:2]
        else:
            target, distractors = lst[0], [o for o in lst[1:]][:2]
        tm = target["meta"]
        q = (f"Which booking reference covers my stay at the Grand {city} Hotel "
             f"in {tm['month']} {tm['year']}?")
        spans = _spans_for(target, [tm["conf"]])
        expected = {target["doc_id"]: GRADE_MUST}
        for o in distractors:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-hard-negative-{len(cases) + 1:03d}",
            slice="hard_negative", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[tm["conf"]],
            expected_spans=spans,
        ))
        n += 1
    return cases[:25]


def build_unanswerable(docs: list[dict]) -> list[EvalCase]:
    """15 unanswerable cases: every referenced entity is verified ABSENT from
    the whole corpus text; expected_state is INSUFFICIENT."""
    cases = []
    templates = []
    for v in MISSING_VENDORS:
        templates.append(f"How much did {v} invoice us for in 2025?")
    for p in MISSING_PROJECTS:
        templates.append(f"What is the approved budget for project {p}?")
    for inv in MISSING_INV_NOS[:4]:
        templates.append(f"Find invoice {inv}")
    for dec in MISSING_DECISIONS[:3]:
        templates.append(f"What was decided in decision {dec}?")
    for c in MISSING_CITIES[:2]:
        templates.append(f"When did I travel to {c}?")
    templates.append(f"Which flight was {MISSING_FLIGHTS[0]}?")
    templates.append(
        f"What did {MISSING_VENDORS[0]} charge for project {MISSING_PROJECTS[0]}?")
    for i, q in enumerate(templates[:15]):
        # Guard: the question must NOT be answerable by the corpus. Every
        # distinctive entity named in the question (vendor, project code,
        # invoice/decision number, city, flight) must match nothing in the
        # corpus - neither as a substring of a corpus entity, nor with a
        # corpus entity as a substring of the question (so 'Helios Design
        # Studio' disqualifies a question naming any real vendor).
        for entity in (MISSING_VENDORS + MISSING_PROJECTS + MISSING_INV_NOS
                       + MISSING_DECISIONS + MISSING_CITIES + MISSING_FLIGHTS):
            if entity not in q:
                continue
            for d in docs:
                m = d["meta"]
                for corpus_entity in (m.get("vendor", ""), m.get("project", ""),
                                      m.get("invoice_no", ""),
                                      m.get("decision_no", ""), m.get("city", ""),
                                      m.get("flight", "")):
                    if not corpus_entity:
                        continue
                    if (entity in corpus_entity or corpus_entity in entity
                            or corpus_entity in q):
                        raise ValueError(
                            f"unanswerable case leaks into corpus: {entity!r} "
                            f"in {q!r} collides with corpus entity "
                            f"{corpus_entity!r}")
        cases.append(EvalCase(
            id=f"golden-unanswerable-{i + 1:03d}",
            slice="unanswerable", question=q,
            expected_docs={}, answerable=False,
            expected_state=INSUFFICIENT,
            expected_answer_facts=[],
            expected_spans=[],
        ))
    return cases


def build_table(docs: list[dict]) -> list[EvalCase]:
    """15 table cases: row-level facts from the expenses sheet blocks - the
    TABLE route (numeric + vendor + project tokens)."""
    cases = []
    expenses = [d for d in docs if d["family"] == "expenses"]
    rows = []
    for d in expenses:
        for r in d["meta"]["rows"]:
            rows.append((d, r))
    # rows that share a vendor across blocks: pick vendor-discriminating ones
    by_vendor: dict[str, list[tuple[dict, dict]]] = {}
    for d, r in rows:
        by_vendor.setdefault(r["vendor"], []).append((d, r))
    picked: list[tuple[dict, dict, list[tuple[dict, dict]]]] = []
    for vendor, lst in sorted(by_vendor.items()):
        if len(lst) >= 2 and len(picked) < 8:
            # distractor rows must live in DIFFERENT blocks than the target
            # (same block = same doc_id would overwrite the grade-2 target)
            other_block = [x for x in lst[1:] if x[0]["doc_id"] != lst[0][0]["doc_id"]]
            if not other_block:
                continue
            picked.append((lst[0][0], lst[0][1], other_block[:2]))

    for i, (doc, row, distractors) in enumerate(picked):
        q = (f"How much did I spend on {row['vendor']} on {row['date']} "
             f"for {row['project']}?")
        needle = f"{row['vendor']} | {row['amount']:,.2f} USD | {row['project']}"
        spans = _spans_for(doc, [needle])
        expected = {doc["doc_id"]: GRADE_MUST}
        for odoc, _ in distractors:
            expected[odoc["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-table-{len(cases) + 1:03d}",
            slice="table", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[row["vendor"], f"{row['amount']:,.2f}"],
            expected_spans=spans,
        ))
    # remaining: single-row lookups keyed by (project, date) - unique rows the
    # other 8 cases do not already target; distractors are the OTHER blocks
    used_pairs = {(row["project"], row["date"]) for _d, row in
                  [(p[0], p[1]) for p in picked]}
    for doc, row in rows:
        if len(cases) >= 15:
            break
        if (row["project"], row["date"]) in used_pairs:
            continue
        other_blocks = [d for d in expenses if d["doc_id"] != doc["doc_id"]][:2]
        q = (f"What is the {row['project']} expense with {row['vendor']} "
             f"dated {row['date']} in the 2025 sheet?")
        needle = f"{row['date']} | {row['vendor']} | {row['amount']:,.2f} USD"
        spans = _spans_for(doc, [needle])
        expected = {doc["doc_id"]: GRADE_MUST}
        for o in other_blocks:
            expected[o["doc_id"]] = GRADE_HARD_NEG
        cases.append(EvalCase(
            id=f"golden-table-{len(cases) + 1:03d}",
            slice="table", question=q,
            expected_docs=expected, answerable=True, expected_state=ANSWERED,
            expected_answer_facts=[row["vendor"], f"{row['amount']:,.2f}"],
            expected_spans=spans,
        ))
        used_pairs.add((row["project"], row["date"]))
    return cases[:15]


def main() -> int:
    manifest = load_manifest(MANIFEST_PATH)
    docs = manifest["docs"]
    manifest_docs = {d["doc_id"]: d for d in docs}

    builders = [
        ("exact", build_exact),
        ("temporal", build_temporal),
        ("multi_hop", build_multi_hop),
        ("paraphrase", build_paraphrase),
        ("hard_negative", build_hard_negative),
        ("unanswerable", build_unanswerable),
        ("table", build_table),
    ]
    cases: list[EvalCase] = []
    for name, fn in builders:
        built = fn(docs)
        print(f"{name}: {len(built)}")
        cases.extend(built)

    validate_all(cases, manifest_docs)  # raises unless answerable + in-manifest
    # every question must be unique (the runner maps question -> case; and
    # no two golden cases may be the same query)
    questions = [c.question for c in cases]
    dupes = {q for q in questions if questions.count(q) > 1}
    if dupes:
        raise ValueError(f"duplicate case questions: {sorted(dupes)!r}")
    paths = save_cases(cases, OUT_DIR)
    print(f"wrote {len(paths)} case files to {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
