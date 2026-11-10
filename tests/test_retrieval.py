"""Small sanity tests for the retrieval module. These are hermetic:
they use tiny fake blocks, not the real papers."""
from __future__ import annotations

from data.parser import Block
from relay.retrieval import (
    chunk_blocks, Index, search, search_mmr, tokenize,
)


def _fake_blocks():
    # Each block is padded so the chunker's min-size filter (30 tokens)
    # does not throw everything away. Real paper blocks are much larger.
    pad = " filler " * 20
    return [
        Block(paper="p1", page=1, section="1 Intro",
              text="transformers use attention heads the base model has 8 heads" + pad),
        Block(paper="p1", page=2, section="2 Method",
              text="dropout rate 0.1 in training adam optimizer beta2 0.98" + pad),
        Block(paper="p2", page=1, section="1 Intro",
              text="mimi is an audio codec operating at 12.5 Hz frame rate" + pad),
        Block(paper="p2", page=2, section="2 Method",
              text="moshi uses a multi-stream transformer over audio and text" + pad),
    ]


def test_tokenize_keeps_numbers():
    toks = tokenize("the model has 8 heads and d_model 512")
    assert "8" in toks
    assert "512" in toks


def test_chunk_blocks_small_input():
    blocks = _fake_blocks()
    chunks = chunk_blocks(blocks, chunk_tokens=40, overlap=5)
    assert len(chunks) >= 1
    assert all(c.paper in {"p1", "p2"} for c in chunks)
    # section must be one that existed in the source
    sections = {b.section for b in blocks}
    assert all(c.section in sections for c in chunks)


def test_search_finds_relevant_chunk():
    blocks = _fake_blocks()
    chunks = chunk_blocks(blocks, chunk_tokens=40, overlap=5)
    idx = Index.build(chunks)
    hits = search(idx, "how many attention heads", top_k=2)
    assert hits, "expected at least one hit"
    top_text = hits[0][0].text.lower()
    assert "heads" in top_text


def test_mmr_returns_at_most_final_k():
    blocks = _fake_blocks()
    chunks = chunk_blocks(blocks, chunk_tokens=40, overlap=5)
    idx = Index.build(chunks)
    hits = search_mmr(idx, "attention heads", fetch_k=4, final_k=2)
    assert len(hits) <= 2


def test_citation_format():
    blocks = _fake_blocks()
    chunks = chunk_blocks(blocks, chunk_tokens=40, overlap=5)
    c = chunks[0]
    s = c.cite()
    assert s.startswith("[")
    assert c.paper in s
    assert "p." in s
