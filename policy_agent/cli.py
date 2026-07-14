from __future__ import annotations

import argparse
import sys
from collections import Counter

from .azure_agent import AzurePolicyAgents
from .cloud_db import GCPRepository
from .service import PolicyAgentService


def check(_args: argparse.Namespace) -> int:
    statuses = {**AzurePolicyAgents.config_status(), **GCPRepository.config_status()}
    for name, present in statuses.items():
        print(f"{name}: {'set' if present else 'missing'}")
    if not all(statuses.values()):
        return 1

    summary = GCPRepository.from_env().check_schema()
    print(f"GCP main_db: connected ({summary['source_handoffs']} triage -> policy handoffs)")
    print(f"Required cloud tables: {summary['required_tables']} verified")
    return 0


def run(args: argparse.Namespace) -> int:
    mode = "pending" if args.pending else "all" if args.all else "trace"
    processed = PolicyAgentService.from_env().run(mode, args.trace)
    for item in processed:
        output = item.output
        print(
            f"{output.case.trace_id}: {output.decision.type} -> {output.handoff.next_agent} "
            f"(input_tokens={item.usage.input_tokens}, output_tokens={item.usage.output_tokens})"
        )

    decisions = Counter(item.output.decision.type for item in processed)
    print(f"Processed: {len(processed)}")
    for decision, count in sorted(decisions.items()):
        print(f"{decision}: {count}")
    return 0


def reset(_args: argparse.Namespace) -> int:
    counts = GCPRepository.from_env().reset_policy_agent_data()
    print("GCP main_db reset to the triage -> policy_agent baseline")
    for name, count in counts.items():
        print(f"{name}: {count}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the Azure-backed Policy Agent against GCP main_db.")
    commands = parser.add_subparsers(dest="command", required=True)

    check_parser = commands.add_parser("check", help="Verify Azure settings and the GCP main_db contract.")
    check_parser.set_defaults(func=check)

    run_parser = commands.add_parser("run", help="Process triage handoffs from GCP main_db.")
    target = run_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pending", action="store_true", help="Process workflows currently waiting at policy_agent.")
    target.add_argument("--all", action="store_true", help="Reprocess and upsert every triage -> policy handoff.")
    target.add_argument("--trace", help="Reprocess one trace ID.")
    run_parser.set_defaults(func=run)

    reset_parser = commands.add_parser(
        "reset",
        help="Delete Policy Agent artifacts and restore the triage-only cloud baseline.",
    )
    reset_parser.add_argument(
        "--confirm",
        required=True,
        choices=["main_db"],
        help="Required acknowledgement of the cloud database being reset.",
    )
    reset_parser.set_defaults(func=reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
