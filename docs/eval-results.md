# Evaluation results

The harness lives in `backend/evals/` (corpus, `cases.json`, runner). To run it:

```bash
bash scripts/eval.sh --detach --label NAME   # ~15 min on CPU; report in eval-results/
bash scripts/retrieval-probe.sh              # retrieval + rerank scores only, no LLM, seconds
```

**Corpus:** six fictional company documents, one per supported format (PDF, DOCX, XLSX, PPTX, HTML, MD).

**Cases:** 34 in total:
- 22 single-turn questions with known source and facts, some with two valid sources
- 2 follow-ups that need conversation history
- 4 unanswerable questions, including a near miss ("paternity leave" next to the real annual-leave page)
- 4 summaries (`summary_of`), one of them typed as an ordinary question
- 2 questions about the collection itself (`library`), answered from the database

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
| `phi4-mini`, small-GPU settings† | `277b9220b4` | 1.000 | 0.875 | 0.958 | 1.000 | 0 | 0.75 | 17.7 |
| `phi4-mini`, small-GPU settings† + `EMBED_NUM_GPU=0` | `277b9220b4` | 1.000 | 0.917 | 0.960 | 1.000 | 0 | 0.75 | 4.3 |
| 7B, small-GPU settings† + `EMBED_NUM_GPU=0` | `277b9220b4` | 1.000 | 1.000 | 0.964 | 1.000 | 0 | 1.0 | 9.9 |
| **`qwen3:4b-instruct-2507-q4_K_M`, small-GPU settings† + `EMBED_NUM_GPU=0` (small-GPU bundle)** | `277b9220b4` | **1.000** | **1.000** | 0.963 | **1.000** | **0** | **1.0** | **5.4** |
| Qwen3 4B, main on 2026-11-12 (baseline for the next row) | `277b9220b4` | 1.000 | 1.000 | 0.963 | 1.000 | 0 | 1.0 | 4.9 |
| **Qwen3 4B, `<excerpt>` tags + collection rule (2026-11-12)** | `89d969a6d5` | **1.000** | **1.000** | 0.929 | **1.000** | **0** | **1.0** | **4.9** |
| **7B, `<excerpt>` tags + collection rule, small-GPU settings†** | `89d969a6d5` | **1.000** | **1.000** | **1.000** | **1.000** | **0** | **1.0** | 11.4 |
| Qwen3 4B, laptop install, `OLLAMA_KV_CACHE_TYPE=q8_0`, streamed (2026-11-13)‡ | `89d969a6d5` | 1.000 | 1.000 | 0.929 | 1.000 | 0 | 1.0 | 4.9 (first word 3.7) |

‡ Asked over `/api/ask/stream` as the UI does, so the run also records time to the first word
(`first_word_seconds`, `median_first_word_seconds`). Identical to the 2026-11-12 row on every
metric; summaries 4/4 accurate, 3/4 cited, median 16 s. The dev `.env` already had q8_0 then,
so this mostly confirms that the earlier runs used it. The eval corpus has short chunks
(~200 characters), so its prompts are small: 3–5 s to the first word. The real documents show
the actual latency (see "Real documents").

Summary cases (`summary_of` in `cases.json`, added 2026-11-08): Qwen3 4B cited and accurate 2/2,
median 22 s. In an earlier hand check one of its two summaries was accurate but uncited, so summary
citations vary a little between runs; the eval now tracks `summary_cited`.

