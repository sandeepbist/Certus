#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from evals.pgvector_recall import (  # noqa: E402
    PgvectorEvaluationError,
    check_pgvector_report,
    report_json,
    run_pgvector_evaluation,
)


LOCAL_DATABASE_URL = "postgresql://nexus:nexus_dev_password@localhost:5432/nexus"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compare filtered pgvector HNSW rankings with exact search.",
    )
    parser.add_argument(
        "--database-url",
        default=os.getenv("PGVECTOR_EVAL_DATABASE_URL")
        or os.getenv("DATABASE_URL")
        or LOCAL_DATABASE_URL,
        help="PostgreSQL DSN; defaults to the local Certus development database.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        report = run_pgvector_evaluation(args.database_url)
        failures = check_pgvector_report(report) if args.check else []
    except (PgvectorEvaluationError, OSError) as error:
        print(f"pgvector evaluation error: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        print(
            f"pgvector evaluation failed without exposing connection details: "
            f"{type(error).__name__}",
            file=sys.stderr,
        )
        return 2

    payload = report_json(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    if args.json:
        print(payload, end="")
    else:
        aggregate = report["aggregate"]
        print(
            "pgvector filtered ANN evaluation: "
            f"version={report['run']['pgvector_version']} "
            f"rows={report['run']['row_count']} "
            f"cases={report['run']['case_count']} "
            f"min-recall@20={aggregate['minimum_recall_at_20']:.3f} "
            f"complete={aggregate['complete_case_rate']:.3f} "
            f"joined-cases={len(report['production_query_cases'])} "
            "joined-min-recall@20="
            f"{aggregate['production_query_minimum_recall_at_20']:.3f} "
            "joined-hnsw="
            f"{aggregate['production_query_hnsw_plan_case_rate']:.3f} "
            f"joined-fts-gin={int(report['lexical_query_cases'][0]['fts_plan']['uses_gin'])} "
            f"rrf-recall@5={report['seed_corpus']['evidence_recall_at_5']:.3f} "
            f"rrf-mrr@20={report['seed_corpus']['mrr_at_20']:.3f} "
            "persistent-rows=0 provider-calls=0"
        )
    for failure in failures:
        print(f"pgvector evaluation regression: {failure}", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
