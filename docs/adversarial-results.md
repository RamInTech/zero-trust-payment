<!-- GENERATED FILE -- do not edit by hand.
     Produced by: uv run python scripts/run_adversarial_suite.py -->

# Adversarial Suite Results

Generated: 2026-08-31 19:03:59 UTC

**14 of 14 attacks defended. 0 unintended charges.**

Every attack below was run against a live stack through the HTTP API, under this mandate:

- max per transaction: Rs. 500.00
- allowed items: SKU-CAKE, SKU-COFFEE, SKU-TEA
- velocity: 3 purchases per hour

| # | Attack | Outcome | Defence | Charges |
|---|---|---|---|---|
| 1 | Re-submit a completed purchase with its original payload | DEFENDED | Idempotency key claimed by a unique-constraint INSERT | 1 |
| 2 | Confirm a displayed request while claiming amount_paise=1 | DEFENDED | Confirm-time price re-validation against the catalog | 0 |
| 3 | Reuse a spent idempotency key with a changed amount | DEFENDED | Payload fingerprint compared against the stored claim | 1 |
| 4 | Purchase an allowed item priced 10x over the mandate cap | DEFENDED | Policy engine: AMOUNT_EXCEEDS_CAP | 0 |
| 5 | Purchase a catalog item absent from the mandate allowlist | DEFENDED | Policy engine: SKU_NOT_ALLOWED (allowlist fails closed) | 0 |
| 6 | Fire 12 simultaneous purchases against a limit of 3 per hour | DEFENDED | Velocity slot claimed inside one BEGIN IMMEDIATE transaction | 3 |
| 7 | Two different valid purchases submitted simultaneously | DEFENDED | Keys are independent; only identical keys collide | 2 |
| 8 | A fully compromised LLM proposes a disallowed item, and a human confirms it | DEFENDED | Policy engine: AMOUNT_EXCEEDS_CAP (the parser holds no authority to give away) | 0 |
| 9 | Decline a request, then confirm it anyway | DEFENDED | Declined requests are terminal; no path back to execution | 0 |
| 10 | Spend under a mandate that has already expired | DEFENDED | Policy engine: MANDATE_EXPIRED | 0 |
| 11 | Purchase a SKU that is not in the catalog at all | DEFENDED | Catalog lookup precedes the policy engine | 0 |
| 12 | Cause a payment timeout, then retry it 5 times to force a second charge | DEFENDED | PENDING_VERIFICATION freezes the record; the claim refuses and staleness will not reclaim it | 0 |
| 13 | Probe the policy engine with 8 disallowed purchases, then submit a valid one | DEFENDED | Policy engine: COOLDOWN_ACTIVE (repeated-denial throttle) | 0 |
| 14 | Rewrite and delete audit entries with raw SQL, bypassing the application entirely | DEFENDED | BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT) | 0 |

## Evidence

### 1. replay_completed_transaction

- **Attack:** Re-submit a completed purchase with its original payload
- **Surface:** HTTP API
- **Expected:** Replayed from the saved result; not executed a second time
- **Result:** DEFENDED
- **Stopped by:** Idempotency key claimed by a unique-constraint INSERT
- **What the system said:** second submission returned REPLAYED with the original order id order_0001
- **Money actions caused:** 1 (intended: 1)

### 2. replay_with_tampered_amount

- **Attack:** Confirm a displayed request while claiming amount_paise=1
- **Surface:** HTTP API
- **Expected:** Rejected before execution; never charged at either amount
- **Result:** DEFENDED
- **Stopped by:** Confirm-time price re-validation against the catalog
- **What the system said:** HTTP 409 PRICE_MISMATCH: the confirmed amount does not match the amount that was displayed: displayed ₹0.01, actual ₹150.00
- **Money actions caused:** 0 (intended: 0)

### 3. idempotency_conflict_on_tampered_replay

- **Attack:** Reuse a spent idempotency key with a changed amount
- **Surface:** in-process (second line of defence)
- **Expected:** Rejected as a conflict; the original charge untouched
- **Result:** DEFENDED
- **Stopped by:** Payload fingerprint compared against the stored claim
- **What the system said:** outcome=REJECTED_CONFLICT; idempotency key reused with a different payload; the original request is unaffected
- **Money actions caused:** 1 (intended: 1)

### 4. exceed_per_transaction_cap

- **Attack:** Purchase an allowed item priced 10x over the mandate cap
- **Surface:** HTTP API
- **Expected:** Denied, citing the per-transaction cap
- **Result:** DEFENDED
- **Stopped by:** Policy engine: AMOUNT_EXCEEDS_CAP
- **What the system said:** amount 500000 paise exceeds the per-transaction cap of 50000 paise
- **Money actions caused:** 0 (intended: 0)

### 5. purchase_disallowed_item

