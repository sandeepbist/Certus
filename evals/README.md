# Certus evaluation harness

This directory contains versioned, provider-free regression datasets and runners. It is a release gate for measured retrieval behavior, not a claim that the current seed corpus represents production quality.

Run the committed gate:

```bash
bun run eval:retrieval
```

Write a full local report when investigating a change:

```bash
python3 scripts/evaluate-retrieval.py --check --output output/retrieval-eval.json
```

The report records the manifest checksum, source/runner fingerprint, chunk and vector storage estimate, exact dense, lexical, and hybrid/RRF arms, per-query rankings, exact evidence spans, cohort metrics, and local latency. It makes zero provider calls.

Dataset rules:

- Source files are immutable inputs identified by SHA-256.
- Every answered or conflicting-evidence query has expected facts and exact character-span evidence.
- Conflicting-evidence queries require at least two source spans so the gate can prove that competing evidence survives retrieval.
- Insufficient-evidence queries have neither expected facts nor evidence.
- Source paths cannot escape their dataset directory.
- Manifest changes invalidate the committed baseline identity and require an explicit review.
- Runner, chunker, embedding, or RRF changes invalidate the run fingerprint and require an explicit review.

Manifest schema v2 records the expected runtime answer disposition (`answered`, `insufficient_evidence`, or `conflicting_evidence`) rather than reducing every query to a boolean. The loader remains backward-compatible with schema v1 manifests.

The seed runner deliberately uses brute-force cosine search as the deterministic dense oracle. Its lexical arm is a stable token-overlap baseline rather than PostgreSQL FTS/BM25. The table seed currently proves text retrieval only. The separate live gate covers production semantic and lexical SQL plans, but final production RRF over this corpus, typed table evaluation, human annotation tooling, confidence intervals, generation/claim metrics, multilingual/layout/OCR cohorts, and held-out private release sets remain required before the evaluation workstream is complete.

Run the disposable filtered-ANN matrix when PostgreSQL is available:

```bash
bun run eval:pgvector
```

This creates a transaction-local 3,170-row, 1,536-dimensional fixture and HNSW index. It covers three noisy tenants, a sparse tenant, tenant/user/profile/readiness/current-version boundaries, rare tags, date windows, and requested result sizes 5/20/50. Every case forces and analyzes the approximate plan, compares it with an index-disabled exact oracle, records planner/execution/buffer telemetry, and must meet the recall/completeness gate. The evaluator and production retrieval share the same bounded transaction-local HNSW scan policy. The current controlled pgvector 0.8.6 run reached `1.000` minimum recall and completeness after lower scan budgets demonstrably failed.

The evaluator also creates transaction-local document/version/derivation/chunk relations and executes the exact semantic and lexical SQL used by serving. Twenty-three semantic cases—including explicit document-ID/title scope, distinct source/recorded authority, recorded fallback, current scope, year/date/instant as-of cutoffs, and before/after/inclusive year/calendar-date/RFC-3339-instant windows over two immutable versions—must preserve exact-oracle recall/completeness and select HNSW inside the materialized nearest-neighbor CTE. A lexical case must select the FTS GIN index and return only exact authorized rows from indexed FTS and escaped literal-phrase search despite wildcard and scope decoys; it also proves as-of, lower-bounded-year, exact-calendar-day, exact-microsecond, and case-insensitive exact-title selection in both lexical arms. Finally, all checksum-bound seed documents and queries pass through production chunking, local embeddings, detected-year/authority filtering, joined SQL, and final RRF with Recall@5 `1.000`, MRR@20 `0.950`, and conflict completeness `1.000`. The entire fixture is rolled back; it writes no persistent rows and makes no provider calls. This gate is not a substitute for a private production corpus, concurrent load, or deployment memory and tail-latency evaluation.

Run the live shadow embedding-generation contract when PostgreSQL is available:

```bash
bun run eval:embedding-generations
```

This uses the actual migrated PostgreSQL schema and stored procedures. It proves empty-workspace activation/cutover/rollback/staleness and, when an isolated local scope of at most 100 ready chunks exists, exercises real snapshot membership, bounded `SKIP LOCKED` claims, lease-owner fencing, failure retry, exact coverage accounting, and sealing. All lifecycle and non-empty rows are inside a transaction that is rolled back; the runner then proves that its synthetic scope retained zero rows. Existing vectors may be copied only as provider-free control-plane payloads—the test makes no semantic-quality claim and performs zero provider calls.

The shadow vector relation deliberately has no global ANN index. Different embedding profiles must never share one HNSW graph. A later serving migration must create a physically compatible profile/generation index and pass the exact-versus-ANN gate before runtime retrieval can switch to it.