`<excerpt>` tags + collection rule (2026-11-12, the "6 documents indexed" fix; see "Real
documents"):
- **Questions:** unchanged on both models. Qwen3 4B adds a note citing the handbook's mileage
  page next to the spreadsheet answer (`motorcycle-mileage`), hence 0.929. The 7B's precision
  rose to 1.000.
- **Summaries:** accurate on both prompts. Citations vary between runs:
  - Qwen3 4B cited 7 of 10 summaries with the new tags, against 3 of 4 on main.
  - The 7B cited 2 of 4 with either prompt, on different cases each time.
  - The 7B's one "inaccurate" summary wrote "March 31st" for "31 March" (on main too); the case
    now accepts both.
  - Summary medians: Qwen3 4B 16 s, 7B 57–59 s.
- **Collection questions:** `library_accuracy` 2/2 on both models, with no model call.

† `LLM_NUM_CTX=4096`, `LLM_MAX_TOKENS=768`, `SUMMARY_CHUNKS=6`, as `install.sh` sets on a GPU under
6 GB. `phi4-mini` is faster but leaves some answers uncited and answered one question the documents
don't cover, so it isn't shipped. The 7B's speed-up came from `EMBED_NUM_GPU=0` (see "Latency").

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
- **The real cause (found later the same day, from the per-answer timing log):** the card can't
  hold the LLM and `bge-m3` together, so Ollama evicted one to load the other on *every*
  question: 4–11 s of search time plus reloading the LLM. rag-claude never hit this because it
  embeds on the CPU. `EMBED_NUM_GPU=0` keeps `bge-m3` on the CPU (a question embeds in ~0.1 s)
  and the LLM stays loaded. With it, plus `LLM_NUM_CTX=4096`, `LLM_MAX_TOKENS=768` and
  `SUMMARY_CHUNKS=6` (summaries read 6 excerpts, as rag-claude does on-prem), measured in the eval
  on this laptop:

  | Model | Median per question | Summary (2 docs) | Quality |
  |---|---|---|---|
  | `qwen2.5:7b-instruct` | **9.9 s** (max 25.8 s, the first) | 33–36 s, cited | unchanged (all 1.000) |
  | `phi4-mini` | 4.3 s | 18–24 s, one uncited | citation recall 0.917, abstention 0.75 |

  `install.sh` applies these settings on a GPU under 6 GB. The 7B now meets 10 s (median) for
  questions on a 4 GB card; summaries still take ~35 s there.
- **Small-GPU bundle: `qwen3:4b-instruct-2507` (Apache 2.0).** It fits a 4 GB card (90% GPU) and
  matched the 7B on every eval metric: 5.4 s median per question, 22 s per summary. Built with
  `RELEASE_LLM_MODEL=qwen3:4b-instruct-2507-q4_K_M`. The 7B stays the default for 8 GB+ GPUs.
  `qwen2.5:3b` was not considered: its licence doesn't allow commercial use.
- **`OLLAMA_KV_CACHE_TYPE=q8_0` on the 4 GB card (2026-11-09).** With f16 the Qwen3 4B's
  context memory (576 MiB at `num_ctx` 4096) left 4 of its 37 layers on the CPU (`ollama ps`:
  10%/90% CPU/GPU). q8_0 halves it to 306 MiB and the whole model fits ("100% GPU"). Synthetic
  RAG prompt (~1,250 tokens, 6 excerpts), model loaded, 3 runs each:

  | KV cache | Placement | Prompt tok/s | Output tok/s | 150–190-token answer, total |
  |---|---|---|---|---|
  | f16 | 10% CPU | 132–136 | 5.4–13.1 | 23–36 s |
  | q8_0 | 100% GPU | 133–143 | 15–24 | 17–19 s |

  Writing is about twice as fast; reading the prompt is unchanged (~9 s for 1,250 tokens on this
  card). That makes the prompt the floor for the first word: roughly 0.7 s per 100 tokens of
  excerpts. A larger `num_batch` (1024) didn't speed up prompt reading and pushed layers back
  onto the CPU. `install.sh` now sets q8_0 with the other small-GPU settings. Answer quality
  with q8_0 has not been re-run through the eval yet; do that before the next small-GPU bundle.
- **Indexing evicted the LLM.** The worker didn't read `.env`, so its embedding calls went
  without `EMBED_NUM_GPU=0`, Ollama reloaded `bge-m3` onto the GPU and pushed the LLM out; the
  next question paid the reload. The worker now has `env_file` like the api.
- **Answer cache.** A repeated question that retrieves the same excerpts is answered from
  memory (key: model settings plus the exact messages, so ACLs and document changes are
  respected without invalidation). The eval sends `debug: true`, which bypasses the cache.
- **Indexing delayed questions by minutes (2026-11-09, laptop install, real share).** While a
  share was being indexed (bge-m3 on the CPU, batches of 32 excerpts, 100–200 s each), a
  question's embedding waited in Ollama's queue behind the batch: one real question took 71 s to
  retrieve, 91 s in total; direct probes waited 100 s and 213 s (0.06 s when idle). Indexing
  also took the CPU, which slowed the prompt reading (12–17 s to the first word instead of ~8–13).
  - Tried and rejected: a second model name for the same weights (`ollama cp`) shares the runner
    and queue; `OLLAMA_NUM_PARALLEL=2` doubles the LLM's context memory and pushed 10% of the 4B
    off the 4 GB card.
  - Fix: one excerpt per embedding request (`EMBED_BATCH=1`; Ollama embeds them one at a time
    anyway) and the worker waits before each one while an answer is in progress (a shared
    advisory lock held by the api, `app/db/priority.py`). Retrieval during indexing: 0.1–0.2 s.
    Answers during indexing now take as long as with the worker stopped (same question type:
    16.5 s / 54.7 s vs 12.8 s / 54.1 s first word / total).
  - What's left on this card is the model itself: ~120 prompt tok/s (6 excerpts, ~1,500 tokens:
    ~12 s to the first word) and 13–30 output tok/s (a 550-token answer: ~40 s). A typed
    "summarize X" retrieves 6 excerpts; the sidebar's Summary reads the document's own excerpts.
