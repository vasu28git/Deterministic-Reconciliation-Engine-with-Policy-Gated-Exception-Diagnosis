"""
Checkpoint 1 — Data Integrity Assertion Script
Run this before building the matching engine.
All assertions must pass before proceeding.
"""

import json
import os
import sys

DATA_DIR = os.path.join(os.path.dirname(__file__), "..", "data")

def load(filename):
    with open(os.path.join(DATA_DIR, filename), encoding="utf-8") as f:
        return json.load(f)

def check_no_floats(data, filename):
    """Recursively scan every value in the JSON and confirm no floats exist."""
    def scan(obj, path):
        if isinstance(obj, float):
            print(f"  FAIL — float found in {filename} at path: {path} = {obj}")
            return False
        elif isinstance(obj, dict):
            return all(scan(v, f"{path}.{k}") for k, v in obj.items())
        elif isinstance(obj, list):
            return all(scan(item, f"{path}[{i}]") for i, item in enumerate(obj))
        return True
    return scan(data, filename)

print("=" * 55)
print("CHECKPOINT 1 — DATA INTEGRITY ASSERTIONS")
print("=" * 55)

orders     = load("orders.json")
settlements = load("settlements.json")
bank_feed  = load("bank_feed.json")
ground_truth = load("ground_truth.json")

all_passed = True

# --------------------------------------------------
# CHECK 1: No floating-point values in any file
# --------------------------------------------------
print("\n[CHECK 1] No floating-point values in any JSON file...")

files = {
    "orders.json": orders,
    "settlements.json": settlements,
    "bank_feed.json": bank_feed,
    "ground_truth.json": ground_truth
}

float_clean = True
for filename, data in files.items():
    result = check_no_floats(data, filename)
    if result:
        print(f"  PASS — {filename} contains no floats")
    else:
        float_clean = False

if not float_clean:
    all_passed = False
else:
    print("  ALL FILES: integer-paise only. No IEEE-754 risk.")

# --------------------------------------------------
# CHECK 2: CLEAN_1TO1 — gross - fee - tax == net_amount
# --------------------------------------------------
print("\n[CHECK 2] CLEAN_1TO1 — gross - fee - tax == net_amount...")

# Build a lookup: order_id -> settlement record
settlement_map = {s["order_id"]: s for s in settlements}

clean_records = [r for r in ground_truth if r["expected_status"] == "CLEAN_1TO1"]
print(f"  Found {len(clean_records)} CLEAN_1TO1 records to check.")

clean_passed = 0
clean_failed = 0

for record in clean_records:
    order_id = record["order_id"]
    s = settlement_map.get(order_id)

    if s is None:
        print(f"  FAIL — No settlement found for {order_id}")
        clean_failed += 1
        all_passed = False
        continue

    gross = s["gross_amount"]
    fee   = s["fee"]
    tax   = s["tax"]
    net   = s["net_amount"]
    expected_net = gross - fee - tax

    if expected_net != net:
        print(f"  FAIL — {order_id}: gross({gross}) - fee({fee}) - tax({tax}) = {expected_net}, but net_amount = {net}")
        clean_failed += 1
        all_passed = False
    else:
        clean_passed += 1

print(f"  PASS: {clean_passed} | FAIL: {clean_failed}")

# --------------------------------------------------
# CHECK 3: BULK_NTO1 — 8 net amounts sum == bank deposit
# --------------------------------------------------
print("\n[CHECK 3] BULK_NTO1 — sum of 8 net amounts == single bank deposit...")

BULK_SETTLEMENT_ID = "set_bulk_990"
BULK_UTR           = "CMS99482019482"

bulk_records = [r for r in ground_truth if r["expected_status"] == "BULK_NTO1"]
print(f"  Found {len(bulk_records)} BULK_NTO1 records.")

# Sum the net amounts from settlements
bulk_order_ids = [r["order_id"] for r in bulk_records]
bulk_nets = []

for order_id in bulk_order_ids:
    s = settlement_map.get(order_id)
    if s is None:
        print(f"  FAIL — No settlement found for bulk order {order_id}")
        all_passed = False
        continue
    if s["settlement_id"] != BULK_SETTLEMENT_ID:
        print(f"  FAIL — {order_id} has wrong settlement_id: {s['settlement_id']}")
        all_passed = False
        continue
    bulk_nets.append(s["net_amount"])

sum_of_nets = sum(bulk_nets)

# Find the matching bank deposit by UTR
bank_entry = next((b for b in bank_feed if b["bank_utr"] == BULK_UTR), None)

if bank_entry is None:
    print(f"  FAIL — No bank entry found with UTR: {BULK_UTR}")
    all_passed = False
else:
    bank_credit = bank_entry["credit_amount"]
    if sum_of_nets == bank_credit:
        print(f"  PASS — Sum of 8 net amounts ({sum_of_nets} paise) == bank deposit ({bank_credit} paise)")
    else:
        print(f"  FAIL — Sum of nets = {sum_of_nets} paise, but bank deposit = {bank_credit} paise")
        print(f"         Difference: {abs(sum_of_nets - bank_credit)} paise")
        all_passed = False

# --------------------------------------------------
# FINAL RESULT
# --------------------------------------------------
print("\n" + "=" * 55)
if all_passed:
    print("ALL CHECKS PASSED — Data is clean. Safe to proceed.")
else:
    print("ONE OR MORE CHECKS FAILED — Fix data before proceeding.")
    sys.exit(1)
print("=" * 55)