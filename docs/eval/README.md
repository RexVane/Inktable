# Ordo Retrieval Evaluation

The authoritative question set lives in `services/api/tests/evalset.py`, with
stable evidence annotations in `gold-evidence-spans.json`. The v8 contract has
77 frozen questions:

- 46 answerable questions (96 evidence requirements, 691 exact gold spans)
- 7 metadata, file type, and scope questions
- 12 questions whose evidence is missing from the corpus
- 12 deliberately unanswerable questions

Retrieval and compression metrics evaluate the 53 runnable, non-corpus-missing
cases; the real-model QA run evaluates 65 cases (answerable, metadata, and
unanswerable). Corpus-missing cases are retained in the contract but are not
sent to a model. Do not fold metadata or cross-document groups into a legacy
single-document metric without stating the denominator.

## Current v8 snapshot (2026-08-16)

These are frozen, private-corpus measurements, not live product telemetry. They
were produced from Git baseline `6a7ce93` and one 29,434-chunk database snapshot;
each number below names its checked-in artifact. Re-running against another DB,
model/provider, or commit creates a different experiment and must not overwrite
this snapshot.

The production reranker result in `v8-final-local.json` remains
`local-static-v3`: Recall@5 94.34%,
MRR@10 88.11%, nDCG@10 91.04%, and P95 2,669 ms. The RRF control reaches
Recall@5 96.23% but lower MRR/nDCG (86.45%/88.69%). The pinned ONNX
Cross-Encoder reaches MRR/nDCG 90.97%/92.63% on its 48-case comparison, only
about +7.95%/+7.85% relative to its RRF control and with 16.5 s P95 latency.
The historical “+15% relative nDCG” rule is retired: at the observed RRF
nDCG it demanded roughly 97%, making one rank-2 item enough to fail the entire
selection. The release gate is now absolute and multi-objective: nDCG@10 ≥90%,
Recall@5 ≥95%, Recall@20 regression ≤2 percentage points, and Rerank P95 ≤1.5s.
The Cross-Encoder fails that latency gate and therefore remains non-default.

The formal compression result (`v8-final-compress.json`) is 98.96% Gold
Evidence Recall, 97.83% complete-case recall, 61.98% median compression,
247.5 ms P95, and zero offset round-trip errors. X10 is the sole upstream
miss.

The final real-model QA artifact is `v8-final-qa.json`, produced with
`kocode / gpt-5.6-sol`. All 65 cases completed without provider failures or
degraded execution. “65/65” therefore means run/format/flow completeness, not
that all expected evidence was cited. Citation support is 95.16% (118/124
claims), but this is a **same-model self-judgment**, not an independent human or
model-blinded verdict. Exact citation integrity and statement coverage are both
100%, and correct refusal is 100% (12/12). A13 and S17 use the explicit fallback
path. The stricter end-to-end result is **Gold-evidence citation recall 68.93%
(71/103)** and is reported as a first-class limitation rather than hidden behind
the flow-completion gate. Results
contain hashes, counts, and timings only; they never persist answer text,
snippets, keys, or source paths.

Run the current local-static retrieval baseline against the local library:

```bash
cd services/api
uv run python tests/run_eval.py \
  --label v8-final-local \
  --json ../../docs/eval/v8-final-local.json
```

Run the compression gate:

```bash
uv run python tests/run_compress_eval.py \
  --json ../../docs/eval/v8-final-compress.json
```

Run real-model QA only with an explicitly selected, read-only cc-switch
database. The provider key is held in memory and is never written to the
artifact:

```bash
uv run python scripts/run_qa_eval.py \
  --ccswitch-db <backup.db> --provider kocode \
  --case-delay 90 --judge-retries 2 --judge-retry-delay 30 \
  --json ../../output/qa-kocode-sol-full-v3.json
```

Frozen v8 outputs:

- `v8-final-local.json`: production `local-static-v3` retrieval result.
- `v8-final-rrf.json`: explicit RRF-only control.
- `v8-post-clean-cross-default.json`: pinned ONNX Cross-Encoder comparison.
- `v8-final-compress.json`: exact Gold Evidence compression gate.
- `v8-final-qa.json`: final 65-case `kocode / gpt-5.6-sol` QA gate.
- `gold-evidence-spans.json`: content-addressed Gold contract.

Historical v7 outputs:

- `v7-m0-shared-pipeline.json`: shared-pipeline baseline before the final QA
  stage migration.
- `v7-m1-shared-pipeline.json`: M1 result after diversify, neighbor expansion,
  and assembly moved into the shared pipeline.
- `v7-m2-hierarchy-routing.json`: schema v2 hierarchy and soft-routing result.
- `v7-m3-rrf-baseline.json`: hierarchy candidates with explicit RRF-only
  fallback.
- `v7-m3-local-static-rerank.json`: first local reranker benchmark using the
  already bundled static embedding model.

The M1 run is behaviorally identical to M0: Recall@5 is 78.3%, strict pass
rate is 61.7%, and every per-question outcome and difficulty metric is
unchanged. The command exits with status 1 because the pre-M2 Recall@5 remains
below the 80% release threshold; this is a recorded product gap, not an M1
regression.

M2 also reports Document Recall@50 because hierarchy routing is a deep-recall
stage. It reaches 98.3% while Recall@5 remains 78.3%, isolating ranking as the
next bottleneck. This is not yet Gold Evidence Recall@50: exact gold chunk/span
annotations must be completed before the M4 compression gate.

The M3 local-static adapter improves Recall@5 from 78.3% to 83.3% and strict
pass rate from 65.0% to 76.7%, with Recall@20 increasing from 96.7% to 98.3%.
However, MRR@10 and nDCG@10 improve only 5.6% and 4.7% relative to the RRF
baseline, below the 15% model-selection gate. The adapter remains enabled and
explicitly degradable, but it is not recorded as a completed cross-encoder
milestone.

The current M4 check is a proxy evaluation over the 60 answerable questions.
It counts a case as retained when an annotated answer keyword occurs in an
exact source span selected for its ContextPack; it is therefore reported as
keyword evidence recall, not formal Evidence Recall against gold spans. This
proxy reaches 90.0%, with about 68% median character compression and about
59ms compression-stage P95 latency. The missed cases are A10, F20, F30, F31,
P19, and P20; some are upstream candidate-retrieval failures rather than
compression losses. Exact gold-span annotation is still incomplete, and the
proxy is below the 95% Evidence Recall gate, so M4 remains in progress.

The M3b round (`v7-m3b-local-static-v2.json`, paired control
`v7-m3b-rrf-control.json`) keeps the same frozen 72 questions and adds five
pipeline changes driven by per-case runtime probes:

1. Comparative-question decomposition: "A 和 B 分别…" queries add one
   sub-query recall route per entity (head-limited, fusion-only, K3-safe).
2. Term extraction now always re-segments CJK whitespace tokens, so glued
   non-words like "版本开发" no longer poison coverage features.
3. Rerank input selection deduplicates identical `text_hash` chunks across
   contents, so near-identical file copies cannot flood the candidate pool.
4. LocalStaticReranker v2 adds numeric-answer, explicit file-type, and
   filename-coverage features on top of semantic/coverage/RRF/exact.
5. A post-rerank redundancy pass softly demotes same-document chunks that
   add no new query-term coverage, keeping complementary chunks visible.

Against the same-day RRF control this lifts Recall@5 from 85.0% to 95.0%,
strict pass rate from 73.3% to 90.0%, MRR@10 from 73.2% to 78.8%, and
nDCG@10 from 77.3% to 83.0% (p50 50ms). The relative MRR/nDCG gains
(+7.6%/+7.5%) still do not reach the 15% cross-encoder selection gate, so
M3 remains open; the remaining failures (F29/F31/P19 chunk-level ranking,
X06 near-duplicate flood) are exactly the cases a real cross-encoder or
fuzzy duplicate grouping would address.