- Not yet measured: the 7B entirely on an 8 GB+ GPU. Measure it on the first customer-class GPU
  and record it here, for both a question and a summary.

## GPU memory budget (2026-11-13, estimates)

Since 2026-11-13 every install uses Qwen3 4B, and `install.sh` picks the memory settings from
the card size. The 4 GB row is measured; the other rows are **estimates**, built from:
- `ollama ps` on the laptop: 4B at 2.9 GB with `num_ctx` 4096 and q8_0; bge-m3 1.2 GB
- the models' context memory per token:
  - Qwen3 4B: 36 layers × 8 KV heads × 128 → 144 KiB f16, 76.5 KiB q8_0
  - Qwen2.5 7B: 28 layers × 4 KV heads × 128 → 56 KiB f16
- weights: 4B ≈ 2.6 GB with compute buffers; 7B ≈ 4.4 GB + ~0.4 GB buffers
- reserves: Ollama's 512 MiB margin (`LLAMA_ARG_FIT_TARGET`) plus ~300 MiB CUDA context

Check each tier with the operations.md checklist on the first real card.

| Card (usable) | Settings | 4B + context | bge-m3 | Total | Spare |
|---|---|---|---|---|---|
| 4 GB (~3.2 GB) | ctx 4096, q8_0, bge-m3 on CPU | 2.9 GB (measured) | CPU | 2.9 GB | ~0.3 GB |
| 6 GB (~5.1 GB) | ctx 8192, q8_0 | 2.6 + 0.6 = 3.2 GB | 1.2 GB | 4.4 GB | ~0.7 GB |
| 6 GB, if f16 | ctx 8192, f16 | 2.6 + 1.15 = 3.75 GB | 1.2 GB | 4.95 GB | ~0.15 GB: too tight, hence q8_0 |
| 8 GB (~7.1 GB) | ctx 8192, f16 (defaults) | 3.75 GB | 1.2 GB | 4.95 GB | ~2.1 GB |
| 8 GB, 7B option | ctx 8192, f16 | 4.8 + 0.45 = 5.25 GB | 1.2 GB | 6.45 GB | ~0.65 GB: tight, so `EMBED_NUM_GPU=0` is recommended |
| 12 GB+, 7B option | ctx 8192, f16 | 5.25 GB | 1.2 GB | 6.45 GB | 4 GB+ |

**A larger `LLM_NUM_CTX` doesn't help with the current settings.** A question uses ~1,500–2,000
prompt tokens plus up to 1,024 answer tokens, and a 6-excerpt summary about the same, so 8192
already leaves room. The context only needs to grow if `SUMMARY_CHUNKS` is raised for fuller
summaries, and every extra excerpt adds ~0.7–2 s before the first word on a slow card.
Suggested for a 12 GB+ card, if customers want longer summaries: `LLM_NUM_CTX=16384` (+1.15 GB
f16) with `SUMMARY_CHUNKS=10`. Not a default: it would break the 10-second target on
summaries for smaller cards.

## Real documents (2026-11-09, laptop install, Qwen3 4B, 14 documents)

40 hand-written cases against the customer-style documents on the laptop install, asked as a
non-admin user (cases and answers kept out of the repo; they quote those documents):
retrieval recall 1.0, citation recall 0.971, citation precision 0.947, answer accuracy 0.971,
abstention 1.0 (4/4), follow-up 1/1, summaries 1/2. Median 14 s per question.

**Re-run 2026-11-13** (q8_0 KV cache, streamed, 15 documents after `ENC_User_Manual.pdf`
finished indexing, same cases plus two typed summaries; 34 answerable, 4 unanswerable, 4 summaries):

