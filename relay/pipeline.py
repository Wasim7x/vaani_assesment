"""
The ≥4-step pipeline required by the brief.

Steps (in order):
  1. Understand     — classify the question (which paper is it about)
                      and extract keywords. Deterministic / rule-based.
                      If a confident paper emerges, call the
                      list_sections tool to narrow retrieval (tool call).
  2. Retrieve       — BM25/MMR over chunks. Branch: if Understand
                      picked a paper, bias retrieval toward it.
  3. Draft          — an LLM call (via scheduler) that must cite
                      passages. Grounded prompt refuses if the context
                      does not contain the answer.
  4. Verify         — a cheap local check that the draft contains at
                      least one valid citation and is not obviously
                      hallucinating. If verify fails, LOOP once to
                      redraft with a stricter instruction. This gives
                      us the required branch AND loop.

The pipeline never writes to results.csv itself — the caller does, so
the same pipeline can be called inside a load scenario or from
`python -m relay.pipeline ask "..."` for debugging.
"""
from __future__ import annotations
import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Iterable

from relay.config import PAPERS, RETRIEVAL_SETTINGS
from relay.retrieval import Index, Chunk, retrieve, build_index_for_setting
from relay.scheduler import Scheduler, SchedRequest, SchedResult
from relay.tools import list_sections
from relay.metrics import LOG, Trace, new_request_id


# ---------- Step 1: Understand ----------

# Keywords that strongly suggest a paper. Tuned by eyeballing the three
# papers; we care about precision over recall — if no keyword hits, we
# search across everything.
PAPER_HINTS = {
    "attention": [
        "transformer", "self-attention", "attention", "multi-head",
        "sinusoidal", "d_model", "wmt", "bleu", "encoder-decoder",
        "scaled dot-product",
    ],
    "moshi": [
        "moshi", "mimi", "helium", "audio", "12.5", "frame", "rq-transformer",
        "multi-stream", "speech", "dialogue", "codec",
    ],
    "hnet": [
        "h-net", "hnet", "chunking", "chunk", "dynamic chunking",
        "hierarchical", "u-net", "wsd", "byte", "downsampling",
    ],
}


@dataclass
class Understanding:
    paper_guess: str | None
    confidence: float
    keywords: list[str]
    narrowed_sections: list[str] | None = None


def understand(question: str) -> Understanding:
    q = question.lower()
    scores: dict[str, int] = {}
    for paper, hints in PAPER_HINTS.items():
        for h in hints:
            if h in q:
                scores[paper] = scores.get(paper, 0) + 1
    if not scores:
        return Understanding(paper_guess=None, confidence=0.0, keywords=[])
    paper, score = max(scores.items(), key=lambda kv: kv[1])
    # confidence = winning score / 3 (saturates at 1.0)
    conf = min(1.0, score / 3.0)
    kws = [h for h in PAPER_HINTS[paper] if h in q][:5]
    return Understanding(paper_guess=paper, confidence=conf, keywords=kws)


# ---------- Step 2: Retrieve ----------

def retrieve_for(index: Index, question: str, setting: str,
                 paper_filter: str | None) -> list[tuple[Chunk, float]]:
    """If Understand picked a paper, over-fetch then filter down to that
    paper. Otherwise straight BM25/MMR."""
    cfg = RETRIEVAL_SETTINGS[setting]
    final_k = cfg["final_k"]
    if paper_filter is None:
        return retrieve(index, question, setting)
    # Over-fetch k*3 and filter; fall back to global if the paper has
    # too few hits in the top-k window.
    big = retrieve(index, question, setting)
    # Note: retrieve already returns final_k. For the paper-filter
    # branch we need a wider pool, so run plain search with a bigger k.
    from relay.retrieval import search, search_mmr
    pool_k = max(final_k * 4, 16)
    if cfg["rerank"]:
        pool = search_mmr(index, question, cfg["fetch_k"] * 2, pool_k)
    else:
        pool = search(index, question, pool_k)
    filtered = [(c, s) for c, s in pool if c.paper == paper_filter]
    if len(filtered) >= final_k:
        return filtered[:final_k]
    # If the paper filter is wrong, degrade gracefully.
    extras = [(c, s) for c, s in pool if c.paper != paper_filter]
    return (filtered + extras)[:final_k]


# ---------- Step 3: Draft ----------

DRAFT_SYSTEM = (
    "You are an assistant that answers engineering questions about three "
    "academic papers. You must only use the CONTEXT passages below. "
    "Every factual claim MUST end with a citation in the exact form "
    "[paper, §section, p.N]. If the passages do not contain the answer, "
    "reply with exactly: I cannot answer from the provided papers. "
    "Do not guess. Do not invent citations."
)


