import json
import os
import random
from datetime import datetime, timedelta

# Enforce determinism for reproducible evaluation
random.seed(77)  # Test run 2 — new seed, larger dataset

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
os.makedirs(DATA_DIR, exist_ok=True)

ORDERS = []
SETTLEMENTS = []
BANK_FEED = []
GROUND_TRUTH = []

# Base parameters
START_TIME = datetime(2026, 9, 1, 9, 0, 0)
RUNNING_BALANCE = 50000000  # Starting balance: ₹5,00,000.00 (in paise)

# Contracted Merchant Rate Sheet (MDR)
CONTRACT_RATES = {
    "upi": 0.00,
    "debit_card": 0.009,       # 0.9%
    "credit_card": 0.018,      # 1.8%
    "corporate_card": 0.028    # 2.8%
}

def calculate_fee_and_tax(gross_paise: int, method: str, override_rate: float = None):
    rate = override_rate if override_rate is not None else CONTRACT_RATES[method]
    fee_paise = int(gross_paise * rate)
    # 18% statutory GST assessed on the MDR fee only
    tax_paise = int(fee_paise * 0.18)
    net_paise = gross_paise - fee_paise - tax_paise
    return fee_paise, tax_paise, net_paise

order_counter = 1000
payment_counter = 2000
bank_counter = 5000
settlement_counter = 300

# ---------------------------------------------------------
# 1. GENERATE 112 CLEAN 1:1 TRANSACTIONS  (112 + 8 bulk = 120 matched)
# ---------------------------------------------------------
for _ in range(112):
    order_counter += 1
    payment_counter += 1
    bank_counter += 1
    settlement_counter += 1

    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    settlement_id = f"set_{settlement_counter}"
    utr = f"CMS{random.randint(100000000, 999999999)}"
    
    method = random.choice(["upi", "debit_card", "credit_card", "corporate_card"])
    gross_paise = random.randint(500, 15000) * 100  # ₹500 to ₹15,000
    fee_paise, tax_paise, net_paise = calculate_fee_and_tax(gross_paise, method)
    
    txn_time = START_TIME + timedelta(minutes=random.randint(5, 300))
    settle_time = txn_time + timedelta(hours=18)
    
    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_{random.randint(100, 999)}",
        "customer_name": f"Customer {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": txn_time.isoformat(),
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": fee_paise,
        "tax": tax_paise,
        "net_amount": net_paise,
        "settlement_id": settlement_id,
        "settlement_utr": utr,
        "settled_at": settle_time.isoformat()
    })

    RUNNING_BALANCE += net_paise
    BANK_FEED.append({
        "bank_txn_id": f"bnk_{bank_counter}",
        "credit_amount": net_paise,
        "bank_utr": utr,
        "narration": f"NEFT-RZPX-{settlement_id}-{utr}",
        "value_date": settle_time.strftime("%Y-%m-%d"),
        "balance": RUNNING_BALANCE
    })

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": f"bnk_{bank_counter}",
        "expected_status": "CLEAN_1TO1",
        "expected_variance_paise": 0,
        "notes": "Exact 1:1 match across OMS, Settlement, and Bank Feed"
    })

# ---------------------------------------------------------
# NOISE INJECTION: String truncation on first 4 bank entries
# Doc P61: "Introduce character clipping and whitespace variations
# in bank narrations (such as truncating NEFT-RZPX-994021482-CORP
# to NEFT-RZPX-99402...) to evaluate fuzzy token matching."
# The bank_utr field is always intact for UTR exact-match;
# only the free-text narration string is clipped to force the
# fuzzy token matching tier into action during the demo.
# ---------------------------------------------------------
for i in range(min(4, len(BANK_FEED))):
    original = BANK_FEED[i]["narration"]
    # Clip at character 20 and append ellipsis — mirrors production
    # truncation from banking system narration field width limits.
    BANK_FEED[i]["narration"] = original[:20] + "..."

# ---------------------------------------------------------
# 2. GENERATE 8 BULK BATCHED TRANSACTIONS (N:1 AGGREGATION)
# ---------------------------------------------------------
bulk_settlement_id = "set_bulk_990"
bulk_utr = "CMS99482019482"
bulk_net_total = 0
bulk_order_ids = []

bulk_time = START_TIME + timedelta(hours=24)
bulk_settle_time = bulk_time + timedelta(hours=12)

