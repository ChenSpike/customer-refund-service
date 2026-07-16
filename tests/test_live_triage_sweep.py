"""
Live 20-case triage sweep — the triage-side counterpart of Derrick's
Policy Agent 20-case test.

Ground truth comes from the shared main_db `tickets` table itself: each of
Derrick's 20 seeded tickets carries the customer's raw_text (input) and the
expected refund_reason (label). Tickets with refund_reason NULL have no order
ID in the text, so correct triage behavior is to ask for it (his
`request_info` route).

Per-case expected outcome sets (tolerant where the case is genuinely ambiguous):
  - reason label            → classified reason must match
  - NULL reason             → "awaiting" (clarification question)
  - POL-012 (injection txt) → "damaged" OR "content_filter_blocked"
                              (Azure's filter is probabilistic)
  - POL-013 / POL-018       → label OR "awaiting" (both mention the
                              non-existent ORD-POL-999; chasing it lands in
                              Case B, which is also correct triage behavior)

Prints a per-case report with token usage. Self-cleans all rows it wrote.
Marked `live`: ~20 runs x 1-2 Azure calls, a few minutes and real API spend.
"""
import os

import mysql.connector
import pytest
from dotenv import load_dotenv

from db import backend

load_dotenv()

# ticket_id -> set of acceptable outcomes (see module docstring).
# The seeded refund_reason is the ticket's FINAL label; a few cases can't be
# resolved in a single triage turn, so those accept the correct single-turn
# behavior too:
SPECIAL_EXPECTATIONS = {
    # text contains NO order ID — single-turn triage must ask for it
    "TICKET-POL-004": {"doesnt_like_it", "awaiting"},
    # Azure's jailbreak filter is probabilistic on the injection wording
    "TICKET-POL-012": {"damaged", "content_filter_blocked"},
    # mentions the non-existent ORD-POL-999 → chasing it lands in Case B
    "TICKET-POL-013": {"doesnt_like_it", "awaiting"},
    "TICKET-POL-018": {"damaged", "awaiting"},
    # refund-STATUS inquiry ("has my refund been issued?") — no stated reason;
    # the 4-reason enum has no such intent, so the fallback is acceptable
    "TICKET-POL-020": {"damaged", "doesnt_like_it"},
}


def _main_db_conn():
    return mysql.connector.connect(
        host=os.environ["GCP_MYSQL_HOST"],
        user=os.environ["GCP_MYSQL_USER"],
        password=os.environ["GCP_MYSQL_PASSWORD"],
        database=os.environ.get("GCP_MYSQL_DATABASE", "main_db"),
        connection_timeout=int(os.environ.get("GCP_MYSQL_CONNECT_TIMEOUT", "5")),
    )


def _cleanup(pairs: list[tuple[str, str]]) -> None:
    """Delete the sweep's own rows, child tables first."""
    if not pairs:
        return
    conn = _main_db_conn()
    cur = conn.cursor()
    for trace_id, ticket_id in pairs:
        cur.execute("DELETE FROM agent_handoffs WHERE trace_id=%s", (trace_id,))
        cur.execute("DELETE FROM governance_events WHERE trace_id=%s", (trace_id,))
        cur.execute("DELETE FROM audit_log WHERE trace_id=%s", (trace_id,))
        cur.execute("DELETE FROM workflow_runs WHERE trace_id=%s", (trace_id,))
        cur.execute("DELETE FROM tickets WHERE ticket_id=%s", (ticket_id,))
    conn.commit()
    cur.close()
    conn.close()


def _outcome(patch: dict) -> str:
    if patch.get("content_filter_blocked"):
        return "content_filter_blocked"
    if patch.get("awaiting_order_id"):
        return "awaiting"
    return patch["triage_output"]["customer_request"]["refund_reason"]


@pytest.mark.live
def test_live_triage_sweep_over_derricks_20_cases(monkeypatch):
    monkeypatch.setenv("DB_BACKEND", "mysql")
    backend.reset_backend_cache()
    if backend.active_backend() != "mysql":
        pytest.skip("GCP main_db unreachable")

    from agents.triage_agent import triage_node

    cases = backend.query_all(
        "SELECT ticket_id, customer_id, raw_text, refund_reason "
        "FROM tickets WHERE ticket_id LIKE 'TICKET-POL-%' ORDER BY ticket_id")
    assert len(cases) == 20, "expected Derrick's 20 seeded tickets"

    written: list[tuple[str, str]] = []
    results = []
    try:
        for case in cases:
            expected = SPECIAL_EXPECTATIONS.get(
                case["ticket_id"],
                {case["refund_reason"]} if case["refund_reason"] else {"awaiting"},
            )
            patch = triage_node({"user_id": case["customer_id"],
                                 "message": case["raw_text"]})
            written.append((patch["trace_id"], patch["ticket_id"]))
            got = _outcome(patch)
            results.append({
                "ticket": case["ticket_id"],
                "expected": expected,
                "got": got,
                "ok": got in expected,
                "tokens": (patch.get("llm_input_tokens", 0),
                           patch.get("llm_output_tokens", 0)),
            })

        # ── report card (printed AND persisted, like Derrick's report) ───────
        total_in = sum(r["tokens"][0] for r in results)
        total_out = sum(r["tokens"][1] for r in results)
        passed = sum(r["ok"] for r in results)
        lines = ["=== Triage 20-case sweep (ground truth: main_db.tickets) ==="]
        for r in results:
            mark = "PASS" if r["ok"] else "FAIL"
            lines.append(f"  {r['ticket']}  {mark}  got={r['got']:32} "
                         f"expected={'|'.join(sorted(r['expected'])):40} "
                         f"tokens={r['tokens'][0]}/{r['tokens'][1]}")
        lines.append(f"  score: {passed}/20   "
                     f"total tokens in/out: {total_in}/{total_out}")
        report = "\n".join(lines)
        print("\n" + report)

        from pathlib import Path
        report_dir = Path(__file__).parent.parent / "reports"
        report_dir.mkdir(exist_ok=True)
        (report_dir / "triage_sweep_report.txt").write_text(report + "\n")

        failed = [r["ticket"] for r in results if not r["ok"]]
        assert not failed, f"triage outcome mismatch on: {failed}"
    finally:
        _cleanup(written)
        backend.reset_backend_cache()
