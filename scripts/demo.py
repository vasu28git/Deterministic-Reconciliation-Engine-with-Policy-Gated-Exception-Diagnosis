import json
import os
import sys

# Ensure project root is on PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from engine.matcher import DeterministicMatcher
from agent.diagnostician import LLMDiagnostician
from agent.policy_gate import PolicyGate

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")


def main():
    print("\n" + "=" * 80)
    print("        INSPECTING AI DIAGNOSTIC WORK & POLICY GATE DECISIONS")
    print("=" * 80)

    # 1. Run Matcher to get the 13 unmatched exceptions
    matcher = DeterministicMatcher(DATA_DIR)
    reconciled, unmatched, stats = matcher.run()
    print(f"\n[ENGINE STATS] Reconciled: {len(reconciled)} | Unmatched to Diagnose: {len(unmatched)}")
    print(f"[THROUGHPUT]   {stats['throughput_records_per_sec']} rec/s | "
          f"Runtime: {stats['runtime_seconds']}s | "
          f"Total Orders Processed: {stats['total_orders']}")

    # 2. Run LLM Diagnostician (live Gemini call with automatic rate-limit throttling)
    diagnostician = LLMDiagnostician()
    print(f"[ENGINE STATUS] Gemini client active: {diagnostician.client is not None}\n")
    diagnoses = diagnostician.diagnose_batch(unmatched)

    # 3. Run Policy Gate for deterministic verification & double-entry invariant enforcement
    gate = PolicyGate()

    print("\n" + "=" * 80)
    print("                      DETAILED EXCEPTION AUDIT TRACE")
    print("=" * 80)

    for idx, (exc, diag) in enumerate(zip(unmatched, diagnoses), start=1):
        result = gate.evaluate_and_enforce(exc, diag)
        action = result.get("action")

        rec_id = exc.get("order_id") or exc.get("bank_txn_id") or "UNKNOWN"
        stage = exc.get("discrepancy_stage", "N/A")
        variance = exc.get("variance_paise", 0)
        gross = exc.get("gross_amount") or 0

        print(f"\n[{idx}/13] RECORD: {rec_id}")
        print(f"  ├─ Discrepancy Stage:     {stage}")
        print(f"  ├─ Gross Transaction:     ₹{gross / 100:,.2f} ({gross} paise)")
        print(f"  ├─ Discrepancy Variance:  ₹{variance / 100:,.2f} ({variance} paise)")
        print(f"  │")
        print(f"  ├─ [AI PROPOSAL]")
        print(f"  │   ├─ Category:          {diag.discrepancy_category}")
        print(f"  │   ├─ Confidence:        {diag.confidence_score * 100:.1f}%")
        print(f"  │   ├─ Recommended Action:{diag.recommended_action}")
        print(f"  │   └─ AI Rationale:      \"{diag.diagnostic_rationale}\"")
        print(f"  │")
        print(f"  └─ [POLICY GATE DECISION]: {action}")

        if action == "COMMITTED_TO_LEDGER":
            entry = result["entry"]
            debits = entry["double_entry"]
            print(f"      ├─ Status: Balanced & Committed")
            print(
                f"      └─ Journal: Debit Bank ₹{debits['debit_cash_bank_paise']/100:,.2f} | "
                f"Debit TDS ₹{debits['debit_tds_receivable_paise']/100:,.2f} | "
                f"Debit Fee+Tax ₹{(debits['debit_mdr_fee_paise'] + debits['debit_gst_tax_paise'])/100:,.2f} | "
                f"Credit Sales ₹{debits['credit_gross_sales_paise']/100:,.2f}"
            )
        else:
            entry = result.get("entry", {})
            print(f"      ├─ Reason Code: {entry.get('reason_code')}")
            print(f"      └─ Status: Quarantined to Honest Exception Ledger")
        print("-" * 80)

    print("\n>>> Inspection complete. All 13 exception diagnoses and policy gate decisions reviewed.\n")


if __name__ == "__main__":
    main()