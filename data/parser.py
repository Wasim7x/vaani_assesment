"""
Turn the three arXiv PDFs into blocks. Each block has a paper key,
a page number, a section, and a bit of text. We save them to
data/text/<paper>.jsonl, so retrieval does not need PyMuPDF.

How we find sections: we walk the lines. A numbered heading can be
split across two lines (PyMuPDF puts "3.1" on one line and "Encoder
and Decoder Stacks" on the next), so we peek at the next line when
we see a bare "3", "3.1", or "3.2.1". A short line like "Abstract"
or "Introduction" is also treated as a section. The current section
carries forward until the next heading.

This is a rough rule, but good enough to cite as
[paper, §section, p.N] so a reviewer can look it up in the PDF.
"""
from __future__ import annotations
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import pymupdf

from relay.config import PAPERS, DATA_PDFS, DATA_TEXT


HEADING_WORDS = {
    "abstract", "introduction", "related work", "background",
    "method", "methods", "approach", "model", "architecture",
    "experiments", "results", "evaluation", "ablation", "ablations",
    "discussion", "limitations", "conclusion", "conclusions",
    "references", "acknowledgements", "acknowledgments",
    "appendix", "appendices",
}

# Real section numbers in these 3 papers go at most 3 levels deep, each
# level is a small integer that starts with 1-9 (never 0). Keeping the
# pattern this tight stops table values like "0.156" or "23.75" from
# being mistaken for section numbers.
BARE_NUM = re.compile(r"^([1-9]\d?(?:\.[1-9]){0,3})\.?$")
NUM_WITH_TITLE = re.compile(r"^([1-9]\d?(?:\.[1-9]){0,3})\s+([A-Z][^\n]{4,100})$")
PAGE_NUM = re.compile(r"^\d{1,3}$")   # footer page numbers
# Known measurement units that look like heading titles but are not
# (e.g. the "Hz" in "12.5 Hz").
UNIT_LIKE = re.compile(
    r"^(Hz|MHz|GHz|kHz|ms|s|m|GB|MB|KB|B|dB|bit|bits|bytes?|"
    r"tokens?|layers?|params?|FLOPs|watts?)\b", re.IGNORECASE
)


@dataclass
class Block:
    paper: str
    page: int
    section: str
    text: str


def is_heading_word(line: str) -> str | None:
    s = line.strip().rstrip(".:")
    if 0 < len(s) <= 40 and s.lower() in HEADING_WORDS:
        return s.title()
    return None


def parse_paper(key: str) -> list[Block]:
    pdf = DATA_PDFS / f"{key}.pdf"
    doc = pymupdf.open(pdf)
    blocks: list[Block] = []
    current_section = "(front matter)"
    for pno, page in enumerate(doc, start=1):
        raw = page.get_text("text")
        lines = [ln.strip() for ln in raw.splitlines()]
        buf: list[str] = []

        def flush():
            if not buf:
                return
            text = " ".join(buf).strip()
            text = re.sub(r"\s+", " ", text)
            # join hyphenated line-breaks: "self- attention" -> "self-attention"
            text = re.sub(r"(\w)-\s+(\w)", r"\1\2", text)
            if text:
                blocks.append(Block(paper=key, page=pno,
                                    section=current_section, text=text))
            buf.clear()

        i = 0
        while i < len(lines):
            ln = lines[i]
            if not ln or PAGE_NUM.match(ln):
                i += 1
                continue

            # numbered heading split across two lines
            m = BARE_NUM.match(ln)
            if m and i + 1 < len(lines):
                nxt = lines[i + 1].strip()
                if (nxt and nxt[0].isupper() and 5 <= len(nxt) <= 100
                        and not UNIT_LIKE.match(nxt)):
                    flush()
                    current_section = f"{m.group(1)} {nxt}"
                    i += 2
                    continue

            # numbered heading on a single line
            m2 = NUM_WITH_TITLE.match(ln)
            if m2 and not UNIT_LIKE.match(m2.group(2)):
                flush()
                current_section = ln
                i += 1
                continue

            # keyword heading on its own line
            kw = is_heading_word(ln)
            if kw:
                flush()
                current_section = kw
                i += 1
                continue

            buf.append(ln)
            i += 1
        flush()
    doc.close()
    return blocks


def write_jsonl(blocks: list[Block], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8") as f:
        for b in blocks:
            f.write(json.dumps(asdict(b), ensure_ascii=False) + "\n")


def load_blocks(key: str) -> list[Block]:
    path = DATA_TEXT / f"{key}.jsonl"
    out: list[Block] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            out.append(Block(**d))
    return out


def main() -> int:
    for key in PAPERS:
        print(f"[parse] {key}")
        blocks = parse_paper(key)
        write_jsonl(blocks, DATA_TEXT / f"{key}.jsonl")
        n_pages = max((b.page for b in blocks), default=0)
        sections = sorted({b.section for b in blocks})
        print(f"        {len(blocks)} blocks, {n_pages} pages, {len(sections)} sections")
        for s in sections[:12]:
            print(f"          - {s}")
        if len(sections) > 12:
            print(f"          ... (+{len(sections)-12} more)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
