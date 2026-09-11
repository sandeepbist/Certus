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

The seed runner deliberately uses brute-force cosine search as the deterministic dense oracle. Its lexical arm is a stable token-overlap baseline rather than PostgreSQL FTS/BM25. The table seed currently proves text retrieval only. The separate live gate covers production semantic/lexical SQL and final RRF over this corpus, but typed table evaluation, human annotation tooling, confidence intervals, generation/claim metrics, multilingual/layout/OCR cohorts, and held-out private release sets remain required before the evaluation workstream is complete.

Run the disposable filtered-ANN matrix when PostgreSQL is available:

```bash
bun run eval:pgvector
```

This creates a transaction-local 3,170-row, 1,536-dimensional fixture and HNSW index. It covers three noisy tenants, a sparse tenant, tenant/user/profile/readiness/current-version boundaries, rare tags, date windows, and requested result sizes 5/20/50. Every case forces and analyzes the approximate plan, compares it with an index-disabled exact oracle, records planner/execution/buffer telemetry, and must meet the recall/completeness gate. The evaluator and production retrieval share the same bounded transaction-local HNSW scan policy. The current controlled pgvector 0.8.6 run reached `1.000` minimum recall and completeness after lower scan budgets demonstrably failed.

The evaluator also creates transaction-local document/version/derivation/chunk relations and executes the exact semantic and lexical SQL used by serving. Twenty-three semantic cases—including explicit document-ID/title scope, distinct source/recorded authority, recorded fallback, current scope, year/date/instant as-of cutoffs, and before/after/inclusive year/calendar-date/RFC-3339-instant windows over two immutable versions—must preserve exact-oracle recall/completeness and select the profile-specific HNSW graph inside the materialized nearest-neighbor CTE. A separate generation-aware case must return only vectors bound to the locked active generation. PostgreSQL may select its HNSW graph or, for at most 256 generation vectors, the generation primary key plus an exact distance sort; both paths are compared with the exact oracle and an unbounded non-ANN fallback fails the gate. A lexical case must select the FTS GIN index and return only exact authorized rows from indexed FTS and escaped literal-phrase search despite wildcard and scope decoys. Finally, all checksum-bound seed documents and queries pass through production chunking, local embeddings, detected-year/authority filtering, joined SQL, and final RRF with Recall@5 `1.000`, MRR@20 `0.950`, and conflict completeness `1.000`. The entire fixture is rolled back; it writes no persistent rows and makes no provider calls. This gate is not a substitute for a private production corpus, concurrent load, or deployment memory and tail-latency evaluation.

Run the live shadow embedding-generation contract when PostgreSQL is available:

```bash
bun run eval:embedding-generations
```

This uses the actual migrated PostgreSQL schema and stored procedures. It proves empty-workspace activation/cutover/rollback/staleness and, when an isolated local scope of at most 100 ready chunks exists, exercises real snapshot membership, fair profile-compatible worker dispatch, bounded `SKIP LOCKED` claims, lease-owner fencing, capped failure attempts, exact coverage accounting, sealing, HNSW serving, atomic vector-membership cutover and rollback, post-activation insert rejection, and removal from serving on staleness. All lifecycle and non-empty rows are inside a transaction that is rolled back; the runner then proves that its synthetic scope retained zero rows. The provider-free worker probe uses the deterministic local embedding implementation, makes no hosted provider calls, and is not itself a semantic-quality claim.

The versioned vector relation has one partial HNSW graph per supported coordinate-space profile, and only vectors whose complete generation is currently active belong to those graphs. Runtime retrieval locks the compatible active generation for the query transaction and records its ID in each document evidence witness. Workspaces without a compatible active generation continue to use the established profile-isolated chunk vectors.

Completed generations are qualified by the durable embedding worker through a database-authored integrity gate. The gate recomputes exact snapshot membership, content hashes, vector dimensions and norms, the declared normalization contract, and constant-memory SHA-256 fingerprints before moving a generation to `ready`. Caller-authored approval reports cannot activate a generation. This qualification deliberately records `quality_claim: not_evaluated`: semantic model quality still requires the held-out, human-labeled release evaluation described above. A scoped owner or administrator may activate a ready generation with a rollback window from one hour through 30 days.
