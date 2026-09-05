import json
import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
from engine.matcher import DeterministicMatcher

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

with open(os.path.join(DATA_DIR, "ground_truth.json")) as f:
    ground_truth = json.load(f)

matcher = DeterministicMatcher(DATA_DIR)
reconciled, unmatched, metrics = matcher.run()

# 1. Throughput Assertion (Must be fast)
assert metrics["runtime_seconds"] < 0.20, f"Matcher too slow: {metrics['runtime_seconds']}s"
assert metrics["throughput_records_per_sec"] > 200, f"Low throughput: {metrics['throughput_records_per_sec']} rec/s"

# 2. Count Assertions
# Clean (44) + Bulk (8) = 52 Reconciled
assert len(reconciled) == 52, f"Expected 52 reconciled records, got {len(reconciled)}"
# Fee Overcharge (4) + Timing Lag (3) + TDS (4) + Orphan (2) = 13 Unmatched Exceptions
assert len(unmatched) == 13, f"Expected 13 unmatched exceptions, got {len(unmatched)}"

# 3. Precision Check: Assert 0 False Positives
# Every single reconciled record MUST be tagged as CLEAN_1TO1 or BULK_NTO1 in ground truth
gt_dict = {entry.get("order_id") or entry.get("bank_txn_id"): entry for entry in ground_truth}

for rec in reconciled:
    oid = rec["order_id"]
    gt = gt_dict.get(oid)
    assert gt is not None, f"Reconciled order {oid} not found in ground truth!"
    assert gt["expected_status"] in ["CLEAN_1TO1", "BULK_NTO1"], (
        f"FALSE POSITIVE! Order {oid} with expected status {gt['expected_status']} "
        f"was falsely reconciled as {rec['reconciliation_type']}!"
    )

# 4. Exception Breakdown Check
stages = {}
for u in unmatched:
    stg = u["discrepancy_stage"]
    stages[stg] = stages.get(stg, 0) + 1

expected_stages = {
    "TIER_1_MDR_COMPLIANCE_VIOLATION": 4,  # Fee overcharges
    "TIER_1_MISSING_BANK_RECORD": 3,       # Timing lags
    "TIER_1_NET_CREDIT_MISMATCH": 4,       # TDS withholdings
    "TIER_4_UNMAPPED_BANK_ORPHAN": 2       # True bank orphans
}

assert stages == expected_stages, f"Exception stage mismatch: {stages} != {expected_stages}"

print("==========================================================")
print(" CHECKPOINT 2 PASSED: DETERMINISTIC SPINE VERIFIED")
print("==========================================================")
print(f" Reconciled Records:        {len(reconciled)} (44 clean 1:1 + 8 bulk N:1)")
print(f" Quarantined Exceptions:    {len(unmatched)} records")
print(f" False Positives (FP):      0 records (100% Precision)")
print(f" Throughput:                {metrics['throughput_records_per_sec']} records/sec")
print(f" Execution Latency:         {metrics['runtime_seconds'] * 1000:.2f} ms")
print("==========================================================")