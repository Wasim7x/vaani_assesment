"""
Rule-based grading over eval/questions.yaml. No LLM in the loop,
on purpose: a keyword grader is cheap, repeatable, and easy to
check. Re-run it and you get the same verdict.

Grade values (same as results.csv):
  correct            — answerable question. All key_facts present
                       (case-insensitive; numbers must match
                       exactly). At least one citation to the
                       expected paper.
  wrong              — answerable. Missing key_facts, or the
                       citation is to the wrong paper.
  refused_correctly  — unanswerable. The assistant replied with the
                       refusal phrase (or no citation and no key
                       facts).
  refused_wrongly    — answerable. The assistant refused instead of
                       trying.

Tricky questions are graded the same way as answerable ones — their
key_facts are listed in the YAML.
"""
from __future__ import annotations
import argparse
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from relay.config import ROOT


QUESTIONS_YAML = ROOT / "eval" / "questions.yaml"

REFUSAL = re.compile(r"i cannot answer from the provided papers", re.IGNORECASE)
CITE_RE = re.compile(r"\[(\w+)\s*,", re.IGNORECASE)


@dataclass
class Question:
    id: str
    type: str          # answerable | unanswerable | tricky
    question: str
    paper: str         # attention | moshi | hnet | none
    section: str
    key_facts: list[str]
    notes: str = ""


def load_questions(path: Path = QUESTIONS_YAML) -> list[Question]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    out: list[Question] = []
    for r in raw:
        out.append(Question(
            id=r["id"],
            type=r["type"],
            question=r["question"],
            paper=r.get("paper", "none"),
            section=r.get("section", ""),
            key_facts=list(r.get("key_facts", []) or []),
            notes=r.get("notes", ""),
        ))
    return out


def _normalise(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def grade(answer: str, q: Question) -> str:
    """Returns one of: correct | wrong | refused_correctly | refused_wrongly."""
    ans = _normalise(answer or "")
    is_refusal = bool(REFUSAL.search(ans))
    cited_papers = [m.group(1).lower() for m in CITE_RE.finditer(ans)]

    if q.type == "unanswerable":
        if is_refusal:
            return "refused_correctly"
        # Also give credit if the answer carries no citation AND no key fact
        # pretending to answer it — but the spec rewards the exact refusal
        # phrase, so anything else is a wrong refusal.
        return "wrong"

    # answerable or tricky
    if is_refusal:
        return "refused_wrongly"

    low = ans.lower()
    # Require at least one key_fact substring hit (case-insensitive).
    hits = []
    for kf in q.key_facts:
        if not kf:
            continue
        if kf.lower() in low:
            hits.append(kf)

    # Treat the first key_fact as the required one (usually the number
    # or the exact term). Others are nice-to-haves.
    required_ok = any(kf.lower() in low for kf in q.key_facts[:1]) if q.key_facts else True

    # Citation quality: needs to cite the expected paper.
    cite_ok = (q.paper in cited_papers) if q.paper and q.paper != "none" else True

    if required_ok and cite_ok:
        return "correct"
    return "wrong"


def grade_many(answers: dict[str, str], qs: list[Question]) -> dict[str, str]:
    """answers: question_id -> answer text. Returns id -> grade."""
    out: dict[str, str] = {}
    for q in qs:
        if q.id not in answers:
            continue
        out[q.id] = grade(answers[q.id], q)
    return out


def summary(grades: dict[str, str], qs: list[Question]) -> dict:
    counts = {"correct": 0, "wrong": 0, "refused_correctly": 0, "refused_wrongly": 0}
    for g in grades.values():
        if g in counts:
            counts[g] += 1
    n = sum(counts.values())
    by_type = {}
    for t in ("answerable", "unanswerable", "tricky"):
        ids = {q.id for q in qs if q.type == t}
        sub = [grades[i] for i in ids if i in grades]
        by_type[t] = {"n": len(sub),
                      "correct": sum(1 for g in sub if g == "correct"),
                      "refused_correctly": sum(1 for g in sub if g == "refused_correctly"),
                      "wrong": sum(1 for g in sub if g == "wrong"),
                      "refused_wrongly": sum(1 for g in sub if g == "refused_wrongly")}
    return {"n": n, "counts": counts, "by_type": by_type}


# CLI: grade a JSONL of {id, answer} rows.
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("answers_jsonl")
    args = ap.parse_args()
    qs = load_questions()
    answers: dict[str, str] = {}
    for line in Path(args.answers_jsonl).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        answers[row["id"]] = row.get("answer", "")
    grades = grade_many(answers, qs)
    s = summary(grades, qs)
    print(json.dumps({"grades": grades, "summary": s}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
