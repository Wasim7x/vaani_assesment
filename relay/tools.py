"""
Tools the pipeline can call. The brief asks for at least one tool
call; we expose two:

  list_sections(paper): return the section list parsed out of a PDF.
  page_text(paper, page): return the raw text of one page.

Both are local lookups over data/text/*.jsonl. They cost no API
calls, so the tool call stays cheap and the result is the same each
time.

The pipeline uses `list_sections` to narrow retrieval when Understand
is confident about the paper but not the section. It uses
`page_text` as a fallback when a candidate chunk is small or clipped.
"""
from __future__ import annotations
from functools import lru_cache
from typing import Iterable

from relay.config import PAPERS
from data.parser import load_blocks, Block


@lru_cache(maxsize=4)
def _blocks(paper: str) -> list[Block]:
    return load_blocks(paper)


def list_sections(paper: str) -> list[str]:
    """Distinct section titles for a paper, in document order."""
    if paper not in PAPERS:
        return []
    seen: set[str] = set()
    out: list[str] = []
    for b in _blocks(paper):
        if b.section not in seen:
            seen.add(b.section)
            out.append(b.section)
    return out


def page_text(paper: str, page: int) -> str:
    """Return all parsed text from a single page."""
    if paper not in PAPERS:
        return ""
    return " ".join(b.text for b in _blocks(paper) if b.page == page)


TOOLS = {
    "list_sections": list_sections,
    "page_text": page_text,
}
