# Phase 1 — Idempotency Core

## 1. What this phase builds, in plain language

This phase builds one small thing: a way to make sure that a payment happens
**at most once**, even if the request to make it arrives many times.

That sounds trivial until you think about how requests actually arrive. A user
double-taps a button. A network connection times out, so the client retries —
but the original request had already gone through, and the *response* was what
got lost. A process crashes halfway through charging someone. An AI agent, being
a program with a retry loop, fires the same request four times in a second
because it didn't get an answer fast enough.

In every one of those cases the merchant's server sees N requests that all say
"charge ₹500 for order X." It has no way, from the request alone, to tell
"the user wants to buy this four times" apart from "the user wants to buy this
once and the network is being unreliable."

So the client attaches an **idempotency key** — a unique ID it generates once per
*intent to buy*, and reuses on every retry of that same intent. The server's job,
which is what this phase implements, is to look at that key and decide: is this
the first time I'm seeing this, or is this a repeat of something I already did?
If it's a repeat, don't charge again — hand back the answer from the first time.

Deliberately, this phase talks to a **fake** payment processor that has no safety
of its own and charges every single time you call it. That's the whole point: if
the tests show one charge for thirty-two simultaneous requests, that safety is
coming from the code written here, and not borrowed from somewhere else.

## 2. The core concepts used

### Idempotency

**Definition:** an operation is idempotent if doing it many times has the same
effect as doing it once. Setting `x = 5` is idempotent. `x = x + 5` is not.

Payments are naturally *not* idempotent — charging twice takes twice the money.
The trick is that you can't change that fact about money, so instead you wrap the
non-idempotent operation in a layer that remembers whether it already ran. The
key is what lets the layer recognise "this again."

**Why we needed it rather than something simpler:** the obvious simpler idea is
"check whether a charge for this order already exists, and if so skip." That
fails for a real reason — it's two separate steps (check, then charge) with a gap
between them, and two requests can both run the check *before* either runs the
charge. Both see "no charge yet." Both charge. The check-then-act pattern is not
safe under concurrency, and payments are exactly where concurrency shows up.

### A unique database constraint as a concurrency primitive

**Definition:** a `UNIQUE`/`PRIMARY KEY` constraint tells the database that no
two rows may share a value in that column. If two transactions try to insert the
same value, the database lets exactly one succeed and raises an error for the
other. Not "usually" — the database's job is to guarantee it.

This is the single most important design decision in the phase. Claiming a key
is an `INSERT` of that key into a table where it's the primary key:

```sql
CREATE TABLE idempotency_records (
    key TEXT PRIMARY KEY,   -- the unique constraint IS the guarantee
    ...
);
```

The insert either succeeds — you won the claim, go do the charge — or raises
`IntegrityError` — someone else already has it, go find out what they did.

**Why this rather than a lock?** An application-level lock (a `threading.Lock`,
a flag in memory) only works within one process. Restart the process and the lock
is gone; run two copies of the server and each has its own lock protecting
nothing. More subtly, a lock is something *you* have to remember to take
correctly everywhere; a unique constraint is enforced by the database no matter
which code path does the insert or how badly it's written. The guarantee stops
depending on programmer discipline, which is exactly what you want for the one
piece of logic that must never fail.

This is also why the answer to "what about clock skew, or two servers racing?"
is easy here: nothing in the correctness argument depends on time or on
coordination between callers. It depends on one atomic insert.

### A state machine for the request lifecycle

**Definition:** modelling a thing as a fixed set of states with defined
transitions between them, instead of a pile of boolean flags.

A key is in exactly one of `PROCESSING`, `COMPLETED`, or `FAILED`, and a request
resolves to exactly one of five outcomes:

| Outcome | Meaning |
|---|---|
| `EXECUTED` | Key was new. We ran the real charge. |
| `REPLAYED` | Key finished earlier. Return the *saved* result; charge nothing. |
| `RECLAIMED` | Prior claimant went stale (crashed). We took over and ran it. |
| `IN_PROGRESS` | Key is genuinely mid-flight right now. Caller should retry later. |
| `REJECTED_CONFLICT` | Same key, *different* payload. Rejected outright. |

