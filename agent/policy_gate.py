import time
from typing import Dict, List, Any, Tuple, Set
from agent.diagnostician import DiagnosticReport

# Shared arithmetic tolerance for paise-level rounding across ALL invariant
# checks in this module. Using one constant everywhere prevents drift between
# an "acceptance" check and a later "commit" check disagreeing on precision.
TDS_TOLERANCE_PAISE = 1

class PolicyGate:
    def __init__(self):
        self.resolved_ledger_entries: List[Dict[str, Any]] = []
        self.honest_exception_ledger: List[Dict[str, Any]] = []
        # Idempotency Registry: tracks composite keys to prevent duplicate commitments
        self.committed_idempotency_keys: Set[str] = set()

    def evaluate_and_enforce(
        self,
        exc: Dict[str, Any],
        diagnosis: DiagnosticReport
    ) -> Dict[str, Any]:
        """
        Validates LLM diagnostic hypotheses against code invariants.
        Enforces transactional idempotency and formal double-entry balancing.
        """
        rec_id = diagnosis.record_id
        action_type = diagnosis.recommended_action
        idempotency_key = f"{rec_id}:{action_type}"

        # -------------------------------------------------------------
        # INVARIANT 0: TRANSACTIONAL IDEMPOTENCY CHECK
        # -------------------------------------------------------------
        if idempotency_key in self.committed_idempotency_keys:
            return {
                "action": "REJECTED_DUPLICATE",
                "reason_code": "IDEMPOTENCY_COLLISION",
                "idempotency_key": idempotency_key
            }

        gross = exc.get("gross_amount") or 0
        billed_fee = exc.get("billed_fee", 0)
        billed_tax = exc.get("billed_tax", 0)
        bank_credit = exc.get("bank_credit", 0)
        variance = exc.get("variance_paise", 0)
        source_stage = exc.get("discrepancy_stage")

        # -------------------------------------------------------------
        # POLICY RULE 1: STATUTORY TDS (SECTION 194H) VALIDATION
        # -------------------------------------------------------------
        if diagnosis.discrepancy_category == "TDS_WITHHOLDING":

            # CRITICAL GUARD: TDS is only ever a valid explanation for a
            # TIER_1_NET_CREDIT_MISMATCH exception. `variance_paise` means a
            # different quantity for every other stage (e.g. for
            # TIER_1_MDR_COMPLIANCE_VIOLATION it is fee_diff + tax_diff, a
            # fee overcharge amount that has nothing to do with TDS). Without
            # this stage check, a hallucinated or adversarial diagnosis that
            # mislabels a non-TDS exception as TDS_WITHHOLDING could pass the
            # numeric check below purely by coincidence (e.g. a fee overcharge
            # that happens to equal 10% of gross) and get committed to the
            # ledger as a fabricated TDS entry. This guard must run BEFORE
            # any arithmetic check, not after.
            if source_stage != "TIER_1_NET_CREDIT_MISMATCH":
                quarantine_record = {
                    "idempotency_key": idempotency_key,
                    "record_id": rec_id,
                    "order_id": exc.get("order_id"),
                    "payment_id": exc.get("payment_id"),
                    "bank_txn_id": exc.get("bank_txn_id"),
                    "rule_id": "RULE_SEC194H_TDS_ASSERTION",
                    "reason_code": "POLICY_REJECTED_TDS_STAGE_MISMATCH",
                    "variance_paise": variance,
                    "audit_rationale": (
                        f"LLM proposed TDS_WITHHOLDING but the source exception's "
                        f"discrepancy_stage was '{source_stage}', not "
                        f"TIER_1_NET_CREDIT_MISMATCH. Rejected as a likely "
                        f"hallucinated or mislabeled categorization; this "
                        f"variance figure does not represent a settlement-vs-bank "
                        f"credit shortfall and cannot be a genuine TDS case."
                    ),
                    "status": "QUARANTINED_MANUAL_AUDIT",
                    "timestamp": time.time()
                }
                self.honest_exception_ledger.append(quarantine_record)
                self.committed_idempotency_keys.add(idempotency_key)
                return {"action": "QUARANTINED", "entry": quarantine_record}

            expected_tds = int(gross * 0.10)

            # Strict Invariant Assertion: Variance must equal exactly 10% of
            # gross invoice, within a single shared rounding tolerance used
            # consistently for both the acceptance check and the double-entry
            # balance check below (see TDS_TOLERANCE_PAISE).
            if abs(variance - expected_tds) <= TDS_TOLERANCE_PAISE:
                # Double-entry balance check:
                # Debit: Bank + Debit: TDS Receivable + Debit: MDR + Debit: GST == Credit: Gross Sales
                debit_total = bank_credit + expected_tds + billed_fee + billed_tax
                credit_total = gross
                delta = debit_total - credit_total

                if abs(delta) <= TDS_TOLERANCE_PAISE:
                    journal_entry = {
                        "idempotency_key": idempotency_key,
                        "order_id": exc["order_id"],
                        "payment_id": exc["payment_id"],
                        "bank_txn_id": exc["bank_txn_id"],
                        "rule_id": "RULE_SEC194H_TDS_ASSERTION",
                        "reason_code": "TDS_10PCT_WITHHELD_VERIFIED",
                        "status": "RECONCILED_WITH_TDS_SPLIT",
                        "reconciliation_type": "TDS_WITHHOLDING_SPLIT",
                        "double_entry": {
                            "debit_cash_bank_paise": bank_credit,
                            "debit_tds_receivable_paise": expected_tds,
                            "debit_mdr_fee_paise": billed_fee,
                            "debit_gst_tax_paise": billed_tax,
                            "credit_gross_sales_paise": gross,
                            "delta_paise": delta
                        },
                        "audit_rationale": diagnosis.diagnostic_rationale,
                        "timestamp": time.time()
                    }
                    self.resolved_ledger_entries.append(journal_entry)
                    self.committed_idempotency_keys.add(idempotency_key)
                    return {"action": "COMMITTED_TO_LEDGER", "entry": journal_entry}

            # Arithmetic verification failed: isolate into exception ledger
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "rule_id": "RULE_SEC194H_TDS_ASSERTION",
                "reason_code": "POLICY_REJECTED_TDS_ARITHMETIC_FAILURE",
                "variance_paise": variance,
                "claimed_tds_paise": expected_tds,
                "audit_rationale": "LLM suggested TDS withholding, but variance does not match 10% of gross invoice.",
                "status": "QUARANTINED_MANUAL_AUDIT",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # -------------------------------------------------------------
        # POLICY RULE 2: CONFIRMED GATEWAY MDR OVERCHARGE
        # -------------------------------------------------------------
        elif diagnosis.discrepancy_category == "FEE_OVERCHARGE":
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "order_id": exc.get("order_id"),
                "payment_id": exc.get("payment_id"),
                "bank_txn_id": exc.get("bank_txn_id"),
                "rule_id": "RULE_CONTRACTUAL_MDR_COMPLIANCE",
                "reason_code": "CONFIRMED_GATEWAY_FEE_LEAKAGE",
                "variance_paise": variance,
                "fee_overcharge_paise": exc.get("fee_diff_paise"),
                "tax_overcharge_paise": exc.get("tax_diff_paise"),
                "audit_rationale": diagnosis.diagnostic_rationale,
                "recommended_action": diagnosis.recommended_action,
                "status": "QUARANTINED_FOR_DISPUTE",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # -------------------------------------------------------------
        # POLICY RULE 3: IN-TRANSIT TIMING LAG
        # -------------------------------------------------------------
        elif diagnosis.discrepancy_category == "TIMING_LAG":
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "order_id": exc.get("order_id"),
                "payment_id": exc.get("payment_id"),
                "bank_txn_id": None,
                "rule_id": "RULE_SETTLEMENT_CLEARING_WINDOW",
                "reason_code": "SETTLEMENT_IN_TRANSIT_T2_LAG",
                "variance_paise": variance,
                "audit_rationale": diagnosis.diagnostic_rationale,
                "recommended_action": "DEFER_TO_NEXT_CYCLE",
                "status": "MONITORING_SETTLEMENT_WINDOW",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # -------------------------------------------------------------
        # POLICY RULE 4: UNMAPPED BANK ORPHAN
        # -------------------------------------------------------------
        elif diagnosis.discrepancy_category == "TRUE_ORPHAN":
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "order_id": None,
                "payment_id": None,
                "bank_txn_id": exc.get("bank_txn_id"),
                "rule_id": "RULE_BANK_UNMAPPED_CREDIT",
                "reason_code": "UNMAPPED_BANK_DEPOSIT_ORPHAN",
                "variance_paise": variance,
                "narration": exc.get("narration"),
                "audit_rationale": diagnosis.diagnostic_rationale,
                "recommended_action": "ESCALATE_TREASURY_INVESTIGATION",
                "status": "QUARANTINED_UNKNOWN_SOURCE",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # -------------------------------------------------------------
        # POLICY RULE 5: BULK SETTLEMENT AGGREGATION FAILURE
        # -------------------------------------------------------------
        elif diagnosis.discrepancy_category == "BULK_AGGREGATION_FAILURE":
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "order_id": exc.get("order_id"),
                "payment_id": exc.get("payment_id"),
                "bank_txn_id": exc.get("bank_txn_id"),
                "rule_id": "RULE_BULK_BATCH_SUM_INTEGRITY",
                "reason_code": "BULK_SETTLEMENT_SUM_MISMATCH",
                "variance_paise": variance,
                "audit_rationale": diagnosis.diagnostic_rationale,
                "recommended_action": "ESCALATE_HUMAN_REVIEW",
                "status": "QUARANTINED_MANUAL_AUDIT",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # -------------------------------------------------------------
        # POLICY RULE 6: MISSING GATEWAY SETTLEMENT EVENT
        # -------------------------------------------------------------
        elif diagnosis.discrepancy_category == "MISSING_SETTLEMENT_EVENT":
            quarantine_record = {
                "idempotency_key": idempotency_key,
                "record_id": rec_id,
                "order_id": exc.get("order_id"),
                "payment_id": None,
                "bank_txn_id": None,
                "rule_id": "RULE_OMS_GATEWAY_SETTLEMENT_PRESENCE",
                "reason_code": "MISSING_GATEWAY_SETTLEMENT_EVENT",
                "variance_paise": variance,
                "audit_rationale": diagnosis.diagnostic_rationale,
                "recommended_action": "ESCALATE_HUMAN_REVIEW",
                "status": "QUARANTINED_MANUAL_AUDIT",
                "timestamp": time.time()
            }
            self.honest_exception_ledger.append(quarantine_record)
            self.committed_idempotency_keys.add(idempotency_key)
            return {"action": "QUARANTINED", "entry": quarantine_record}

        # Catch-all fallback
        quarantine_record = {
            "idempotency_key": idempotency_key,
            "record_id": rec_id,
            "rule_id": "RULE_UNCLASSIFIED_FALLBACK",
            "reason_code": "UNCLASSIFIED_DISCREPANCY",
            "variance_paise": variance,
            "audit_rationale": diagnosis.diagnostic_rationale,
            "status": "QUARANTINED_MANUAL_AUDIT",
            "timestamp": time.time()
        }
        self.honest_exception_ledger.append(quarantine_record)
        self.committed_idempotency_keys.add(idempotency_key)
        return {"action": "QUARANTINED", "entry": quarantine_record}

    def process_all_exceptions(
        self,
        exceptions: List[Dict[str, Any]],
        diagnoses: List[DiagnosticReport]
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
        for exc, diag in zip(exceptions, diagnoses):
            self.evaluate_and_enforce(exc, diag)
        return self.resolved_ledger_entries, self.honest_exception_ledger