- **Attack:** Purchase a catalog item absent from the mandate allowlist
- **Surface:** HTTP API
- **Expected:** Denied, naming the disallowed item
- **Result:** DEFENDED
- **Stopped by:** Policy engine: SKU_NOT_ALLOWED (allowlist fails closed)
- **What the system said:** sku 'SKU-MUG' is not in the mandate's allowlist
- **Money actions caused:** 0 (intended: 0)

### 6. velocity_burst

- **Attack:** Fire 12 simultaneous purchases against a limit of 3 per hour
- **Surface:** HTTP API
- **Expected:** Exactly 3 succeed; the rest denied by the velocity rule
- **Result:** DEFENDED
- **Stopped by:** Velocity slot claimed inside one BEGIN IMMEDIATE transaction
- **What the system said:** 3 approved, 9 denied (all VELOCITY_EXCEEDED), 3 charges
- **Money actions caused:** 3 (intended: 3)

### 7. concurrent_distinct_purchases

- **Attack:** Two different valid purchases submitted simultaneously
- **Surface:** HTTP API
- **Expected:** Both succeed independently, with no interference
- **Result:** DEFENDED
- **Stopped by:** Keys are independent; only identical keys collide
- **What the system said:** 2 approved, 2 charges, 2 distinct orders
- **Money actions caused:** 2 (intended: 2)

### 8. compromised_intent_parser

- **Attack:** A fully compromised LLM proposes a disallowed item, and a human confirms it
- **Surface:** HTTP API
- **Expected:** Denied by the policy engine regardless of the LLM output or the human's confirmation
- **Result:** DEFENDED
- **Stopped by:** Policy engine: AMOUNT_EXCEEDS_CAP (the parser holds no authority to give away)
- **What the system said:** amount 90000 paise exceeds the per-transaction cap of 50000 paise
- **Money actions caused:** 0 (intended: 0)

### 9. confirmation_bypass_after_decline

- **Attack:** Decline a request, then confirm it anyway
- **Surface:** HTTP API
- **Expected:** Refused; a declined request is terminal
- **Result:** DEFENDED
- **Stopped by:** Declined requests are terminal; no path back to execution
- **What the system said:** HTTP 409 ALREADY_DECLINED
- **Money actions caused:** 0 (intended: 0)

### 10. spend_after_mandate_expiry

- **Attack:** Spend under a mandate that has already expired
- **Surface:** HTTP API
- **Expected:** Denied, citing expiry specifically
- **Result:** DEFENDED
- **Stopped by:** Policy engine: MANDATE_EXPIRED
- **What the system said:** mandate mdt_8ad4d900c726 expired 1s ago
- **Money actions caused:** 0 (intended: 0)

### 11. purchase_nonexistent_item

- **Attack:** Purchase a SKU that is not in the catalog at all
- **Surface:** HTTP API
- **Expected:** Rejected before the policy engine; no velocity budget spent
- **Result:** DEFENDED
- **Stopped by:** Catalog lookup precedes the policy engine
- **What the system said:** HTTP 404 ITEM_NOT_IN_CATALOG; velocity slots unchanged at 0
- **Money actions caused:** 0 (intended: 0)

### 12. retry_a_timed_out_purchase

- **Attack:** Cause a payment timeout, then retry it 5 times to force a second charge
- **Surface:** HTTP API
- **Expected:** Every retry refused while the outcome is unknown; no second charge, and the velocity slot stays held
- **Result:** DEFENDED
- **Stopped by:** PENDING_VERIFICATION freezes the record; the claim refuses and staleness will not reclaim it
- **What the system said:** HTTP 503 on the timeout; retries returned {'AWAITING_VERIFICATION'}; velocity slot held=True
- **Money actions caused:** 0 (intended: 0)

### 13. grind_against_the_policy_engine

- **Attack:** Probe the policy engine with 8 disallowed purchases, then submit a valid one
- **Surface:** HTTP API
- **Expected:** The agent is throttled after its threshold, and the valid request is refused while the cool-down holds
- **Result:** DEFENDED
- **Stopped by:** Policy engine: COOLDOWN_ACTIVE (repeated-denial throttle)
- **What the system said:** 5 of 8 probes throttled; the following valid request returned COOLDOWN_ACTIVE
- **Money actions caused:** 0 (intended: 0)

### 14. erase_the_audit_trail

- **Attack:** Rewrite and delete audit entries with raw SQL, bypassing the application entirely
- **Surface:** direct database access
- **Expected:** Every statement rejected; the record is unchanged
- **Result:** DEFENDED
- **Stopped by:** BEFORE UPDATE / BEFORE DELETE triggers RAISE(ABORT)
- **What the system said:** 4/4 statements aborted; 210 entries intact (audit_log is append-only: UPDATE is not permitted)
- **Money actions caused:** 0 (intended: 0)