**Why we needed this rather than a boolean "already done?":** because "not done"
covers at least three genuinely different situations that need different
answers — nobody has started, someone is working on it right now, and someone
started but died. Collapsing them into one flag forces you to guess, and each
guess is a bug: treat in-flight as not-started and you double charge; treat
crashed as in-flight and the key jams forever. Naming the states is what makes
the right behaviour writable. It also pays off in Phase 4, where each outcome
maps to exactly one audit event type.

### Compare-and-swap (for the reclaim path)

**Definition:** update a row only if it still holds the value you last read
(`UPDATE ... WHERE key = ? AND claimed_at = ?`). If someone changed it first,
your update affects zero rows and you know you lost the race.

**Why we needed it:** the moment a record goes stale, *every* waiting retry sees
"stale, I can take this." Without CAS, several of them would each conclude they'd
reclaimed it and all charge. The conditional update means exactly one wins
(`rowcount == 1`) and the rest fall back to `IN_PROGRESS`.

## 3. The intuition

**The cloakroom ticket.**

You hand your coat to the attendant and get ticket #42. The ticket isn't your
coat — it's a claim on one specific act of coat-handling.

- You come back and present #42: you get *your coat back*. You don't get a second
  coat, because #42 was only ever a claim on one coat. That's **replay** — the
  saved result, returned again.
- Your friend shows up with a photocopy of #42 while you're still at the bar:
  the attendant sees the ticket is already spoken for and says "someone's got
  this one, wait." That's **in progress** — not a refusal, just "not yet."
- Someone hands over a ticket numbered #42 but claiming a *different* coat: the
  attendant refuses flatly. That's **conflict**, and it matters — it's the
  difference between an honest retry and a tampered one.
- The attendant goes on break mid-handover and never comes back. Without a rule,
  coat #42 is trapped behind a ticket nobody will ever finish processing. So
  there's a policy: if a ticket has been "being handled" for longer than any real
  handover could take, a new attendant takes it over. That's the **staleness
  timeout**, and it's why a crash doesn't permanently jam a key.

The crucial part is *where the safety lives*. It isn't in the customer being
honest, and it isn't in the attendant being careful. It's in the fact that the
ticket book has exactly one #42 in it. That's the unique constraint. Anyone can
present any ticket; only one claim per number can ever exist.

This maps directly onto the project's thesis. We aren't asking the AI agent to
retry politely or to keep track of what it already bought. The agent can hammer
the same key thirty-two times in the same millisecond — the ticket book still
only has one #42.

**On the staleness timeout specifically**, the tension is worth naming: too short
and you reclaim a key from a claimant that was merely slow (which double
charges); too long and a crashed process jams a customer's purchase for that long
(which merely annoys). Those failures are not equally bad, which is why the
timeout leans long — 30 seconds by default, far beyond any real charge round-trip.

## 4. What could go wrong without this phase

**Concretely, without the claim-by-unique-insert:**

A user confirms a ₹50,000 purchase. The button is slow, so they tap it twice,
40 ms apart.

1. Request A arrives. It queries: any completed charge for this order? No.
2. Request B arrives 40 ms later. Same query. Still no — A hasn't finished
   charging yet, so there is nothing for B to find.
3. A calls the processor. ₹50,000 leaves the customer's account.
4. B calls the processor. Another ₹50,000 leaves the customer's account.

Both requests behaved "correctly" by their own logic. The customer is out
₹100,000, and the merchant gets a chargeback, which is exactly the risk that
makes a merchant refuse agent-initiated payments in the first place.

**Without the conflict check:**

An agent legitimately gets key `k` for a ₹500 purchase. Something — a bug, or an
attacker replaying a captured request — resends key `k` with the amount changed
to ₹5,00,000. If the system only matched on the key, it would look up `k`, find
it completed, and cheerfully return the stored ₹500 receipt for what was
submitted as a ₹5,00,000 purchase. The two sides now disagree about what
happened, and the log says everything is fine. Rejecting the mismatch outright is
what keeps "the key" and "the thing the key authorised" welded together.

