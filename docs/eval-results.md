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
| **7B: `qwen2.5:7b-instruct`, prompt v5** | `277b9220b4` | **1.000** | **1.000** | 0.964 | **1.000** | **0** | **1.0** | 44.1 |
| 7B, Ollama 0.12.3 → 0.35.1 + GPU tuning (2026-11-08) | `277b9220b4` | 1.000 | 1.000 | 0.964 | 1.000 | 0 | 1.0 | 49.3* |

The 7B row is the customer default model. It was measured on the same laptop with 13 of its 29
layers on a 4 GB NVIDIA T500 and the rest on the CPU (`DEV_GPU=1`). Treat its latency as a
worst case; a server GPU with 8 GB+ holds the whole model.

\* Run with the laptop short of memory (0.9–1.4 GB free), so its timing isn't comparable. On this
4 GB card the tuning doesn't change the 7B's speed (see "Latency"); quality is unchanged.

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
   - The remaining 3B miss (`it-setup-time`) is the citation-for-fact pattern.
   - **7B is clearly better.** It answered all 24 answerable cases correctly, including
     `it-setup-time`, and kept perfect abstention. Its one weakness is a single extra citation: on
     `carryover-days` it also cited an unrelated page next to a correct caveat about part-time
     staff. It also gives fuller answers; asked about mileage, it broke the rate down per vehicle
     from the spreadsheet.
   - The prompt was tuned on 3B and transfers to 7B without changes.
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

## Latency (2026-11-08, development laptop, NVIDIA T500 4 GB)

**Target: answers within 10 seconds.** Decision: keep the 7B model for quality and make an NVIDIA
GPU with 8 GB+ the requirement for that target (`docs/operations.md`, preflight warnings). A CPU or
a 4 GB GPU still works, slowly.

Measured with synthetic RAG-sized prompts (unique per run, so Ollama's prompt cache doesn't help),
model already loaded, `num_ctx` 8192:

| Model | Placement | Prompt tok/s | Output tok/s | Question (~1,400 tok): first word / total | Summary (~3,500 tok): total |
|---|---|---|---|---|---|
| `qwen2.5:7b-instruct` | 45% CPU / 55% GPU | 71 | 6 | 20 s / 30 s | ~2–3 min (one real summary: 2 min 45 s) |
| `qwen2.5:3b-instruct` | 100% GPU | 203 | 14 | 9 s / 16 s | 32 s |
| `qwen2.5:1.5b-instruct` | 100% GPU | 440 | 22 | 5 s / 8 s | 19 s |

- Compared with rag-claude (Weld AI) on the same laptop: its `phi4-mini` on Ollama 0.35.1 with
  flash attention and a 512 MiB card margin ran 91% on the GPU, 12.5 s per question and 25 s per
  summary. Our 7B with the same runtime and tuning: 56% GPU, 30 s and 51 s, i.e. no faster. The
  speed difference is the model fitting the card, not the settings. We adopted the runtime and
  tuning anyway (they matter once the model fits) and kept the 7B for quality.
- No model meets 10 s for summaries on this GPU. The 1.5B meets it for questions only, and it
  has not been through the answer-quality eval.
- Prompt processing dominates: summaries send several times more context than questions.
- Not yet measured: the 7B entirely on an 8 GB+ GPU. Measure it on the first customer-class GPU
  and record it here, for both a question and a summary.

## Before changing prompts or retrieval

1. Run `bash scripts/eval.sh --detach --label <change>` and compare with the last shipped run.
2. Add a case to `cases.json` for any failure found by hand. This file grows; it never shrinks.
3. Record the run in the table above with its `prompt_version`.
