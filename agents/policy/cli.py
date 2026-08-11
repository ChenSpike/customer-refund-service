from __future__ import annotations

import argparse
import sys
from collections import Counter

from .azure import AzureJsonClient
from .cloud_db import GCPRepository
from .policy_node import load_policy_context, load_precedent_context
from .service import PolicyAgentService


def check(_args: argparse.Namespace) -> int:
    policy_context = load_policy_context("v1.0")
    precedents = load_precedent_context("v1.0", policy_context=policy_context)
    precedent_state = "available" if precedents.available else "unavailable"
    print(f"Precedent memory: {precedent_state} ({precedents.status}; {precedents.reason})")

    statuses = {**AzureJsonClient.config_status(), **GCPRepository.config_status()}
    for name, present in statuses.items():
        print(f"{name}: {'set' if present else 'missing'}")
    if not all(statuses.values()):
        return 1

    summary = GCPRepository.from_env().check_schema()
    print(
        "GCP main_db: connected "
        f"({summary['source_handoffs']} benchmark / {summary['all_source_handoffs']} total triage -> policy handoffs)"
    )
    print(f"Required cloud tables: {summary['required_tables']} verified")
    print("Policy subgraph: policy -> policy_governance -> policy_handoff")
    print("Parent integration: policy_agent -> policy_persistence -> downstream mapper")
    return 0


def migrate(_args: argparse.Namespace) -> int:
    changed = GCPRepository.from_env().migrate_schema()
    print("GCP main_db Policy Agent schema migration verified")
    for name, applied in changed.items():
        print(f"{name}: {'applied' if applied else 'already present'}")
    return 0


def run(args: argparse.Namespace) -> int:
    mode = "pending" if args.pending else "all" if args.all else "trace"
    processed = PolicyAgentService.from_env().run(mode, args.trace)
    for item in processed:
        output = item.output
        print(
            f"{output.case.trace_id}: {output.decision.type} -> {output.handoff.next_agent} "
            f"(confidence={output.decision.confidence}/{output.decision.confidence_level}, "
            f"input_tokens={item.usage.input_tokens}, output_tokens={item.usage.output_tokens})"
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
    parser = argparse.ArgumentParser(description="Run the Azure-backed LangGraph Policy Agent on GCP main_db.")
    commands = parser.add_subparsers(dest="command", required=True)

    check_parser = commands.add_parser("check", help="Verify Azure, LangGraph, and GCP main_db configuration.")
    check_parser.set_defaults(func=check)

    migrate_parser = commands.add_parser("migrate", help="Apply the Policy Agent-owned GCP schema migration.")
    migrate_parser.add_argument("--confirm", required=True, choices=["main_db"])
    migrate_parser.set_defaults(func=migrate)

    run_parser = commands.add_parser("run", help="Process triage handoffs from GCP main_db.")
    target = run_parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--pending", action="store_true", help="Process workflows currently waiting at policy_agent.")
    target.add_argument("--all", action="store_true", help="Reprocess every triage -> policy handoff.")
    target.add_argument("--trace", help="Reprocess one trace ID.")
    run_parser.set_defaults(func=run)

    reset_parser = commands.add_parser(
        "reset",
        help="Delete benchmark Policy Agent artifacts and restore the triage-only cloud baseline.",
    )
    reset_parser.add_argument("--confirm", required=True, choices=["main_db"])
    reset_parser.set_defaults(func=reset)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