**Without the staleness timeout (this was a real, previously identified gap in
this project):**

A process claims key `k`, marks it `PROCESSING`, and is killed by an OOM before
it charges anything. The record says `PROCESSING` forever. Every retry of that
purchase, for the rest of the database's life, is told "in progress, try later."
The customer's purchase can never complete, and — worse — it can never complete
*under that key*, so an exasperated user or agent mints a fresh key and retries,
which is precisely the double-charge path the whole system exists to prevent.
A safety mechanism that jams is not safe; it just fails in a different direction.

**Without CAS on the reclaim:**

The record above goes stale at second 30. Sixteen queued retries all wake, all
read `claimed_at`, and all compute "this is older than 30s, I'll take it." All
sixteen charge. The staleness fix would have reintroduced the exact bug it was
added to prevent — which is why `test_concurrent_reclaim_of_a_stale_key_charges_once`
exists as its own test rather than being assumed.

## 5. Test evidence

Test file: `tests/test_idempotency.py` (44 tests). Implementation under test:
`zerotrust/idempotency.py`, `zerotrust/processor.py`.

| Test | What it proves |
|---|---|
| `test_sequential_retry_charges_once_and_replays` | Second call returns `REPLAYED` with the *identical* saved response; 1 charge. |
| `test_many_sequential_retries_still_charge_once` | 25 retries → 1 `EXECUTED`, 24 `REPLAYED`, 1 charge. |
| `test_concurrent_identical_key_charges_exactly_once` | 32 barrier-synchronised threads on one key → exactly 1 executor, 1 charge, every other thread got `REPLAYED` or `IN_PROGRESS`. **Parametrised 20×.** |
| `test_same_key_different_payload_is_rejected` | Tampered amount → `REJECTED_CONFLICT`; original charge still 1, still at the original amount. |
| `test_conflict_against_an_in_flight_key_is_also_rejected` | Conflict is caught even before the original completes, not only after. |
| `test_different_keys_charge_independently` | Distinct keys don't interfere; 2 charges, distinct charge IDs. |
| `test_concurrent_distinct_orders_do_not_contaminate` | 8 orders × 6 concurrent retries = 48 threads → exactly 8 charges, each at its own amount. **Parametrised 5×.** |
| `test_stale_processing_record_is_reclaimed` | A hung claimant's record is reclaimed after the timeout, completes, charges exactly once, and replays normally afterwards. Injected clock, so it's deterministic. |
| `test_stale_reclaim_works_on_a_real_clock` | Same property on a real 0.3s timeout — proves the behaviour isn't an artifact of the fake clock. |
| `test_concurrent_reclaim_of_a_stale_key_charges_once` | 16 threads racing to reclaim one stale record → exactly 1 wins, 1 charge. **Parametrised 10×.** |
| `test_fresh_in_flight_key_is_not_reclaimed` | At 29s of a 30s timeout the key still blocks with `IN_PROGRESS` — reclaim doesn't weaken the core guarantee. |
| `test_action_that_raises_leaves_the_key_retryable` | A failed attempt marks `FAILED` (not stuck `PROCESSING`), the retry proceeds, total charges = 1. |

### Actual output

```
$ uv run pytest tests/ -q
44 passed in 4.65s
44 passed in 5.36s
44 passed in 5.04s
44 passed in 4.44s
44 passed in 4.87s
```

**Flakiness discipline:** the full suite was run **5 consecutive times, all
green**. Because the race tests are themselves parametrised, the
32-thread identical-key storm executed **100 times** across those runs, the
stale-reclaim race **50 times**, and the multi-order test **25 times**. No
failures in any run.

The honest caveat: concurrency tests prove the presence of correct behaviour
under the interleavings that actually occurred, never the absence of a bad
interleaving. The stronger argument is structural — correctness rests on one
atomic insert against a primary key, not on timing — and the tests are evidence
for that argument rather than a substitute for it.

## 6. Design decisions and trade-offs