for i in range(8):
    order_counter += 1
    payment_counter += 1
    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    bulk_order_ids.append(order_id)

    method = random.choice(["upi", "credit_card", "debit_card"])
    gross_paise = random.randint(800, 3000) * 100
    fee_paise, tax_paise, net_paise = calculate_fee_and_tax(gross_paise, method)
    bulk_net_total += net_paise

    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_{random.randint(100, 999)}",
        "customer_name": f"Bulk Buyer {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": (bulk_time + timedelta(minutes=i*7)).isoformat(),  # staggered slightly
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": fee_paise,
        "tax": tax_paise,
        "net_amount": net_paise,
        "settlement_id": bulk_settlement_id,
        "settlement_utr": bulk_utr,
        "settled_at": bulk_settle_time.isoformat()
    })

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": f"bnk_{bank_counter + 1}",  # all 8 point to same bulk bank entry
        "expected_status": "BULK_NTO1",
        "expected_variance_paise": 0,
        "notes": f"Part of N:1 batch payout {bulk_settlement_id}"
    })

bank_counter += 1
RUNNING_BALANCE += bulk_net_total
BANK_FEED.append({
    "bank_txn_id": f"bnk_{bank_counter}",
    "credit_amount": bulk_net_total,
    "bank_utr": bulk_utr,
    "narration": f"NEFT-RZPX-BULK-{bulk_settlement_id}-{bulk_utr}",
    "value_date": bulk_settle_time.strftime("%Y-%m-%d"),
    "balance": RUNNING_BALANCE
})

# ---------------------------------------------------------
# 3a. EDGE CASE: 5 CREDIT CARD FEE OVERCHARGE (2.5% billed vs 1.8% contracted)
# ---------------------------------------------------------
for _ in range(5):
    order_counter += 1
    payment_counter += 1
    bank_counter += 1
    settlement_counter += 1

    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    settlement_id = f"set_{settlement_counter}"
    utr = f"CMS{random.randint(100000000, 999999999)}"

    # Contracted rate: 1.8%, Injected billing rate: 2.5% (Overcharge)
    method = "credit_card"
    gross_paise = random.randint(10000, 25000) * 100
    legit_fee, legit_tax, legit_net = calculate_fee_and_tax(gross_paise, method)
    billed_fee, billed_tax, billed_net = calculate_fee_and_tax(gross_paise, method, override_rate=0.025)
    leakage_paise = (billed_fee + billed_tax) - (legit_fee + legit_tax)

    txn_time = START_TIME + timedelta(hours=36)
    settle_time = txn_time + timedelta(hours=14)

    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_{random.randint(100, 999)}",
        "customer_name": f"Corporate Client {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": txn_time.isoformat(),
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": billed_fee,
        "tax": billed_tax,
        "net_amount": billed_net,
        "settlement_id": settlement_id,
        "settlement_utr": utr,
        "settled_at": settle_time.isoformat()
    })

    RUNNING_BALANCE += billed_net
    BANK_FEED.append({
        "bank_txn_id": f"bnk_{bank_counter}",
        "credit_amount": billed_net,
        "bank_utr": utr,
        "narration": f"NEFT-RZPX-{settlement_id}-{utr}",
        "value_date": settle_time.strftime("%Y-%m-%d"),
        "balance": RUNNING_BALANCE
    })

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": f"bnk_{bank_counter}",
        "expected_status": "FEE_OVERCHARGE",
        "expected_variance_paise": leakage_paise,
        "notes": f"Gateway billed 2.5% MDR instead of contracted 1.8%. Fee leakage: {leakage_paise} paise"
    })

