# Inktable Retrieval Evaluation

The authoritative v7 question set lives in `services/api/tests/evalset.py`.
It contains 72 frozen questions:

- 20 single-document fact questions
- 10 paraphrase questions
- 10 same-document cross-chunk synthesis questions
- 10 cross-document synthesis questions
- 10 metadata, file type, and scope questions
- 12 questions with no support in the library

Each answerable case has one or more gold document-name hints. Fact and
same-document cases also use answer keywords as a lightweight evidence anchor.
The cross-document and metadata groups are kept separate because they require
the v7 QueryPlan and multi-document metrics; they must not be folded into the
legacy single-document Recall@5 number.

Run the current baseline against the local library:

```bash
cd services/api
uv run python tests/run_eval.py \
  --label v7-m1-shared-pipeline \
  --json ../../docs/eval/v7-m1-shared-pipeline.json
```

Frozen milestone outputs:

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

The JSON output records query text, ranks, scores, latency, and document names.
It does not persist chunk text or source file contents. Existing
`baseline-fts5.json` and `with-vector.json` remain the immutable 30-question
pre-v7 baselines.

Evaluation labels are frozen before M2. Change a gold annotation only when the
source library changes or the original annotation is demonstrably wrong; do
not adjust it to make a retrieval implementation score better.
