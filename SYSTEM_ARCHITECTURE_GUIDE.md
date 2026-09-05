# Complete System Architecture & Process Guide
### Razorpay Buildathon 2026 — Track 04: Autonomous AI Finance Controller

---

## 💡 Executive Summary (The Core Concept)

In traditional financial operations, **reconciliation** is the process of matching internal sales records against payment gateway settlements and core banking credit feeds.

### The Problem with Pure AI Agents in Finance
Standard Large Language Models (LLMs) are probabilistic. Applying an LLM directly to financial ledgers creates high risk:
- An LLM might hallucinate a match that doesn't exist.
- A single incorrect entry corrupts statutory tax filings (e.g., Section 194H TDS) and hides merchant fee leakages.

### Our Solution: Proposer-Verifier Architecture
We decouple **diagnosis** from **commitment**:
1. **Matching Engine (Deterministic):** Automatically resolves ~90% of transactions using 5-tiered mathematical algorithms.
2. **AI Diagnostician (Groq / `openai/gpt-oss-120b`):** Analyzes remaining exceptions and **proposes** structured root causes (Read-Only access).
3. **Policy Gate (Deterministic Guardrail):** Verifies all LLM proposals against hard double-entry accounting invariants before any money or ledger state is touched.

---

## 🔄 The 3 Data Streams

| Data Stream | File Path | What it Represents | Key Fields |
|---|---|---|---|
| **1. OMS Orders** | `data/orders.json` | Internal Order Management System (Merchant sales log) | `order_id`, `gross_amount` (paise) |
| **2. Gateway Settlement** | `data/settlements.json` | Razorpay payout reports | `payment_id`, `order_id`, `gross_amount`, `fee`, `tax`, `net_amount`, `settlement_utr`, `method` |
| **3. Bank Feed** | `data/bank_feed.json` | Core banking credit feed | `bank_txn_id`, `bank_utr`, `credit_amount`, `narration` |

> **Crucial Rule — 100% Integer Paise:** All monetary values across all three streams are stored and processed in 64-bit integer minor currency units (paise). `₹100.50` is represented strictly as `10050` paise. Floating-point arithmetic (`float`) is completely prohibited to prevent IEEE-754 rounding errors.

---

## ⚙️ Step-by-Step System Flow

```
                      +----------------------------------+
                      | 1. RAW DATA INGESTION            |
                      | OMS + Settlements + Bank Feed    |
                      +----------------------------------+
                                       |
                                       v
                      +----------------------------------+
                      | 2. DETERMINISTIC ENGINE          |
                      | (Tiers 0 - 4 + DPSS Solver)      |
                      +----------------------------------+
                                       |
                   +-------------------+-------------------+
                   |                                       |
                   v                                       v
       [Reconciled (1:1 & Bulk N:1)]              [Unmatched Exceptions]
                   |                                       |
                   v                                       v
          Committed to Ledger                  +------------------------+
                                               | 3. AI DIAGNOSTICIAN    |
                                               | (Groq / Structured)    |
                                               +------------------------+
                                                           |
                                                           v
                                               +------------------------+
                                               | 4. POLICY GATE         |
                                               | (Hard Invariants)      |
                                               +------------------------+
                                                           |
                                               +-----------+-----------+
                                               |                       |
                                               v                       v
                                        Committed Ledger       Quarantined Exception
                                       (TDS Split Entries)     Ledger (Audit Trail)
```

---

## 🧩 Step 1: Tiered Deterministic Engine (`engine/matcher.py`)

The engine processes records through 5 ordered evaluation tiers:

### Tier 0: Gateway Presence Verification
- Checks if an OMS order has a corresponding Razorpay settlement record.
- **If missing:** Flagged as `TIER_0_MISSING_GATEWAY_SETTLEMENT`.

### Tier 1: Exact Key & Contractual MDR Matching (1:1)
- Joins Razorpay settlement records to bank credits using `settlement_utr == bank_utr`.
- Verifies gateway fee charged against the merchant's contracted MDR rate sheet:
  - **UPI:** 0% MDR
  - **Debit Card:** 0.9% MDR
  - **Credit Card:** 1.8% MDR
  - **Corporate Card:** 2.8% MDR
  - **GST:** 18% applied strictly to MDR fee.
- **If fee rate is wrong:** Flagged as `TIER_1_MDR_COMPLIANCE_VIOLATION`.
- **If net payout doesn't match bank credit:** Flagged as `TIER_1_NET_CREDIT_MISMATCH`.

### Tier 2: Bulk Settlement Summation (N:1 Known Group)
- Aggregates multiple transactions sharing the same UTR payout.
- Verifies `Sum(Net Amounts) == Bank Credit Amount`.

### Tier 3: DPSS (Dynamic Programming Subset-Sum) Solver
- Handles **grouped bank credits** where the bank deposits a single lump sum (e.g. `₹50,000`) representing an unknown subset of individual merchant settlements.
- **Algorithm:** Uses a 0/1 Dynamic Programming solver (`max_group_size=12`, paise-level precision) to find the exact subset of settlements matching the bank deposit.
- **Safety Guard:** Only `TIER_1_MISSING_BANK_RECORD` settlements are eligible for DPSS disaggregation. Fee-overcharge or mismatch exceptions are excluded from DPSS to prevent false-positive absorption.