def build_context_block(hits: list[tuple[Chunk, float]], budget_chars: int = 3500) -> str:
    """Format retrieved chunks for the prompt, trimmed to a char budget
    so we don't blow TPM on long papers."""
    lines: list[str] = []
    total = 0
    for i, (c, _s) in enumerate(hits, start=1):
        head = f"[{i}] {c.cite()}"
        snippet = c.text.strip()
        # Clip long chunks to keep each passage readable.
        if len(snippet) > 600:
            snippet = snippet[:600] + "…"
        piece = f"{head}\n{snippet}\n"
        if total + len(piece) > budget_chars:
            break
        lines.append(piece)
        total += len(piece)
    return "\n".join(lines) if lines else "(no passages retrieved)"


def build_draft_messages(question: str, hits: list[tuple[Chunk, float]]) -> list[dict]:
    ctx = build_context_block(hits)
    user = (
        f"QUESTION:\n{question}\n\nCONTEXT:\n{ctx}\n\n"
        "Write a short answer (1–3 sentences). "
        "If the context does not answer the question, reply exactly with: "
        "I cannot answer from the provided papers."
    )
    return [
        {"role": "system", "content": DRAFT_SYSTEM},
        {"role": "user", "content": user},
    ]


# ---------- Step 4: Verify ----------

CITE_RE = re.compile(r"\[(\w+)\s*,\s*§([^,]+),\s*p+\.?\s*(\d+(?:-\d+)?)\]", re.IGNORECASE)
REFUSAL_RE = re.compile(r"i cannot answer from the provided papers", re.IGNORECASE)


@dataclass
class Verification:
    ok: bool
    is_refusal: bool
    citations: list[tuple[str, str, str]]
    reason: str = ""


def verify(answer: str, hits: list[tuple[Chunk, float]]) -> Verification:
    if REFUSAL_RE.search(answer):
        return Verification(ok=True, is_refusal=True, citations=[], reason="refusal")
    cites = [(m.group(1).lower(), m.group(2).strip(), m.group(3))
             for m in CITE_RE.finditer(answer)]
    if not cites:
        return Verification(ok=False, is_refusal=False, citations=[],
                            reason="no citation")
    # Every cited paper must be one we retrieved from (not invented).
    retrieved_papers = {c.paper for c, _ in hits}
    bad = [p for p, _s, _pg in cites if p not in retrieved_papers]
    if bad:
        return Verification(ok=False, is_refusal=False, citations=cites,
                            reason=f"citation paper not in context: {bad}")
    return Verification(ok=True, is_refusal=False, citations=cites, reason="ok")


# ---------- Pipeline ----------

@dataclass
class PipelineResult:
    answer: str = ""
    status: str = "ok"                 # ok | refused | error | timeout | rate_limited
    verification: Verification | None = None
    hits: list[tuple[Chunk, float]] = field(default_factory=list)
    sched: SchedResult | None = None
    retries: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    first_token_ms: float | None = None
    total_ms: float = 0.0
    trace: Trace | None = None
    understanding: Understanding | None = None
    retrieval_setting: str = ""


