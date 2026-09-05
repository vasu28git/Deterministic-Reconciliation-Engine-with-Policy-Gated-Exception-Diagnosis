import json
import os
import re
import time
from dotenv import load_dotenv, find_dotenv

# Load .env from the project root automatically (works regardless of CWD)
load_dotenv(find_dotenv())
from typing import Dict, List, Any, Optional
from pydantic import BaseModel, Field
from groq import Groq

# -------------------------------------------------------------
# 1. STRICT STRUCTURED DIAGNOSTIC SCHEMA
# -------------------------------------------------------------
class DiagnosticReport(BaseModel):
    record_id: str
    discrepancy_category: str = Field(
        description=(
            "One of: FEE_OVERCHARGE, TIMING_LAG, TDS_WITHHOLDING, TRUE_ORPHAN, "
            "BULK_AGGREGATION_FAILURE, MISSING_SETTLEMENT_EVENT, UNKNOWN"
        )
    )
    suspected_variance_paise: int
    confidence_score: float = Field(ge=0.0, le=1.0)
    diagnostic_rationale: str = Field(
        description="Audit-ready explanation citing contract terms or statutory tax rules."
    )
    recommended_action: str = Field(
        description="One of: DISPUTE_GATEWAY_MDR, POST_TDS_SPLIT_ENTRY, DEFER_TO_NEXT_CYCLE, ESCALATE_HUMAN_REVIEW"
    )

# -------------------------------------------------------------
# 2. FINTECH DOMAIN KNOWLEDGE SYSTEM PROMPT
# -------------------------------------------------------------
SYSTEM_PROMPT = """
You are an expert Autonomous Financial Controller specializing in Indian Payment Systems and Razorpay Settlement Mechanics.
Your job is to inspect financial exception tuples that failed deterministic reconciliation and diagnose the root cause.

Operational Rules:
1. CONTRACTED MDR RATES:
   - UPI: 0.0%
   - Domestic Debit Card: 0.9%
   - Domestic Credit Card: 1.8%
   - Corporate Card: 2.8%
   - Statutory GST: 18% applied strictly to the MDR fee only.
   If billed_fee exceeds expected_fee, categorize as FEE_OVERCHARGE.

2. STATUTORY TAX DEDUCTED AT SOURCE (TDS):
   - Under Section 194H of the Indian Income Tax Act, corporate B2B payments frequently withhold exactly 10% TDS on the gross transaction value.
   - Only ever propose TDS_WITHHOLDING for a record whose discrepancy_stage is TIER_1_NET_CREDIT_MISMATCH.
     Never propose TDS_WITHHOLDING for a fee/tax compliance violation, a bulk aggregation
     mismatch, or a missing-settlement record — variance in those cases means something
     else entirely, and mislabeling them as TDS is a critical hallucination the policy
     gate will reject.
   - If Net Settlement - Bank Credit == 10% of Gross Amount, categorize as TDS_WITHHOLDING.

3. SETTLEMENT CLEARING WINDOWS (SLA):
   - Standard payout is T+1 banking days.
   - Weekend captures (Friday evening / Saturday / Sunday) clear on Tuesday morning (T+2 cutoff lag).
   - If bank_credit is missing and transaction occurred over the weekend, categorize as TIMING_LAG.

4. UNMAPPED CREDITS:
   - If bank_txn_id exists but order_id and payment_id are null, categorize as TRUE_ORPHAN.

5. BULK SETTLEMENT AGGREGATION FAILURES:
   - If discrepancy_stage is TIER_2_BULK_SUM_MISMATCH, categorize as BULK_AGGREGATION_FAILURE.
     This means a batch of settlements grouped under one payout UTR does not sum to the
     bank credit — never reclassify this as TDS or fee overcharge.

6. MISSING GATEWAY SETTLEMENT EVENTS:
   - If discrepancy_stage is TIER_0_MISSING_GATEWAY_SETTLEMENT, categorize as
     MISSING_SETTLEMENT_EVENT. An order exists in the OMS with no corresponding
     settlement record from the payment gateway at all.

Respond ONLY with a valid JSON object (or JSON array for batch calls) matching the DiagnosticReport schema.
"""

# Groq model — confirmed available on this account (openai/gpt-oss-120b = 120B parameter GPT-class)
# Other available options: openai/gpt-oss-20b, qwen/qwen3.8-27b
GROQ_MODEL = "openai/gpt-oss-120b"

