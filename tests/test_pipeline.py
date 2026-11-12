"""Pipeline unit tests: the parts that don't need an LLM — understand,
verify, context builder."""
from __future__ import annotations

from relay.pipeline import understand, verify, build_context_block
from relay.retrieval import Chunk


def test_understand_attention():
    u = understand("What is the dimension d_model of the base Transformer?")
    assert u.paper_guess == "attention"
    assert u.confidence > 0


def test_understand_moshi():
    u = understand("What frame rate does Mimi codec use?")
    assert u.paper_guess == "moshi"


def test_understand_unknown_question():
    u = understand("What is the weather today?")
    assert u.paper_guess is None
    assert u.confidence == 0.0


def _hits(papers: list[str]) -> list[tuple[Chunk, float]]:
    out = []
    for p in papers:
        out.append((Chunk(paper=p, section="1 X", start_page=1,
                          end_page=1, text="some text"), 1.0))
    return out


def test_verify_refusal_is_ok():
    v = verify("I cannot answer from the provided papers.", _hits(["attention"]))
    assert v.ok and v.is_refusal


def test_verify_missing_citation_fails():
    v = verify("The answer is 512.", _hits(["attention"]))
    assert not v.ok


def test_verify_valid_citation_passes():
    v = verify("d_model = 512 [attention, §3.1 Encoder, p.3].",
               _hits(["attention"]))
    assert v.ok


def test_verify_wrong_paper_fails():
    v = verify("d_model = 512 [moshi, §3.1 Something, p.1].",
               _hits(["attention"]))
    assert not v.ok
    assert "citation paper not in context" in v.reason


def test_build_context_respects_budget():
    hits = []
    for i in range(10):
        hits.append((Chunk(paper="attention", section="1 X",
                           start_page=i, end_page=i, text="x" * 500), 1.0))
    ctx = build_context_block(hits, budget_chars=1000)
    # Keeps us under ~2x budget allowing for headers; must not include all 10
    assert len(ctx) < 2500