| Metric | 2026-11-09 | 2026-11-13 |
|---|---|---|
| Retrieval recall (document level) | 1.0 | 1.0 |
| Citation recall | 0.971 | 0.941 |
| Citation precision | 0.947 | 1.000 |
| Answer accuracy | 0.971 | 0.941 |
| Abstention | 1.0 | 1.0 |
| Summaries accurate | 1/2 | 4/4 |
| Median first word / total, questions | – / 14 s | **10.1 s** / 13.9 s |
| Median first word / total, summaries | – | 7.5 s / 33 s |

- The two misses are retrieval, not the KV cache. `cr-priority` is the form-style DOCX problem
  below. `rn-xfp` passed on 11-09 from page 116. Since the PDF re-index (pypdfium2, document name
  in the embedding input), six near-identical transceiver descriptions (pp. 117–128) outrank it.
  The chunk is intact: "XFP/10G/850I/MM/LC … operating at a wavelength of 850 nm".
- This set's retrieval recall only checks the document, not the page, so it hides misses like
  `rn-xfp`.
- First word on answered questions: 10.3 s median, range 6.9–18.0 s. The slowest are the
  technical PDFs, whose excerpts are full 1,000-character chunks.

## Single answer model, release 1.0.0-rc7 (2026-11-13, laptop install, q8_0)

From rc7, Qwen3 4B is the default on every install. The 7B ships in the same bundle as an option
(see "GPU memory budget"). The laptop install was upgraded from 1.0.0-rc6.small. Its `.env` kept
the small-card settings (ctx 4096, 768 tokens, `EMBED_NUM_GPU=0`, `OLLAMA_KV_CACHE_TYPE=q8_0`).

- **Placement:** `ollama ps` shows `qwen3:4b-instruct-2507-q4_K_M` at 100% GPU (2.9 GB) and
  `bge-m3` at 100% CPU. That's as intended for a card under 6 GB.
- **Model IDs** in the rc7 ollama image:
  - `qwen3:4b-instruct-2507-q4_K_M` `0edcdef34593`
  - `qwen2.5:7b-instruct` `845dbda0ea48`
  - `bge-m3` `790764642607`
- **Image sizes:** the ollama image is 11.9 GB and the bundle 12 GB.

**This is the q8_0 KV-cache result.** Both earlier small-GPU runs most likely already used q8_0,
and the numbers match them:

| Metric | Eval corpus (34) | Real documents (42) |
|---|---|---|
| Retrieval recall | 1.000 | 1.000 (document level) |
| Citation recall | 1.000 | 0.941 |
| Citation precision | 0.929 | 1.000 |
| Answer accuracy | 1.000 | 0.941 |
| False not-found | 0 | 0.059 (`cr-priority`, `rn-xfp`; see above) |
| Abstention | 1.0 | 1.0 |
| Summaries accurate / cited | 4/4, 3/4 | 4/4\*, 4/4 |
| Collection questions | 2/2 | – |
| Median first word, questions | 3.6 s | 9.7 s (answered only: 10.3 s, range 6.7–16.7 s; 15 of 32 under 10 s) |
| Median total, questions | 4.7 s | 13.6 s |
| Median first word / total, summaries | – / 16.2 s | 7.4 s / 34.7 s |

\* `sum-cp-typed` wrote "a 5-working-day acknowledgement". It was scored as missing "5 working
days", so the case now accepts the hyphenated form.

**One CUDA error during the first run:** at 10:47, llama-server aborted on a matrix multiply
(`CUDA error: unknown error`). Windows logged no driver event. Ollama restarted the runner without
restarting the container, but the eval in progress failed and the next two answers waited 36–67 s
for the reload. A clean rerun produced the numbers above, with no further errors in ~80 questions.
The error was not seen before on this card. If it recurs, look at the WSL2/NVIDIA driver before
the model settings.

- **Summary of a short document skipped a section (fixed).** Complaints_Procedure.docx is 7
  small chunks; sampling 6 dropped the one with the response deadlines. A document that fits in
  per-document chunks × `CHUNK_SIZE` characters is now read whole (case `summary-short-sections`).
- **Form-style DOCX loses context between chunks.** The priority field of a change-request form
  sat in a chunk without the form's title, so "what priority was the X change request given?"
  never retrieved it; the model correctly said not found. Merged table cells are repeated once
  per cell, which pads chunks. Candidates: collapse repeated cells in the DOCX loader; prefix
  chunks with the document title. Both need a re-index and the full eval.
