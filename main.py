"""Safe command-line entry point for the fixed final demo corpus.

Importing this module performs no database or Azure operation.  Live execution
requires an explicit mode and confirmation of the ``final`` database target.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Sequence
from urllib.parse import urlsplit

from demo.catalog import DEFAULT_MANIFEST_PATH, FINAL_DATABASE
from demo.runner import DemoRunner


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the fixed demo01-demo20 refund workflow")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST_PATH,
        help="canonical demo fixture (default: database/fixtures/demo_cases.json)",
    )
    parser.add_argument("--mode", choices=("offline", "live"), default="offline")
    parser.add_argument(
        "--workers",
        type=int,
        choices=range(1, 5),
        default=1,
        help="bounded parallel case workers (1-4; use 2 for the live demo)",
    )
    parser.add_argument(
        "--confirm-live",
        metavar="DATABASE",
        help="required for live mode; must be exactly 'final'",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--output",
        type=Path,
        help="also write the complete result as UTF-8 JSON (for a durable demo report)",
    )

    commands = parser.add_subparsers(dest="command")
    commands.add_parser("list", help="list the exact 20 allowlisted cases")
    run = commands.add_parser("run", help="run one seeded case")
    run.add_argument("case_id", help="demo01 through demo20")
    commands.add_parser("run-all", help="run all 20 cases with failure isolation")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0

    if args.mode == "live":
        if args.confirm_live != FINAL_DATABASE:
            parser.error("live mode requires --confirm-live final")
        _prepare_live_environment()

    runner = DemoRunner(mode=args.mode, manifest_path=args.manifest, workers=args.workers)
    if args.command == "list":
        payload = {
            "database": runner.catalog.database,
            "evaluation_date": runner.catalog.evaluation_date,
            "cases": [case.public_summary() for case in runner.catalog.cases],
        }
        _emit(payload, as_json=args.json, output=args.output)
        return 0

    if args.command == "run":
        result = runner.run_case(args.case_id)
        _emit(result, as_json=args.json, output=args.output)
        return 0 if result["success"] and result["matched_expectations"] else 1

    result = runner.run_batch()
    _emit(result, as_json=args.json, output=args.output)
    return 0 if result["failed"] == 0 and result["matched_expectations"] == 20 else 1


def _prepare_live_environment() -> None:
    """Load credentials only after explicit live confirmation."""

    from dotenv import load_dotenv

    load_dotenv()
    for gcp, plain in (
        ("GCP_MYSQL_HOST", "MYSQL_HOST"),
        ("GCP_MYSQL_USER", "MYSQL_USER"),
        ("GCP_MYSQL_PASSWORD", "MYSQL_PASSWORD"),
        ("GCP_MYSQL_PORT", "MYSQL_PORT"),
        ("GCP_MYSQL_DATABASE", "MYSQL_DATABASE"),
        ("GCP_MYSQL_CONNECT_TIMEOUT", "MYSQL_CONNECT_TIMEOUT"),
    ):
        if os.getenv(gcp) and not os.getenv(plain):
            os.environ[plain] = os.environ[gcp]
    os.environ.setdefault("MYSQL_DATABASE", FINAL_DATABASE)
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    if endpoint:
        parts = urlsplit(endpoint)
        os.environ["AZURE_OPENAI_ENDPOINT"] = f"{parts.scheme}://{parts.netloc}/"
    os.environ.setdefault("AZURE_OPENAI_DEPLOYMENT", "gpt-5.4")


def _print(payload: object, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    if isinstance(payload, dict) and "cases" in payload and "requested" in payload:
        print(
            f"{payload['matched_expectations']}/{payload['requested']} cases matched "
            f"({payload['failed']} execution failures, {payload['elapsed_ms']} ms)"
        )
        for result in payload["cases"]:
            marker = "PASS" if result["success"] and result["matched_expectations"] else "FAIL"
            print(f"{marker} {result['case_id']} ({result['timings_ms']['total']} ms)")
        return
    if isinstance(payload, dict) and "cases" in payload:
        for case in payload["cases"]:
            print(
                f"{case['case_id']}: {case['selected_order_id']} -> "
                f"{case['expectations']['route']} / {case['expectations']['outcome']}"
            )
        return
    print(json.dumps(payload, indent=2, sort_keys=True, default=str))


def _emit(payload: object, *, as_json: bool, output: Path | None) -> None:
    _print(payload, as_json=as_json)
    if output is None:
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
