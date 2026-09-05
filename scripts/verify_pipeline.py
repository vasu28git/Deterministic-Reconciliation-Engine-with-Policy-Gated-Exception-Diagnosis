import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from engine.matcher import DeterministicMatcher
from agent.diagnostician import LLMDiagnostician, DiagnosticReport
from agent.policy_gate import PolicyGate

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

# 1. Run Deterministic Matcher
matcher = DeterministicMatcher(DATA_DIR)
reconciled, unmatched, stats = matcher.run()
assert len(unmatched) == 13, f"Expected 13 unmatched, got {len(unmatched)}"

# 2. Run LLM Diagnostician
diagnostician = LLMDiagnostician()
diagnoses = diagnostician.diagnose_batch(unmatched)
assert len(diagnoses) == 13, f"Expected 13 diagnoses, got {len(diagnoses)}"

# 3. Run Policy Gate
gate = PolicyGate()
resolved_tds, honest_exceptions = gate.process_all_exceptions(unmatched, diagnoses)

# -------------------------------------------------------------
# AUDIT SUITE
# -------------------------------------------------------------
# 1. Exactly 4 TDS records resolved with balanced double-entry splits
assert len(resolved_tds) == 4, f"Expected 4 resolved TDS entries, got {len(resolved_tds)}"
for entry in resolved_tds:
    assert abs(entry["double_entry"]["delta_paise"]) <= 1, f"Invariant broken in entry: {entry}"
    assert entry["status"] == "RECONCILED_WITH_TDS_SPLIT"
    assert entry["rule_id"] == "RULE_SEC194H_TDS_ASSERTION"
    assert entry["reason_code"] == "TDS_10PCT_WITHHELD_VERIFIED"

# 2. Exactly 9 exceptions quarantined in Honest Exception Ledger
assert len(honest_exceptions) == 9, f"Expected 9 honest exceptions, got {len(honest_exceptions)}"

# 3. Exception Reason Code Breakdown
reason_codes = {}
for exc in honest_exceptions:
    code = exc["reason_code"]
    reason_codes[code] = reason_codes.get(code, 0) + 1

expected_codes = {
    "CONFIRMED_GATEWAY_FEE_LEAKAGE": 4,
    "SETTLEMENT_IN_TRANSIT_T2_LAG": 3,
    "UNMAPPED_BANK_DEPOSIT_ORPHAN": 2
}
assert reason_codes == expected_codes, f"Reason code mismatch: {reason_codes} != {expected_codes}"

# -------------------------------------------------------------
# TEST 1: REPLAY ATTACK (TRANSACTIONAL IDEMPOTENCY)
# -------------------------------------------------------------
replay_result = gate.evaluate_and_enforce(unmatched[0], diagnoses[0])
assert replay_result["action"] == "REJECTED_DUPLICATE", "Security failure: duplicate commit permitted!"
assert replay_result["reason_code"] == "IDEMPOTENCY_COLLISION"

# -------------------------------------------------------------
# TEST 2: ADVERSARIAL ARITHMETIC HALLUCINATION
# -------------------------------------------------------------
# LLM claims 10% TDS on a 25% discrepancy
fake_arithmetic_exc = {
    "order_id": "ord_fake_arithmetic",
    "discrepancy_stage": "TIER_1_NET_CREDIT_MISMATCH",
    "gross_amount": 100000,   # ₹1,000.00
    "variance_paise": 25000,  # ₹250.00 (25%, NOT 10%)
    "billed_fee": 0,
    "billed_tax": 0,
    "bank_credit": 75000
}
fake_arithmetic_diag = DiagnosticReport(
    record_id="ord_fake_arithmetic",
    discrepancy_category="TDS_WITHHOLDING",
    suspected_variance_paise=25000,
    confidence_score=0.99,
    diagnostic_rationale="Hallucinated claim of 10% TDS on an arbitrary 25% delta.",
    recommended_action="POST_TDS_SPLIT_ENTRY"
)
test_gate_1 = PolicyGate()
res_1 = test_gate_1.evaluate_and_enforce(fake_arithmetic_exc, fake_arithmetic_diag)

assert res_1["action"] == "QUARANTINED", "Policy gate permitted hallucinated arithmetic!"
assert res_1["entry"]["reason_code"] == "POLICY_REJECTED_TDS_ARITHMETIC_FAILURE"

# -------------------------------------------------------------
# TEST 3: ADVERSARIAL STAGE MISMATCH (COINCIDENTAL 10% ATTACK)
# -------------------------------------------------------------
# A fee-overcharge record where the variance coincidentally equals 10% of gross
fake_stage_exc = {
    "order_id": "ord_fake_stage_attack",
    "discrepancy_stage": "TIER_1_MDR_COMPLIANCE_VIOLATION",  # Overcharge stage
    "gross_amount": 100000,   # ₹1,000.00
    "variance_paise": 10000,  # Exactly 10,000 paise (10% coincidentally)
    "billed_fee": 11800,
    "billed_tax": 0,
    "bank_credit": 88200
}
fake_stage_diag = DiagnosticReport(
    record_id="ord_fake_stage_attack",
    discrepancy_category="TDS_WITHHOLDING",  # LLM mistakenly classifies as TDS
    suspected_variance_paise=10000,
    confidence_score=0.95,
    diagnostic_rationale="Coincidental 10% delta falsely classified as Section 194H TDS.",
    recommended_action="POST_TDS_SPLIT_ENTRY"
)
test_gate_2 = PolicyGate()
res_2 = test_gate_2.evaluate_and_enforce(fake_stage_exc, fake_stage_diag)

assert res_2["action"] == "QUARANTINED", "Security flaw: Policy gate permitted coincidental TDS match on invalid stage!"
assert res_2["entry"]["reason_code"] == "POLICY_REJECTED_TDS_STAGE_MISMATCH"

print("==========================================================")
print(" CHECKPOINT 3 PASSED: HARDENED PROPOSER-VERIFIER AUDITED")
print("==========================================================")
print(f" Deterministic Reconciled:   {len(reconciled)} (Clean 1:1 + Bulk N:1)")
print(f" AI + Gate Reconciled (TDS): {len(resolved_tds)} (Balanced split journal entries)")
print(f" Final Quarantined Ledger:   {len(honest_exceptions)} (Fee leaks, timing, orphans)")
print(f" Total Records Accounted:    {len(reconciled) + len(resolved_tds) + len(honest_exceptions)} / 65")
print(f" Transactional Idempotency:  PASSED (Replay collisions blocked)")
print(f" Adversarial Math Invariant: PASSED (Intercepted 25% delta hallucination)")
print(f" Adversarial Stage Guard:    PASSED (Intercepted coincidental 10% fee breach)")
print("==========================================================")