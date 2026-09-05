import json
import os
import re
import time
from typing import Dict, List, Any, Tuple, Set, Optional

# Contracted Merchant Rate Sheet

CONTRACT_RATES = {
    "upi": 0.00,
    "debit_card": 0.009,       # 0.9%
    "credit_card": 0.018,      # 1.8%
    "corporate_card": 0.028    # 2.8%
}

class DeterministicMatcher:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.settlements: Dict[str, Dict[str, Any]] = {}
        self.bank_feed: Dict[str, Dict[str, Any]] = {}

        # Output Containers
        self.reconciled_records: List[Dict[str, Any]] = []
        self.unmatched_queue: List[Dict[str, Any]] = []

        # State Tracking Sets
        self.reconciled_order_ids: Set[str] = set()
        self.reconciled_payment_ids: Set[str] = set()
        self.touched_bank_txns: Set[str] = set()

    def load_data(self):
        with open(os.path.join(self.data_dir, "orders.json"), encoding="utf-8") as f:
            for o in json.load(f):
                self.orders[o["order_id"]] = o

        with open(os.path.join(self.data_dir, "settlements.json"), encoding="utf-8") as f:
            for s in json.load(f):
                self.settlements[s["payment_id"]] = s

        with open(os.path.join(self.data_dir, "bank_feed.json"), encoding="utf-8") as f:
            for b in json.load(f):
                self.bank_feed[b["bank_txn_id"]] = b

    def _extract_utr_from_narration(self, narration: str) -> str:
        """Extracts CMS UTR token from semi-structured bank narration."""
        match = re.search(r"(CMS\d{9,16})", narration)
        return match.group(1) if match else ""

    def _fuzzy_narration_lookup(
        self,
        settlement_utr: str,
        bank_by_narration_tokens: Dict[str, Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """
        Classical token-based fuzzy matching for truncated bank narrations.

        Doc P52: "applies classical token-based fuzzy matching for minor
        string discrepancies" — handles cases like a bank narration clipped
        to 'NEFT-RZPX-99402...' that no longer contains the full CMS UTR
        needed for exact-key lookup.

        Strategy: split the settlement UTR and each bank narration into
        alphanumeric tokens; declare a match if any narration token is a
        prefix of the settlement UTR (or vice versa) with at least
        MIN_PREFIX_LEN characters of overlap. This is conservative — it
        requires a substantial token overlap before linking, so a short
        coincidental prefix cannot accidentally link unrelated records.

        This is intentionally NOT used when the exact UTR key lookup
        succeeds — fuzzy matching only activates as a fallback to keep
        the dominant path fully deterministic.
        """
        MIN_PREFIX_LEN = 8  # minimum shared characters to be considered a match

        utr_tokens = re.findall(r"[A-Za-z0-9]+", settlement_utr)

        for narration_key, bank_entry in bank_by_narration_tokens.items():
            narr_tokens = re.findall(r"[A-Za-z0-9]+", narration_key)
            for u_tok in utr_tokens:
                for n_tok in narr_tokens:
                    # Check if one token is a prefix of the other (truncation case)
                    overlap = min(len(u_tok), len(n_tok))
                    if overlap >= MIN_PREFIX_LEN and u_tok[:overlap] == n_tok[:overlap]:
                        return bank_entry
        return None

    def subset_sum_disaggregate(
        self,
        target: int,
        candidates: List[Dict[str, Any]],
        max_group_size: int = 12
    ) -> Optional[List[Dict[str, Any]]]:
        """
        Dynamic programming subset-sum solver.

        Used ONLY for the genuinely unknown-combination case: given a bank credit
        with no UTR/narration match, find which combination of currently-unclaimed,
        individually-ungrouped settlement records sums exactly to it.

        NOTE: This is intentionally NOT used to re-verify an already-known
        settlement_utr group (that membership is already certain — verifying it
        via subset-sum with an early-exit DP risks returning a smaller partial
        subset that also happens to sum correctly, producing a false mismatch on
        the true full-group total). For known groups, plain summation is used
        instead (see `run()` Tier 2 logic).

        Finds the FIRST exact-sum subset using ALL available candidates as the
        search space; does not assume or require a specific group size. Because
        there is no "correct" group size to target here, an early exit on first
        exact match is safe: any valid combination that sums to `target` is a
        genuine candidate resolution, not a false partial match of a known group.
        """
        n = len(candidates)
        if n == 0 or target <= 0:
            return None

        # dp[achieved_sum] = list of candidate indices summing to that amount
        dp: Dict[int, List[int]] = {0: []}

        for idx, s in enumerate(candidates):
            amt = s["net_amount"]
            new_dp = dict(dp)
            for achieved_sum, idx_list in dp.items():
                if len(idx_list) >= max_group_size:
                    continue
                new_sum = achieved_sum + amt
                if new_sum > target:
                    continue
                if new_sum not in new_dp:
                    new_dp[new_sum] = idx_list + [idx]
            dp = new_dp
            if target in dp:
                break  # Safe here: any exact-sum combination is a valid resolution

        if target in dp and dp[target]:
            return [candidates[i] for i in dp[target]]
        return None

    def run(self) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
        start_time = time.perf_counter()
        self.load_data()

        # Index settlements by order_id for 3-way OMS audit
        settlements_by_order: Dict[str, Dict[str, Any]] = {
            s["order_id"]: s for s in self.settlements.values()
        }

        # -------------------------------------------------------------
        # TIER 0: OMS-TO-GATEWAY RECONCILIATION
        # -------------------------------------------------------------
        for order_id, order in self.orders.items():
            if order_id not in settlements_by_order:
                self.unmatched_queue.append({
                    "order_id": order_id,
                    "payment_id": None,
                    "bank_txn_id": None,
                    "discrepancy_stage": "TIER_0_MISSING_GATEWAY_SETTLEMENT",
                    "variance_paise": order["gross_amount"],
                    "gross_amount": order["gross_amount"],
                    "billed_fee": 0,
                    "billed_tax": 0,
                    "expected_fee": 0,
                    "expected_tax": 0,
                    "fee_diff_paise": 0,
                    "tax_diff_paise": 0,
                    "method": None,
                    "settled_at": None,
                    "narration": None,
                    "bank_credit": None,
                    "context": f"Order {order_id} captured in OMS but missing settlement event from gateway."
                })

        # Build bank lookup indices
        bank_by_utr: Dict[str, Dict[str, Any]] = {}
        # Fuzzy index: narration string → bank entry (for truncated narration fallback)
        # Only populated for entries whose narration did NOT yield a clean CMS UTR
        # via regex — those are already covered by bank_by_utr.
        bank_by_narration_tokens: Dict[str, Dict[str, Any]] = {}
        for b_id, b in self.bank_feed.items():
            narration = b.get("narration", "")
            utr = b.get("bank_utr") or self._extract_utr_from_narration(narration)
            if utr:
                bank_by_utr[utr] = b
            else:
                # Narration is truncated / regex unextractable — index by raw narration
                # string for the fuzzy token fallback tier (doc P52, P61).
                bank_by_narration_tokens[narration] = b

        # Group settlements by settlement_utr (this membership is already known/certain)
        settlements_by_utr: Dict[str, List[Dict[str, Any]]] = {}
        for p_id, s in self.settlements.items():
            utr = s["settlement_utr"]
            settlements_by_utr.setdefault(utr, []).append(s)

        # -------------------------------------------------------------
        # TIER 1 & TIER 2: SETTLEMENT-TO-BANK RECONCILIATION (known groups)
        # -------------------------------------------------------------
        for utr, s_group in settlements_by_utr.items():
            bank_entry = bank_by_utr.get(utr)

            # FUZZY FALLBACK (doc P52, P61): If UTR exact-key lookup missed,
            # try classical token-based prefix matching against bank narrations
            # that were too truncated for the CMS regex to extract a clean UTR.
            # Only fires when bank_by_narration_tokens is non-empty (i.e., when
            # there are actually truncated narration entries in this batch).
            if not bank_entry and bank_by_narration_tokens:
                bank_entry = self._fuzzy_narration_lookup(utr, bank_by_narration_tokens)
                if bank_entry:
                    # Remove from the fuzzy index so this bank entry is not
                    # reused for a different settlement group.
                    narr_key = bank_entry.get("narration", "")
                    bank_by_narration_tokens.pop(narr_key, None)

            # If UTR not found in bank feed -> TIMING LAG (In-Transit).
            # NOTE: We do NOT attempt DPSS here. This group's membership is
            # already fully known (grouped by settlement_utr) — the open
            # question is only "did the payout clear yet", not "which
            # transactions belong together". DPSS is reserved for the true
            # unknown-combination case handled separately below, after this
            # main loop, once we know which bank credits remain unclaimed.
            if not bank_entry:
                for s in s_group:
                    self.unmatched_queue.append({
                        "order_id": s["order_id"],
                        "payment_id": s["payment_id"],
                        "bank_txn_id": None,
                        "discrepancy_stage": "TIER_1_MISSING_BANK_RECORD",
                        "variance_paise": s["net_amount"],
                        "gross_amount": s["gross_amount"],
                        "billed_fee": s["fee"],
                        "billed_tax": s["tax"],
                        "expected_fee": int(s["gross_amount"] * CONTRACT_RATES.get(s["method"], 0.0)),
                        "expected_tax": int(int(s["gross_amount"] * CONTRACT_RATES.get(s["method"], 0.0)) * 0.18),
                        "fee_diff_paise": 0,
                        "tax_diff_paise": 0,
                        "method": s["method"],
                        "settled_at": s["settled_at"],
                        "narration": None,
                        "bank_credit": None,
                        "context": "Settlement record exists, but UTR was not cleared in bank statement (in-transit)."
                    })
                continue

            bank_txn_id = bank_entry["bank_txn_id"]
            self.touched_bank_txns.add(bank_txn_id)
            bank_credit = bank_entry["credit_amount"]

            # Sub-case: Bulk N:1 Group Aggregation.
            # Membership is already certain (grouped by known UTR) — verify by
            # plain summation, NOT subset-sum. Using DPSS here would risk
            # returning a smaller partial subset that also happens to sum
            # correctly, producing a false TIER_2 mismatch on a group that is
            # actually fine in full.
            if len(s_group) > 1:
                total_net_group = sum(s["net_amount"] for s in s_group)
                if total_net_group == bank_credit:
                    for s in s_group:
                        self.reconciled_records.append({
                            "order_id": s["order_id"],
                            "payment_id": s["payment_id"],
                            "bank_txn_id": bank_txn_id,
                            "reconciliation_type": "BULK_NTO1",
                            "reconciliation_method": "UTR_GROUP_SUM_MATCH",
                            "gross_amount": s["gross_amount"],
                            "net_amount": s["net_amount"],
                            "fee": s["fee"],
                            "tax": s["tax"],
                            "bank_credit": bank_credit,
                            "variance_paise": 0
                        })
                        self.reconciled_order_ids.add(s["order_id"])
                        self.reconciled_payment_ids.add(s["payment_id"])
                else:
                    self.unmatched_queue.extend([{
                        "order_id": s["order_id"],
                        "payment_id": s["payment_id"],
                        "bank_txn_id": bank_txn_id,
                        "discrepancy_stage": "TIER_2_BULK_SUM_MISMATCH",
                        "variance_paise": abs(total_net_group - bank_credit),
                        "gross_amount": s["gross_amount"],
                        "billed_fee": s["fee"],
                        "billed_tax": s["tax"],
                        "expected_fee": 0,
                        "expected_tax": 0,
                        "fee_diff_paise": 0,
                        "tax_diff_paise": 0,
                        "method": s["method"],
                        "settled_at": s["settled_at"],
                        "narration": bank_entry["narration"],
                        "bank_credit": bank_credit,
                        "context": f"Bulk sum mismatch: group total {total_net_group} != bank credit {bank_credit}."
                    } for s in s_group])
                continue

            # Sub-case: Single 1:1 Transaction Check
            s = s_group[0]
            gross = s["gross_amount"]
            method = s["method"]
            contract_rate = CONTRACT_RATES.get(method, 0.0)

            expected_fee = int(gross * contract_rate)
            expected_tax = int(expected_fee * 0.18)

            fee_diff = s["fee"] - expected_fee
            tax_diff = s["tax"] - expected_tax
            net_delta = s["net_amount"] - bank_credit

            if fee_diff != 0 or tax_diff != 0:
                self.unmatched_queue.append({
                    "order_id": s["order_id"],
                    "payment_id": s["payment_id"],
                    "bank_txn_id": bank_txn_id,
                    "discrepancy_stage": "TIER_1_MDR_COMPLIANCE_VIOLATION",
                    "variance_paise": fee_diff + tax_diff,
                    "gross_amount": gross,
                    "billed_fee": s["fee"],
                    "billed_tax": s["tax"],
                    "expected_fee": expected_fee,
                    "expected_tax": expected_tax,
                    "fee_diff_paise": fee_diff,
                    "tax_diff_paise": tax_diff,
                    "method": method,
                    "settled_at": s["settled_at"],
                    "narration": bank_entry["narration"],
                    "bank_credit": bank_credit,
                    "context": f"Fee/Tax breach: Fee diff={fee_diff}p, Tax diff={tax_diff}p."
                })
                continue

            if net_delta != 0:
                self.unmatched_queue.append({
                    "order_id": s["order_id"],
                    "payment_id": s["payment_id"],
                    "bank_txn_id": bank_txn_id,
                    "discrepancy_stage": "TIER_1_NET_CREDIT_MISMATCH",
                    "variance_paise": net_delta,
                    "gross_amount": gross,
                    "billed_fee": s["fee"],
                    "billed_tax": s["tax"],
                    "expected_fee": expected_fee,
                    "expected_tax": expected_tax,
                    "fee_diff_paise": 0,
                    "tax_diff_paise": 0,
                    "method": method,
                    "settled_at": s["settled_at"],
                    "narration": bank_entry["narration"],
                    "bank_credit": bank_credit,
                    "context": f"Settlement net ({s['net_amount']}) != Bank credit ({bank_credit}). Delta: {net_delta} paise."
                })
                continue

            self.reconciled_records.append({
                "order_id": s["order_id"],
                "payment_id": s["payment_id"],
                "bank_txn_id": bank_txn_id,
                "reconciliation_type": "CLEAN_1TO1",
                "reconciliation_method": "UTR_EXACT_MATCH",
                "gross_amount": gross,
                "net_amount": s["net_amount"],
                "fee": s["fee"],
                "tax": s["tax"],
                "bank_credit": bank_credit,
                "variance_paise": 0
            })
            self.reconciled_order_ids.add(s["order_id"])
            self.reconciled_payment_ids.add(s["payment_id"])

        # -------------------------------------------------------------
        # TIER 3: DPSS FALLBACK FOR TRUE UNKNOWN-COMBINATION MATCHING
        # Only runs against bank credits still unclaimed after Tiers 1-2,
        # searching genuinely ungrouped/unclaimed settlement records for
        # a combination that sums exactly to the credit. This is the real
        # subset-sum problem: membership is NOT already known here.
        # -------------------------------------------------------------
        unclaimed_bank_credits = [
            b for b in self.bank_feed.values()
            if b["bank_txn_id"] not in self.touched_bank_txns
        ]
        unclaimed_settlements = [
            s for s in self.settlements.values()
            if s["payment_id"] not in self.reconciled_payment_ids
        ]

        # Only settlements with NO bank match at all are legitimate DPSS
        # candidates. A settlement already flagged with a SPECIFIC known problem
        # (e.g. TIER_1_MDR_COMPLIANCE_VIOLATION, TIER_1_NET_CREDIT_MISMATCH,
        # TIER_2_BULK_SUM_MISMATCH) already found its bank entry — it must NOT
        # be reconsidered here, or a genuine fee-overcharge/mismatch could be
        # silently absorbed into an unrelated bulk match and hidden from the
        # exception ledger. Only records still stuck at "we never found a bank
        # credit at all" (TIER_1_MISSING_BANK_RECORD) are real candidates for
        # belonging to a not-yet-identified bulk credit.
        DPSS_ELIGIBLE_STAGES = {"TIER_1_MISSING_BANK_RECORD"}

        exception_payment_ids = {
            q["payment_id"] for q in self.unmatched_queue
            if q.get("payment_id") and q.get("discrepancy_stage") in DPSS_ELIGIBLE_STAGES
        }

        for candidate_bank in unclaimed_bank_credits:
            pool = [s for s in unclaimed_settlements if s["payment_id"] in exception_payment_ids]  # only TIER_1_MISSING_BANK_RECORD eligible
            if not pool:
                continue
            target = candidate_bank["credit_amount"]
            matched_subset = self.subset_sum_disaggregate(target, pool)
            if matched_subset:
                bank_txn_id = candidate_bank["bank_txn_id"]
                self.touched_bank_txns.add(bank_txn_id)
                matched_ids = {s["payment_id"] for s in matched_subset}

                # Remove these from the exception queue since DPSS resolved them
                self.unmatched_queue = [
                    q for q in self.unmatched_queue if q.get("payment_id") not in matched_ids
                ]
                exception_payment_ids -= matched_ids

                for s in matched_subset:
                    self.reconciled_records.append({
                        "order_id": s["order_id"],
                        "payment_id": s["payment_id"],
                        "bank_txn_id": bank_txn_id,
                        "reconciliation_type": "BULK_NTO1",
                        "reconciliation_method": "DPSS_DISAGGREGATION",
                        "gross_amount": s["gross_amount"],
                        "net_amount": s["net_amount"],
                        "fee": s["fee"],
                        "tax": s["tax"],
                        "bank_credit": target,
                        "variance_paise": 0
                    })
                    self.reconciled_order_ids.add(s["order_id"])
                    self.reconciled_payment_ids.add(s["payment_id"])

        # -------------------------------------------------------------
        # TIER 4: O(1) BANK ORPHAN IDENTIFICATION
        # -------------------------------------------------------------
        for b_id, b in self.bank_feed.items():
            if b_id not in self.touched_bank_txns:
                self.unmatched_queue.append({
                    "order_id": None,
                    "payment_id": None,
                    "bank_txn_id": b_id,
                    "discrepancy_stage": "TIER_4_UNMAPPED_BANK_ORPHAN",
                    "variance_paise": b["credit_amount"],
                    "gross_amount": None,
                    "billed_fee": 0,
                    "billed_tax": 0,
                    "expected_fee": 0,
                    "expected_tax": 0,
                    "fee_diff_paise": 0,
                    "tax_diff_paise": 0,
                    "method": None,
                    "settled_at": None,
                    "narration": b["narration"],
                    "bank_credit": b["credit_amount"],
                    "context": f"Bank credit of {b['credit_amount']} paise has no matching OMS order or Razorpay settlement."
                })

        runtime_sec = time.perf_counter() - start_time
        throughput = len(self.orders) / runtime_sec if runtime_sec > 0 else 0

        metrics = {
            "total_orders": len(self.orders),
            "reconciled_count": len(self.reconciled_records),
            "unmatched_count": len(self.unmatched_queue),
            "runtime_seconds": round(runtime_sec, 4),
            "throughput_records_per_sec": round(throughput, 1)
        }

        return self.reconciled_records, self.unmatched_queue, metrics


if __name__ == "__main__":
    DATA_PATH = os.path.join(os.path.dirname(__file__), "..", "data")
    matcher = DeterministicMatcher(DATA_PATH)
    reconciled, unmatched, stats = matcher.run()
    print("\n--- MATCHING RESULTS ---")
    print(f"Reconciled: {stats['reconciled_count']}")
    print(f"Unmatched Exceptions: {stats['unmatched_count']}")
    print(f"Throughput: {stats['throughput_records_per_sec']} records/sec ({stats['runtime_seconds']}s)")