class LLMDiagnostician:
    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.getenv("GROQ_API_KEY")
        self.client = None
        if self.api_key:
            try:
                self.client = Groq(api_key=self.api_key)
                print(f"[LLMDiagnostician] Groq client initialized (model: {GROQ_MODEL})")
            except Exception as e:
                print(f"[LLMDiagnostician] Groq client init failed ({e}); deterministic fallback active.")
                self.client = None

    def _rule_based_fallback_diagnosis(self, exc: Dict[str, Any]) -> DiagnosticReport:
        """
        Deterministic diagnostic fallback used when running in offline/evaluator mode
        without third-party API credentials, or whenever a live LLM call fails.
        """
        stage = exc.get("discrepancy_stage", "")
        gross = exc.get("gross_amount") or 0
        variance = exc.get("variance_paise", 0)
        rec_id = exc.get("order_id") or exc.get("bank_txn_id") or "unknown"

        if stage == "TIER_1_MDR_COMPLIANCE_VIOLATION":
            fee_diff = exc.get("fee_diff_paise", 0)
            tax_diff = exc.get("tax_diff_paise", 0)
            return DiagnosticReport(
                record_id=rec_id,
                discrepancy_category="FEE_OVERCHARGE",
                suspected_variance_paise=variance,
                confidence_score=0.98,
                diagnostic_rationale=(
                    f"Contractual MDR breach: Gateway billed {exc.get('billed_fee')}p fee "
                    f"(contracted: {exc.get('expected_fee')}p) + {exc.get('billed_tax')}p tax. "
                    f"Net fee leakage: {fee_diff + tax_diff} paise."
                ),
                recommended_action="DISPUTE_GATEWAY_MDR"
            )

        elif stage == "TIER_1_NET_CREDIT_MISMATCH":
            expected_tds = int(gross * 0.10)
            if abs(variance - expected_tds) <= 1:
                return DiagnosticReport(
                    record_id=rec_id,
                    discrepancy_category="TDS_WITHHOLDING",
                    suspected_variance_paise=variance,
                    confidence_score=0.99,
                    diagnostic_rationale=(
                        f"Statutory withholding identified under Section 194H. "
                        f"Variance of {variance} paise matches 10% TDS on gross invoice of {gross} paise."
                    ),
                    recommended_action="POST_TDS_SPLIT_ENTRY"
                )
            else:
                return DiagnosticReport(
                    record_id=rec_id,
                    discrepancy_category="UNKNOWN",
                    suspected_variance_paise=variance,
                    confidence_score=0.30,
                    diagnostic_rationale=f"Unexplained variance of {variance} paise between settlement net and bank credit.",
                    recommended_action="ESCALATE_HUMAN_REVIEW"
                )

        elif stage == "TIER_1_MISSING_BANK_RECORD":
            return DiagnosticReport(
                record_id=rec_id,
                discrepancy_category="TIMING_LAG",
                suspected_variance_paise=variance,
                confidence_score=0.95,
                diagnostic_rationale=(
                    f"Settlement initiated at {exc.get('settled_at')} in-transit across "
                    f"weekend banking window. Payout expected to clear by next business day."
                ),
                recommended_action="DEFER_TO_NEXT_CYCLE"
            )

        elif stage == "TIER_2_BULK_SUM_MISMATCH":
            return DiagnosticReport(
                record_id=rec_id,
                discrepancy_category="BULK_AGGREGATION_FAILURE",
                suspected_variance_paise=variance,
                confidence_score=0.85,
                diagnostic_rationale=(
                    f"Batch settlement aggregation failure. The sum of grouped settlement "
                    f"net amounts does not match the single bulk bank credit for this payout "
                    f"batch. Unexplained variance of {variance} paise. Requires manual review "
                    f"of the batch grouping (possible partial refund, missing sub-transaction, "
                    f"or an incorrectly grouped UTR)."
                ),
                recommended_action="ESCALATE_HUMAN_REVIEW"
            )

        elif stage == "TIER_0_MISSING_GATEWAY_SETTLEMENT":
            return DiagnosticReport(
                record_id=rec_id,
                discrepancy_category="MISSING_SETTLEMENT_EVENT",
                suspected_variance_paise=variance,
                confidence_score=0.90,
                diagnostic_rationale=(
                    f"Order captured in the OMS with gross amount {gross}p, but no "
                    f"corresponding settlement event was ever received from the payment "
                    f"gateway. This may indicate a dropped webhook, a failed capture that "
                    f"was not rolled back in the OMS, or a gateway-side processing delay."
                ),
                recommended_action="ESCALATE_HUMAN_REVIEW"
            )

        elif stage == "TIER_4_UNMAPPED_BANK_ORPHAN":
            return DiagnosticReport(
                record_id=rec_id,
                discrepancy_category="TRUE_ORPHAN",
                suspected_variance_paise=variance,
                confidence_score=0.99,
                diagnostic_rationale=(
                    f"Direct credit of {variance} paise received with narration "
                    f"'{exc.get('narration')}'. No matching OMS order or Razorpay payment."
                ),
                recommended_action="ESCALATE_HUMAN_REVIEW"
            )

        return DiagnosticReport(
            record_id=rec_id,
            discrepancy_category="UNKNOWN",
            suspected_variance_paise=variance,
            confidence_score=0.10,
            diagnostic_rationale="Unclassified operational variance.",
            recommended_action="ESCALATE_HUMAN_REVIEW"
        )


    def _diagnose_chunk(self, chunk: List[Dict[str, Any]]) -> List[DiagnosticReport]:
        """
        Calls the Groq API for a small chunk of exceptions (max CHUNK_SIZE records).
        Retries once on 429 rate-limit errors using the wait time from the error message.
        Raises on any other failure so the caller can fall back to rule-based diagnosis.
        """
        prompt = (
            "Diagnose each of the following financial reconciliation exceptions. "
            "Return ONLY a JSON object with a single key 'results' containing a JSON array. "
            "Each array element must match the DiagnosticReport schema: "
            "(record_id, discrepancy_category, suspected_variance_paise, confidence_score, "
            "diagnostic_rationale, recommended_action). "
            f"There are exactly {len(chunk)} exceptions — return exactly {len(chunk)} results in the same order.\n\n"
            f"Exceptions:\n{json.dumps(chunk, indent=2)}"
        )

        MAX_RETRIES = 3
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(
                    model=GROQ_MODEL,
                    messages=[
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": prompt}
                    ],
                    temperature=0.0,
                    response_format={"type": "json_object"},
                )
                break  # Success — exit retry loop

            except Exception as e:
                err_str = str(e)
                # Parse "try again in X.XXs" from Groq 429 error message
                wait_match = re.search(r"try again in ([0-9.]+)s", err_str, re.IGNORECASE)
                if wait_match and attempt < MAX_RETRIES - 1:
                    wait_secs = float(wait_match.group(1)) + 2.0  # add 2s buffer
                    print(f"[LLMDiagnostician] Rate limit hit — waiting {wait_secs:.1f}s before retry "
                          f"(attempt {attempt + 1}/{MAX_RETRIES})...")
                    time.sleep(wait_secs)
                else:
                    raise  # Non-429 error or exhausted retries

        raw_text = response.choices[0].message.content.strip()

        # Strip markdown fences if present
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)

        parsed = json.loads(raw_text)

        # Unwrap {"results": [...]} or any dict-wrapped array
        if isinstance(parsed, dict):
            for v in parsed.values():
                if isinstance(v, list):
                    parsed = v
                    break

        if not isinstance(parsed, list) or len(parsed) != len(chunk):
            raise ValueError(
                f"Expected {len(chunk)} results, got "
                f"{len(parsed) if isinstance(parsed, list) else type(parsed).__name__}"
            )

        reports = []
        for item, exc in zip(parsed, chunk):
            report = DiagnosticReport.model_validate(item)
            if not report.record_id or report.record_id == "unknown":
                report.record_id = (
                    exc.get("order_id")
                    or exc.get("bank_txn_id")
                    or exc.get("payment_id")
                    or "unknown"
                )
            reports.append(report)
        return reports


    def diagnose_batch(self, exceptions: List[Dict[str, Any]]) -> List[DiagnosticReport]:
        """
        Diagnoses all exceptions using chunked Groq API calls (CHUNK_SIZE records per call).
        Chunking prevents token-length truncation for large exception batches.
        Falls back gracefully to rule-based diagnosis per-record if any chunk fails.
        """
        CHUNK_SIZE = 5  # Safe token budget per call for gpt-oss-120b

        if not exceptions:
            return []

        if self.client:
            all_reports: List[DiagnosticReport] = []
            failed_chunks: List[Dict[str, Any]] = []

            # Split into chunks
            chunks = [exceptions[i:i + CHUNK_SIZE] for i in range(0, len(exceptions), CHUNK_SIZE)]

            for chunk_idx, chunk in enumerate(chunks):
                try:
                    chunk_reports = self._diagnose_chunk(chunk)
                    all_reports.extend(chunk_reports)
                    print(f"[LLMDiagnostician] Chunk {chunk_idx + 1}/{len(chunks)}: "
                          f"{len(chunk_reports)} records diagnosed via Groq ({GROQ_MODEL}).")
                except Exception as e:
                    print(f"[LLMDiagnostician] Chunk {chunk_idx + 1}/{len(chunks)} failed ({e}); "
                          f"using deterministic fallback for {len(chunk)} records.")
                    failed_chunks.extend(chunk)
                    # Use rule-based for this chunk immediately
                    all_reports.extend(
                        self._rule_based_fallback_diagnosis(exc) for exc in chunk
                    )

            if not failed_chunks:
                print(f"[LLMDiagnostician] All {len(exceptions)} records diagnosed by Groq AI agent.")
            return all_reports

        # No client — full deterministic fallback
        return [self._rule_based_fallback_diagnosis(exc) for exc in exceptions]

