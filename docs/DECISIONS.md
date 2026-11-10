# Decisions

Each design choice, the alternative, the measured evidence, and what it
cost. New entries go at the top.

---

## D-002 — Three retrieval settings: small/8, medium/4, medium/12→4 with MMR

- **Chose:** A = 150-token chunks, top-8, no rerank. B = 400-token
  chunks, top-4, no rerank. C = 400-token chunks, fetch-12 then
  MMR-dedupe to top-4.
- **Alternatives:** TF-IDF+cosine, a cross-encoder rerank, semantic
  chunking by section boundary only.
- **Evidence:** not measured yet (Phase 6). Hypothesis: A has coverage
  but wastes tokens; B is cheap but misses when the answer straddles a
  chunk; C should beat B at similar token cost.
- **Cost:** three retrieval runs over all questions; tokens per answer
  for B and C stay roughly equal.

## D-001 — Model: llama-3.1-8b-instant

- **Chose:** `llama-3.1-8b-instant` for every pipeline step.
- **Alternative:** `llama-3.3-70b-versatile` for drafting, 8b for
  Understand/Verify.
- **Evidence:** not yet measured; the brief rewards staying inside
  limits more than raw answer quality, and 8b's higher RPM/TPM budget
  on the free tier lets us run more scenarios.
- **Cost:** some tricky questions may be answered worse. We keep the
  single-model choice until Phase 6 shows it is the bottleneck.