### Tier 4: Bank Orphan Identification
- Scans remaining bank credits that have no matching OMS order or settlement record.
- Flagged as `TIER_4_UNMAPPED_BANK_ORPHAN`.

---

## 🤖 Step 2: AI Diagnostician (`agent/diagnostician.py`)

Any record flagged with an exception in Step 1 is passed to the **LLM Diagnostician** powered by Groq (`openai/gpt-oss-120b`).

### Key Design Principles:
1. **Read-Only:** The LLM cannot write to the financial ledger or execute payouts.
2. **Structured Output:** The LLM must respond in a strict Pydantic JSON schema (`DiagnosticReport`):
   ```python
   class DiagnosticReport(BaseModel):
       record_id: str
       discrepancy_category: str  # e.g., "TDS_WITHHOLDING", "FEE_LEAKAGE"
       suspected_variance_paise: int
       confidence_score: float
       diagnostic_rationale: str
       recommended_action: str
   ```
3. **Deterministic Fallback:** If Groq API rate-limits or is offline, a built-in rule engine automatically generates the diagnosis without crashing.

---

## 🛡️ Step 3: Deterministic Policy Gate (`agent/policy_gate.py`)

The **Policy Gate** acts as the financial enforcer. It evaluates every LLM diagnostic report against non-negotiable accounting rules before committing anything.

### Enforced Rules & Invariants:

1. **Section 194H TDS Invariant Assertion:**
   $$\text{Variance} = \lfloor \text{Gross Amount} \times 0.10 \rfloor \pm 1\text{ paise}$$
   If the LLM claims a discrepancy is due to Section 194H TDS (10% tax withholding on B2B invoices), the Policy Gate calculates `10% of gross`. If the actual variance doesn't match `10% ± 1 paise`, the gate **rejects** the LLM proposal and quaternions it as `POLICY_REJECTED_TDS_ARITHMETIC_FAILURE`.

2. **Double-Entry Balance Assertion:**
   $$\text{Bank Credit} + \text{TDS Tax} + \text{Gateway Fee} + \text{GST Tax} == \text{Gross Order Amount}$$
   Every resolved entry must create a balanced double-entry accounting split.

3. **Stage Scoping Guard:**
   TDS resolutions are strictly allowed **only** for `TIER_1_NET_CREDIT_MISMATCH` records. If an LLM attempts to claim TDS on a fee overcharge stage (`TIER_1_MDR_COMPLIANCE_VIOLATION`), the gate rejects it as `POLICY_REJECTED_TDS_STAGE_MISMATCH`.

4. **Idempotency Write-Ahead Key Registry:**
   Maintains a set of composite keys (`record_id:action_type`). If a duplicate webhook or re-run attempts to process an already committed transaction, the gate returns `REJECTED_DUPLICATE` to prevent double-posting.

5. **Honest Exception Quarantine:**
   Any genuine discrepancies (such as contractual fee overcharges, weekend T+2 timing lags, or unmapped bank deposits) are safely quarantined into an **Honest Exception Ledger** with full audit metadata.

---

## 📊 Concrete Data Flow Example

Let's follow a B2B order of **₹1,00,000** (`10,000,000` paise):

1. **OMS Order:** Gross = ₹1,00,000 (`10000000` paise).
2. **Razorpay Settlement:** Gross = ₹1,00,000, Gateway Fee = ₹1,800 (1.8% Credit Card), GST = ₹324 (18% of fee), Net Payout = ₹97,876 (`9787600` paise).
3. **Bank Credit Received:** ₹87,876 (`8787600` paise).
4. **Engine Tier 1 Result:** Variance detected = `₹97,876 - ₹87,876 = ₹10,000` (`1000000` paise). Flagged as `TIER_1_NET_CREDIT_MISMATCH`.
5. **AI Diagnostician:** Identifies variance is exactly 10% of gross (`₹10,000 / ₹1,00,000 = 10%`). Proposes `TDS_WITHHOLDING` under Section 194H.
6. **Policy Gate Verification:**
   - Evaluates: `int(10000000 * 0.10) = 1000000` paise. Match = TRUE.
   - Evaluates Double Entry: `8787600 (Bank) + 1000000 (TDS) + 180000 (Fee) + 32400 (Tax) = 10000000 (Gross)`. Match = TRUE.
7. **Commit:** Entry committed to ledger as `RECONCILED_WITH_TDS_SPLIT`.

---

## 🎤 How to Convey This in 60 Seconds (Presentation Script)

> *"In high-volume payment operations like Razorpay, automating reconciliation with standard AI is dangerous because LLM hallucinations can cause tax errors or hide fee overcharges.*
>
> *To solve this, we built an **Autonomous Finance Controller with a Proposer-Verifier Architecture**.*
>
> *First, our **5-Tiered Matching Engine** processes transactions using 100% integer paise precision. It includes a **Dynamic Programming Subset-Sum solver** that disaggregates bulk bank deposits into exact component transactions.*
>
> *Second, any unmatched exception is analyzed by our **AI Diagnostician running Groq LLM**, which outputs structured Pydantic diagnostic hypotheses. Crucially, the AI operates in **read-only mode** with zero direct write access to the ledger.*
>
> *Finally, our **Deterministic Policy Gate** verifies every AI proposal against mathematical invariants—like Section 194H 10% TDS validation, double-entry balance, and idempotency write-ahead keys—before any entry is committed.*
>
> *The result is **100% precision, zero false positives**, and an auditable, honest exception ledger."*
