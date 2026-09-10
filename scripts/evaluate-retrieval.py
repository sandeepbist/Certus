#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPOSITORY_ROOT))

from evals.retrieval import (  # noqa: E402
    EvaluationDataError,
    check_baseline,
    evaluate_dataset,
    load_dataset,
)


DEFAULT_MANIFEST = (
    REPOSITORY_ROOT / "evals/datasets/certus_seed_v1/manifest.json"
)
DEFAULT_BASELINE = REPOSITORY_ROOT / "evals/baselines/certus_seed_v1.json"


def _write_report_atomic(path: Path, report: dict) -> None:
    resolved = path.resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=resolved.parent,
            prefix=f".{resolved.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, resolved)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run Certus's provider-free retrieval regression harness."
    )
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Fail when the corpus/runner identity changes or a quality gate regresses.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Optionally write the complete JSON report atomically.",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the complete JSON report instead of the concise summary.",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        dataset = load_dataset(args.manifest)
        report = evaluate_dataset(dataset)
        failures = check_baseline(report, args.baseline) if args.check else []
    except EvaluationDataError as error:
        print(f"retrieval evaluation contract error: {error}", file=sys.stderr)
        return 2

    if args.output:
        _write_report_atomic(args.output, report)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        hybrid = report["methods"]["hybrid_rrf"]["aggregate"]
        print(
            "retrieval evaluation: "
            f"dataset={report['dataset']['dataset_id']}@"
            f"{report['dataset']['dataset_version']} "
            f"queries={report['dataset']['query_count']} "
            f"recall@5={hybrid['answerable_evidence_recall_at_5']:.3f} "
            f"mrr@20={hybrid['answerable_mrr_at_20']:.3f} "
            f"conflict-complete@5={hybrid['conflicting_evidence_complete_at_5']:.3f} "
            f"no-answer-empty={hybrid['no_answer_empty_rate']:.3f} "
            f"p95={report['latency_ms']['p95']:.3f}ms "
            "provider-calls=0"
        )

    if failures:
        for failure in failures:
            print(f"retrieval evaluation regression: {failure}", file=sys.stderr)
        return 1
    if args.check:
        print("retrieval evaluation gates passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
