"""
BM25 retrieval over the three papers. Three settings:
  A = 150-token chunks, top-8, no rerank
  B = 400-token chunks, top-4, no rerank
  C = 400-token chunks, over-fetch 12 then MMR-dedupe down to 4

Chunks carry (paper, section, start_page, end_page, text) so every
retrieved passage can be cited as [paper, §section, p.N].

Notes on the rerank in C:
  MMR = maximal marginal relevance. We score each candidate by
    score = normalised_bm25 - alpha * max_jaccard_with_already_picked
  and pick greedily. alpha = 0.5 is the usual default; it means a
  chunk that duplicates one we already have must beat it by > 50% of
  the BM25 range to be picked. Jaccard is on the lowercased word set
  (stopwords left in on purpose — "the" and "of" are shared by all
  chunks so they do not distinguish, and BM25 already discounts them).
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Iterable

from rank_bm25 import BM25Okapi

from relay.config import RETRIEVAL_SETTINGS, PAPERS
from data.parser import load_blocks, Block


@dataclass
class Chunk:
    paper: str
    section: str
    start_page: int
    end_page: int
    text: str

    def cite(self) -> str:
        pages = (f"p.{self.start_page}" if self.start_page == self.end_page
                 else f"pp.{self.start_page}-{self.end_page}")
        # trim section prefix like "3.2.1 " to keep citation short
        return f"[{self.paper}, §{self.section}, {pages}]"


# Keep numbers. BM25 needs "512" to retrieve "d_model = 512", and the
# chunk text shown to the LLM must not drop them either.
WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9\-\.]*")


def tokenize(text: str) -> list[str]:
    toks = []
    for t in WORD_RE.findall(text):
        t = t.lower().strip(".-")
        if t:
            toks.append(t)
    return toks


def chunk_blocks(blocks: list[Block], chunk_tokens: int, overlap: int) -> list[Chunk]:
    """Slide a window of ~N whitespace-tokens over each paper's text, with
    section + start page taken from the token the window began at. Keep
    the original text (numbers, punctuation) in each chunk."""
    chunks: list[Chunk] = []
    by_paper: dict[str, list[Block]] = {}
    for b in blocks:
        by_paper.setdefault(b.paper, []).append(b)

    for paper, bs in by_paper.items():
        # list of (raw_token, section, page). raw_token keeps the original
        # casing and punctuation so the chunk reads naturally.
        flat: list[tuple[str, str, int]] = []
        for b in bs:
            for t in b.text.split():
                flat.append((t, b.section, b.page))

        step = max(1, chunk_tokens - overlap)
        i = 0
        while i < len(flat):
            window = flat[i: i + chunk_tokens]
            if len(window) < max(30, chunk_tokens // 4):
                break
            text = " ".join(w[0] for w in window)
            section = window[0][1]
            start_page = window[0][2]
            end_page = window[-1][2]
            chunks.append(Chunk(paper=paper, section=section,
                                start_page=start_page, end_page=end_page,
                                text=text))
            i += step
    return chunks


@dataclass
class Index:
    chunks: list[Chunk]
    bm25: BM25Okapi = field(repr=False)

    @classmethod
    def build(cls, chunks: list[Chunk]) -> "Index":
        corpus = [tokenize(c.text) for c in chunks]
        return cls(chunks=chunks, bm25=BM25Okapi(corpus))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    return inter / union if union else 0.0


def search(index: Index, query: str, top_k: int) -> list[tuple[Chunk, float]]:
    scores = index.bm25.get_scores(tokenize(query))
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    return [(index.chunks[i], float(scores[i])) for i in order[:top_k]]


def search_mmr(index: Index, query: str, fetch_k: int, final_k: int,
               alpha: float = 0.5) -> list[tuple[Chunk, float]]:
    pool = search(index, query, fetch_k)
    if not pool:
        return []
    max_score = max(s for _, s in pool) or 1.0
    picked: list[tuple[Chunk, float]] = []
    picked_tokens: list[set[str]] = []
    remaining = pool[:]
    while remaining and len(picked) < final_k:
        best_i, best_val = -1, -1e9
        for idx, (c, s) in enumerate(remaining):
            norm = s / max_score
            toks = set(tokenize(c.text))
            penalty = max((_jaccard(toks, pt) for pt in picked_tokens), default=0.0)
            v = norm - alpha * penalty
            if v > best_val:
                best_val, best_i = v, idx
        c, s = remaining.pop(best_i)
        picked.append((c, s))
        picked_tokens.append(set(tokenize(c.text)))
    return picked


def retrieve(index: Index, query: str, setting: str) -> list[tuple[Chunk, float]]:
    cfg = RETRIEVAL_SETTINGS[setting]
    if cfg["rerank"]:
        return search_mmr(index, query, cfg["fetch_k"], cfg["final_k"])
    return search(index, query, cfg["final_k"])


def build_index_for_setting(setting: str) -> Index:
    cfg = RETRIEVAL_SETTINGS[setting]
    all_blocks: list[Block] = []
    for key in PAPERS:
        all_blocks.extend(load_blocks(key))
    chunks = chunk_blocks(all_blocks, cfg["chunk_tokens"], cfg["overlap"])
    return Index.build(chunks)