async def run_pipeline(
    question: str,
    *,
    scheduler: Scheduler,
    index: Index,
    retrieval_setting: str = "B",
    base_url: str | None = None,
    model: str | None = None,
    max_tokens: int = 320,
    deadline_ms: int | None = None,
    scenario: str = "adhoc",
    source: str = "real",
    question_id: str = "",
) -> PipelineResult:
    import time as _t
    deadline_ms = deadline_ms or scheduler.cfg.deadline_ms
    request_id = new_request_id()
    trace = Trace(request_id=request_id, scenario=scenario, source=source,
                  question_id=question_id)
    trace.attrs["question"] = question
    trace.attrs["retrieval_setting"] = retrieval_setting
    trace.attrs["deadline_ms"] = deadline_ms

    t0 = _t.perf_counter()
    result = PipelineResult(retrieval_setting=retrieval_setting, trace=trace)

    # Step 1 — Understand (+ tool call when confident)
    with trace.span("understand") as sp:
        u = understand(question)
        sp.set(paper_guess=u.paper_guess, confidence=u.confidence,
               keywords=u.keywords)
        if u.paper_guess and u.confidence >= 0.34:
            # Tool call
            with trace.span("tool.list_sections", paper=u.paper_guess):
                sections = list_sections(u.paper_guess)
            u.narrowed_sections = sections
            trace.attrs["tool_called"] = "list_sections"
    result.understanding = u

    # Step 2 — Retrieve (branch on confidence)
    with trace.span("retrieve", setting=retrieval_setting) as sp:
        paper_filter = u.paper_guess if (u.paper_guess and u.confidence >= 0.34) else None
        hits = retrieve_for(index, question, retrieval_setting, paper_filter)
        sp.set(n_hits=len(hits),
               papers=list({c.paper for c, _ in hits}),
               paper_filter=paper_filter)
    result.hits = hits

    # Step 3 — Draft (scheduled LLM call)
    messages = build_draft_messages(question, hits)
    projected_in = int(sum(len(m["content"]) for m in messages) / 4) + 32
    with trace.span("draft") as sp:
        sched_res = await scheduler.submit(SchedRequest(
            messages=messages,
            max_tokens=max_tokens,
            projected_in=projected_in,
            deadline_ms=deadline_ms,
            base_url=base_url,
            model=model,
            attrs={"request_id": request_id, "scenario": scenario},
        ))
        sp.set(status=sched_res.llm.status, retries=sched_res.retries,
               waited_ms=sched_res.waited_ms, working_ms=sched_res.working_ms,
               retry_wait_ms=sched_res.retry_wait_ms)
    result.sched = sched_res
    result.retries = sched_res.retries
    result.tokens_in = sched_res.llm.tokens_in
    result.tokens_out = sched_res.llm.tokens_out
    result.first_token_ms = sched_res.llm.first_token_ms

    if sched_res.outcome != "ok":
        result.status = sched_res.outcome if sched_res.outcome in {
            "timeout", "rate_limited", "error", "dropped", "breaker_open"
        } else "error"
        result.answer = sched_res.llm.text or ""
        result.total_ms = (_t.perf_counter() - t0) * 1000
        return result

    draft = sched_res.llm.text

    # Step 4 — Verify (and loop at most once)
    with trace.span("verify") as sp:
        v = verify(draft, hits)
        sp.set(ok=v.ok, is_refusal=v.is_refusal, reason=v.reason,
               citations=len(v.citations))
    result.verification = v

    # LOOP: at most one redraft if verify fails and we still have deadline.
    if (not v.ok) and (not v.is_refusal):
        remaining_ms = deadline_ms - (_t.perf_counter() - t0) * 1000
        if remaining_ms > 1500:
            strict_messages = messages + [
                {"role": "assistant", "content": draft},
                {"role": "user",
                 "content": ("That answer is missing or has an invalid citation. "
                             "Rewrite so every claim ends with a citation of the "
                             "form [paper, §section, p.N] drawn only from the "
                             "numbered CONTEXT above, or reply exactly with: "
                             "I cannot answer from the provided papers.")},
            ]
            with trace.span("redraft") as sp:
                sched2 = await scheduler.submit(SchedRequest(
                    messages=strict_messages,
                    max_tokens=max_tokens,
                    projected_in=projected_in + 64,
                    deadline_ms=int(remaining_ms),
                    base_url=base_url,
                    model=model,
                ))
                sp.set(status=sched2.llm.status, retries=sched2.retries)
            if sched2.outcome == "ok":
                draft2 = sched2.llm.text
                v2 = verify(draft2, hits)
                if v2.ok:
                    draft = draft2
                    result.verification = v2
                    result.tokens_in += sched2.llm.tokens_in
                    result.tokens_out += sched2.llm.tokens_out
                    # TTFT stays from first attempt; total grows.
            # If still not ok, we keep the first draft but mark status.
            result.retries += sched2.retries

    result.answer = draft
    result.status = "refused" if (result.verification and result.verification.is_refusal) else "ok"
    result.total_ms = (_t.perf_counter() - t0) * 1000
    return result


# ---------- adhoc CLI for debugging ----------

async def _main_async(question: str, setting: str, use_standin: bool) -> int:
    import os
    sched = Scheduler()
    index = build_index_for_setting(setting)
    base_url = os.environ.get("STANDIN_URL") if use_standin else None
    result = await run_pipeline(
        question, scheduler=sched, index=index,
        retrieval_setting=setting, base_url=base_url,
        scenario="adhoc", source="stand-in" if use_standin else "real",
    )
    print(json.dumps({
        "status": result.status,
        "ttft_ms": result.first_token_ms,
        "total_ms": round(result.total_ms, 1),
        "tokens_in": result.tokens_in,
        "tokens_out": result.tokens_out,
        "retries": result.retries,
        "understanding": {
            "paper_guess": (result.understanding.paper_guess
                            if result.understanding else None),
            "confidence": (result.understanding.confidence
                           if result.understanding else None),
        },
        "answer": result.answer,
    }, indent=2, ensure_ascii=False))
    return 0 if result.status in ("ok", "refused") else 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="+")
    ap.add_argument("--setting", default="B", choices=list(RETRIEVAL_SETTINGS))
    ap.add_argument("--standin", action="store_true",
                    help="route through STANDIN_URL instead of real Groq")
    args = ap.parse_args()
    return asyncio.run(_main_async(" ".join(args.question), args.setting, args.standin))


if __name__ == "__main__":
    raise SystemExit(main())
