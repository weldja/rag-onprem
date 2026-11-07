# Evaluation results

The harness lives in `backend/evals/` (corpus, `cases.json`, runner). To run it:

```bash
bash scripts/eval.sh --detach --label NAME   # ~15 min on CPU; report in eval-results/
bash scripts/retrieval-probe.sh              # retrieval + rerank scores only, no LLM, seconds
```

**Corpus:** six fictional company documents, one per supported format (PDF, DOCX, XLSX, PPTX, HTML, MD).

**Cases:** 28 in total:
- 22 single-turn questions with known source and facts, some with two valid sources
- 2 follow-ups that need conversation history
- 4 unanswerable questions, including a near miss ("paternity leave" next to the real annual-leave page)

The eval user sees only the eval corpus.

## Metrics

| Metric | Meaning |
|---|---|
| retrieval recall | an expected source was among the chunks given to the LLM |
| citation recall | the answer cited an expected source |
| citation precision | share of all citations that point at an expected source |
| answer accuracy | every expected fact appears in the answer |
| false not-found | an answerable question was answered "not found" |
| abstention | an unanswerable question was correctly answered "not found" |

## Results (2026-11-07, `qwen2.5:3b-instruct`, `bge-m3`, CPU, development laptop)

| Run | Prompt | Retrieval recall | Citation recall | Citation precision | Answer accuracy | False not-found | Abstention | Median s |
|---|---|---|---|---|---|---|---|---|
| baseline | `15bd5455d3` | 1.000 | 0.957 | 0.923 | 0.913 | 0 | 1.0 | 27.9 |
| prompt v2 (with an example sentence) | `723625a88f` | 1.000 | 0.957 | 0.816 | 0.957 | 0 | 1.0 | 28.3 |
| prompt v3 | `294f6402d2` | 1.000 | 1.000 | 0.893 | 0.957 | 0 | 1.0 | 29.3 |
| v3 + reranker MiniLM-L6 | `294f6402d2` | 1.000 | 1.000 | 0.862 | 0.957 | 0 | 1.0 | 29.9 |
| **prompt v5 (shipped)** | `277b9220b4` | **1.000** | **1.000** | **1.000** | **0.958** | **0** | **1.0** | 29.0 |

The v5 run includes one more case (`carryover-days`), added after an end-to-end test exposed a
regression that v3 caused on that phrasing. The v2 run was also affected by a duplicated corpus
(see "Findings", point 3).

## Findings

1. **Retrieval isn't the bottleneck.** Hybrid search finds an expected source for every answerable
   case, and the similarity gate plus the LLM's own refusal handled all unanswerable cases.
2. **The 3B model is sensitive to prompt wording.**
   - It sometimes writes a citation in place of the fact, as in "IT setup is at [1]."
   - It copies concrete examples from the prompt into unrelated answers (v2), so prompts must not
     contain example sentences.
   - It is pulled toward the quoted refusal sentence. v3 refused an answerable question, and even
     the baseline once wrote "I could find that in your documents". v5 moved the refusal rule to
     the end and limited it to "no excerpt relates to the question at all".
   - The remaining v5 miss (`it-setup-time`) is the citation-for-fact pattern.
   - Customers run 7B by default, which should be less fragile, but that hasn't been measured on
     this machine for lack of memory.
3. **Duplicate documents waste context.** Office files embed timestamps, so a re-saved copy uploads
   as a new document with identical text. Retrieval now drops chunks whose text duplicates one
   already selected.
4. **The reranker (`Xenova/ms-marco-MiniLM-L-6-v2`) is not enabled by default.**
   - It separates relevant from irrelevant prose far better than cosine similarity: relevant
     chunks mostly score +2 to +9, irrelevant ones −3 to −11.
   - It misjudges spreadsheet rows. The correct "Dinner | 30" chunk scored −9.4, below an
     irrelevant page.
   - Any `RERANK_MIN_SCORE` that removes over-cited chunks would therefore drop that answer.
     Without a threshold the reranker only reorders, which didn't change any metric.
   - It's available as an option (`RERANKER_MODEL`). Worth re-testing with a table-aware or
     multilingual cross-encoder, if memory allows.

## Before changing prompts or retrieval

1. Run `bash scripts/eval.sh --detach --label <change>` and compare with the last shipped run.
2. Add a case to `cases.json` for any failure found by hand. This file grows; it never shrinks.
3. Record the run in the table above with its `prompt_version`.
