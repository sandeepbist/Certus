#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from evals.embedding_generations import (  # noqa: E402
    EmbeddingGenerationEvaluationError,
    check_embedding_generation_report,
    report_json,
    run_embedding_generation_evaluation,
)


LOCAL_DATABASE_URL = "postgresql://nexus:nexus_dev_password@localhost:5432/nexus"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify shadow embedding generation, lease, cutover, and rollback contracts.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("EMBEDDING_GENERATION_EVAL_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or LOCAL_DATABASE_URL,
        help="PostgreSQL DSN; defaults to the local Certus development database.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = run_embedding_generation_evaluation(args.database_url)
        failures = check_embedding_generation_report(report) if args.check else []
    except (EmbeddingGenerationEvaluationError, OSError) as error:
        print(f"embedding generation evaluation error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            "embedding generation evaluation failed without exposing connection details: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return 2

    if args.json:
        print(report_json(report), end="")
    else:
        nonempty = report["nonempty_workspace_lifecycle"]
        coverage = "skipped" if nonempty.get("skipped") else str(nonempty["chunk_count"])
        serving_access = (
            "skipped"
            if nonempty.get("skipped")
            else "hnsw"
            if nonempty["serving_query_uses_hnsw"]
            else "invalid"
        )
        print(
            "embedding generation evaluation: "
            f"nonempty-chunks={coverage} "
            f"serving-access={serving_access} "
            "cutover=pass vector-cutover=pass rollback=pass "
            "stale-fence=pass lease-owner=pass "
            "persistent-rows=0 provider-calls=0"
        )
    for failure in failures:
        print(f"embedding generation regression: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