**Staleness timeout: 30 seconds, injectable.** Long enough that no genuine charge
round-trip could be mistaken for a crash (the asymmetry from §3: reclaiming too
early double-charges, reclaiming too late merely delays). It's a constructor
argument rather than a constant so tests can use 0.3s, and so a future deployment
with a slower provider can raise it without a code change.

**An injectable clock.** Rejected: `time.sleep(31)` in tests, which would make
the suite unbearably slow and *still* be timing-dependent. A `clock` callable
defaulting to `time.time` makes staleness deterministic. One real-clock test is
kept alongside it so the fake clock can't hide a bug in the real path.

**Payload fingerprint = SHA-256 of canonical JSON** (sorted keys, no whitespace).
Rejected: comparing dicts directly (can't be stored in a column) and storing the
raw payload (bulkier, and invites accidentally logging sensitive fields).
Canonicalising first means `{"a":1,"b":2}` and `{"b":2,"a":1}` are correctly
treated as the same payload rather than a spurious conflict.

**`BEGIN IMMEDIATE` for every write transaction.** SQLite defaults to deferred
transactions, which take a read lock first and try to upgrade to a write lock
later — and that upgrade is where `SQLITE_BUSY` and deadlocks come from under
concurrency. Taking the write lock up front is a one-line change that removes
that whole class of problem. WAL mode is on for the same reason: readers don't
block the writer.

**A fresh connection per operation, not a shared one.** SQLite connections aren't
safely shareable across threads without care, and this is not a performance-
critical path. Correctness bought cheaply.

**Conflict returns a `Result`, doesn't raise.** Every outcome — including the
rejections — comes back as the same `Result` type with a named `Outcome`. This is
deliberately shaped for Phase 4: one call produces exactly one outcome, which
maps to exactly one audit log entry, with no exception paths that could slip past
the logger. The trade-off is that a careless caller could ignore the outcome; the
mitigation is that `Result.executed` makes "did money move?" a single explicit
check.

**A failed action marks `FAILED` rather than deleting the row.** Deleting would
lose the attempt history; leaving it `PROCESSING` would jam the key until the
staleness timeout for no reason, since we *know* it failed. `FAILED` lets the
next retry proceed immediately with an incremented attempt count.

**Mock processor with a `latency_seconds` knob.** Without a deliberate delay
between claim and completion, racing threads mostly arrive after the first one
finished and just see `REPLAYED` — the test would pass without ever exercising
the `IN_PROGRESS` path. The latency widens the window so the interesting
interleaving actually happens.

## 7. Open questions and known limitations carried forward

- **Keys are globally scoped, not namespaced per agent.** Two unrelated agents
  could collide on the same key string — accidentally, or deliberately as a
  denial-of-service. Namespacing (`agent_id:key`) is listed in RAZORPAY.md §6 and
  becomes decidable once the mandate schema exists (it's an open item), since
  that's what defines an agent's identity. **Deferred to Phase 3.**
- **No retention window or cleanup job.** Records accumulate forever. The
  industry convention is a 24h expiry; the table already stores `created_at`, so
  the sweep is straightforward, but it isn't written. **Deferred.**
- **`FAILED` assumes the action did not move money.** True for the mock
  processor, and true for a call that failed before reaching the provider — but a
  timeout leaves the real outcome genuinely unknown, and marking that `FAILED`
  and retrying could double charge against a real provider. This is exactly what
  Phase 7's `PENDING_VERIFICATION` state and reconciliation exist to resolve.
  **This limitation must be revisited in Phase 2/7 before any real capture path
  is trusted.** It is the sharpest known gap leaving this phase.
- **`IN_PROGRESS` gives no retry-after guidance beyond a human-readable string.**
  The caller decides when to retry. Fine for now; a structured field may be worth
  adding when the agent-facing API lands in Phase 5.
- **Single instance assumed.** The correctness argument rests on the database's
  unique constraint, so it should survive multiple processes pointing at one
  store — but that is argued, not tested, and SQLite is not the right store for
  it. Out of scope per RAZORPAY.md §6.
- **Response payloads are stored as JSON text with no size bound.** Harmless at
  demo scale; worth noting before anything larger goes through it.
