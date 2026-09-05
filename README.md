# Autonomous Financial Reconciliation Engine
### Razorpay Buildathon 2026 — Track 04 (Autonomous AI Finance Controller)

An autonomous, closed-loop financial reconciliation system designed for high-volume payment ecosystems. It ingests order management system (OMS) logs, Razorpay settlement reports, and bank statement feeds to deterministically reconcile transactions, diagnose edge-case discrepancies using LLMs, and enforce double-entry accounting invariants before ledger commitment.

---

## Key Features

- **Integer-Paise Precision:** Eliminates floating-point rounding errors by processing all monetary values in 64-bit integer minor currency units (paise).
- **Tiered Hybrid Engine (Tiers 0–4):** Combines exact multi-key joins, bulk UTR summation, dynamic programming subset-sum (DPSS) disaggregation, and bank narration fuzzy matching.
- **Guardrailed LLM Diagnostician:** Uses Groq (`openai/gpt-oss-120b`) with Pydantic structured outputs (`DiagnosticReport`). The LLM operates in read-only mode with zero direct write access to ledger state.
- **Deterministic Policy Gate:** Validates all LLM-proposed resolutions against strict financial invariants (Debits = Credits, Section 194H TDS withholding calculations, contractual MDR rates, and idempotency write-ahead keys).
- **Honest Exception Ledger:** Quarantines genuine fee overcharges, timing lags, and unmapped bank orphans with audit-ready metadata.

---

## System Architecture

<img width="900" height="1110" alt="Razor Arch" src="https://github.com/user-attachments/assets/aa1303f5-9a0d-4d5a-bd9a-067335f471e9" />


---

## Quickstart

### 1. Prerequisites
- Python 3.10+
- Groq API Key (Optional — a deterministic fallback runs automatically if no key is provided)

### 2. Setup
```bash
# Clone repository
git clone <repository-url>
cd razorpay-buildathon

# Install dependencies
pip install -r requirements.txt

# Configure environment variables (optional for live Groq AI)
cp .env.example .env
# Set GROQ_API_KEY=your_groq_api_key in .env
```

### 3. Run Pipeline CLI
Run the complete end-to-end dataset generation, data validation, reconciliation engine, and policy gate pipeline:
```bash
python run.py
```

### 4. Launch Interactive Web UI
Launch the Streamlit dashboard for real-time audit control, exception inspection, and metrics visualization:
```bash
streamlit run app.py
```

---

## Reconciliation Engine Mechanics

| Tier | Function | Description |
| :--- | :--- | :--- |
| **Tier 0** | OMS-Gateway Presence | Identifies orders missing settlement events |
| **Tier 1** | Exact Key Matching | Reconciles 1:1 transactions via UTR & verifies MDR rates |
| **Tier 2** | Bulk UTR Summation | Aggregates known batch settlements against bank deposits |
| **Tier 3** | DPSS Disaggregation | Uses Dynamic Programming Subset-Sum solver to resolve grouped bank credits |
| **Tier 4** | Bank Orphan Detection | Flags unmapped bank credits for human audit |

---

## Policy Gate Invariants

1. **Double-Entry Balance:** Every committed transaction must satisfy `Debit Amount == Credit Amount`.
2. **TDS Section 194H Invariant:** Enforces 10% withholding validation:
   $$\text{Variance} = \lfloor \text{Gross Amount} \times 0.10 \rfloor \pm 1\text{ paise}$$
3. **Contractual MDR Rate Compliance:**
   - UPI: 0% MDR
   - Debit Card: 0.9% MDR
   - Credit Card: 1.8% MDR
   - Corporate Card: 2.8% MDR
   - GST: 18% applied strictly to MDR fee
4. **Idempotency Guard:** Composite write-ahead key (`record_id:action_type`) prevents duplicate postings and replay attacks.

---

## Repository Structure

```
├── run.py                 # Primary entry point (Dataset gen + Validation + Pipeline)
├── app.py                 # Streamlit web dashboard
├── generate_dataset.py    # Synthetic dataset generator (Paise integer-only)
├── requirements.txt       # Python dependencies
├── agent/
│   ├── diagnostician.py   # LLM Diagnostic agent (Groq SDK + Structured Pydantic outputs)
│   └── policy_gate.py     # Invariant verifier & idempotency ledger manager
├── engine/
│   └── matcher.py         # 5-tiered deterministic reconciliation & DPSS solver
├── scripts/
│   ├── verify_pipeline.py # Full pipeline unit test
│   ├── verify_engine.py   # Matching engine unit test
│   └── verify_data_integrity.py # Integer-paise float verification
└── data/
    ├── orders.json        # OMS transaction records
    ├── settlements.json   # Razorpay settlement logs
    ├── bank_feed.json     # Core banking statement feeds
    └── ground_truth.json  # Ground-truth evaluation benchmarks
```

---

## Verification & Metrics

The system processes 150 synthetic transactions containing edge cases (fee overcharges, TDS withholdings, timing delays, and unmapped bank orphans):

- **Deterministic Match Rate:** ~90% (Exact + Bulk DPSS)
- **AI Policy Gate Resolution:** 100% of valid TDS Section 194H variances auto-resolved
- **Precision:** 100% (Zero false positives committed to financial ledger)
- **Execution Throughput:** ~1,000+ records/sec on deterministic matching