# ---------------------------------------------------------
# 3b. EDGE CASE: 4 UPI FEE OVERCHARGE (UPI = 0% MDR, any fee is 100% illegal)
# ---------------------------------------------------------
for _ in range(4):
    order_counter += 1
    payment_counter += 1
    bank_counter += 1
    settlement_counter += 1

    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    settlement_id = f"set_{settlement_counter}"
    utr = f"CMS{random.randint(100000000, 999999999)}"

    # UPI contracted rate = 0.0%, but gateway charged 0.9% (debit card rate — billing error)
    method = "upi"
    gross_paise = random.randint(5000, 12000) * 100
    legit_fee, legit_tax, legit_net = calculate_fee_and_tax(gross_paise, method)  # 0 fee
    billed_fee, billed_tax, billed_net = calculate_fee_and_tax(gross_paise, method, override_rate=0.009)
    leakage_paise = (billed_fee + billed_tax) - (legit_fee + legit_tax)  # full overcharge

    txn_time = START_TIME + timedelta(hours=10)
    settle_time = txn_time + timedelta(hours=8)

    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_{random.randint(100, 999)}",
        "customer_name": f"UPI Merchant {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": txn_time.isoformat(),
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": billed_fee,
        "tax": billed_tax,
        "net_amount": billed_net,
        "settlement_id": settlement_id,
        "settlement_utr": utr,
        "settled_at": settle_time.isoformat()
    })

    RUNNING_BALANCE += billed_net
    BANK_FEED.append({
        "bank_txn_id": f"bnk_{bank_counter}",
        "credit_amount": billed_net,
        "bank_utr": utr,
        "narration": f"UPI-RZPX-{settlement_id}-{utr}",
        "value_date": settle_time.strftime("%Y-%m-%d"),
        "balance": RUNNING_BALANCE
    })

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": f"bnk_{bank_counter}",
        "expected_status": "FEE_OVERCHARGE",
        "expected_variance_paise": leakage_paise,
        "notes": f"EDGE CASE: UPI has 0% contracted MDR. Gateway illegally charged debit-card rate (0.9%). Full overcharge: {leakage_paise} paise"
    })

# ---------------------------------------------------------
# 4. GENERATE 3 TIMING LAGS (T+2 SETTLEMENT IN-TRANSIT) — Weekend Cutoff Edge Case
# ---------------------------------------------------------
for _ in range(3):
    order_counter += 1
    payment_counter += 1
    settlement_counter += 1

    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    settlement_id = f"set_{settlement_counter}"
    utr = f"CMS{random.randint(100000000, 999999999)}"

    method = "upi"
    gross_paise = random.randint(2000, 6000) * 100
    fee_paise, tax_paise, net_paise = calculate_fee_and_tax(gross_paise, method)

    # Friday transaction settling next Tuesday (after reporting cut-off)
    txn_time = datetime(2026, 9, 4, 21, 30, 0)
    settle_time = datetime(2026, 9, 8, 11, 0, 0)

    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_{random.randint(100, 999)}",
        "customer_name": f"Late Buyer {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": txn_time.isoformat(),
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": fee_paise,
        "tax": tax_paise,
        "net_amount": net_paise,
        "settlement_id": settlement_id,
        "settlement_utr": utr,
        "settled_at": settle_time.isoformat()
    })

    # Intentionally omitted from bank_feed — simulates in-transit timing lag
    # bank_txn_id is None because no bank entry exists yet

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": None,  # no bank entry exists yet — correctly None
        "expected_status": "TIMING_LAG",
        "expected_variance_paise": net_paise,
        "notes": "Settlement in transit across weekend window (T+2 cutoff lag). No bank entry expected."
    })

# ---------------------------------------------------------
# 5. GENERATE 15 STATUTORY TDS WITHHOLDINGS (SECTION 194H)
#    EDGE CASES: includes extreme high-value TDS (Rs.5,00,000 invoice)
# ---------------------------------------------------------
for tds_idx in range(15):
    order_counter += 1
    payment_counter += 1
    bank_counter += 1
    settlement_counter += 1

    order_id = f"ord_{order_counter}"
    payment_id = f"pay_{payment_counter}"
    settlement_id = f"set_{settlement_counter}"
    utr = f"CMS{random.randint(100000000, 999999999)}"

    method = "corporate_card"
    # EDGE CASE: Last 3 TDS records are extreme high-value (Rs.5,00,000 invoice)
    if tds_idx >= 12:
        gross_paise = 50000000  # Rs.5,00,000 — extreme high-value B2B invoice
    else:
        gross_paise = random.randint(30000, 80000) * 100  # B2B invoice Rs.30,000 to Rs.80,000
    fee_paise, tax_paise, net_paise = calculate_fee_and_tax(gross_paise, method)

    # 10% statutory TDS withheld on gross amount under Section 194H
    tds_withholding_paise = int(gross_paise * 0.10)
    bank_credit_paise = net_paise - tds_withholding_paise

    txn_time = START_TIME + timedelta(hours=48)
    settle_time = txn_time + timedelta(hours=16)

    ORDERS.append({
        "order_id": order_id,
        "customer_id": f"cust_corp_{order_counter}",
        "customer_name": f"Enterprise Partner {order_counter}",
        "gross_amount": gross_paise,
        "currency": "INR",
        "order_timestamp": txn_time.isoformat(),
        "status": "CAPTURED"
    })

    SETTLEMENTS.append({
        "payment_id": payment_id,
        "order_id": order_id,
        "method": method,
        "gross_amount": gross_paise,
        "fee": fee_paise,
        "tax": tax_paise,
        "net_amount": net_paise,
        "settlement_id": settlement_id,
        "settlement_utr": utr,
        "settled_at": settle_time.isoformat()
    })

    RUNNING_BALANCE += bank_credit_paise
    BANK_FEED.append({
        "bank_txn_id": f"bnk_{bank_counter}",
        "credit_amount": bank_credit_paise,
        "bank_utr": utr,
        "narration": f"NEFT-RZPX-CORP-TDS194H-{settlement_id}-{utr}",
        "value_date": settle_time.strftime("%Y-%m-%d"),
        "balance": RUNNING_BALANCE
    })

    GROUND_TRUTH.append({
        "order_id": order_id,
        "payment_id": payment_id,
        "bank_txn_id": f"bnk_{bank_counter}",
        "expected_status": "TDS_WITHHOLDING",
        "expected_variance_paise": tds_withholding_paise,
        "notes": f"Enterprise counterparty withheld 10% TDS (Section 194H): {tds_withholding_paise} paise"
    })