The M3c round (`v7-m3c-local-static-v3.json`) upgrades the local adapter
with IDF-weighted term coverage (per-query normalized, FTS-backed document
frequencies), a term-proximity feature, comparative-question lexical
scoring restricted to entity sub-queries, and vector-based cross-content
near-duplicate demotion (cosine >= 0.95, measured: edited file copies sit
at 0.965-0.973 while related-but-distinct documents sit near 0.61). Result:
MRR@10 80.5%, nDCG@10 84.0%, Recall@5 95.0%, strict 90.0%, p50 46ms.
Relative to RRF the gains are +10.0% MRR / +8.7% nDCG — the local-static
path has plateaued below the 15% gate; probe evidence shows the remaining
failures need query-conditioned semantics (F29/F31/P19) or deeper recall
(X06 gold chunk at RRF rank 154), both out of reach for a static embedding.

The M4 compression gate is now measured by a repeatable harness
(`tests/run_compress_eval.py`) that mirrors the /ask context path. As of
M3c retrieval: keyword evidence recall 95.0% (gate 95%), median character
compression 61.1% (gate >= 35%), zero offset round-trip errors, compress
P95 3.7ms. Frozen output: `v7-m4-compress-eval.json`. This remains a
keyword-anchored proxy; exact gold-span annotation is still open.

The JSON output records query text, ranks, scores, latency, and document names.
It does not persist chunk text or source file contents. Existing
`baseline-fts5.json` and `with-vector.json` remain the immutable 30-question
pre-v7 baselines.

Evaluation labels are frozen before M2. Change a gold annotation only when the
source library changes or the original annotation is demonstrably wrong; do
not adjust it to make a retrieval implementation score better.

## 2026-08-26: verbatim-quote verification (diagnostic only)

A fifth post-generation check asks the model to append a `===引文===` block
mapping each used `[Cn]` to a verbatim excerpt, then verifies that excerpt
appears in **the cited chunk** (`app/qa/quotes.py`). This targets the gap
between "exact citation integrity 100%" (a format property) and "Gold-evidence
citation recall 68.93%" (a content property): a model can cite the right file
while stating a number that file does not contain, and the `[Cn]` stays
well-formed. Enforcement is **off by default**
(`ORDO_QUOTE_ENFORCE=1` strips citations whose quote fails), because
changing answer behaviour needs a 65-case QA baseline and that re-verification
is still blocked on provider availability.

**Local probe (`output/probe-quote-cost-20260826.json`) — inconclusive, and
reported as such.** Local Ollama `qwen3:8b`, isolated eval DB, 10
gold-answerable cases, only variable = the quote clause:

| arm | answered | refused | other | P50 |
|---|---:|---:|---:|---:|
| no quote block | 1 | 8 | 1 | 17.3 s |
| with quote block | 1 | 7 | 2 | 18.6 s |

The clause did not change the answered count, but the baseline answered only
1 of 10, so **this sample has no power to detect a change**. The probe script
now prints that limitation instead of a reassuring "no regression". Retrieval
was verified healthy for these queries (120 candidates, no degradation), so the
refusals come from the model: `qwen3:8b` (8B, Q4) does not follow the strict
one-fact-per-line + mandatory-`[Cn]` + refuse-without-direct-evidence contract.
It is adequate for abstract generation, not for cited answering.

Of the 5 quotes the model did emit, **0 verified** — consistent with a model
that is not copying source text, and the concrete reason enforcement stays off:
turning it on with this model would strip every citation.

## 2026-08-26: investigation journal is not an evaluation surface

The new `journal` table records successful Q&A for recall by the user. It is
deliberately excluded from retrieval and from context assembly, so it does not
affect any metric in this document. A test asserts that `retrieval/*` and
`qa/answer.py` never import it: journal text is model output, and feeding it
back as evidence would let a later answer cite an earlier answer while the four
hard checks still see a well-formed citation.