- **Long summaries sometimes have no `[n]` markers** (leaf/spine procedure: cited in 2 of 4
  runs, content correct each time). Re-check with the shorter summary prompt.
- **One PDF (ENC Administrator Manual R11.3) extracts without spaces between words**
  ("EnsembleControllerR11.3AdministratorManual"). Both questions on it still passed, but
  keyword search can't match words run together. *Fixed 2026-11-10:* PDFs are now read with
  pypdfium2 (already in the image for OCR), which keeps the spaces and read the 474-page manual
  in 1.4 s against pypdf's 17 s.
- **"summarise this doc" typed with one document in scope answered "not found"** (2026-11-10):
  it ran as a question, retrieving whatever matched the words "summarise this doc". The server
  now runs such requests as summaries (`answer.asks_for_summary`).
- **"how many docs are indexed" answered "6" when 13 were indexed** (2026-11-12). The model
  counted the six retrieved excerpts, which were wrapped in `<document id="n">` tags. Fixed in
  two layers:
  - Questions about the collection itself are answered from the database, with the ACL and
    scope applied (`retrieval/library.py`; cases `how-many-docs`, `which-docs`, scored as
    `library_accuracy`).
  - Excerpts are now wrapped in `<excerpt>` tags, and the answer prompt says they are a few
    passages picked for the question, so they can't show how many documents exist. This covers
    phrasings the detector doesn't catch.

## Prompt length vs time to first word (2026-11-13, laptop install, Qwen3 4B, q8_0)

Goal: first words within 10 s on the 4 GB T500. On this card reading the prompt sets the floor
(~150 tokens/s), so fewer or shorter excerpts should help. The api now logs `prompt_tokens`
and `prompt` (ms) per answer, and the eval records both. Excerpt count is `TOP_K`; the new
`EXCERPT_MAX_CHARS` trims each excerpt to the run of lines or sentences covering the most
question words. It keeps the excerpt's id and page, and leaves an excerpt whole when no word
matches. Answers only; summaries read whole excerpts.

Real documents (42 cases; 34 answerable):

| Variant | Prompt tokens (median) | First word, median | Under 10 s | Citation recall | Citation precision | Answer accuracy |
|---|---|---|---|---|---|---|
| **6 excerpts (current)** | 1,572 | 10.1 s | 12 / 32 | 0.941 | **1.000** | **0.941** |
| 5 excerpts (2 runs, identical) | 1,385 | 8.1 s | 25–27 / 32 | 0.941 | 0.889 | 0.941 |
| 4 excerpts | 1,153 | 6.3 s | 31 / 32 | 0.941 | 0.917 | 0.912 |
| 4 excerpts, trimmed to 500 chars | 897 | 4.6 s | 30 / 30 | 0.882 | 1.000 | 0.824 |

Eval corpus (34 cases): every variant kept retrieval recall, citation recall and answer
accuracy at 1.000. Citation precision was 0.929 / 0.963 / 0.963 / 0.963 and median first word
3.6 / 2.9 / 2.3 / 2.5 s. Its chunks are short (~200 characters), so trimming changed nothing
there.

- **5 excerpts:** same accuracy, 2 s faster to the first word. But one answer
  (`ob-beneficial-owner`) cited all five excerpts, four of them irrelevant, so precision fell
  from 1.000 to 0.889. It repeated exactly on a second run: at temperature 0.1 the output is
  deterministic for a given prompt.
- **4 excerpts:** also loses `sd-faulted` (the right page no longer reaches the model), and one
  answer over-cites.
- **Trimming:** cuts the answer out of four more excerpts (`leaf-offload`, `els-ha`,
  `enc-eventlog`, `sd-faulted`). Keyword overlap doesn't find the relevant part of technical
  text reliably. Off by default (`EXCERPT_MAX_CHARS=0`).
- **Decision:** keep 6 excerpts. No shorter prompt kept answer accuracy with citation precision
  no worse. 5 excerpts is the candidate if one over-cited answer in 34 is acceptable for 2 s
  less wait (`TOP_K=5`; not applied). The remaining 10 s misses need the model to read faster
  (an 8 GB+ GPU), not a shorter prompt.

## Before changing prompts or retrieval

1. Run `bash scripts/eval.sh --detach --label <change>` and compare with the last shipped run.
2. Add a case to `cases.json` for any failure found by hand. This file grows; it never shrinks.
3. Record the run in the table above with its `prompt_version`.