# ---------------------------------------------------------
# 6. GENERATE 3 TRUE UNMAPPED ORPHAN CREDITS IN BANK
#    EDGE CASES: includes one with ambiguous IMPS narration (fuzzy match stress test)
# ---------------------------------------------------------
for orphan_idx in range(3):
    bank_counter += 1
    orphan_credit = random.randint(1500, 5000) * 100
    RUNNING_BALANCE += orphan_credit
    bank_txn_id = f"bnk_{bank_counter}"

    if orphan_idx == 2:
        # EDGE CASE: Ambiguous IMPS narration — no clear UTR, mimics a valid NEFT prefix
        # Tests that the fuzzy matcher does NOT accidentally link this to a real settlement
        orphan_utr = f"IMPS{random.randint(100000000, 999999999)}"
        narration = f"IMPS-CR-{orphan_utr[:8]}...-UNKNOWN"
    else:
        orphan_utr = f"UNMAPPED{random.randint(100000, 999999)}"
        narration = f"DIRECT-DEPOSIT-UNKNOWN-REF-{orphan_utr}"

    BANK_FEED.append({
        "bank_txn_id": bank_txn_id,
        "credit_amount": orphan_credit,
        "bank_utr": orphan_utr,
        "narration": narration,
        "value_date": "2026-09-03",
        "balance": RUNNING_BALANCE
    })

    GROUND_TRUTH.append({
        "order_id": None,
        "payment_id": None,
        "bank_txn_id": bank_txn_id,
        "expected_status": "TRUE_ORPHAN",
        "expected_variance_paise": orphan_credit,
        "notes": (
            "EDGE CASE: Ambiguous IMPS narration — fuzzy matcher must NOT link to a real settlement."
            if orphan_idx == 2
            else "Unmapped bank credit with no corresponding OMS order or gateway settlement."
        )
    })

# ---------------------------------------------------------
# WRITE OUTPUT FILES
# ---------------------------------------------------------
def save_file(filename, data):
    filepath = os.path.join(DATA_DIR, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    print(f"Generated {filename} ({len(data)} records)")

save_file("orders.json", ORDERS)
save_file("settlements.json", SETTLEMENTS)
save_file("bank_feed.json", BANK_FEED)
save_file("ground_truth.json", GROUND_TRUTH)

print(f"\nSynthetic data generation complete.")
print(f"Total Orders:      {len(ORDERS)}")
print(f"Total Settlements: {len(SETTLEMENTS)}")
print(f"Total Bank Feed:   {len(BANK_FEED)}")
print(f"Total Ground Truth:{len(GROUND_TRUTH)}")
print(f"\nEdge Cases Injected:")
print(f"  - 2 credit card fee overcharges (2.5% vs contracted 1.8%)")
print(f"  - 2 UPI fee overcharges (UPI=0% MDR, illegal debit-card rate charged)")
print(f"  - 2 extreme high-value TDS (Rs.5,00,000 B2B invoice)")
print(f"  - 1 ambiguous IMPS orphan (fuzzy match false-positive stress test)")